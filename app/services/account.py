from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.account import LinkedAccount, User
from app.schemas.account import AccountCreate
from app.services.identity import IdentityService
from app.services.oauth import TokenCipher


class AccountService:
    def __init__(self) -> None:
        self.cipher = TokenCipher()
        self.identity = IdentityService()

    def get_or_create_user(self, db: Session, email: str, name: str | None = None) -> User:
        user = db.query(User).filter(User.email == email).first()
        if user:
            return user
        user = User(email=email, name=name)
        db.add(user)
        db.flush()
        return user

    def upsert_linked_account(self, db: Session, payload: AccountCreate) -> LinkedAccount:
        user = self.get_or_create_user(db, payload.user_email)
        metadata = self.identity.owner_metadata(
            user,
            payload.account_identifier,
            payload.label,
            payload.metadata,
        )
        account = (
            db.query(LinkedAccount)
            .filter(
                LinkedAccount.user_id == user.id,
                LinkedAccount.source == payload.source.value,
                LinkedAccount.account_identifier == payload.account_identifier,
            )
            .first()
        )
        encrypted_access = self.cipher.encrypt(payload.access_token)
        encrypted_refresh = self.cipher.encrypt(payload.refresh_token)
        if account:
            account.label = payload.label
            account.access_token = encrypted_access or account.access_token
            account.refresh_token = encrypted_refresh or account.refresh_token
            account.token_type = payload.token_type or account.token_type
            account.expires_at = payload.expires_at or account.expires_at
            account.metadata_json = {**(account.metadata_json or {}), **metadata}
            account.is_active = True
        else:
            account = LinkedAccount(
                user_id=user.id,
                source=payload.source.value,
                label=payload.label,
                account_identifier=payload.account_identifier,
                access_token=encrypted_access,
                refresh_token=encrypted_refresh,
                token_type=payload.token_type,
                expires_at=payload.expires_at,
                metadata_json=metadata,
                is_active=True,
            )
            db.add(account)
        db.flush()
        return account

    def active_accounts_for_user(self, db: Session, user_email: str) -> list[LinkedAccount]:
        user = db.query(User).filter(User.email == user_email).first()
        if not user:
            return []
        self.ensure_owner_metadata(db, user)
        return (
            db.query(LinkedAccount)
            .filter(LinkedAccount.user_id == user.id, LinkedAccount.is_active.is_(True))
            .all()
        )

    def ensure_owner_metadata(self, db: Session, user: User) -> None:
        accounts = (
            db.query(LinkedAccount)
            .filter(LinkedAccount.user_id == user.id, LinkedAccount.is_active.is_(True))
            .all()
        )
        for account in accounts:
            account.metadata_json = self.identity.owner_metadata(
                user,
                account.account_identifier,
                account.label,
                account.metadata_json or {},
            )
        db.flush()
