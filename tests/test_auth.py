"""Tests unitaires pour la logique d'authentification (sans DB)."""
import time
from datetime import datetime, timezone

import pytest
from jose import jwt

from relay.auth import (
    FORBIDDEN_FORWARD_HEADERS,
    create_tunnel_token,
    csrf_protect,
    hash_password,
    verify_password,
)
from relay.config import get_settings


def test_argon2_hash_and_verify_roundtrip():
    """Hash + verify d'un mot de passe — roundtrip valide."""
    h = hash_password("Sup3rSecret!")
    assert h != "Sup3rSecret!"
    assert h.startswith("$argon2id$")
    assert verify_password("Sup3rSecret!", h) is True


def test_argon2_verify_wrong_password_fails():
    """verify_password renvoie False pour un mauvais mot de passe."""
    h = hash_password("correct-horse-battery-staple")
    assert verify_password("wrong-password", h) is False


def test_argon2_verify_empty_hash_fails():
    """verify_password renvoie False si le hash est vide."""
    assert verify_password("anything", "") is False
    assert verify_password("anything", None) is False  # type: ignore[arg-type]


def test_hash_is_unique_per_call():
    """Deux hash du même mot de passe diffèrent (sel aléatoire)."""
    h1 = hash_password("SamePassword123")
    h2 = hash_password("SamePassword123")
    assert h1 != h2
    assert verify_password("SamePassword123", h1)
    assert verify_password("SamePassword123", h2)


def test_tunnel_token_is_valid_jwt_with_required_claims():
    """create_tunnel_token génère un JWT HS256 décodable avec sub/tunnel_id/iat/exp/jti."""
    settings = get_settings()

    class FakeUser:
        id = 42

    class FakeTunnel:
        tunnel_id = "alice"

    token, expires_in = create_tunnel_token(FakeUser(), FakeTunnel())
    assert expires_in == settings.TUNNEL_TOKEN_TTL_SECONDS

    payload = jwt.decode(
        token, settings.BEN_TUNNEL_SECRET, algorithms=["HS256"]
    )
    assert payload["sub"] == "42"
    assert payload["tunnel_id"] == "alice"
    assert "iat" in payload
    assert "exp" in payload
    assert "jti" in payload
    # exp = iat + TTL
    assert payload["exp"] - payload["iat"] == settings.TUNNEL_TOKEN_TTL_SECONDS


def test_tunnel_token_invalid_signature_rejected():
    """Un JWT signé avec une autre clé est rejeté par jose."""
    settings = get_settings()

    class FakeUser:
        id = 1

    class FakeTunnel:
        tunnel_id = "alice"

    token, _ = create_tunnel_token(FakeUser(), FakeTunnel())
    # Modifier la signature → decode doit lever une exception.
    with pytest.raises(Exception):
        jwt.decode(token, "wrong-secret", algorithms=["HS256"])
    # La bonne clé fonctionne.
    payload = jwt.decode(token, settings.BEN_TUNNEL_SECRET, algorithms=["HS256"])
    assert payload["sub"] == "1"


def test_forbidden_headers_include_ssrf_vectors():
    """La liste des headers strippés avant forwarding inclut bien les vecteurs SSRF."""
    for h in ("host", "x-forwarded-for", "x-forwarded-proto", "x-real-ip"):
        assert h in FORBIDDEN_FORWARD_HEADERS
