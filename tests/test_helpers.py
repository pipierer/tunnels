"""Tests unitaires pour les helpers de l'API (slug, rate limiter, tokens_invalidated_at)."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from relay.routes.api import (
    _LoginRateLimiter,
    _random_password,
    slugify,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Alice", "alice"),
        ("  Bob  ", "bob"),
        ("Charles Dupont", "charles-dupont"),
        ("Élise Müller", "lise-m-ller"),  # accents → non-ASCII supprimés, deviennent des tirets
        ("Jean-Pierre", "jean-pierre"),
        ("a" * 100, "a" * 24),  # tronqué à 24
        ("!!!", "user"),  # fallback si vide
        ("", "user"),
        ("123", "123"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_random_password_length_and_charset():
    """Le mot de passe généré fait 16 chars, alphanumérique."""
    pw = _random_password(16)
    assert len(pw) == 16
    assert pw.isalnum()


def test_random_password_uniqueness():
    """Deux appels donnent deux mots de passe différents (très probable)."""
    pw1 = _random_password(16)
    pw2 = _random_password(16)
    assert pw1 != pw2


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
def test_login_rate_limiter_allows_under_threshold():
    """5 tentatives max par 5 min — en dessous c'est autorisé."""
    rl = _LoginRateLimiter()
    for _ in range(4):
        rl.record_failure("alice@example.com")
    assert rl.is_locked("alice@example.com") is False


def test_login_rate_limiter_locks_at_threshold():
    """Au 5e échec → verrouillé."""
    rl = _LoginRateLimiter()
    for _ in range(5):
        rl.record_failure("alice@example.com")
    assert rl.is_locked("alice@example.com") is True


def test_login_rate_limiter_per_email_isolation():
    """Le verrouillage d'un email n'affecte pas un autre."""
    rl = _LoginRateLimiter()
    for _ in range(5):
        rl.record_failure("alice@example.com")
    assert rl.is_locked("alice@example.com") is True
    assert rl.is_locked("bob@example.com") is False


def test_login_rate_limiter_reset_clears_counter():
    rl = _LoginRateLimiter()
    for _ in range(5):
        rl.record_failure("alice@example.com")
    assert rl.is_locked("alice@example.com") is True
    rl.reset("alice@example.com")
    assert rl.is_locked("alice@example.com") is False


def test_login_rate_limiter_window_expires():
    """Les échecs de plus de 5 min ne comptent plus."""
    rl = _LoginRateLimiter()
    # Simuler 5 échecs « il y a 6 minutes » en injectant directement les timestamps.
    old_ts = datetime.now(timezone.utc).timestamp() - 360  # 6 min
    rl._attempts["alice@example.com"] = [old_ts] * 5
    # is_locked prune d'abord → ne doit plus être verrouillé.
    assert rl.is_locked("alice@example.com") is False
