from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.settings import get_settings
from app.routers.auth import router as auth_router
from app.routers.health import router as health_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_settings()
    yield


app = FastAPI(title="Vekro Backend", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(health_router)
