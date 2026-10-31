"""Tests du protocole WebSocket BenTunnel (mock du côté serveur).

On simule un serveur BenTunnel qui envoie des HTTP_REQUEST et vérifie que le
client TunnelClient réagit correctement : PING→PONG, HTTP_REQUEST→forward→
HTTP_RESPONSE, etc. Le forward local est mocké pour ne pas dépendre d'un
vrai serveur HTTP.
"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
sys.path.insert(0, str(CLIENT_DIR))

import ws_client  # noqa: E402
from ws_client import TunnelClient  # noqa: E402


class FakeWS:
    """Un fake WebSocket qui simule ws.recv() / ws.send()."""

    def __init__(self, incoming_messages):
        # incoming_messages: liste de messages à renvoyer via recv().
        # Chaque entrée est soit un str (JSON) soit un bytes (binaire).
        self._incoming = list(incoming_messages)
        self.sent: list = []  # tous les messages envoyés (str ou bytes)
        self.open = True
        self._closed = False

    async def recv(self):
        if self._closed:
            raise ConnectionClosed(1000, "")
        if not self._incoming:
            # Bloque indéfiniment jusqu'à ce qu'on annule la tâche.
            await asyncio.sleep(3600)
        msg = self._incoming.pop(0)
        return msg

    async def send(self, data):
        if isinstance(data, (bytes, bytearray)):
            self.sent.append(bytes(data))
        else:
            self.sent.append(data)
        # Si on a reçu un message attendu (PONG/HTTP_RESPONSE), on l'écrit.
        return None

    async def close(self):
        self.open = False
        self._closed = True


class ConnectionClosed(Exception):
    def __init__(self, code, reason):
        self.code = code
        self.reason = reason
        super().__init__(f"closed code={code}")


def _make_msg(type_, payload, request_id=None, tunnel_id="alice"):
    return json.dumps({
        "type": type_,
        "request_id": request_id,
        "tunnel_id": tunnel_id,
        "payload": payload,
    })


def _parse_sent(sent_msg):
    """Si sent_msg est str, le parse en JSON."""
    if isinstance(sent_msg, str):
        return json.loads(sent_msg)
    return sent_msg  # bytes


@pytest.mark.asyncio
async def test_client_responds_to_ping_with_pong():
    """Le client reçoit PING et répond PONG avec le même ts."""
    logs = []
    fake = FakeWS([
        _make_msg("CLIENT_CONNECTED", {"tunnel_id": "alice", "server_time": 1, "max_request_bytes": 5*1024*1024, "max_response_bytes": 5*1024*1024}),
        _make_msg("PING", {"ts": 12345}, request_id="rid-1"),
    ])

    client = TunnelClient(
        server_url="https://test.invalid",
        session_cookie="sess",
        csrf_token="csrf",
        tunnel_id="alice",
        local_port=3000,
        public_url="https://test.invalid/tunnels/alice",
        on_log=lambda level, msg: logs.append((level, msg)),
    )

    # On patch fetch_tunnel_token et websockets.connect pour utiliser notre fake.
    async def fake_fetch(*args, **kwargs):
        return {"token": "tok", "expires_in": 300, "ws_url": "wss://test.invalid/ws"}

    class FakeConnect:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return fake

        async def __aexit__(self, *args):
            pass

    with patch.object(ws_client, "fetch_tunnel_token", fake_fetch), \
         patch.object(ws_client, "websockets", MagicMock(connect=FakeConnect)):
        # Lancer le client en tâche, l'annuler après un court délai.
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.3)
        await client.stop()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            task.cancel()

    # Vérifier qu'un PONG a été envoyé avec ts=12345.
    pong_messages = [
        _parse_sent(m) for m in fake.sent
        if isinstance(m, str)
    ]
    pongs = [m for m in pong_messages if m.get("type") == "PONG"]
    assert len(pongs) >= 1
    assert pongs[0]["payload"]["ts"] == 12345


@pytest.mark.asyncio
async def test_client_forwards_http_request_and_sends_response():
    """Le client reçoit HTTP_REQUEST, forward au local, renvoie HTTP_RESPONSE."""
    logs = []
    fake = FakeWS([
        _make_msg("CLIENT_CONNECTED", {"tunnel_id": "alice", "server_time": 1, "max_request_bytes": 5*1024*1024, "max_response_bytes": 5*1024*1024}),
        _make_msg(
            "HTTP_REQUEST",
            {
                "method": "GET",
                "path": "/",
                "query_string": "",
                "headers": {},
                "body_b64": None,
                "body_size": 0,
            },
            request_id="req-1",
        ),
    ])

    client = TunnelClient(
        server_url="https://test.invalid",
        session_cookie="sess",
        csrf_token="csrf",
        tunnel_id="alice",
        local_port=3000,
        public_url="https://test.invalid/tunnels/alice",
        on_log=lambda level, msg: logs.append((level, msg)),
    )

    # Mock fetch_tunnel_token et websockets.connect.
    async def fake_fetch(*args, **kwargs):
        return {"token": "tok", "expires_in": 300, "ws_url": "wss://test.invalid/ws"}

    class FakeConnect:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return fake

        async def __aexit__(self, *args):
            pass

    # Mock forward_to_local pour renvoyer une réponse connue.
    async def fake_forward(port, method, path, qs, headers, body):
        return (200, {"content-type": ["text/plain"]}, b"Hello from local")

    with patch.object(ws_client, "fetch_tunnel_token", fake_fetch), \
         patch.object(ws_client, "websockets", MagicMock(connect=FakeConnect)), \
         patch.object(ws_client, "forward_to_local", fake_forward):
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.5)
        await client.stop()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            task.cancel()

    # Vérifier qu'un HTTP_RESPONSE a été envoyé avec status_code=200 et le body.
    sent_messages = [_parse_sent(m) for m in fake.sent if isinstance(m, str)]
    responses = [m for m in sent_messages if m.get("type") == "HTTP_RESPONSE"]
    assert len(responses) >= 1
    resp = responses[0]
    assert resp["request_id"] == "req-1"
    assert resp["payload"]["status_code"] == 200
    assert resp["payload"]["body_size"] == len(b"Hello from local")
    assert resp["payload"]["has_body"] is True
    # Body inliné en base64 (car < 4 KB).
    decoded = base64.b64decode(resp["payload"]["body_b64"])
    assert decoded == b"Hello from local"


@pytest.mark.asyncio
async def test_client_sends_large_body_as_binary_frame():
    """Pour un corps > 4 KB, le client envoie une binary frame séparée."""
    logs = []
    fake = FakeWS([
        _make_msg("CLIENT_CONNECTED", {"tunnel_id": "alice", "server_time": 1, "max_request_bytes": 5*1024*1024, "max_response_bytes": 5*1024*1024}),
        _make_msg(
            "HTTP_REQUEST",
            {
                "method": "GET",
                "path": "/",
                "query_string": "",
                "headers": {},
                "body_b64": None,
                "body_size": 0,
            },
            request_id="req-big",
        ),
    ])

    client = TunnelClient(
        server_url="https://test.invalid",
        session_cookie="sess",
        csrf_token="csrf",
        tunnel_id="alice",
        local_port=3000,
        public_url="https://test.invalid/tunnels/alice",
        on_log=lambda level, msg: logs.append((level, msg)),
    )

    async def fake_fetch(*args, **kwargs):
        return {"token": "tok", "expires_in": 300, "ws_url": "wss://test.invalid/ws"}

    class FakeConnect:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return fake

        async def __aexit__(self, *args):
            pass

    # 10 KB de corps binaire.
    big_body = bytes(range(256)) * 40  # 10 240 octets
    async def fake_forward(port, method, path, qs, headers, body):
        return (200, {"content-type": ["application/octet-stream"]}, big_body)

    with patch.object(ws_client, "fetch_tunnel_token", fake_fetch), \
         patch.object(ws_client, "websockets", MagicMock(connect=FakeConnect)), \
         patch.object(ws_client, "forward_to_local", fake_forward):
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.5)
        await client.stop()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            task.cancel()

    # On doit avoir un HTTP_RESPONSE JSON suivi d'une binary frame.
    sent_str = [m for m in fake.sent if isinstance(m, str)]
    sent_bin = [m for m in fake.sent if isinstance(m, (bytes, bytearray))]
    responses = [json.loads(m) for m in sent_str if json.loads(m).get("type") == "HTTP_RESPONSE"]
    assert len(responses) >= 1
    assert responses[0]["payload"]["body_size"] == len(big_body)
    assert responses[0]["payload"]["body_b64"] is None  # trop gros pour inline
    # La binary frame suivante contient le corps.
    assert len(sent_bin) >= 1
    assert bytes(sent_bin[-1]) == big_body


@pytest.mark.asyncio
async def test_client_disconnects_on_suspended_status():
    """Si le serveur envoie TUNNEL_STATUS suspended, le client log et se déconnecte."""
    logs = []
    fake = FakeWS([
        _make_msg("CLIENT_CONNECTED", {"tunnel_id": "alice", "server_time": 1, "max_request_bytes": 5*1024*1024, "max_response_bytes": 5*1024*1024}),
        _make_msg("TUNNEL_STATUS", {"status": "suspended"}),
    ])

    client = TunnelClient(
        server_url="https://test.invalid",
        session_cookie="sess",
        csrf_token="csrf",
        tunnel_id="alice",
        local_port=3000,
        public_url="https://test.invalid/tunnels/alice",
        on_log=lambda level, msg: logs.append((level, msg)),
    )

    async def fake_fetch(*args, **kwargs):
        return {"token": "tok", "expires_in": 300, "ws_url": "wss://test.invalid/ws"}

    class FakeConnect:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return fake

        async def __aexit__(self, *args):
            pass

    with patch.object(ws_client, "fetch_tunnel_token", fake_fetch), \
         patch.object(ws_client, "websockets", MagicMock(connect=FakeConnect)):
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.5)
        await client.stop()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            task.cancel()

    # Vérifier qu'un log ERROR mentionne la suspension.
    error_logs = [msg for level, msg in logs if level == "ERROR"]
    assert any("suspend" in msg.lower() for msg in error_logs)


@pytest.mark.asyncio
async def test_client_redacts_jwt_in_logs():
    """Les JWT ne doivent jamais apparaître en clair dans les logs."""
    logs = []
    fake = FakeWS([])  # aucune réponse — on force une erreur réseau

    client = TunnelClient(
        server_url="https://test.invalid",
        session_cookie="sess",
        csrf_token="csrf",
        tunnel_id="alice",
        local_port=3000,
        public_url="https://test.invalid/tunnels/alice",
        on_log=lambda level, msg: logs.append((level, msg)),
    )

    # fetch_tunnel_token renvoie un faux JWT — il doit être redacté dans les logs.
    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiJ9.abc123signature"

    async def fake_fetch(*args, **kwargs):
        return {"token": fake_jwt, "expires_in": 300, "ws_url": "wss://test.invalid/ws"}

    class FakeConnect:
        def __init__(self, *args, **kwargs):
            raise OSError("connection refused")

    with patch.object(ws_client, "fetch_tunnel_token", fake_fetch), \
         patch.object(ws_client, "websockets", MagicMock(connect=FakeConnect)):
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.3)
        client.stopped = True
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            task.cancel()

    # Aucun log ne doit contenir le JWT en clair.
    for level, msg in logs:
        assert fake_jwt not in msg, f"JWT leak in log: {msg}"
        assert "eyJ" not in msg, f"JWT prefix leak in log: {msg}"
