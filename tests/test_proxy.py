"""Tests du reverse proxy HTTP : page offline quand tunnel déconnecté."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def client(monkeypatch):
    """Crée un TestClient en mockant init_db/bootstrap_admin (pas de DB réelle)."""
    # Patch les fonctions DB pour qu'elles ne plantent pas au démarrage.
    async def fake_init_db():
        return None

    async def fake_bootstrap_admin():
        return None

    async def fake_dispose():
        return None

    monkeypatch.setattr("relay.db.init_db", fake_init_db)
    monkeypatch.setattr("relay.db.bootstrap_admin", fake_bootstrap_admin)
    monkeypatch.setattr("relay.db.dispose_engine", fake_dispose)

    from relay.main import app
    with TestClient(app) as c:
        yield c


def test_health_endpoint_returns_200(client):
    """GET /health renvoie 200 avec status ok (utilisé par le cron keep-alive)."""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "uptime" in data


def test_offline_tunnel_returns_503_with_html(client, monkeypatch):
    """Un tunnel non connecté renvoie 503 avec une page HTML « hors ligne »."""
    from unittest.mock import MagicMock
    # Mock ws_manager.is_connected pour qu'il renvoie False.
    from relay.ws_relay import ws_manager
    monkeypatch.setattr(ws_manager, "is_connected", lambda tid: False)

    # _lookup_tunnel fait 2 SELECT : un pour Tunnel, un pour User.
    # On doit donc renvoyer tunnel mock au 1er appel, user mock au 2e.
    tunnel_mock = MagicMock()
    tunnel_mock.tunnel_id = "alice"
    tunnel_mock.user_id = 1
    tunnel_mock.is_active = 0
    user_mock = MagicMock()
    user_mock.id = 1
    user_mock.is_suspended = False  # important : bool falsy

    call_count = {"n": 0}

    def fake_get_session_factory():
        def factory():
            class FakeSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def execute(self, stmt):
                    call_count["n"] += 1
                    result = MagicMock()
                    if call_count["n"] == 1:
                        result.scalar_one_or_none = MagicMock(return_value=tunnel_mock)
                    else:
                        result.scalar_one_or_none = MagicMock(return_value=user_mock)
                    return result

            return FakeSession()
        return factory

    monkeypatch.setattr("relay.http_proxy.get_session_factory", fake_get_session_factory)

    r = client.get("/tunnels/alice/")
    assert r.status_code == 503
    assert "hors ligne" in r.text.lower() or "offline" in r.text.lower()


def test_unknown_tunnel_returns_404(client, monkeypatch):
    """Un tunnel qui n'existe pas en DB renvoie 404."""
    from unittest.mock import MagicMock
    from relay.ws_relay import ws_manager
    monkeypatch.setattr(ws_manager, "is_connected", lambda tid: False)

    def fake_get_session_factory():
        def factory():
            class FakeSession:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def execute(self, stmt):
                    result = MagicMock()
                    # Pour un tunnel inexistant, on renvoie None pour le premier SELECT (tunnel).
                    # Mais _lookup_tunnel fait aussi un SELECT User ensuite — on retourne None pour les deux.
                    result.scalar_one_or_none = MagicMock(return_value=None)
                    return result

            return FakeSession()
        return factory

    monkeypatch.setattr("relay.http_proxy.get_session_factory", fake_get_session_factory)

    r = client.get("/tunnels/nonexistent/")
    assert r.status_code == 404


def test_home_page_returns_200(client):
    """La page d'accueil se charge."""
    r = client.get("/")
    assert r.status_code == 200
    assert "bentunnel" in r.text.lower()


def test_login_page_returns_200(client):
    r = client.get("/login")
    assert r.status_code == 200


def test_register_page_returns_200(client):
    r = client.get("/register")
    assert r.status_code == 200


def test_static_css_served(client):
    """Le CSS est servi sous /static/css/."""
    r = client.get("/static/css/style.css")
    assert r.status_code == 200
    assert "css" in r.text.lower() or ":" in r.text


def test_robots_txt_returns_200(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "User-agent" in r.text
