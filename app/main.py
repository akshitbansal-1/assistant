from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from starlette.middleware.sessions import SessionMiddleware

import app.models  # noqa: F401 - register SQLAlchemy models before create_all
from app.api.routes import router
from app.config import get_settings
from app.db import Base, engine
from app.ui.routes import router as ui_router
from app.utils.logging import configure_logging


configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    insp = inspect(engine)
    existing_cols = [c["name"] for c in insp.get_columns("linked_accounts")]
    if "last_fetched_at" not in existing_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE linked_accounts ADD COLUMN last_fetched_at TIMESTAMP NULL"))
    yield


app = FastAPI(title="Daily Work Intelligence Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=get_settings().secret_key)
app.include_router(router)
app.include_router(ui_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "static")), name="static")
