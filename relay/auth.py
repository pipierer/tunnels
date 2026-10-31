"""Authentication, sessions, CSRF and JWT helpers.

Implements:
- Argon2id password hashing (PROTOCOL.md §3.1).
- DB-backed sessions with random 32-byte hex tokens.
- CSRF double-submit cookie protection.
- Short-lived HS256 tunnel tokens with jti revocation.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db import get_db
from .models import AuditLog, RevokedToken, Session, Tunnel, User

logger = logging.getLogger("bentunnel.auth")

# Argon2id parameters mandated by the security section.
_pw_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

# Headers stripped from forwarded HTTP requests (anti-SSRF).
FORBIDDEN_FORWARD_HEADERS = {
    "host",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-real-ip",
    "connection",
    "content-length",
    "transfer-encoding",
}


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """Return an Argon2id hash of *plain*."""
    return _pw_hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time verify *plain* against an Argon2id *hashed* value."""
    if not hashed:
        return False
    try:
        return _pw_hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:  # pragma: no cover — malformed hash
        return False


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def _new_token() -> str:
    """64-char hex string (32 random bytes)."""
    return secrets.token_hex(32)


async def create_session(
    db: AsyncSession, user: User, request: Request
) -> Session:
    """Create a :class:`Session` row bound to *user*.

    Both ``session_token`` and ``csrf_token`` are 32-byte hex strings.
    Expires 7 days from now. The IP and User-Agent are recorded.
    """
    settings = get_settings()
    session = Session(
        user_id=user.id,
        session_token=_new_token(),
        csrf_token=_new_token(),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.SESSION_TTL_DAYS),
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """Resolve the current user from the session cookie, or ``None``."""
    settings = get_settings()
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        return None
    result = await db.execute(
        select(Session).where(Session.session_token == token)
    )
    session = result.scalar_one_or_none()
    if session is None:
        return None
    # Expiry check (timezone-aware compare).
    now = datetime.now(timezone.utc)
    exp = session.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < now:
        return None
    user_result = await db.execute(select(User).where(User.id == session.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        return None
    if user.is_suspended:
        return None
    return user


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """Dependency alias for :func:`get_current_user_optional`."""
    return await get_current_user_optional(request, db)


async def require_auth(
    user: Optional[User] = Depends(get_current_user),
) -> User:
    """401 if not logged in."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentification requise",
        )
    return user


async def require_admin(user: User = Depends(require_auth)) -> User:
    """403 if not admin."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Réservé aux administrateurs",
        )
    return user


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
async def csrf_protect(
    request: Request,
) -> None:
    """Double-submit cookie CSRF protection for mutating requests.

    Only enforced on POST/PUT/PATCH/DELETE. The ``X-CSRF-Token`` header
    must equal the ``bentunnel_csrf`` cookie value.

    Endpoints reachable without authentication (register/login) are
    exempted: the CSRF cookie is only issued *by* those endpoints, so
    requiring it on the very first call would be impossible. We detect
    that case by checking for the presence of the session cookie — if
    there is none, the caller is unauthenticated and CSRF does not
    apply.
    """
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    settings = get_settings()
    # Exempt unauthenticated callers (register/login).
    if not request.cookies.get(settings.SESSION_COOKIE_NAME):
        return
    cookie_val = request.cookies.get(settings.CSRF_COOKIE_NAME)
    header_val = request.headers.get("X-CSRF-Token")
    if (
        not cookie_val
        or not header_val
        or not secrets.compare_digest(cookie_val, header_val)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token CSRF invalide",
        )


# ---------------------------------------------------------------------------
# Tunnel JWT tokens
# ---------------------------------------------------------------------------
def create_tunnel_token(user: User, tunnel: Tunnel) -> tuple[str, int]:
    """Return ``(jwt, expires_in_seconds)`` for the given user/tunnel."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "tunnel_id": tunnel.tunnel_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.TUNNEL_TOKEN_TTL_SECONDS)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.BEN_TUNNEL_SECRET, algorithm="HS256")
    return token, settings.TUNNEL_TOKEN_TTL_SECONDS


async def verify_tunnel_token(
    token: str, db: AsyncSession
) -> tuple[User, Tunnel]:
    """Validate a tunnel JWT and return ``(user, tunnel)``.

    Raises :class:`HTTPException` (401) on any failure.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.BEN_TUNNEL_SECRET,
            algorithms=["HS256"],
            options={"require": ["sub", "tunnel_id", "iat", "exp", "jti"]},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token tunnel invalide: {exc}",
        ) from exc

    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(401, "Token tunnel invalide") from exc

    jti = payload["jti"]
    tunnel_id = payload["tunnel_id"]

    # 1. jti not revoked
    revoked = await db.execute(
        select(RevokedToken).where(RevokedToken.jti == jti)
    )
    if revoked.scalar_one_or_none() is not None:
        raise HTTPException(401, "Token tunnel révoqué")

    # 2. user exists and is not suspended
    user_row = await db.execute(select(User).where(User.id == user_id))
    user = user_row.scalar_one_or_none()
    if user is None:
        raise HTTPException(401, "Utilisateur introuvable")
    if user.is_suspended:
        raise HTTPException(403, "Utilisateur suspendu")

    # 3. tokens_invalidated_at: JWT iat must be >= that timestamp
    if user.tokens_invalidated_at is not None:
        invalidated = user.tokens_invalidated_at
        if invalidated.tzinfo is None:
            invalidated = invalidated.replace(tzinfo=timezone.utc)
        iat = datetime.fromtimestamp(int(payload["iat"]), tz=timezone.utc)
        if iat < invalidated:
            raise HTTPException(401, "Token tunnel invalidé par l'admin")

    # 4. tunnel belongs to user and is not deleted
    tunnel_row = await db.execute(
        select(Tunnel).where(
            Tunnel.tunnel_id == tunnel_id, Tunnel.user_id == user.id
        )
    )
    tunnel = tunnel_row.scalar_one_or_none()
    if tunnel is None:
        raise HTTPException(401, "Tunnel introuvable ou non autorisé")

    return user, tunnel


async def revoke_token(
    db: AsyncSession, jti: str, user_id: int, exp: datetime
) -> None:
    """Insert a row into ``revoked_tokens``."""
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    revoked = RevokedToken(jti=jti, user_id=user_id, expires_at=exp)
    db.add(revoked)
    await db.commit()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
async def audit_log(
    db: AsyncSession,
    user_id: Optional[int],
    action: str,
    ip: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    """Insert a row into ``audit_logs``."""
    entry = AuditLog(
        user_id=user_id,
        action=action[:64],
        ip_address=(ip or "")[:45] or None,
        details=details,
    )
    db.add(entry)
    await db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> Optional[str]:
    """Best-effort client IP extraction (Render trusts X-Forwarded-For)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:45] or None
    if request.client:
        return (request.client.host or "")[:45] or None
    return None
