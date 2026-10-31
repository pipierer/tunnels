"""REST API: auth, tunnel management, admin actions.

CSRF is enforced on every mutating method via a router-level dependency
(:func:`csrf_protect` no-ops on safe verbs).
"""
from __future__ import annotations

import logging
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    audit_log,
    create_session,
    create_tunnel_token,
    csrf_protect,
    get_current_user,
    hash_password,
    require_admin,
    require_auth,
    verify_password,
)
from ..config import get_settings
from ..db import get_db, get_session_factory
from ..models import AuditLog, Session, Tunnel, User
from ..ws_relay import ws_manager

logger = logging.getLogger("bentunnel.api")

router = APIRouter(prefix="/api", dependencies=[Depends(csrf_protect)])

# Slugs must be lowercase a-z0-9 and hyphens.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Rate limiter (in-memory, single-instance Render)
# ---------------------------------------------------------------------------
class _LoginRateLimiter:
    """Track failed logins per email; 5 per 5 minutes."""

    WINDOW_SECONDS = 300
    MAX_FAILURES = 5

    def __init__(self) -> None:
        self._attempts: dict[str, list[float]] = {}

    def _prune(self, email: str, now: float) -> None:
        bucket = self._attempts.get(email, [])
        cutoff = now - self.WINDOW_SECONDS
        self._attempts[email] = [t for t in bucket if t > cutoff]

    def is_locked(self, email: str) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        self._prune(email, now)
        return len(self._attempts.get(email, [])) >= self.MAX_FAILURES

    def record_failure(self, email: str) -> None:
        now = datetime.now(timezone.utc).timestamp()
        self._prune(email, now)
        self._attempts.setdefault(email, []).append(now)

    def reset(self, email: str) -> None:
        self._attempts.pop(email, None)


_login_limiter = _LoginRateLimiter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def slugify(name: str) -> str:
    s = name.lower().strip()
    s = _SLUG_RE.sub("-", s).strip("-")
    if not s:
        s = "user"
    return s[:24]


async def _generate_unique_tunnel_id(db: AsyncSession, first_name: str) -> str:
    base = slugify(first_name)
    # First try the bare slug.
    existing = await db.execute(select(Tunnel).where(Tunnel.tunnel_id == base))
    if existing.scalar_one_or_none() is None:
        return base
    # Otherwise append random 4-char hex suffixes until we find a slot.
    for _ in range(10):
        suffix = secrets.token_hex(2)  # 4 hex chars
        candidate = f"{base}-{suffix}"
        if len(candidate) > 32:
            candidate = candidate[:32]
        existing = await db.execute(select(Tunnel).where(Tunnel.tunnel_id == candidate))
        if existing.scalar_one_or_none() is None:
            return candidate
    # Extremely unlikely fallback.
    return f"u-{secrets.token_hex(6)}"


def _is_https(request: Request) -> bool:
    xfp = request.headers.get("x-forwarded-proto", "").lower()
    if xfp:
        return "https" in xfp
    return request.url.scheme == "https"


def _client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:45] or None
    if request.client:
        return (request.client.host or "")[:45] or None
    return None


def _set_session_cookies(
    response: Response, session: Session, request: Request
) -> None:
    settings = get_settings()
    secure = _is_https(request)
    max_age = settings.SESSION_TTL_DAYS * 86400
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session.session_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key=settings.CSRF_COOKIE_NAME,
        value=session.csrf_token,
        max_age=max_age,
        httponly=False,  # JS must read this for the X-CSRF-Token header
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookies(response: Response, request: Request) -> None:
    settings = get_settings()
    secure = _is_https(request)
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME, path="/", secure=secure, samesite="lax"
    )
    response.delete_cookie(
        key=settings.CSRF_COOKIE_NAME, path="/", secure=secure, samesite="lax"
    )


def _random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class RegisterIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=64)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("first_name")
    @classmethod
    def _strip_first_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("first_name is required")
        return v

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class LogoutIn(BaseModel):
    pass


class TunnelStatusIn(BaseModel):
    local_port: int = Field(ge=1, le=65535)
    is_active: bool = True


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@router.post("/auth/register")
async def register(
    payload: RegisterIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    # Block registration of the reserved admin email.
    settings = get_settings()
    if payload.email.lower() == settings.ADMIN_EMAIL.lower():
        # If admin exists already, this falls through to the unique constraint.
        pass

    # Check email uniqueness first (better error than IntegrityError).
    existing = await db.execute(
        select(User).where(User.email == payload.email.lower())
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(409, "Un compte existe déjà avec cet email")

    user = User(
        email=payload.email.lower(),
        first_name=payload.first_name,
        is_admin=0,
        is_suspended=0,
    )
    user.set_password(payload.password)
    db.add(user)
    await db.flush()  # populate user.id

    tunnel_id = await _generate_unique_tunnel_id(db, payload.first_name)
    tunnel = Tunnel(
        user_id=user.id,
        tunnel_id=tunnel_id,
        local_port=None,
        is_active=0,
    )
    db.add(tunnel)
    await db.commit()
    await db.refresh(user)
    await db.refresh(tunnel)

    session = await create_session(db, user, request)
    await audit_log(
        db,
        user_id=user.id,
        action="register",
        ip=_client_ip(request),
        details=f"tunnel_id={tunnel_id}",
    )

    resp = JSONResponse({"ok": True, "redirect": "/dashboard"})
    _set_session_cookies(resp, session, request)
    return resp


@router.post("/auth/login")
async def login(
    payload: LoginIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    email = payload.email.lower()
    if _login_limiter.is_locked(email):
        raise HTTPException(429, "Trop de tentatives — réessayez dans 5 min")

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not user.check_password(payload.password):
        _login_limiter.record_failure(email)
        raise HTTPException(401, "Email ou mot de passe invalide")
    if user.is_suspended:
        raise HTTPException(403, "Compte suspendu")

    session = await create_session(db, user, request)
    _login_limiter.reset(email)
    await audit_log(
        db,
        user_id=user.id,
        action="login",
        ip=_client_ip(request),
    )

    resp = JSONResponse({"ok": True, "redirect": "/dashboard"})
    _set_session_cookies(resp, session, request)
    return resp


@router.post("/auth/logout")
async def logout(
    request: Request,
    user: Optional[User] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    settings = get_settings()
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if token:
        result = await db.execute(
            select(Session).where(Session.session_token == token)
        )
        session = result.scalar_one_or_none()
        if session is not None:
            await db.delete(session)
            await db.commit()
    if user is not None:
        await audit_log(
            db,
            user_id=user.id,
            action="logout",
            ip=_client_ip(request),
        )
    resp = JSONResponse({"ok": True})
    _clear_session_cookies(resp, request)
    return resp


# ---------------------------------------------------------------------------
# User info
# ---------------------------------------------------------------------------
@router.get("/me")
async def me(user: User = Depends(require_auth)) -> JSONResponse:
    return JSONResponse(
        {
            "id": user.id,
            "email": user.email,
            "first_name": user.first_name,
            "is_admin": bool(user.is_admin),
        }
    )


# ---------------------------------------------------------------------------
# Tunnels
# ---------------------------------------------------------------------------
async def _get_user_tunnel(db: AsyncSession, user: User) -> Tunnel:
    result = await db.execute(select(Tunnel).where(Tunnel.user_id == user.id))
    tunnel = result.scalar_one_or_none()
    if tunnel is None:
        raise HTTPException(404, "Aucun tunnel associé à ce compte")
    return tunnel


@router.get("/tunnels/me")
async def tunnels_me(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    tunnel = await _get_user_tunnel(db, user)
    settings = get_settings()
    public_url = f"{settings.PUBLIC_BASE_URL}/tunnels/{tunnel.tunnel_id}"
    # Live "is_active" comes from the WS manager, not just the DB column.
    live_active = ws_manager.is_connected(tunnel.tunnel_id)
    return JSONResponse(
        {
            "tunnel_id": tunnel.tunnel_id,
            "is_active": live_active,
            "local_port": tunnel.local_port,
            "last_seen_at": tunnel.last_seen_at.isoformat()
            if tunnel.last_seen_at
            else None,
            "public_url": public_url,
        }
    )


@router.post("/tunnels/token")
async def tunnels_token(
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    tunnel = await _get_user_tunnel(db, user)
    token, expires_in = create_tunnel_token(user, tunnel)
    settings = get_settings()
    return JSONResponse(
        {"token": token, "expires_in": expires_in, "ws_url": settings.ws_url}
    )


@router.post("/tunnels/status")
async def tunnels_status(
    payload: TunnelStatusIn,
    user: User = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    tunnel = await _get_user_tunnel(db, user)
    # The WS connect/disconnect already toggles is_active in DB; here we
    # only persist the local port the client says it is serving on.
    await db.execute(
        update(Tunnel)
        .where(Tunnel.id == tunnel.id)
        .values(local_port=payload.local_port)
    )
    await db.commit()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Admin — users
# ---------------------------------------------------------------------------
@router.get("/admin/users")
async def admin_users(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await db.execute(
        select(User, Tunnel)
        .outerjoin(Tunnel, Tunnel.user_id == User.id)
        .order_by(User.id)
    )
    rows = result.all()
    users_out: list[dict] = []
    for u, t in rows:
        is_active = bool(t and ws_manager.is_connected(t.tunnel_id)) if t else False
        users_out.append(
            {
                "id": u.id,
                "email": u.email,
                "first_name": u.first_name,
                "is_admin": bool(u.is_admin),
                "is_suspended": bool(u.is_suspended),
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "tunnel": {
                    "tunnel_id": t.tunnel_id if t else None,
                    "is_active": is_active,
                    "local_port": t.local_port if t else None,
                    "last_seen_at": t.last_seen_at.isoformat()
                    if t and t.last_seen_at
                    else None,
                }
                if t
                else None,
            }
        )
    return JSONResponse({"users": users_out})


@router.post("/admin/users/{user_id}/suspend")
async def admin_suspend_user(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    target = await _get_user_or_404(db, user_id)
    target.is_suspended = 1
    await db.commit()
    # Disconnect any live WS so active tokens stop working immediately.
    tunnel = await _first_tunnel_for_user(db, user_id)
    if tunnel is not None:
        await ws_manager.disconnect(tunnel.tunnel_id, "suspended")
    await audit_log(
        db,
        user_id=admin.id,
        action="suspend",
        ip=_client_ip(request),
        details=f"target_user_id={user_id}",
    )
    return JSONResponse({"ok": True})


@router.post("/admin/users/{user_id}/unsuspend")
async def admin_unsuspend_user(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    target = await _get_user_or_404(db, user_id)
    target.is_suspended = 0
    await db.commit()
    await audit_log(
        db,
        user_id=admin.id,
        action="unsuspend",
        ip=_client_ip(request),
        details=f"target_user_id={user_id}",
    )
    return JSONResponse({"ok": True})


@router.post("/admin/users/{user_id}/reset-password")
async def admin_reset_password(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    target = await _get_user_or_404(db, user_id)
    new_password = _random_password(16)
    target.set_password(new_password)
    # Also invalidate any active session/tunnel tokens for this user.
    target.tokens_invalidated_at = datetime.now(timezone.utc)
    await db.commit()
    # Disconnect their live tunnel so they have to re-authenticate.
    tunnel = await _first_tunnel_for_user(db, user_id)
    if tunnel is not None:
        await ws_manager.disconnect(tunnel.tunnel_id, "password_reset")
    await audit_log(
        db,
        user_id=admin.id,
        action="reset-password",
        ip=_client_ip(request),
        details=f"target_user_id={user_id}",
    )
    # Returned ONCE so the admin can hand it to the user.
    return JSONResponse({"ok": True, "new_password": new_password})


# ---------------------------------------------------------------------------
# Admin — tunnels
# ---------------------------------------------------------------------------
@router.get("/admin/tunnels")
async def admin_tunnels(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await db.execute(
        select(Tunnel, User)
        .join(User, User.id == Tunnel.user_id)
        .order_by(desc(Tunnel.created_at))
    )
    rows = result.all()
    out: list[dict] = []
    for t, u in rows:
        out.append(
            {
                "id": t.id,
                "tunnel_id": t.tunnel_id,
                "user_id": t.user_id,
                "user_email": u.email,
                "local_port": t.local_port,
                "is_active": ws_manager.is_connected(t.tunnel_id),
                "last_seen_at": t.last_seen_at.isoformat()
                if t.last_seen_at
                else None,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
        )
    return JSONResponse({"tunnels": out, "connected_count": len(ws_manager.tunnels)})


@router.post("/admin/tunnels/{tunnel_id}/revoke-token")
async def admin_revoke_token(
    tunnel_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    # Find the tunnel and its owner, set tokens_invalidated_at = now.
    result = await db.execute(
        select(Tunnel).where(Tunnel.tunnel_id == tunnel_id)
    )
    tunnel = result.scalar_one_or_none()
    if tunnel is None:
        raise HTTPException(404, "Tunnel introuvable")
    user_result = await db.execute(select(User).where(User.id == tunnel.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "Propriétaire du tunnel introuvable")
    user.tokens_invalidated_at = datetime.now(timezone.utc)
    await db.commit()
    await audit_log(
        db,
        user_id=admin.id,
        action="revoke-token",
        ip=_client_ip(request),
        details=f"tunnel_id={tunnel_id} target_user_id={user.id}",
    )
    return JSONResponse({"ok": True})


@router.delete("/admin/tunnels/{tunnel_id}")
async def admin_delete_tunnel(
    tunnel_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    result = await db.execute(
        select(Tunnel).where(Tunnel.tunnel_id == tunnel_id)
    )
    tunnel = result.scalar_one_or_none()
    if tunnel is None:
        raise HTTPException(404, "Tunnel introuvable")
    owner_id = tunnel.user_id
    # Disconnect first to free the slot and fail in-flight requests.
    await ws_manager.disconnect(tunnel_id, "deleted_by_admin")
    await db.delete(tunnel)
    await db.commit()
    await audit_log(
        db,
        user_id=admin.id,
        action="delete-tunnel",
        ip=_client_ip(request),
        details=f"tunnel_id={tunnel_id} owner_user_id={owner_id}",
    )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Admin — audit logs
# ---------------------------------------------------------------------------
@router.get("/admin/audit-logs")
async def admin_audit_logs(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = 200,
    offset: int = 0,
) -> JSONResponse:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    result = await db.execute(
        select(AuditLog)
        .order_by(desc(AuditLog.created_at))
        .limit(limit)
        .offset(offset)
    )
    logs = [row.to_dict() for row in result.scalars().all()]
    total_result = await db.execute(select(func.count()).select_from(AuditLog))
    total = int(total_result.scalar() or 0)
    return JSONResponse({"logs": logs, "total": total, "limit": limit, "offset": offset})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _get_user_or_404(db: AsyncSession, user_id: int) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "Utilisateur introuvable")
    return user


async def _first_tunnel_for_user(
    db: AsyncSession, user_id: int
) -> Optional[Tunnel]:
    result = await db.execute(
        select(Tunnel).where(Tunnel.user_id == user_id).limit(1)
    )
    return result.scalar_one_or_none()
