from __future__ import annotations

from agent_runtime.base import AgentModel
from agent_runtime.errors import AgentRuntimeError
from agent_runtime.registry import RuntimeRegistry


class AgentCatalog:
    def __init__(self, registry: RuntimeRegistry) -> None:
        self._registry = registry

    async def list_models(self, runtime_id: str) -> list[AgentModel]:
        models = await self._registry.get(runtime_id).list_models()
        seen: set[str] = set()
        for model in models:
            if model.runtime != runtime_id or model.id in seen:
                raise AgentRuntimeError(
                    code="runtime_protocol_mismatch",
                    message="The runtime returned an ambiguous model catalog.",
                    status_code=503,
                    runtime=runtime_id,
                )
            seen.add(model.id)
        return models

    async def resolve(self, runtime_id: str, public_model_id: str) -> AgentModel:
        models = await self.list_models(runtime_id)
        for model in models:
            if model.id == public_model_id:
                return model
        raise AgentRuntimeError(
            code="agent_model_not_found",
            message=f"Agent model is not available: {public_model_id}",
            status_code=404,
            runtime=runtime_id,
        )
