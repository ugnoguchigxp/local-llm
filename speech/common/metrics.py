from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

try:
    import resource
except ImportError:  # pragma: no cover
    resource = None


@dataclass
class Metrics:
    service: str
    started_at: float = field(default_factory=time.monotonic)
    _counters: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    _gauges: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self._counters[name] += value

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            result = {**self._counters, **self._gauges}
        result["speech_uptime_seconds"] = time.monotonic() - self.started_at
        result["speech_process_rss_bytes"] = float(_rss_bytes())
        return result

    def prometheus(self) -> str:
        lines = [
            f'# speech service="{self.service}"',
        ]
        for name, value in sorted(self.snapshot().items()):
            safe_name = _metric_name(name)
            lines.append(f'{safe_name}{{service="{self.service}"}} {value}')
        return "\n".join(lines) + "\n"


def _metric_name(name: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch in "_:" else "_" for ch in name)
    return normalized if normalized.startswith("speech_") else f"speech_{normalized}"


def _rss_bytes() -> int:
    if resource is None:
        return 0
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if os.uname().sysname == "Darwin":
        return int(usage.ru_maxrss)
    return int(usage.ru_maxrss * 1024)
