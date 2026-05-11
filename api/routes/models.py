from __future__ import annotations

from fastapi import APIRouter

from api.schemas import ModelListResponse
from core.model import get_model_manager

router = APIRouter(tags=["models"])


@router.get("/v1/models", response_model=ModelListResponse)
def list_models() -> ModelListResponse:
    manager = get_model_manager()
    return ModelListResponse(object="list", data=manager.list_models())
