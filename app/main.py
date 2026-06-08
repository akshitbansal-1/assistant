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
    _add_column_if_missing(insp, "linked_accounts", "last_fetched_at", "TIMESTAMP NULL")
    _add_column_if_missing(insp, "linked_accounts", "user_access_token", "TEXT NULL")
    _add_column_if_missing(insp, "people", "manager_person_id", "VARCHAR NULL")
    _add_column_if_missing(insp, "action_proposals", "rejected_by_person_id", "VARCHAR NULL")
    _add_column_if_missing(insp, "action_proposals", "rejected_at", "TIMESTAMP NULL")
    _add_column_if_missing(insp, "action_proposals", "rejection_reason", "TEXT NULL")
    _add_column_if_missing(insp, "action_proposals", "original_payload_json", "JSON NULL")
    yield


def _add_column_if_missing(insp, table_name: str, column_name: str, column_sql: str) -> None:
    existing_cols = [c["name"] for c in insp.get_columns(table_name)]
    if column_name not in existing_cols:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"))


app = FastAPI(title="Communication Loop Tracker", version="0.2.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=get_settings().secret_key)
app.include_router(router)
app.include_router(ui_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "static")), name="static")
