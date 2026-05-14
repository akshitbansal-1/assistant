from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.account import User
from app.models.communication import ActionProposal, AuditLog, CommunicationTask, FollowUp, Person
from app.models.summary import DailySummary
from app.schemas.account import AccountCreate, AccountRead, OAuthCallbackRequest, OAuthStartResponse, UserRead
from app.schemas.actions import ActionApprovalRequest, ActionCancelRequest, ActionEditRequest, ActionRejectRequest
from app.schemas.pipeline import PipelineRunRequest, PipelineRunResponse, SummaryView
from app.services.actions import ActionProposalService
from app.services.account import AccountService
from app.services.authorization import AuthorizationService
from app.services.communication import CommunicationLoopService
from app.services.jira_hygiene import JiraHygieneService
from app.services.oauth import OAuthService
from app.services.ingestion import AccountAuthError
from app.services.pipeline import DailyWorkPipeline


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


class WhereIsRequest(BaseModel):
    user_email: str
    person: str
    task: str
    requester: str | None = None


class FollowUpRequest(BaseModel):
    user_email: str
    person: str
    task: str
    question: str
    requester: str | None = None


class FollowUpReplyRequest(BaseModel):
    slack_user_id: str
    text: str
    channel_id: str | None = None
    message_ts: str | None = None


class RetrievalRequest(BaseModel):
    user_email: str
    person: str | None = None
    jira_key: str | None = None
    task: str | None = None
    project: str | None = None
    slack_thread: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    commitment_status: str | None = None
    limit: int = 12


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _user_or_404(db: Session, user_email: str) -> User:
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _default_user(db: Session) -> User:
    settings = get_settings()
    service = AccountService()
    user = service.get_or_create_user(db, settings.default_user_email, "Demo User")
    db.flush()
    return user


async def _verify_slack_request(request: Request) -> bytes:
    body = await request.body()
    secret = get_settings().slack_signing_secret
    if not secret:
        return body
    timestamp = request.headers.get("x-slack-request-timestamp", "")
    signature = request.headers.get("x-slack-signature", "")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Slack signature")
    if abs(time.time() - int(timestamp)) > 60 * 5:
        raise HTTPException(status_code=401, detail="Stale Slack signature")
    base = f"v0:{timestamp}:{body.decode()}".encode()
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
    return body


async def _urlencoded_form(request: Request) -> dict[str, str]:
    body = await request.body()
    parsed = parse_qs(body.decode(), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


@router.get("/users/default", response_model=UserRead)
def default_user(db: Session = Depends(get_db)) -> User:
    service = AccountService()
    user = service.get_or_create_user(db, "demo@example.com", "Demo User")
    db.commit()
    return user


@router.post("/accounts", response_model=AccountRead)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)) -> AccountRead:
    service = AccountService()
    account = service.upsert_linked_account(db, payload)
    db.commit()
    db.refresh(account)
    logger.info("Linked account created/updated user=%s source=%s account=%s", payload.user_email, payload.source, account.id)
    return account


@router.get("/accounts/{user_email}", response_model=list[AccountRead])
def list_accounts(user_email: str, db: Session = Depends(get_db)) -> list[AccountRead]:
    service = AccountService()
    return service.active_accounts_for_user(db, user_email)


@router.get("/oauth/{provider}/start", response_model=OAuthStartResponse)
def oauth_start(provider: str, state: str | None = Query(default=None)) -> OAuthStartResponse:
    service = OAuthService()
    try:
        url, state = service.build_authorization_url(provider, state=state)
        return OAuthStartResponse(authorization_url=url, state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/oauth/{provider}/callback", include_in_schema=False)
def oauth_callback_browser(
    provider: str,
    code: str,
    state: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    oauth = OAuthService()
    accounts = AccountService()
    try:
        state_payload = oauth.decode_state(state)
        token_payload = oauth.exchange_code(provider, code)
        identity = oauth.fetch_account_identity(provider, token_payload)
        user_email = state_payload.get("user_email")
        if not user_email:
            raise ValueError("Missing user_email in OAuth state")
        redirect_to = state_payload.get("redirect_to") or f"/ui/users/{user_email}"
        extra_meta = identity.get("extra_metadata", {})
        account = accounts.upsert_linked_account(
            db,
            AccountCreate(
                user_email=user_email,
                source=provider,
                label=identity["label"],
                account_identifier=identity["account_identifier"],
                access_token=token_payload.get("access_token") or token_payload.get("authed_user", {}).get("access_token"),
                refresh_token=token_payload.get("refresh_token"),
                token_type=token_payload.get("token_type"),
                expires_at=token_payload.get("expires_at"),
                metadata={
                    "oauth_response": {
                        key: value
                        for key, value in token_payload.items()
                        if key not in {"access_token", "refresh_token"}
                    },
                    **extra_meta,
                },
            ),
        )
        db.commit()
        return RedirectResponse(url=f"{redirect_to}?linked={account.source}", status_code=302)
    except Exception as exc:
        db.rollback()
        fallback = "/ui"
        return RedirectResponse(url=f"{fallback}?oauth_error={str(exc)}", status_code=302)


@router.post("/oauth/{provider}/callback", response_model=AccountRead)
def oauth_callback(provider: str, payload: OAuthCallbackRequest, db: Session = Depends(get_db)) -> AccountRead:
    oauth = OAuthService()
    accounts = AccountService()
    try:
        token_payload = oauth.exchange_code(provider, payload.code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OAuth exchange failed: {exc}") from exc

    account = accounts.upsert_linked_account(
        db,
        AccountCreate(
            user_email=payload.user_email,
            source=provider,
            label=payload.label or f"{provider} account",
            account_identifier=payload.account_identifier or token_payload.get("team", {}).get("id") or token_payload.get("bot_user_id") or provider,
            access_token=token_payload.get("access_token"),
            refresh_token=token_payload.get("refresh_token"),
            token_type=token_payload.get("token_type"),
            expires_at=token_payload.get("expires_at"),
            metadata={
                "oauth_response": {
                    key: value
                    for key, value in token_payload.items()
                    if key not in {"access_token", "refresh_token"}
                }
            },
        ),
    )
    db.commit()
    db.refresh(account)
    return account


@router.post("/pipeline/run", response_model=PipelineRunResponse)
def run_pipeline(payload: PipelineRunRequest, db: Session = Depends(get_db)) -> PipelineRunResponse:
    pipeline = DailyWorkPipeline()
    try:
        logger.info("API pipeline run requested user=%s lookback_hours=%d force_fetch=%s", payload.user_email, payload.lookback_hours, payload.force_fetch)
        result = pipeline.run(
            db,
            user_email=payload.user_email,
            lookback_hours=payload.lookback_hours,
            delivery_channel=payload.delivery_channel,
            force_fetch=payload.force_fetch,
        )
    except AccountAuthError as exc:
        logger.warning("API pipeline run blocked by account auth user=%s error=%s", payload.user_email, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    logger.info("API pipeline run completed user=%s summary_id=%s", payload.user_email, result["summary_id"])
    return PipelineRunResponse(**result)


@router.post("/communication/whereis")
def whereis(payload: WhereIsRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = _user_or_404(db, payload.user_email)
    logger.info("API whereis requested user=%s person=%s task=%s", payload.user_email, payload.person, payload.task)
    try:
        result = CommunicationLoopService().answer_whereis(
            db,
            user,
            payload.person,
            payload.task,
            requester=payload.requester,
            enforce_authorization=bool(payload.requester),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    db.commit()
    logger.info("API whereis completed user=%s confidence=%.2f citations=%d", payload.user_email, result.get("confidence", 0), len(result.get("citations", [])))
    return result


@router.post("/communication/retrieve")
def retrieve_context(payload: RetrievalRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = _user_or_404(db, payload.user_email)
    loop = CommunicationLoopService()
    org = loop.get_or_create_organization_for_user(db, user)
    logger.info(
        "API retrieval requested user=%s person=%s jira_key=%s task=%s status=%s",
        payload.user_email,
        payload.person,
        payload.jira_key,
        payload.task,
        payload.commitment_status,
    )
    result = loop.retrieval.retrieve(
        db,
        organization_id=org.id,
        user_id=user.id,
        person=payload.person,
        jira_key=payload.jira_key,
        task_query=payload.task,
        project=payload.project,
        slack_thread=payload.slack_thread,
        start_at=payload.start_at,
        end_at=payload.end_at,
        commitment_status=payload.commitment_status,
        limit=payload.limit,
    )
    response = {
        "person": _serialize_person(result["person"]),
        "tasks": [_serialize_task(task) for task in result["tasks"]],
        "commitments": [
            {
                "id": item.id,
                "task_id": item.task_id,
                "text": item.commitment_text,
                "source": item.source_system,
                "source_url": item.source_url,
                "due_date": item.due_date.isoformat() if item.due_date else None,
                "status": item.status,
                "confidence": item.confidence,
            }
            for item in result["commitments"]
        ],
        "items": [
            {
                "id": item.id,
                "source": item.source,
                "external_id": item.external_id,
                "title": item.title,
                "timestamp": item.timestamp.isoformat() if item.timestamp else None,
                "metadata": item.metadata_json,
            }
            for item in result["items"]
        ],
        "citations": result["citations"],
    }
    logger.info(
        "API retrieval completed user=%s tasks=%d commitments=%d items=%d",
        payload.user_email,
        len(response["tasks"]),
        len(response["commitments"]),
        len(response["items"]),
    )
    return response


@router.post("/communication/followups")
def create_followup(payload: FollowUpRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = _user_or_404(db, payload.user_email)
    logger.info("API follow-up requested user=%s person=%s task=%s", payload.user_email, payload.person, payload.task)
    result = CommunicationLoopService().create_follow_up(
        db,
        user,
        person=payload.person,
        task_query=payload.task,
        question=payload.question,
        requester=payload.requester,
    )
    db.commit()
    follow_up: FollowUp = result["follow_up"]
    proposal: ActionProposal = result["proposal"]
    logger.info("API follow-up created user=%s follow_up=%s proposal=%s", payload.user_email, follow_up.id, proposal.id)
    return {
        "follow_up_id": follow_up.id,
        "proposal_id": proposal.id,
        "proposal_status": proposal.status,
        "approval_required": proposal.requires_approval,
        "whereis": result["whereis"],
    }


@router.post("/communication/followups/reply")
def capture_followup_reply(payload: FollowUpReplyRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    logger.info("API follow-up reply capture requested slack_user=%s channel=%s message_ts=%s", payload.slack_user_id, payload.channel_id, payload.message_ts)
    message = CommunicationLoopService().capture_follow_up_reply(
        db,
        slack_user_id=payload.slack_user_id,
        text=payload.text,
        channel_id=payload.channel_id,
        message_ts=payload.message_ts,
    )
    if not message:
        logger.info("API follow-up reply capture missed slack_user=%s", payload.slack_user_id)
        raise HTTPException(status_code=404, detail="No pending follow-up found for Slack user")
    db.commit()
    logger.info("API follow-up reply captured follow_up=%s message=%s", message.follow_up_id, message.id)
    return {"message_id": message.id, "follow_up_id": message.follow_up_id}


@router.post("/communication/followups/{follow_up_id}/draft-jira")
def draft_jira_update(follow_up_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        logger.info("API Jira draft requested follow_up=%s", follow_up_id)
        proposal = CommunicationLoopService().draft_jira_update_from_follow_up(db, follow_up_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    logger.info("API Jira draft created follow_up=%s proposal=%s", follow_up_id, proposal.id)
    return {"proposal_id": proposal.id, "status": proposal.status, "payload": proposal.payload_json}


@router.post("/actions/{proposal_id}/approve")
def approve_action(proposal_id: str, payload: ActionApprovalRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        logger.info("API action approval requested proposal=%s execute=%s", proposal_id, payload.execute)
        proposal = ActionProposalService().approve(
            db,
            proposal_id,
            approved_by_person_id=payload.approved_by_person_id,
            execute=payload.execute,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    logger.info("API action approval completed proposal=%s status=%s executed_at=%s", proposal.id, proposal.status, proposal.executed_at)
    return {"proposal_id": proposal.id, "status": proposal.status, "executed_at": proposal.executed_at}


@router.post("/actions/{proposal_id}/reject")
def reject_action(proposal_id: str, payload: ActionRejectRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        logger.info("API action rejection requested proposal=%s", proposal_id)
        proposal = ActionProposalService().reject(
            db,
            proposal_id,
            rejected_by_person_id=payload.rejected_by_person_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"proposal_id": proposal.id, "status": proposal.status, "rejection_reason": proposal.rejection_reason}


@router.post("/actions/{proposal_id}/cancel")
def cancel_action(proposal_id: str, payload: ActionCancelRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        proposal = ActionProposalService().cancel(
            db,
            proposal_id,
            actor_person_id=payload.actor_person_id,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"proposal_id": proposal.id, "status": proposal.status}


@router.post("/actions/{proposal_id}/edit")
def edit_action(proposal_id: str, payload: ActionEditRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        proposal = ActionProposalService().edit(
            db,
            proposal_id,
            payload=payload.payload,
            actor_person_id=payload.actor_person_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"proposal_id": proposal.id, "status": proposal.status, "payload": proposal.payload_json}


@router.post("/actions/{proposal_id}/execute")
def execute_action(proposal_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        logger.info("API action execute requested proposal=%s", proposal_id)
        proposal = ActionProposalService().execute(db, proposal_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    logger.info("API action execute completed proposal=%s status=%s external_url=%s", proposal.id, proposal.status, proposal.external_url)
    return {"proposal_id": proposal.id, "status": proposal.status, "executed_at": proposal.executed_at, "external_url": proposal.external_url}


@router.get("/communication/memory/{user_email}")
def task_memory(user_email: str, task: str | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = _user_or_404(db, user_email)
    loop = CommunicationLoopService()
    org = loop.get_or_create_organization_for_user(db, user)
    logger.info("API task memory requested user=%s task=%s", user_email, task)
    query = db.query(CommunicationTask).filter(CommunicationTask.organization_id == org.id)
    if task:
        query = query.filter(CommunicationTask.title.ilike(f"%{task}%"))
    tasks = query.order_by(CommunicationTask.updated_at.desc()).limit(50).all()
    response = {
        "tasks": [
            {
                "id": item.id,
                "title": item.title,
                "jira_key": item.jira_key,
                "status": item.status,
                "latest_status": item.latest_status,
                "blocker": item.blocker,
                "eta": item.eta,
                "confidence": item.confidence,
                "citations": item.source_citations_json,
            }
            for item in tasks
        ]
    }
    logger.info("API task memory completed user=%s tasks=%d", user_email, len(response["tasks"]))
    return response


@router.post("/communication/stale-jira/{user_email}")
def detect_stale_jira(user_email: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = _user_or_404(db, user_email)
    logger.info("API stale Jira requested user=%s", user_email)
    proposals = JiraHygieneService().detect_stale_tickets(db, user)
    db.commit()
    logger.info("API stale Jira completed user=%s proposals=%d", user_email, len(proposals))
    return {
        "proposals": [
            {
                "id": proposal.id,
                "title": proposal.title,
                "reason": proposal.reason,
                "status": proposal.status,
                "payload": proposal.payload_json,
            }
            for proposal in proposals
        ]
    }


@router.post("/slack/commands", include_in_schema=False)
async def slack_commands(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    await _verify_slack_request(request)
    form = await _urlencoded_form(request)
    command = str(form.get("command") or "").strip()
    text = str(form.get("text") or "").strip()
    requester = str(form.get("user_id") or "").strip() or None
    user = _default_user(db)
    loop = CommunicationLoopService()
    logger.info("Slack command received command=%s requester=%s text_chars=%d", command, requester, len(text))
    try:
        if command == "/whereis":
            person, task = _parse_person_task(text)
            result = loop.answer_whereis(db, user, person, task, requester=requester, enforce_authorization=True)
            db.commit()
            return {"response_type": "ephemeral", "text": _format_whereis_slack(result)}
        if command == "/followup":
            person, task, question = _parse_followup(text)
            result = loop.create_follow_up(db, user, person=person, task_query=task, question=question, requester=requester)
            db.commit()
            proposal: ActionProposal = result["proposal"]
            return {
                "response_type": "ephemeral",
                "text": f"Follow-up drafted and waiting for approval. Proposal: {proposal.id}",
                "blocks": ActionProposalService().slack_blocks(proposal),
            }
        if command == "/approve":
            proposal_id = text.split(maxsplit=1)[0] if text else ""
            if not proposal_id:
                raise ValueError("Usage: /approve proposal_id")
            org = loop.get_or_create_organization_for_user(db, user)
            approver = _slack_person_for_user(db, loop, user, org.id, requester)
            if not AuthorizationService().can_approve_actions(db, org.id, approver):
                raise PermissionError("Only managers or admins can approve proposals")
            proposal = ActionProposalService().approve(db, proposal_id, approved_by_person_id=approver.id if approver else None, execute=True)
            db.commit()
            return {"response_type": "ephemeral", "text": f"Proposal {proposal.status}: {proposal.id}"}
        if command == "/pending":
            org = loop.get_or_create_organization_for_user(db, user)
            count = db.query(FollowUp).filter(FollowUp.organization_id == org.id, FollowUp.status.in_(["pending", "sent"])).count()
            db.commit()
            return {"response_type": "ephemeral", "text": f"{count} follow-up(s) pending."}
        if command == "/memory":
            result = loop.answer_whereis(db, user, "", text)
            db.commit()
            return {"response_type": "ephemeral", "text": _format_whereis_slack(result)}
        if command == "/stale-jira":
            proposals = JiraHygieneService().detect_stale_tickets(db, user)
            db.commit()
            return {"response_type": "ephemeral", "text": f"{len(proposals)} Jira update draft(s) waiting for approval."}
    except ValueError as exc:
        logger.warning("Slack command failed command=%s error=%s", command, exc)
        return {"response_type": "ephemeral", "text": str(exc)}
    except PermissionError as exc:
        logger.warning("Slack command denied command=%s requester=%s error=%s", command, requester, exc)
        return {"response_type": "ephemeral", "text": str(exc)}
    logger.warning("Slack command unsupported command=%s", command)
    return {"response_type": "ephemeral", "text": f"Unsupported command: {command}"}


@router.post("/slack/events", include_in_schema=False)
async def slack_events(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    await _verify_slack_request(request)
    payload = await request.json()
    if payload.get("type") == "url_verification":
        logger.info("Slack URL verification received")
        return {"challenge": payload.get("challenge")}
    event = payload.get("event") or {}
    user = _default_user(db)
    loop = CommunicationLoopService()
    org = loop.get_or_create_organization_for_user(db, user)
    dedupe_key = _slack_event_dedupe_key(payload, event)
    if dedupe_key:
        existing = (
            db.query(AuditLog)
            .filter(
                AuditLog.organization_id == org.id,
                AuditLog.action == "slack.event.received",
                AuditLog.entity_id == dedupe_key,
            )
            .first()
        )
        if existing:
            logger.info("Duplicate Slack event ignored dedupe_key=%s", dedupe_key)
            return {"ok": True, "duplicate": True}
        db.add(
            AuditLog(
                organization_id=org.id,
                user_id=user.id,
                action="slack.event.received",
                entity_type="slack_event",
                entity_id=dedupe_key,
                metadata_json={
                    "team_id": payload.get("team_id"),
                    "event_type": event.get("type"),
                    "channel": event.get("channel"),
                    "message_ts": event.get("ts"),
                },
            )
        )
        db.flush()
    logger.info("Slack event received type=%s event_type=%s channel_type=%s", payload.get("type"), event.get("type"), event.get("channel_type"))
    if payload.get("type") == "event_callback" and event.get("type") == "message" and event.get("channel_type") == "im":
        message = loop.capture_follow_up_reply(
            db,
            slack_user_id=event.get("user", ""),
            text=event.get("text", ""),
            channel_id=event.get("channel"),
            message_ts=event.get("ts"),
        )
        if message:
            logger.info("Slack DM reply captured follow_up=%s message=%s", message.follow_up_id, message.id)
    db.commit()
    return {"ok": True}


@router.post("/slack/interactions", include_in_schema=False)
async def slack_interactions(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    await _verify_slack_request(request)
    form = await _urlencoded_form(request)
    try:
        payload = json.loads(str(form.get("payload") or "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid Slack payload") from exc
    if payload.get("type") != "block_actions":
        return {"response_type": "ephemeral", "text": "Unsupported Slack interaction"}
    action = (payload.get("actions") or [{}])[0]
    action_id = action.get("action_id")
    proposal_id = action.get("value")
    slack_user_id = (payload.get("user") or {}).get("id")
    user = _default_user(db)
    loop = CommunicationLoopService()
    org = loop.get_or_create_organization_for_user(db, user)
    approver = _slack_person_for_user(db, loop, user, org.id, slack_user_id)
    if not AuthorizationService().can_approve_actions(db, org.id, approver):
        return {"response_type": "ephemeral", "text": "Only managers or admins can approve or reject proposals."}
    if action_id == "approve_proposal":
        proposal = ActionProposalService().approve(db, proposal_id, approved_by_person_id=approver.id if approver else None, execute=True)
        db.commit()
        return {"response_type": "ephemeral", "replace_original": False, "text": f"Approved and executed proposal {proposal.id}."}
    if action_id == "reject_proposal":
        proposal = ActionProposalService().reject(
            db,
            proposal_id,
            rejected_by_person_id=approver.id if approver else None,
            reason="Rejected from Slack",
        )
        db.commit()
        return {"response_type": "ephemeral", "replace_original": False, "text": f"Rejected proposal {proposal.id}."}
    return {"response_type": "ephemeral", "text": "Unsupported proposal action."}


def _parse_person_task(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        raise ValueError('Usage: /whereis @person task_or_ticket')
    return parts[0].strip(), parts[1].strip()


def _parse_followup(text: str) -> tuple[str, str, str]:
    quoted = re.search(r'"([^"]+)"\s*$', text)
    if not quoted:
        raise ValueError('Usage: /followup @person task_or_ticket "question"')
    question = quoted.group(1).strip()
    head = text[: quoted.start()].strip()
    person, task = _parse_person_task(head)
    return person, task, question


def _format_whereis_slack(result: dict[str, Any]) -> str:
    citations = result.get("citations") or []
    citation_text = ""
    if citations:
        links = []
        for citation in citations[:3]:
            url = citation.get("url")
            title = citation.get("title") or citation.get("external_id") or citation.get("source")
            links.append(f"<{url}|{title}>" if url else str(title))
        citation_text = "\nSources: " + ", ".join(links)
    return f"{result.get('answer', 'No answer available.')}{citation_text}"


def _slack_person_for_user(db: Session, loop: CommunicationLoopService, user: User, organization_id: str, slack_user_id: str | None) -> Person | None:
    if not slack_user_id:
        return None
    return loop.resolve_slack_person(db, user, organization_id, f"<@{slack_user_id}>") or loop.get_or_create_person(
        db,
        organization_id,
        slack_user_id,
        source_system="slack",
        source_id=slack_user_id,
    )


def _slack_event_dedupe_key(payload: dict[str, Any], event: dict[str, Any]) -> str | None:
    event_id = payload.get("event_id")
    if event_id:
        return str(event_id)
    channel = event.get("channel")
    ts = event.get("ts") or event.get("event_ts")
    return f"{channel}:{ts}" if channel and ts else None


def _serialize_person(person) -> dict[str, Any] | None:
    if not person:
        return None
    return {
        "id": person.id,
        "display_name": person.display_name,
        "email": person.email,
        "source_ids": person.source_ids_json,
    }


def _serialize_task(task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "project": task.project,
        "jira_key": task.jira_key,
        "status": task.status,
        "latest_status": task.latest_status,
        "blocker": task.blocker,
        "eta": task.eta,
        "confidence": task.confidence,
        "citations": task.source_citations_json,
    }


@router.get("/summaries/{user_email}", response_model=list[SummaryView])
def list_summaries(user_email: str, db: Session = Depends(get_db)) -> list[DailySummary]:
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return (
        db.query(DailySummary)
        .filter(DailySummary.user_id == user.id)
        .order_by(DailySummary.summary_date.desc())
        .all()
    )


@router.get("/summaries/{user_email}/latest", response_model=SummaryView)
def latest_summary(user_email: str, db: Session = Depends(get_db)) -> DailySummary:
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    summary = (
        db.query(DailySummary)
        .filter(DailySummary.user_id == user.id)
        .order_by(DailySummary.summary_date.desc())
        .first()
    )
    if not summary:
        raise HTTPException(status_code=404, detail="No summaries found")
    return summary
