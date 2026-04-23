from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.db import Base, engine
from app.ui.routes import router as ui_router
from app.utils.logging import configure_logging


configure_logging()
@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Daily Work Intelligence Agent", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(ui_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "static")), name="static")
