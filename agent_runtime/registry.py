from __future__ import annotations

from agent_runtime.base import AgentRuntime
from agent_runtime.errors import AgentRuntimeError


class RuntimeRegistry:
    def __init__(self, runtimes: list[AgentRuntime]) -> None:
        self._runtimes: dict[str, AgentRuntime] = {}
        for runtime in runtimes:
            if runtime.id in self._runtimes:
                raise ValueError(f"Duplicate Agent Runtime id: {runtime.id}")
            self._runtimes[runtime.id] = runtime

    def get(self, runtime_id: str) -> AgentRuntime:
        runtime = self._runtimes.get(runtime_id)
        if runtime is None:
            raise AgentRuntimeError(
                code="runtime_not_found",
                message=f"Unknown Agent Runtime: {runtime_id}",
                status_code=404,
                runtime=runtime_id,
            )
        return runtime

    def all(self) -> list[AgentRuntime]:
        return list(self._runtimes.values())

    async def close(self) -> None:
        first_error: Exception | None = None
        for runtime in self._runtimes.values():
            try:
                await runtime.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
