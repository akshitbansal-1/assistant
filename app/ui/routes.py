from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.account import LinkedAccount, User, UserInvitation
from app.models.communication import ActionProposal, AuditLog, Commitment, CommunicationTask, FollowUp, TaskStatusSnapshot
from app.models.item import WorkItem
from app.models.memory import KnownEntity, TrackedTask
from app.models.summary import DailySummary
from app.schemas.admin import InviteUserRequest
from app.services.account import AccountService
from app.services.actions import ActionProposalService
from app.services.admin import AdminService
from app.services.communication import CommunicationLoopService
from app.services.oauth import OAuthService


BASE_DIR = Path(__file__).resolve().parents[2]
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
router = APIRouter(tags=["ui"])


def _pretty_json(value: object) -> str:
    return json.dumps(value or {}, indent=2, default=str, sort_keys=True)


templates.env.filters["prettyjson"] = _pretty_json


def _account_health(account: LinkedAccount) -> dict[str, object]:
    metadata = account.metadata_json or {}
    now = datetime.now(timezone.utc)
    status = "ok"
    reasons: list[str] = []
    details: list[str] = []

    if not account.is_active:
        status = "error"
        reasons.append("Account is inactive")

    sample_mode = bool(metadata.get("sample_mode"))
    if sample_mode:
        details.append("sample data")
    elif not account.access_token:
        status = "error"
        reasons.append("Missing access token")

    expires_at = account.expires_at
    if expires_at is not None:
        expires_at = expires_at.replace(tzinfo=timezone.utc) if expires_at.tzinfo is None else expires_at.astimezone(timezone.utc)
        if expires_at <= now:
            status = "error"
            reasons.append("Token expired")
        elif expires_at <= now + timedelta(days=7):
            status = "warning" if status == "ok" else status
            reasons.append("Token expires soon")

    if account.source == "slack":
        missing = [key for key in ("team_id", "user_id") if not metadata.get(key)]
        if missing and not sample_mode:
            status = "warning" if status == "ok" else status
            reasons.append(f"Missing Slack metadata: {', '.join(missing)}")
        if not sample_mode and not account.user_access_token:
            status = "warning" if status == "ok" else status
            reasons.append("Missing Slack user token for history reads")
        if metadata.get("team_name"):
            details.append(str(metadata["team_name"]))
    elif account.source == "jira":
        missing = [key for key in ("cloud_id", "base_url") if not metadata.get(key)]
        if missing and not sample_mode:
            status = "warning" if status == "ok" else status
            reasons.append(f"Missing Jira metadata: {', '.join(missing)}")
        if metadata.get("site_url"):
            details.append(str(metadata["site_url"]))
    elif account.source == "notion":
        database_ids = metadata.get("database_ids")
        if database_ids:
            details.append(f"{len(database_ids)} database(s)")

    if not account.last_fetched_at:
        status = "warning" if status == "ok" else status
        reasons.append("Never fetched")

    return {
        "source": account.source,
        "label": account.label,
        "identifier": account.account_identifier,
        "status": status,
        "summary": "Ready" if status == "ok" else "; ".join(reasons),
        "details": ", ".join(details) if details else None,
        "last_fetched_at": account.last_fetched_at,
    }


def _organization_for_user(db: Session, user: User):
    return CommunicationLoopService().get_or_create_organization_for_user(db, user)


async def _urlencoded_form(request: Request) -> dict[str, str]:
    body = await request.body()
    parsed = parse_qs(body.decode(), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


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


class NotionTokenPayload(BaseModel):
    user_email: str
    token: str


@router.post("/ui/connect/notion/token", include_in_schema=False)
def ui_connect_notion_token(
    payload: NotionTokenPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_current_user),
) -> JSONResponse:
    import httpx

    token = payload.token.strip()
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                "https://api.notion.com/v1/users/me",
                headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            )
            resp.raise_for_status()
            me = resp.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid token — could not authenticate with Notion")

    name = me.get("name") or "Notion workspace"
    notion_id = me.get("id") or "notion"

    from app.schemas.account import AccountCreate
    svc = AccountService()
    account = svc.upsert_linked_account(
        db,
        AccountCreate(
            user_email=current_user.email,
            source="notion",
            label=name,
            account_identifier=notion_id,
            access_token=token,
        ),
    )
    db.commit()
    return JSONResponse({"id": str(account.id), "label": account.label})


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
    comm_tasks = db.query(CommunicationTask).filter(CommunicationTask.user_id == user.id).order_by(CommunicationTask.updated_at.desc()).limit(50).all()
    pending_followups = db.query(FollowUp).filter(FollowUp.user_id == user.id, FollowUp.status.in_(["pending", "sent"])).order_by(FollowUp.created_at.desc()).limit(50).all()
    overdue_commitments = (
        db.query(Commitment)
        .filter(
            Commitment.user_id == user.id,
            Commitment.status.in_(["open", "blocked", "stale"]),
            Commitment.due_date.isnot(None),
            Commitment.due_date <= date.today(),
        )
        .order_by(Commitment.due_date.asc())
        .limit(50)
        .all()
    )
    action_proposals = (
        db.query(ActionProposal)
        .filter(ActionProposal.user_id == user.id, ActionProposal.status == "pending_approval")
        .order_by(ActionProposal.created_at.desc())
        .limit(50)
        .all()
    )
    stale_jira = [proposal for proposal in action_proposals if proposal.target_system == "jira"]
    snapshots = (
        db.query(TaskStatusSnapshot)
        .join(CommunicationTask, CommunicationTask.id == TaskStatusSnapshot.task_id)
        .filter(CommunicationTask.user_id == user.id)
        .order_by(TaskStatusSnapshot.created_at.desc())
        .limit(50)
        .all()
    )
    audit_logs = db.query(AuditLog).filter(AuditLog.user_id == user.id).order_by(AuditLog.created_at.desc()).limit(50).all()
    connector_health = [_account_health(account) for account in accounts]

    metrics = {
        "linked_accounts": len(accounts),
        "connector_warnings": sum(1 for item in connector_health if item["status"] != "ok"),
        "summaries": len(summaries),
        "stored_items": db.query(WorkItem).filter(WorkItem.user_id == user.id).count(),
        "tracked_tasks": len(tracked_tasks),
        "known_entities": len(entities),
        "task_memory": len(comm_tasks),
        "pending_followups": len(pending_followups),
        "action_proposals": len(action_proposals),
    }

    return templates.TemplateResponse(
        request,
        "ui_user_detail.html",
        {
            "user": user,
            "metrics": metrics,
            "accounts": accounts,
            "connector_health": connector_health,
            "summaries": summaries,
            "items": items,
            "tracked_tasks": tracked_tasks,
            "entities": entities,
            "comm_tasks": comm_tasks,
            "pending_followups": pending_followups,
            "overdue_commitments": overdue_commitments,
            "stale_jira": stale_jira,
            "action_proposals": action_proposals,
            "snapshots": snapshots,
            "audit_logs": audit_logs,
            "linked_provider": request.query_params.get("linked"),
            "enable_auth": settings.enable_auth,
        },
    )


@router.get("/ui/admin", include_in_schema=False)
def ui_admin(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    org = _organization_for_user(db, current_user)
    admin = AdminService()
    members = admin.organization_members(db, org.id)
    invites = admin.pending_invites(db, org.id)
    people = admin.organization_people(db, org.id)
    manager_by_id = {person.id: person for person in people}
    return templates.TemplateResponse(
        request,
        "ui_admin.html",
        {
            "user": current_user,
            "organization": org,
            "members": members,
            "invites": invites,
            "people": people,
            "manager_by_id": manager_by_id,
            "base_url": str(request.base_url).rstrip("/"),
        },
    )


@router.post("/ui/admin/invites", include_in_schema=False)
async def ui_create_invite(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    form = await _urlencoded_form(request)
    org = _organization_for_user(db, current_user)
    payload = InviteUserRequest(
        email=str(form.get("email") or ""),
        name=str(form.get("name") or "") or None,
        role=str(form.get("role") or "member"),
        manager_email=str(form.get("manager_email") or "") or None,
    )
    AdminService().invite_user(db, organization=org, invited_by=current_user, payload=payload)
    db.commit()
    return RedirectResponse(url="/ui/admin?invited=1", status_code=303)


@router.get("/onboard/{token}", include_in_schema=False)
def ui_onboard(token: str, request: Request, db: Session = Depends(get_db)):
    invite = db.query(UserInvitation).filter(UserInvitation.token == token).first()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    return templates.TemplateResponse(request, "ui_onboard.html", {"invite": invite, "error": request.query_params.get("error")})


@router.post("/onboard/{token}", include_in_schema=False)
async def ui_accept_invite(token: str, request: Request, db: Session = Depends(get_db)):
    form = await _urlencoded_form(request)
    try:
        user = AdminService().accept_invite(db, token=token, name=str(form.get("name") or "") or None)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(url=f"/onboard/{token}?error={exc}", status_code=303)
    db.commit()
    request.session["user_email"] = user.email
    return RedirectResponse(url="/ui/dashboard", status_code=303)


@router.get("/ui/actions/{proposal_id}", include_in_schema=False)
def ui_action_detail(
    proposal_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_current_user),
):
    proposal = db.query(ActionProposal).filter(ActionProposal.id == proposal_id).first()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")
    audit_logs = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "action_proposal", AuditLog.entity_id == proposal.id)
        .order_by(AuditLog.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request,
        "ui_action_detail.html",
        {
            "user": current_user,
            "proposal": proposal,
            "audit_logs": audit_logs,
            "slack_blocks": ActionProposalService().slack_blocks(proposal),
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
