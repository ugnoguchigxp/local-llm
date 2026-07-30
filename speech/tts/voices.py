from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from speech.common.errors import SpeechAPIError


class VoiceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.audio_root = root / "audio"
        self.trash_root = root / "trash"
        self.db_path = root / "voices.sqlite3"
        self._lock = threading.RLock()
        for path in (root, self.audio_root, self.trash_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()

    def create_consent(
        self,
        *,
        name: str,
        language: str,
        owner: str,
        usage_scope: str,
        recording: bytes,
        suffix: str,
    ) -> dict[str, object]:
        if not recording:
            raise SpeechAPIError(400, "consent recording is empty", "empty_recording")
        consent_id = f"cons_{uuid.uuid4().hex[:24]}"
        path = self.audio_root / f"{consent_id}{suffix}"
        created = int(time.time())
        with self._lock:
            path.write_bytes(recording)
            path.chmod(0o600)
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO consents
                            (id, name, language, owner, usage_scope, audio_path,
                             created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            consent_id,
                            name,
                            language,
                            owner,
                            usage_scope,
                            str(path),
                            created,
                        ),
                    )
            except Exception:
                with suppress(OSError):
                    path.unlink()
                raise
        return {
            "id": consent_id,
            "object": "audio.voice_consent",
            "name": name,
            "language": language,
            "owner": owner,
            "usage_scope": usage_scope,
            "created_at": created,
        }

    def create_voice(
        self,
        *,
        name: str,
        language: str,
        reference_text: str,
        consent_id: str | None,
        audio: bytes,
        suffix: str,
        source: str = "clone",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if not audio:
            raise SpeechAPIError(400, "voice audio is empty", "empty_audio_sample")
        if source == "clone" and (not consent_id or not self.get_consent(consent_id)):
            raise SpeechAPIError(
                400,
                "a valid consent id is required",
                "consent_required",
                param="consent",
            )
        voice_id = f"voice_{uuid.uuid4().hex[:24]}"
        path = self.audio_root / f"{voice_id}{suffix}"
        created = int(time.time())
        with self._lock:
            path.write_bytes(audio)
            path.chmod(0o600)
            try:
                with self._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO voices
                            (id, name, language, reference_text, consent_id,
                             audio_path, source, status, metadata_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            voice_id,
                            name,
                            language,
                            reference_text,
                            consent_id,
                            str(path),
                            source,
                            "active",
                            json.dumps(metadata or {}, ensure_ascii=False),
                            created,
                        ),
                    )
            except Exception:
                with suppress(OSError):
                    path.unlink()
                raise
        return self.get_voice(voice_id, include_internal=False) or {}

    def get_consent(self, consent_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM consents WHERE id = ?",
                (consent_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_voice(
        self,
        voice_id: str,
        *,
        include_internal: bool = True,
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM voices WHERE id = ? AND status != 'deleted'",
                (voice_id,),
            ).fetchone()
        if not row:
            return None
        return self._voice_dict(dict(row), include_internal=include_internal)

    def list_voices(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM voices WHERE status != 'deleted' "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [self._voice_dict(dict(row), include_internal=False) for row in rows]

    def delete_voice(self, voice_id: str) -> bool:
        with self._lock:
            voice = self.get_voice(voice_id)
            if not voice:
                return False
            source = Path(str(voice["audio_path"]))
            target: Path | None = None
            if source.exists():
                target = self.trash_root / source.name
                if target.exists():
                    target = self.trash_root / f"{uuid.uuid4().hex[:8]}-{source.name}"
                shutil.move(str(source), str(target))
            try:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE voices SET status = 'deleted' WHERE id = ?",
                        (voice_id,),
                    )
            except Exception:
                if target is not None and target.exists() and not source.exists():
                    with suppress(OSError):
                        shutil.move(str(target), str(source))
                raise
            return True

    def _voice_dict(
        self,
        row: dict[str, object],
        *,
        include_internal: bool,
    ) -> dict[str, object]:
        result = {
            "id": row["id"],
            "object": "audio.voice",
            "name": row["name"],
            "language": row["language"],
            "source": row["source"],
            "status": row["status"],
            "created_at": row["created_at"],
            "metadata": json.loads(str(row.get("metadata_json") or "{}")),
        }
        if include_internal:
            result.update(
                {
                    "reference_text": row["reference_text"],
                    "consent_id": row["consent_id"],
                    "audio_path": row["audio_path"],
                }
            )
        return result

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS consents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    language TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    usage_scope TEXT NOT NULL,
                    audio_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    language TEXT NOT NULL,
                    reference_text TEXT NOT NULL,
                    consent_id TEXT,
                    audio_path TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )


def safe_audio_suffix(filename: str | None, content_type: str | None) -> str:
    allowed = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".webm"}
    suffix = Path(filename or "").suffix.lower()
    if suffix in allowed:
        return suffix
    by_type = {
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/aac": ".aac",
        "audio/flac": ".flac",
        "audio/ogg": ".ogg",
        "audio/webm": ".webm",
    }
    return by_type.get(content_type or "", ".bin")
