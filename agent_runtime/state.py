from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SessionRecord:
    gateway_session_id: str
    runtime_id: str
    native_session_id: str
    public_model_id: str
    native_model_id: str
    provider_id: str
    workspace_mode: str
    workspace_path: str
    approval_policy: str
    initial_native_cursor: str
    last_native_cursor: str
    status: str
    protocol_fingerprint: str
    created_at: int
    updated_at: int
    released_at: int | None


@dataclass(frozen=True)
class TurnRecord:
    gateway_turn_id: str
    gateway_session_id: str
    native_turn_id: str
    status: str
    created_at: int
    updated_at: int


class AgentStateStore:
    def __init__(self, path: Path) -> None:
        configured = Path(os.path.abspath(path.expanduser()))
        if configured.is_symlink():
            raise sqlite3.DatabaseError("agent state database must not be a symlink")
        self.path = configured
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._prepare_database_files()
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        try:
            self._migrate()
            self._prepare_database_files()
        except BaseException:
            self._connection.close()
            raise

    def _prepare_database_files(self) -> None:
        if self.path.exists():
            if not stat.S_ISREG(self.path.stat().st_mode):
                raise sqlite3.DatabaseError("agent state database must be a regular file")
            os.chmod(self.path, 0o600)
        else:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
        for suffix in ("-wal", "-shm"):
            ancillary = Path(f"{self.path}{suffix}")
            if ancillary.is_symlink():
                raise sqlite3.DatabaseError("agent state database sidecar must not be a symlink")
            if ancillary.exists():
                if not stat.S_ISREG(ancillary.stat().st_mode):
                    raise sqlite3.DatabaseError(
                        "agent state database sidecar must be a regular file"
                    )
                os.chmod(ancillary, 0o600)

    def _migrate(self) -> None:
        with self._lock, self._connection:
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, SCHEMA_VERSION}:
                raise sqlite3.DatabaseError(
                    f"unsupported agent state schema version: {version}"
                )
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    gateway_session_id TEXT PRIMARY KEY,
                    runtime_id TEXT NOT NULL,
                    native_session_id TEXT NOT NULL,
                    public_model_id TEXT NOT NULL,
                    native_model_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    workspace_mode TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    approval_policy TEXT NOT NULL,
                    initial_native_cursor TEXT NOT NULL,
                    last_native_cursor TEXT NOT NULL,
                    status TEXT NOT NULL,
                    protocol_fingerprint TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    released_at INTEGER,
                    UNIQUE(runtime_id, native_session_id)
                );
                CREATE TABLE IF NOT EXISTS agent_turns (
                    gateway_turn_id TEXT PRIMARY KEY,
                    gateway_session_id TEXT NOT NULL,
                    native_turn_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(gateway_session_id, native_turn_id),
                    FOREIGN KEY(gateway_session_id) REFERENCES agent_sessions(gateway_session_id)
                );
                CREATE TABLE IF NOT EXISTS agent_idempotency (
                    scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    gateway_resource_id TEXT NOT NULL,
                    native_command_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY(scope, idempotency_key)
                );
                """
            )
            self._validate_schema()
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _validate_schema(self) -> None:
        expected_columns = {
            "agent_sessions": {
                "gateway_session_id",
                "runtime_id",
                "native_session_id",
                "public_model_id",
                "native_model_id",
                "provider_id",
                "workspace_mode",
                "workspace_path",
                "approval_policy",
                "initial_native_cursor",
                "last_native_cursor",
                "status",
                "protocol_fingerprint",
                "created_at",
                "updated_at",
                "released_at",
            },
            "agent_turns": {
                "gateway_turn_id",
                "gateway_session_id",
                "native_turn_id",
                "status",
                "created_at",
                "updated_at",
            },
            "agent_idempotency": {
                "scope",
                "idempotency_key",
                "operation",
                "request_hash",
                "gateway_resource_id",
                "native_command_id",
                "status",
                "created_at",
                "expires_at",
            },
        }
        expected_unique_indexes = {
            "agent_sessions": {("runtime_id", "native_session_id")},
            "agent_turns": {("gateway_session_id", "native_turn_id")},
            "agent_idempotency": {("scope", "idempotency_key")},
        }
        for table, columns in expected_columns.items():
            actual_columns = {
                row["name"]
                for row in self._connection.execute(
                    "SELECT name FROM pragma_table_info(?)",
                    (table,),
                )
            }
            if actual_columns != columns:
                raise sqlite3.DatabaseError(f"agent state table {table} has an invalid schema")
            indexes: set[tuple[str, ...]] = set()
            for index in self._connection.execute(
                "SELECT name, [unique] FROM pragma_index_list(?)",
                (table,),
            ):
                if not index["unique"]:
                    continue
                indexes.add(
                    tuple(
                        row["name"]
                        for row in self._connection.execute(
                            "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                            (index["name"],),
                        )
                    )
                )
            if not expected_unique_indexes[table].issubset(indexes):
                raise sqlite3.DatabaseError(
                    f"agent state table {table} has invalid uniqueness constraints"
                )

    def create_session(self, record: SessionRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_sessions
                (gateway_session_id, runtime_id, native_session_id, public_model_id,
                 native_model_id, provider_id, workspace_mode, workspace_path,
                 approval_policy, initial_native_cursor, last_native_cursor, status,
                 protocol_fingerprint, created_at, updated_at, released_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record.__dict__.values()),
            )

    def get_session(self, gateway_session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_sessions WHERE gateway_session_id = ?",
                (gateway_session_id,),
            ).fetchone()
        return SessionRecord(**dict(row)) if row else None

    def get_session_by_native(self, runtime_id: str, native_session_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_sessions WHERE runtime_id = ? AND native_session_id = ?",
                (runtime_id, native_session_id),
            ).fetchone()
        return SessionRecord(**dict(row)) if row else None

    def update_session(
        self,
        gateway_session_id: str,
        *,
        status: str | None = None,
        last_native_cursor: str | None = None,
        released_at: int | None = None,
        clear_released_at: bool = False,
    ) -> None:
        assignments = ["updated_at = ?"]
        values: list[object] = [int(time.time() * 1000)]
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if last_native_cursor is not None:
            assignments.append("last_native_cursor = ?")
            values.append(last_native_cursor)
        if released_at is not None:
            assignments.append("released_at = ?")
            values.append(released_at)
        elif clear_released_at:
            assignments.append("released_at = NULL")
        values.append(gateway_session_id)
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE agent_sessions SET {', '.join(assignments)} WHERE gateway_session_id = ?",
                values,
            )

    def mark_session_resumed(
        self,
        gateway_session_id: str,
        *,
        status: str,
        expected_last_cursor: str,
        resumed_cursor: str,
    ) -> None:
        now = int(time.time() * 1000)
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE agent_sessions
                SET status = CASE
                        WHEN status IN ('released', 'recovery_required') THEN ?
                        ELSE status
                    END,
                    last_native_cursor = CASE
                        WHEN last_native_cursor = ? THEN ?
                        ELSE last_native_cursor
                    END,
                    released_at = NULL,
                    updated_at = ?
                WHERE gateway_session_id = ?
                """,
                (status, expected_last_cursor, resumed_cursor, now, gateway_session_id),
            )

    def mark_sessions_for_recovery(self, runtime_id: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE agent_sessions
                SET status = 'recovery_required', updated_at = ?
                WHERE runtime_id = ? AND status != 'released'
                """,
                (int(time.time() * 1000), runtime_id),
            )
        return cursor.rowcount

    def create_turn(self, record: TurnRecord) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_turns
                (gateway_turn_id, gateway_session_id, native_turn_id, status,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                tuple(record.__dict__.values()),
            )

    def get_turn(self, gateway_session_id: str, gateway_turn_id: str) -> TurnRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_turns WHERE gateway_session_id = ? AND gateway_turn_id = ?",
                (gateway_session_id, gateway_turn_id),
            ).fetchone()
        return TurnRecord(**dict(row)) if row else None

    def get_turn_by_native(self, gateway_session_id: str, native_turn_id: str) -> TurnRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM agent_turns WHERE gateway_session_id = ? AND native_turn_id = ?",
                (gateway_session_id, native_turn_id),
            ).fetchone()
        return TurnRecord(**dict(row)) if row else None

    def update_turn(self, gateway_turn_id: str, status: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE agent_turns SET status = ?, updated_at = ? WHERE gateway_turn_id = ?",
                (status, int(time.time() * 1000), gateway_turn_id),
            )

    def update_turn_if_nonterminal(self, gateway_turn_id: str, status: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE agent_turns SET status = ?, updated_at = ?
                WHERE gateway_turn_id = ?
                  AND status NOT IN ('completed', 'cancelled', 'failed', 'unqueued')
                """,
                (status, int(time.time() * 1000), gateway_turn_id),
            )
        return cursor.rowcount == 1

    def transition_session_from_event(self, gateway_session_id: str, status: str) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE agent_sessions SET status = ?, updated_at = ?
                WHERE gateway_session_id = ?
                  AND status NOT IN ('released', 'recovery_required')
                """,
                (status, int(time.time() * 1000), gateway_session_id),
            )
        return cursor.rowcount == 1

    def get_idempotency(self, scope: str, key: str) -> sqlite3.Row | None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM agent_idempotency WHERE scope = ? AND idempotency_key = ?",
                (scope, key),
            ).fetchone()
            if (
                row is not None
                and row["status"] == "completed"
                and row["expires_at"] <= int(time.time() * 1000)
            ):
                self._connection.execute(
                    """
                    DELETE FROM agent_idempotency
                    WHERE scope = ? AND idempotency_key = ? AND status = 'completed'
                    """,
                    (scope, key),
                )
                return None
            return row

    def save_idempotency(
        self,
        *,
        scope: str,
        key: str,
        operation: str,
        request_hash: str,
        resource_id: str,
        command_id: str,
        status: str = "completed",
        ttl_seconds: int = 86400,
    ) -> None:
        now = int(time.time() * 1000)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_idempotency
                (scope, idempotency_key, operation, request_hash, gateway_resource_id,
                 native_command_id, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    key,
                    operation,
                    request_hash,
                    resource_id,
                    command_id,
                    status,
                    now,
                    now + ttl_seconds * 1000,
                ),
            )

    def complete_idempotency(self, scope: str, key: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE agent_idempotency SET status = 'completed'
                WHERE scope = ? AND idempotency_key = ? AND status = 'pending'
                """,
                (scope, key),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("idempotency reservation is not pending")

    def ensure_idempotency_completed(self, scope: str, key: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE agent_idempotency SET status = 'completed'
                WHERE scope = ? AND idempotency_key = ? AND status = 'pending'
                """,
                (scope, key),
            )
            if cursor.rowcount == 1:
                return
            row = self._connection.execute(
                """
                SELECT status FROM agent_idempotency
                WHERE scope = ? AND idempotency_key = ?
                """,
                (scope, key),
            ).fetchone()
            if row is None or row["status"] != "completed":
                raise sqlite3.IntegrityError("idempotency reservation is invalid")

    def complete_pending_scope_with_hash(self, scope: str, request_hash: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE agent_idempotency SET status = 'completed'
                WHERE scope = ? AND request_hash = ? AND status = 'pending'
                """,
                (scope, request_hash),
            )
        return cursor.rowcount

    def create_session_and_complete_idempotency(
        self,
        record: SessionRecord,
        *,
        scope: str,
        key: str,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_sessions
                (gateway_session_id, runtime_id, native_session_id, public_model_id,
                 native_model_id, provider_id, workspace_mode, workspace_path,
                 approval_policy, initial_native_cursor, last_native_cursor, status,
                 protocol_fingerprint, created_at, updated_at, released_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record.__dict__.values()),
            )
            cursor = self._connection.execute(
                """
                UPDATE agent_idempotency SET status = 'completed'
                WHERE scope = ? AND idempotency_key = ? AND status = 'pending'
                """,
                (scope, key),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("idempotency reservation is not pending")

    def create_turn_and_complete_idempotency(
        self,
        record: TurnRecord,
        *,
        scope: str,
        key: str,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO agent_turns
                (gateway_turn_id, gateway_session_id, native_turn_id, status,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                tuple(record.__dict__.values()),
            )
            cursor = self._connection.execute(
                """
                UPDATE agent_idempotency SET status = 'completed'
                WHERE scope = ? AND idempotency_key = ? AND status = 'pending'
                """,
                (scope, key),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("idempotency reservation is not pending")
            self._connection.execute(
                """
                UPDATE agent_sessions SET status = 'running', updated_at = ?
                WHERE gateway_session_id = ?
                """,
                (int(time.time() * 1000), record.gateway_session_id),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
