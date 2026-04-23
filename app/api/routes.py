from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.account import User
from app.models.summary import DailySummary
from app.schemas.account import AccountCreate, AccountRead, OAuthCallbackRequest, OAuthStartResponse, UserRead
from app.schemas.pipeline import PipelineRunRequest, PipelineRunResponse, SummaryView
from app.services.account import AccountService
from app.services.oauth import OAuthService
from app.services.pipeline import DailyWorkPipeline


router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
                    }
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
        result = pipeline.run(
            db,
            user_email=payload.user_email,
            lookback_hours=payload.lookback_hours,
            delivery_channel=payload.delivery_channel,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return PipelineRunResponse(**result)


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
