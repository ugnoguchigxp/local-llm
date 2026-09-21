from fastapi import HTTPException

from core.local_inference import local_inference_enabled


def require_local_inference() -> None:
    if not local_inference_enabled():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "local_inference_disabled",
                "message": "Local inference is disabled; external agent runtimes remain available.",
            },
        )
