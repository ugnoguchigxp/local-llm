from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentRuntimeError(Exception):
    code: str
    message: str
    status_code: int = 500
    runtime: str | None = None
    retryable: bool = False
    retry_after: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def as_detail(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "runtime": self.runtime,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "request_id": request_id,
            **({"data": self.data} if self.data else {}),
        }


def runtime_unavailable(message: str, runtime: str = "muse") -> AgentRuntimeError:
    return AgentRuntimeError(
        code="runtime_unavailable",
        message=message,
        status_code=503,
        runtime=runtime,
        retryable=False,
    )


def billing_unverified(message: str, runtime: str = "muse") -> AgentRuntimeError:
    return AgentRuntimeError(
        code="runtime_billing_unverified",
        message=message,
        status_code=503,
        runtime=runtime,
        retryable=False,
    )
