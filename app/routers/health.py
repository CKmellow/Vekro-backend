from fastapi import APIRouter

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    summary="Service health check",
    description="Liveness endpoint used by operators, CI checks, and uptime monitors.",
)
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
