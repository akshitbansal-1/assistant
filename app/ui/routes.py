from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
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


def require_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    settings = get_settings()
    if not settings.enable_auth:
        svc = AccountService()
        local_email = settings.default_user_email or "demo@example.com"
        user = svc.get_or_create_user(db, local_email, "Demo User")
        db.commit()
        return user
    user_email = request.session.get("user_email")
    if not user_email:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/auth/login"},
        )
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/auth/login"},
        )
    return user


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui", status_code=302)


@router.get("/ui", include_in_schema=False)
def ui_index(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.enable_auth:
        local_email = settings.default_user_email or "demo@example.com"
        svc = AccountService()
        svc.get_or_create_user(db, local_email, "Demo User")
        db.commit()
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    if request.session.get("user_email"):
        return RedirectResponse(url="/ui/dashboard", status_code=302)
    return RedirectResponse(url="/auth/login", status_code=302)


@router.get("/ui/connect/{provider}", include_in_schema=False)
def ui_connect_account(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    oauth = OAuthService()
    provider_key = "gmail" if provider == "google" else provider
    try:
        state = oauth.encode_state(
            {
                "user_email": current_user.email,
                "redirect_to": "/ui/dashboard",
            }
        )
        authorization_url, _ = oauth.build_authorization_url(provider_key, state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=authorization_url, status_code=302)


@router.get("/ui/dashboard", include_in_schema=False)
def ui_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    settings = get_settings()
    user = current_user

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
            "enable_auth": settings.enable_auth,
        },
    )


# ── Auth routes ────────────────────────────────────────────────────────────────

@router.get("/auth/login", include_in_schema=False)
def auth_login(request: Request):
    settings = get_settings()
    if not settings.enable_auth:
        return RedirectResponse(url=f"/ui/users/{settings.default_user_email}", status_code=302)
    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "ui_login.html", {"error": error})


@router.get("/auth/google", include_in_schema=False)
def auth_google(request: Request):
    settings = get_settings()
    if not settings.enable_auth:
        return RedirectResponse(url=f"/ui/users/{settings.default_user_email}", status_code=302)
    oauth = OAuthService()
    try:
        state = oauth.encode_state({"redirect_to": "/ui"})
        authorization_url, _ = oauth.build_authorization_url("google_login", state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url=authorization_url, status_code=302)


@router.get("/auth/callback", include_in_schema=False)
def auth_callback(
    request: Request,
    code: str,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    oauth = OAuthService()
    try:
        token_payload = oauth.exchange_code("google_login", code)
        identity = oauth.fetch_account_identity("google_login", token_payload)
        email = identity["email"]
        name = identity["name"]
    except Exception as exc:
        return RedirectResponse(url=f"/auth/login?error={exc}", status_code=302)

    svc = AccountService()
    user = svc.get_or_create_user(db, email, name)
    db.commit()
    request.session["user_email"] = user.email
    return RedirectResponse(url="/ui/dashboard", status_code=302)


@router.get("/auth/logout", include_in_schema=False)
def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/auth/login", status_code=302)
