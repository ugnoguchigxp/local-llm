from __future__ import annotations

from fastapi import APIRouter

from api.local_inference import require_local_inference
from api.schemas import ModelListResponse
from core.commandcode_provider import COMMANDCODE_MODEL_ID, public_commandcode_models
from core.local_inference import local_inference_enabled
from core.model import get_model_manager

router = APIRouter(tags=["models"])


@router.get("/v1/models", response_model=ModelListResponse)
def list_models() -> ModelListResponse:
    data = []
    if local_inference_enabled():
        data.extend(get_model_manager().list_models())
    if COMMANDCODE_MODEL_ID in public_commandcode_models():
        data.append(
            {
                "id": COMMANDCODE_MODEL_ID,
                "object": "model",
                "created": 0,
                "owned_by": "command-code",
            }
        )
    if not data:
        require_local_inference()
    return ModelListResponse(object="list", data=data)
