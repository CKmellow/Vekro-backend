from fastapi import APIRouter

router = APIRouter(tags=["Health"])


@router.get("/health", summary="Service health check")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
