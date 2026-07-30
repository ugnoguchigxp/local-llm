import threading
import time
import sys
import sqlite3
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import daemon_queue
from shared.daemon_queue import QueueBusyError, ServiceProcessLock, SingleWorkerPriorityQueue


def test_single_worker_priority_queue_processes_one_at_a_time(tmp_path):
    active = 0
    max_active = 0
    processed = []

    def handler(item):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        processed.append(item.payload)
        active -= 1
        return f"ok:{item.payload}"

    queue = SingleWorkerPriorityQueue("test", handler, db_path=tmp_path / "queue.sqlite3")
    try:
        assert queue.submit("a") == "ok:a"
        assert queue.submit("b") == "ok:b"
    finally:
        queue.shutdown()

    assert processed == ["a", "b"]
    assert max_active == 1


def test_single_worker_priority_queue_rejects_invalid_priority(tmp_path):
    queue = SingleWorkerPriorityQueue(
        "test",
        lambda item: item.payload,
        db_path=tmp_path / "queue.sqlite3",
    )
    try:
        try:
            queue.submit("x", priority="urgent")
        except ValueError as exc:
            assert "priority" in str(exc)
        else:
            raise AssertionError("invalid priority was accepted")
    finally:
        queue.shutdown()


def test_single_worker_priority_queue_prioritizes_waiting_items(tmp_path):
    gate = threading.Event()
    processed = []

    def handler(item):
        processed.append(item.payload)
        if item.payload == "gate":
            gate.wait(timeout=2)
        return item.payload

    queue = SingleWorkerPriorityQueue("test", handler, db_path=tmp_path / "queue.sqlite3")
    results = {}

    def submit(name, priority):
        results[name] = queue.submit(name, priority=priority, timeout=2)

    try:
        gate_thread = threading.Thread(target=submit, args=("gate", "normal"))
        low_thread = threading.Thread(target=submit, args=("low", "low"))
        high_thread = threading.Thread(target=submit, args=("high", "high"))

        gate_thread.start()
        time.sleep(0.05)
        low_thread.start()
        high_thread.start()
        time.sleep(0.05)
        gate.set()

        gate_thread.join(timeout=2)
        low_thread.join(timeout=2)
        high_thread.join(timeout=2)
    finally:
        queue.shutdown()

    assert results == {"gate": "gate", "high": "high", "low": "low"}
    assert processed == ["gate", "high", "low"]


def test_single_worker_priority_queue_skips_timed_out_waiting_items(tmp_path):
    gate = threading.Event()
    processed = []

    def handler(item):
        processed.append(item.payload)
        if item.payload == "gate":
            gate.wait(timeout=2)
        return item.payload

    queue = SingleWorkerPriorityQueue("test", handler, db_path=tmp_path / "queue.sqlite3")
    gate_error = []

    def submit_gate():
        try:
            queue.submit("gate", timeout=2)
        except Exception as exc:  # pragma: no cover - should not happen
            gate_error.append(exc)

    gate_thread = threading.Thread(target=submit_gate)
    try:
        gate_thread.start()
        time.sleep(0.05)

        try:
            queue.submit("stale", priority="low", timeout=0.01)
        except TimeoutError:
            pass
        else:
            raise AssertionError("timed out item unexpectedly completed")

        gate.set()
        gate_thread.join(timeout=2)
    finally:
        queue.shutdown()

    assert gate_error == []
    assert processed == ["gate"]
    assert queue.health()["cancelledCount"] == 1


def test_single_worker_priority_queue_can_reject_when_busy(tmp_path):
    gate = threading.Event()
    started = threading.Event()

    def handler(item):
        started.set()
        if item.payload == "gate":
            gate.wait(timeout=2)
        return item.payload

    queue = SingleWorkerPriorityQueue("test", handler, db_path=tmp_path / "queue.sqlite3")
    first_error = []

    def submit_gate():
        try:
            queue.submit("gate", timeout=2)
        except Exception as exc:  # pragma: no cover - should not happen
            first_error.append(exc)

    gate_thread = threading.Thread(target=submit_gate)
    try:
        gate_thread.start()
        assert started.wait(timeout=2)

        with pytest.raises(QueueBusyError):
            queue.submit("second", reject_when_busy=True, timeout=2)

        gate.set()
        gate_thread.join(timeout=2)
    finally:
        queue.shutdown()

    assert first_error == []
    assert queue.health()["queueSize"] == 0


def test_service_process_lock_blocks_second_acquire(tmp_path):
    db_path = tmp_path / "queue.sqlite3"
    first = ServiceProcessLock("llm-daemon", db_path=db_path)
    first.acquire()
    second = ServiceProcessLock("llm-daemon", db_path=db_path)
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()


def test_service_process_lock_recovers_pid_reused_by_unrelated_process(tmp_path, monkeypatch):
    db_path = tmp_path / "queue.sqlite3"
    ServiceProcessLock("embedding-daemon", db_path=db_path)
    stale_pid = 639
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO daemon_service_locks(service_name, pid, acquired_at)
            VALUES ('embedding-daemon', ?, ?)
            """,
            (stale_pid, time.time()),
        )

    monkeypatch.setattr(daemon_queue, "_pid_alive", lambda pid: pid == stale_pid)
    monkeypatch.setattr(daemon_queue, "_pid_command", lambda pid: "/System/Library/icdd")

    recovered = ServiceProcessLock("embedding-daemon", db_path=db_path)
    try:
        recovered.acquire()
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT pid FROM daemon_service_locks WHERE service_name = 'embedding-daemon'"
            ).fetchone()
        assert row is not None
        assert row[0] == os.getpid()
    finally:
        recovered.release()


def test_queue_accepts_directory_db_path(tmp_path):
    db_dir = tmp_path / "runtime-dir"
    queue = SingleWorkerPriorityQueue("test", lambda item: item.payload, db_path=db_dir)
    try:
        assert queue.submit("ok") == "ok"
    finally:
        queue.shutdown()

    assert (db_dir / "daemon_queue.sqlite3").exists()


def test_queue_accepts_sqlite_url_from_env(tmp_path, monkeypatch):
    db_file = tmp_path / "runtime" / "queue.sqlite3"
    monkeypatch.setenv("LOCAL_LLM_QUEUE_DB", f"sqlite:///{db_file}")
    queue = SingleWorkerPriorityQueue("test", lambda item: item.payload)
    try:
        assert queue.submit("ok") == "ok"
    finally:
        queue.shutdown()

    assert db_file.exists()


def test_queue_recovers_orphaned_running_jobs_on_startup(tmp_path):
    db_path = tmp_path / "queue.sqlite3"
    queue = SingleWorkerPriorityQueue("local-llm", lambda item: item.payload, db_path=db_path)
    queue.shutdown()

    now = time.perf_counter()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO daemon_jobs (
                queue_name, priority, payload_json, status, queued_at, started_at, owner
            ) VALUES (?, ?, ?, 'running', ?, ?, ?)
            """,
            ("local-llm", 10, '"orphan"', now, now, "999999:local-llm:stale"),
        )

    recovered = SingleWorkerPriorityQueue("local-llm", lambda item: item.payload, db_path=db_path)
    try:
        health = recovered.health()
        assert health["inFlight"] == 0

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT status, error
                FROM daemon_jobs
                WHERE queue_name = 'local-llm'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()

        assert row is not None
        assert row["status"] == "failed"
        assert "orphaned running job recovered after restart" in str(row["error"] or "")
    finally:
        recovered.shutdown()
