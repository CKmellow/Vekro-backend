from fastapi import FastAPI

from app.core.settings import get_settings
from app.routers.auth import router as auth_router
from app.routers.health import router as health_router

app = FastAPI(title="Vekro Backend")

app.include_router(auth_router)
app.include_router(health_router)


@app.on_event("startup")
async def validate_environment() -> None:
    get_settings()
