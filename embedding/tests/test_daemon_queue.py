import threading
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.daemon_queue import SingleWorkerPriorityQueue


def test_single_worker_priority_queue_processes_one_at_a_time():
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

    queue = SingleWorkerPriorityQueue("test", handler)
    try:
        assert queue.submit("a") == "ok:a"
        assert queue.submit("b") == "ok:b"
    finally:
        queue.shutdown()

    assert processed == ["a", "b"]
    assert max_active == 1


def test_single_worker_priority_queue_rejects_invalid_priority():
    queue = SingleWorkerPriorityQueue("test", lambda item: item.payload)
    try:
        try:
            queue.submit("x", priority="urgent")
        except ValueError as exc:
            assert "priority" in str(exc)
        else:
            raise AssertionError("invalid priority was accepted")
    finally:
        queue.shutdown()


def test_single_worker_priority_queue_prioritizes_waiting_items():
    gate = threading.Event()
    processed = []

    def handler(item):
        processed.append(item.payload)
        if item.payload == "gate":
            gate.wait(timeout=2)
        return item.payload

    queue = SingleWorkerPriorityQueue("test", handler)
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


def test_single_worker_priority_queue_skips_timed_out_waiting_items():
    gate = threading.Event()
    processed = []

    def handler(item):
        processed.append(item.payload)
        if item.payload == "gate":
            gate.wait(timeout=2)
        return item.payload

    queue = SingleWorkerPriorityQueue("test", handler)
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
        queue._queue.join()  # noqa: SLF001 - test observes daemon drain behavior
    finally:
        queue.shutdown()

    assert gate_error == []
    assert processed == ["gate"]
    assert queue.health()["cancelledCount"] == 1
