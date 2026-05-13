"""SQLite-backed priority queue for single-worker local daemons."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Generic, TypeVar

PRIORITIES = {
    "high": 0,
    "normal": 10,
    "low": 20,
}
PRIORITY_LABELS = {value: key for key, value in PRIORITIES.items()}

PayloadT = TypeVar("PayloadT")
ResultT = TypeVar("ResultT")


@dataclass
class QueueItem(Generic[PayloadT, ResultT]):
    payload: PayloadT
    queued_at: float = field(default_factory=time.perf_counter)
    event: Event = field(default_factory=Event)
    result: ResultT | None = None
    error: str | None = None
    cancelled: bool = False


class SingleWorkerPriorityQueue(Generic[PayloadT, ResultT]):
    """Accept concurrent callers while processing one daemon task at a time.

    Backed by SQLite so queue state is observable across processes.
    """

    def __init__(
        self,
        name: str,
        handler: Callable[[QueueItem[PayloadT, ResultT]], ResultT],
        db_path: str | Path | None = None,
        poll_interval_sec: float = 0.02,
    ) -> None:
        self.name = name
        self._handler = handler
        self._db_path = self._resolve_db_path(db_path)
        self._poll_interval_sec = max(0.005, poll_interval_sec)
        self._owner = f"{os.getpid()}:{self.name}:{id(self)}"
        self._shutdown = False
        self._worker_wakeup = Event()
        self._state_lock = Lock()
        self._in_flight = 0
        self._processed_count = 0
        self._failed_count = 0
        self._cancelled_count = 0
        self._active_priority: int | None = None
        self._init_db()
        self._worker = Thread(target=self._run_worker, name=f"{name}-worker", daemon=True)
        self._worker.start()

    def submit(
        self,
        payload: PayloadT,
        priority: str = "normal",
        timeout: float | None = None,
    ) -> ResultT:
        if priority not in PRIORITIES:
            raise ValueError("priority must be 'high', 'normal', or 'low'")

        queue_id = self._enqueue_payload(payload=payload, priority=priority)
        item = QueueItem(payload=payload, queued_at=time.perf_counter())
        self._worker_wakeup.set()

        if not self._wait_for_result(queue_id=queue_id, item=item, timeout=timeout):
            item.cancelled = True
            if self._mark_cancelled(queue_id):
                with self._state_lock:
                    self._cancelled_count += 1
            raise TimeoutError(f"{self.name} daemon request timed out")
        if item.error:
            raise RuntimeError(item.error)
        if item.result is None:
            raise RuntimeError(f"{self.name} daemon returned no result")
        return item.result

    def health(self) -> dict[str, Any]:
        queued_count = self._count_rows("queued")
        running_count = self._count_rows("running")
        with self._state_lock:
            return {
                "queueSize": queued_count,
                "inFlight": running_count or self._in_flight,
                "activePriority": (
                    PRIORITY_LABELS.get(self._active_priority)
                    if self._active_priority is not None
                    else None
                ),
                "processedCount": self._processed_count,
                "failedCount": self._failed_count,
                "cancelledCount": self._cancelled_count,
                "workerAlive": self._worker.is_alive(),
            }

    def shutdown(self) -> None:
        self._shutdown = True
        self._worker_wakeup.set()
        self._worker.join(timeout=2)

    def _run_worker(self) -> None:
        while not self._shutdown:
            claimed = self._claim_next_item()
            if claimed is None:
                self._worker_wakeup.wait(timeout=self._poll_interval_sec)
                self._worker_wakeup.clear()
                continue

            queue_id, priority, queued_at, payload = claimed
            item = QueueItem(payload=payload, queued_at=queued_at)

            try:
                if item.cancelled:
                    with self._state_lock:
                        self._cancelled_count += 1
                    continue

                with self._state_lock:
                    self._in_flight += 1
                    self._active_priority = priority

                result = self._handler(item)
                item.result = result
                self._store_completion(queue_id, result=result, error=None)
                with self._state_lock:
                    self._processed_count += 1
            except Exception as exc:  # pragma: no cover - defensive daemon boundary
                item.error = str(exc)
                self._store_completion(queue_id, result=None, error=str(exc))
                with self._state_lock:
                    self._failed_count += 1
            finally:
                with self._state_lock:
                    if self._in_flight > 0:
                        self._in_flight -= 1
                    if self._in_flight == 0:
                        self._active_priority = None
                item.event.set()

    @staticmethod
    def _resolve_db_path(path: str | Path | None) -> Path:
        if path is not None:
            resolved = Path(path).expanduser()
        else:
            raw = os.getenv("LOCAL_LLM_QUEUE_DB", "").strip()
            if raw:
                resolved = Path(raw).expanduser()
            else:
                resolved = Path.home() / ".localLlm" / "runtime" / "daemon_queue.sqlite3"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daemon_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_name TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    cancelled INTEGER NOT NULL DEFAULT 0,
                    queued_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    owner TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_daemon_jobs_queue_status
                ON daemon_jobs(queue_name, status, cancelled, priority, id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daemon_service_locks (
                    service_name TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    acquired_at REAL NOT NULL
                )
                """
            )

    def _enqueue_payload(self, payload: PayloadT, priority: str) -> int:
        queued_at = time.perf_counter()
        payload_json = json.dumps(payload, ensure_ascii=False, default=_json_default)
        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO daemon_jobs (
                    queue_name, priority, payload_json, status, queued_at, owner
                ) VALUES (?, ?, ?, 'queued', ?, ?)
                """,
                (self.name, PRIORITIES[priority], payload_json, queued_at, self._owner),
            )
            return int(cur.lastrowid)

    def _wait_for_result(self, queue_id: int, item: QueueItem[PayloadT, ResultT], timeout: float | None) -> bool:
        deadline = None if timeout is None else (time.perf_counter() + timeout)
        while True:
            row = self._get_job_row(queue_id)
            if row is None:
                item.error = "queue item disappeared"
                return True
            status = str(row["status"])
            if status == "done":
                item.result = _decode_json_field(row["result_json"])
                return True
            if status == "failed":
                item.error = str(row["error"] or "daemon task failed")
                return True
            if deadline is not None and time.perf_counter() >= deadline:
                return False
            time.sleep(self._poll_interval_sec)

    def _get_job_row(self, queue_id: int) -> sqlite3.Row | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, status, result_json, error
                FROM daemon_jobs
                WHERE id = ? AND queue_name = ?
                """,
                (queue_id, self.name),
            ).fetchone()
            return row

    def _mark_cancelled(self, queue_id: int) -> bool:
        with self._connection() as conn:
            cur = conn.execute(
                """
                UPDATE daemon_jobs
                SET cancelled = 1, status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END
                WHERE id = ? AND queue_name = ? AND status = 'queued'
                """,
                (queue_id, self.name),
            )
            return cur.rowcount > 0

    def _claim_next_item(self) -> tuple[int, int, float, PayloadT] | None:
        now = time.perf_counter()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, priority, payload_json, queued_at
                FROM daemon_jobs
                WHERE queue_name = ?
                  AND status = 'queued'
                  AND cancelled = 0
                ORDER BY priority ASC, id ASC
                LIMIT 1
                """,
                (self.name,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None

            conn.execute(
                """
                UPDATE daemon_jobs
                SET status = 'running', started_at = ?, owner = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, self._owner, int(row["id"])),
            )
            conn.execute("COMMIT")

        payload = _decode_json_field(row["payload_json"])
        return (int(row["id"]), int(row["priority"]), float(row["queued_at"]), payload)

    def _store_completion(self, queue_id: int, result: ResultT | None, error: str | None) -> None:
        finished_at = time.perf_counter()
        status = "failed" if error else "done"
        result_json = json.dumps(result, ensure_ascii=False, default=_json_default) if error is None else None
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE daemon_jobs
                SET status = ?,
                    result_json = ?,
                    error = ?,
                    finished_at = ?
                WHERE id = ? AND queue_name = ?
                """,
                (status, result_json, error, finished_at, queue_id, self.name),
            )

    def _count_rows(self, status: str) -> int:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM daemon_jobs
                WHERE queue_name = ? AND status = ?
                """,
                (self.name, status),
            ).fetchone()
        return int(row["c"]) if row else 0


class ServiceProcessLock:
    """Cross-process singleton lock keyed by service name."""

    def __init__(self, service_name: str, db_path: str | Path | None = None) -> None:
        self.service_name = service_name
        self.pid = os.getpid()
        self._db_path = SingleWorkerPriorityQueue._resolve_db_path(db_path)
        self._init_db()

    def acquire(self) -> None:
        now = time.time()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT pid FROM daemon_service_locks WHERE service_name = ?",
                (self.service_name,),
            ).fetchone()
            if row:
                existing_pid = int(row["pid"])
                if _pid_alive(existing_pid):
                    raise RuntimeError(
                        f"{self.service_name} is already running (pid={existing_pid})"
                    )
                conn.execute(
                    """
                    UPDATE daemon_service_locks
                    SET pid = ?, acquired_at = ?
                    WHERE service_name = ?
                    """,
                    (self.pid, now, self.service_name),
                )
                return
            conn.execute(
                """
                INSERT INTO daemon_service_locks(service_name, pid, acquired_at)
                VALUES (?, ?, ?)
                """,
                (self.service_name, self.pid, now),
            )

    def release(self) -> None:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM daemon_service_locks WHERE service_name = ? AND pid = ?",
                (self.service_name, self.pid),
            )

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daemon_service_locks (
                    service_name TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    acquired_at REAL NOT NULL
                )
                """
            )


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dict__"):
        return value.__dict__
    return value


def _decode_json_field(raw: str | None) -> Any:
    if raw is None:
        return None
    return json.loads(raw)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
