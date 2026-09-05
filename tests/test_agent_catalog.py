from __future__ import annotations

import asyncio

import pytest

from agent_runtime.base import AgentModel
from agent_runtime.catalog import AgentCatalog
from agent_runtime.errors import AgentRuntimeError
from agent_runtime.registry import RuntimeRegistry


class CatalogRuntime:
    id = "muse"

    def __init__(self, models):
        self.models = models

    async def list_models(self):
        return self.models


def test_runtime_registry_rejects_duplicate_runtime_ids():
    with pytest.raises(ValueError, match="Duplicate Agent Runtime"):
        RuntimeRegistry([CatalogRuntime([]), CatalogRuntime([])])


def test_catalog_rejects_ambiguous_public_model_ids():
    model = AgentModel("muse/shared", "muse", "provider-a", "shared", "Shared")
    catalog = AgentCatalog(RuntimeRegistry([CatalogRuntime([model, model])]))

    with pytest.raises(AgentRuntimeError) as raised:
        asyncio.run(catalog.list_models("muse"))

    assert raised.value.code == "runtime_protocol_mismatch"
