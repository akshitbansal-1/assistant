from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.account import LinkedAccount, User
from app.models.item import WorkItem
from app.models.memory import KnownEntity, TrackedTask
from app.models.summary import DailySummary
from app.services.account import AccountService
from app.services.oauth import OAuthService


BASE_DIR = Path(__file__).resolve().parents[2]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
router = APIRouter(tags=["ui"])


def _pretty_json(value: object) -> str:
    return json.dumps(value or {}, indent=2, default=str, sort_keys=True)


templates.env.filters["prettyjson"] = _pretty_json


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui", status_code=302)


@router.get("/ui", include_in_schema=False)
def ui_index(request: Request, db: Session = Depends(get_db)):
    AccountService().get_or_create_user(db, "demo@example.com", "Demo User")
    db.commit()
    users = db.query(User).order_by(User.email.asc()).all()
    user_cards: list[dict] = []
    for user in users:
        user_cards.append(
            {
                "user": user,
                "account_count": db.query(LinkedAccount).filter(LinkedAccount.user_id == user.id).count(),
                "summary_count": db.query(DailySummary).filter(DailySummary.user_id == user.id).count(),
                "item_count": db.query(WorkItem).filter(WorkItem.user_id == user.id).count(),
                "task_count": db.query(TrackedTask).filter(TrackedTask.user_id == user.id).count(),
            }
        )
    return templates.TemplateResponse(
        request,
        "ui_index.html",
        {
            "users": user_cards,
            "oauth_error": request.query_params.get("oauth_error"),
        },
    )


@router.get("/ui/users/{user_email:path}/connect/{provider}", include_in_schema=False)
def ui_connect_account(user_email: str, provider: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    oauth = OAuthService()
    provider_key = "gmail" if provider == "google" else provider
    try:
        state = oauth.encode_state(
            {
                "user_email": user.email,
                "redirect_to": f"/ui/users/{user.email}",
            }
        )
        authorization_url, _ = oauth.build_authorization_url(provider_key, state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=authorization_url, status_code=302)


@router.get("/ui/users/{user_email:path}", include_in_schema=False)
def ui_user_detail(user_email: str, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    accounts = (
        db.query(LinkedAccount)
        .filter(LinkedAccount.user_id == user.id)
        .order_by(LinkedAccount.source.asc(), LinkedAccount.created_at.desc())
        .all()
    )
    summaries = (
        db.query(DailySummary)
        .filter(DailySummary.user_id == user.id)
        .order_by(DailySummary.summary_date.desc())
        .all()
    )
    items = (
        db.query(WorkItem)
        .filter(WorkItem.user_id == user.id)
        .order_by(WorkItem.timestamp.desc())
        .limit(150)
        .all()
    )
    tracked_tasks = (
        db.query(TrackedTask)
        .filter(TrackedTask.user_id == user.id)
        .order_by(TrackedTask.last_seen_at.desc())
        .limit(100)
        .all()
    )
    entities = (
        db.query(KnownEntity)
        .filter(KnownEntity.user_id == user.id)
        .order_by(KnownEntity.updated_at.desc())
        .limit(100)
        .all()
    )

    metrics = {
        "linked_accounts": len(accounts),
        "summaries": len(summaries),
        "stored_items": db.query(WorkItem).filter(WorkItem.user_id == user.id).count(),
        "tracked_tasks": len(tracked_tasks),
        "known_entities": len(entities),
    }

    return templates.TemplateResponse(
        request,
        "ui_user_detail.html",
        {
            "user": user,
            "metrics": metrics,
            "accounts": accounts,
            "summaries": summaries,
            "items": items,
            "tracked_tasks": tracked_tasks,
            "entities": entities,
            "linked_provider": request.query_params.get("linked"),
        },
    )
