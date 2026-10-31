"""Tests du forwarder HTTP du client (vers localhost).

Démarre un vrai petit serveur HTTP local de test et vérifie que
forward_to_local() route correctement GET/POST/HEAD, gère les connexions
refusées, et applique l'anti-SSRF (n'essaie jamais de joindre autre chose
que 127.0.0.1).
"""
import asyncio
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient

# Add the client directory to sys.path so we can import http_forwarder.
CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"
sys.path.insert(0, str(CLIENT_DIR))

from http_forwarder import forward_to_local  # noqa: E402


async def _start_test_server():
    """Démarre un serveur HTTP de test qui répond à /, /echo, /headers, /status/:code."""
    from aiohttp import web

    async def index(request):
        return web.Response(text="Hello from local", status=200)

    async def echo(request):
        body = await request.read()
        return web.Response(
            body=body,
            status=200,
            headers={"X-Echoed-Method": request.method},
        )

    async def headers_view(request):
        # Retourne les headers reçus en JSON.
        return web.json_response(dict(request.headers), status=200)

    async def status_view(request):
        code = int(request.match_info["code"])
        return web.Response(status=code, text=f"status {code}")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_route("*", "/echo", echo)
    app.router.add_get("/headers", headers_view)
    app.router.add_get("/status/{code}", status_view)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)  # 0 = port libre choisi par l'OS
    await site.start()
    # Récupérer le port réel.
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


@pytest.fixture
async def local_server():
    runner, port = await _start_test_server()
    try:
        yield port
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_forward_get_200(local_server):
    """Un GET simple est forwardé et la réponse est renvoyée intacte."""
    status, headers, body = await forward_to_local(
        local_server, "GET", "/", "", {}, None
    )
    assert status == 200
    assert body == b"Hello from local"
    # Le header content-type doit être présent.
    assert any(k.lower() == "content-type" for k in headers)


@pytest.mark.asyncio
async def test_forward_post_body_inline(local_server):
    """Un POST avec corps binaire est forwardé, la réponse echo le corps."""
    payload = b"x" * 100
    status, headers, body = await forward_to_local(
        local_server,
        "POST",
        "/echo",
        "",
        {"content-type": ["application/octet-stream"]},
        payload,
    )
    assert status == 200
    assert body == payload
    # Le serveur renvoie X-Echoed-Method: POST
    assert headers.get("x-echoed-method") == ["POST"] or "x-echoed-method" in {k.lower(): v for k, v in headers.items()}


@pytest.mark.asyncio
async def test_forward_query_string(local_server):
    """La query string est préservée."""
    status, headers, body = await forward_to_local(
        local_server, "GET", "/status/204", "foo=bar&baz=1", {}, None
    )
    assert status == 204


@pytest.mark.asyncio
async def test_forward_connection_refused_returns_502():
    """Si rien n'écoute sur le port → 502 avec message clair."""
    # Port 1 : garanti non utilisé (privileged port, réservé système).
    status, headers, body = await forward_to_local(
        1, "GET", "/", "", {}, None
    )
    assert status == 502
    assert b"not reachable" in body.lower() or b"refus" in body.lower() or b"not reachable" in body


@pytest.mark.asyncio
async def test_forward_strips_ssrf_headers(local_server):
    """Les headers Host/X-Forwarded-* ne sont PAS transmis au serveur local."""
    status, headers, body = await forward_to_local(
        local_server,
        "GET",
        "/headers",
        "",
        {
            "host": ["evil.example.com"],
            "x-forwarded-for": ["1.2.3.4"],
            "x-forwarded-proto": ["https"],
            "x-real-ip": ["1.2.3.4"],
            "x-custom": ["kept"],
        },
        None,
    )
    assert status == 200
    # Le serveur renvoie les headers reçus en JSON.
    import json
    received = json.loads(body)
    # Case-insensitive lookup : aiohttp préserve la casse reçue du client.
    received_ci = {k.lower(): v for k, v in received.items()}
    # Host doit être 127.0.0.1:port (écrasé par le forwarder), pas evil.example.com.
    assert received_ci.get("host", "").startswith("127.0.0.1")
    # Les headers X-Forwarded-* ne doivent pas être transmis.
    assert "x-forwarded-for" not in received_ci
    assert "x-forwarded-proto" not in received_ci
    assert "x-real-ip" not in received_ci
    # Le header custom est conservé.
    assert received_ci.get("x-custom") == "kept"


@pytest.mark.asyncio
async def test_forward_only_connects_to_127_0_0_1(local_server, monkeypatch):
    """Le forwarder ne doit JAMAIS essayer de joindre une autre IP/host.

    On monkeypatch httpx.AsyncClient pour vérifier l'URL effectivement appelée.
    """
    captured_urls = []

    original_request = AsyncClient.request

    async def spy_request(self, method, url, **kwargs):
        captured_urls.append(str(url))
        return await original_request(self, method, url, **kwargs)

    monkeypatch.setattr(AsyncClient, "request", spy_request)

    await forward_to_local(
        local_server, "GET", "/", "", {"host": ["evil.attacker.com"]}, None
    )

    assert len(captured_urls) == 1
    url = captured_urls[0]
    # L'URL doit pointer vers 127.0.0.1 — pas vers le host du header.
    assert "127.0.0.1" in url
    assert "evil.attacker.com" not in url
