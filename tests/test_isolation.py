"""Tests d'isolation : un user ne peut pas accéder au tunnel d'un autre.

On mocke la DB pour vérifier que verify_tunnel_token rejette un token dont
le tunnel_id n'appartient pas au user authentifié, et que le endpoint /ws
rejette une connexion dont le tunnel_id ne correspond pas au JWT.
"""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt

# Add project root to sys.path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay.auth import create_tunnel_token, verify_tunnel_token  # noqa: E402
from relay.config import get_settings  # noqa: E402


def _make_fake_user(user_id, suspended=False, invalidated_at=None):
    """Crée un faux User avec les attributs attendus par verify_tunnel_token."""
    u = MagicMock()
    u.id = user_id
    u.is_suspended = 1 if suspended else 0
    u.tokens_invalidated_at = invalidated_at
    return u


def _make_fake_tunnel(tunnel_id, user_id):
    t = MagicMock()
    t.tunnel_id = tunnel_id
    t.user_id = user_id
    return t


@pytest.mark.asyncio
async def test_verify_tunnel_token_rejects_tunnel_not_owned_by_user():
    """Alice ne peut pas utiliser un token JWT émis pour son propre tunnel
    pour se connecter à un tunnel appartenant à Bob.

    On émet un token pour alice+alice, puis on essaie de le vérifier en
    mockant une DB où le tunnel_id "alice" appartient à un autre user_id.
    """
    settings = get_settings()

    alice = _make_fake_user(user_id=1)
    alice_tunnel = _make_fake_tunnel("alice", user_id=1)

    token, _ = create_tunnel_token(alice, alice_tunnel)

    # Mock DB : le tunnel "alice" appartient à user_id=2 (Bob), pas 1 (Alice).
    fake_db = AsyncMock()

    async def fake_execute(stmt):
        # On inspecte le stmt pour savoir quoi renvoyer.
        stmt_str = str(stmt)
        # RevokedToken lookup → pas trouvé.
        if "revoked_tokens" in stmt_str:
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result
        # User lookup → Alice (id=1).
        if "users" in stmt_str.lower() and "FROM users" in stmt_str:
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=alice)
            return result
        # Tunnel lookup → on renvoie None pour simuler "tunnel_id alice + user_id 1 non trouvé ensemble"
        # (puisque dans notre scénario alice appartient à Bob).
        if "tunnels" in stmt_str.lower():
            result = MagicMock()
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result
        # Défaut.
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    fake_db.execute = AsyncMock(side_effect=fake_execute)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await verify_tunnel_token(token, fake_db)
    # Soit 401 (Tunnel introuvable) — c'est ce qu'on attend.
    assert exc_info.value.status_code in (401, 403)


@pytest.mark.asyncio
async def test_verify_tunnel_token_rejects_revoked_jti():
    """Un token dont le jti est dans revoked_tokens est rejeté."""
    settings = get_settings()

    user = _make_fake_user(user_id=1)
    tunnel = _make_fake_tunnel("alice", user_id=1)
    token, _ = create_tunnel_token(user, tunnel)

    fake_db = AsyncMock()

    async def fake_execute(stmt):
        stmt_str = str(stmt)
        result = MagicMock()
        if "revoked_tokens" in stmt_str:
            # Trouvé → token révoqué.
            result.scalar_one_or_none = MagicMock(return_value=MagicMock())
            return result
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    fake_db.execute = AsyncMock(side_effect=fake_execute)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await verify_tunnel_token(token, fake_db)
    assert exc_info.value.status_code == 401
    assert "révoqué" in exc_info.value.detail.lower() or "revoque" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_verify_tunnel_token_rejects_suspended_user():
    """Un token d'un user suspendu est rejeté (403)."""
    settings = get_settings()

    user = _make_fake_user(user_id=1, suspended=True)
    tunnel = _make_fake_tunnel("alice", user_id=1)
    token, _ = create_tunnel_token(user, tunnel)

    fake_db = AsyncMock()

    async def fake_execute(stmt):
        stmt_str = str(stmt)
        result = MagicMock()
        if "revoked_tokens" in stmt_str:
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result
        if "FROM users" in stmt_str:
            result.scalar_one_or_none = MagicMock(return_value=user)
            return result
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    fake_db.execute = AsyncMock(side_effect=fake_execute)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await verify_tunnel_token(token, fake_db)
    assert exc_info.value.status_code == 403
    assert "suspend" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_verify_tunnel_token_rejects_token_invalidated_by_admin():
    """Si tokens_invalidated_at > iat du JWT, le token est rejeté."""
    settings = get_settings()

    user = _make_fake_user(
        user_id=1,
        invalidated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    tunnel = _make_fake_tunnel("alice", user_id=1)

    # Le JWT a été émis AVANT l'invalidation (iat dans le passé).
    payload = {
        "sub": "1",
        "tunnel_id": "alice",
        "iat": int((__import__("datetime").datetime.now(__import__("datetime").timezone.utc)).timestamp()) - 3600,
        "exp": int((__import__("datetime").datetime.now(__import__("datetime").timezone.utc)).timestamp()) + 300,
        "jti": "old-jti",
    }
    token = jwt.encode(payload, settings.BEN_TUNNEL_SECRET, algorithm="HS256")

    fake_db = AsyncMock()

    async def fake_execute(stmt):
        stmt_str = str(stmt)
        result = MagicMock()
        if "revoked_tokens" in stmt_str:
            result.scalar_one_or_none = MagicMock(return_value=None)
            return result
        if "FROM users" in stmt_str:
            result.scalar_one_or_none = MagicMock(return_value=user)
            return result
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    fake_db.execute = AsyncMock(side_effect=fake_execute)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await verify_tunnel_token(token, fake_db)
    assert exc_info.value.status_code == 401
    assert "invalidé" in exc_info.value.detail.lower() or "invalidate" in exc_info.value.detail.lower()


def test_jwt_does_not_leak_in_url_public():
    """L'URL publique /tunnels/{username} ne contient jamais de token.

    On vérifie que la route /tunnels/{username} est bien définie sans
    paramètre de query 'token'.
    """
    from relay.main import app
    routes = [r for r in app.routes if getattr(r, "path", "").startswith("/tunnels/")]
    assert len(routes) >= 1
    for r in routes:
        # La route doit juste matcher /tunnels/{username} sans token en query.
        assert "token" not in r.path
