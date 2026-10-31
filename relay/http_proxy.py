"""HTTP reverse proxy: visitor → /tunnels/{id} → WebSocket → client → 127.0.0.1:port.

This module turns an inbound visitor request into an ``HTTP_REQUEST`` WS
message, awaits the client's ``HTTP_RESPONSE``, and converts it back into a
Starlette :class:`Response`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from .auth import FORBIDDEN_FORWARD_HEADERS
from .db import get_session_factory
from .models import Tunnel, User
from .ws_relay import TunnelError, ws_manager

logger = logging.getLogger("bentunnel.proxy")

OFFLINE_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tunnel hors ligne — BenTunnel</title>
<link rel="stylesheet" href="/static/css/style.css">
</head>
<body class="offline-page">
  <main class="offline-card">
    <div class="offline-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="64" height="64" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"></circle>
        <line x1="4.93" y1="4.93" x2="19.07" y2="19.07"></line>
      </svg>
    </div>
    <h1>Ce tunnel est actuellement hors ligne.</h1>
    <p>Le client local n'est pas connecté. Demandez au propriétaire de relancer&nbsp;:</p>
    <pre><code>python bentunnel.py start PORT</code></pre>
    <a class="btn-primary" href="/">Retour à l'accueil</a>
  </main>
</body>
</html>"""


def _strip_headers(raw_headers) -> dict[str, list[str]]:
    """Convert Starlette scope headers into our wire format.

    - Lowercase header names.
    - Group multiple values for the same header into a list.
    - Drop forbidden forwarding headers (anti-SSRF per PROTOCOL.md §2.4).
    """
    out: dict[str, list[str]] = {}
    for key, value in raw_headers:
        if value is None:
            continue
        name = key.decode("latin-1").lower()
        if name in FORBIDDEN_FORWARD_HEADERS:
            continue
        val = value.decode("latin-1")
        out.setdefault(name, []).append(val)
    return out


def _flatten_headers(headers) -> list[tuple[str, str]]:
    """Flatten headers into a list of (name, value) tuples for Starlette.

    Accepts multiple wire formats defensively:
    - ``{name: [v1, v2]}`` (canonical, sent by BenTunnel client)
    - ``{name: v1}`` (single-value dict)
    - ``[(name, v1), (name, v2)]`` (list of tuples, e.g. Starlette raw)
    - ``[(b"name", b"v1")]`` (bytes variant)
    """
    flat: list[tuple[str, str]] = []
    if headers is None:
        return flat

    # Case 1: dict-like (has .items())
    if hasattr(headers, "items"):
        for name, values in headers.items():
            if values is None:
                continue
            if isinstance(values, (list, tuple)):
                for v in values:
                    flat.append((str(name), str(v)))
            else:
                flat.append((str(name), str(values)))
        return flat

    # Case 2: iterable of (name, value) tuples (possibly bytes)
    try:
        for pair in headers:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            name, value = pair
            if name is None or value is None:
                continue
            if isinstance(name, (bytes, bytearray)):
                name = name.decode("latin-1")
            if isinstance(value, (bytes, bytearray)):
                value = value.decode("latin-1")
            flat.append((str(name), str(value)))
    except TypeError:
        logger.warning("Unexpected headers format: %r", type(headers))
    return flat


async def _lookup_tunnel(tunnel_id: str) -> Optional[tuple[Tunnel, User]]:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(Tunnel).where(Tunnel.tunnel_id == tunnel_id)
        )
        tunnel = result.scalar_one_or_none()
        if tunnel is None:
            return None
        user_result = await session.execute(
            select(User).where(User.id == tunnel.user_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            return None
        return tunnel, user


async def forward_request(
    tunnel_id: str,
    method: str,
    path: str,
    query_string: str,
    headers: dict[str, list[str]],
    body: bytes,
    request: Request,  # noqa: ARG001 — kept for signature parity
) -> Response:
    """Forward a visitor request through the tunnel."""
    try:
        return await _forward_request_impl(
            tunnel_id, method, path, query_string, headers, body
        )
    except Exception as exc:
        logger.exception("forward_request crashed for tunnel %s: %s", tunnel_id, exc)
        return Response(
            content=(
                f"Proxy error: {type(exc).__name__}: {exc}\n".encode("utf-8")
            ),
            status_code=502,
            media_type="text/plain",
        )


async def _forward_request_impl(
    tunnel_id: str,
    method: str,
    path: str,
    query_string: str,
    headers: dict[str, list[str]],
    body: bytes,
) -> Response:
    """Forward a visitor request through the tunnel (inner implementation)."""
    # 1. Lookup tunnel + owner.
    found = await _lookup_tunnel(tunnel_id)
    if found is None:
        return HTMLResponse(
            OFFLINE_HTML, status_code=404, headers={"Cache-Control": "no-store"}
        )
    tunnel, user = found
    if user.is_suspended:
        return HTMLResponse(
            OFFLINE_HTML, status_code=403, headers={"Cache-Control": "no-store"}
        )

    # 2. Connected?
    if not ws_manager.is_connected(tunnel_id):
        return HTMLResponse(
            OFFLINE_HTML, status_code=503, headers={"Cache-Control": "no-store"}
        )

    # 3. Forward.
    try:
        result = await ws_manager.send_request(
            tunnel_id=tunnel_id,
            method=method,
            path=path,
            query_string=query_string,
            headers=headers,
            body=body,
        )
    except TunnelError as exc:
        logger.info(
            "Tunnel %s error: %s (%s) → HTTP %s",
            tunnel_id,
            exc.code,
            exc,
            exc.http_status,
        )
        if exc.http_status == 503:
            return HTMLResponse(
                OFFLINE_HTML, status_code=503, headers={"Cache-Control": "no-store"}
            )
        return Response(
            content=f"Upstream error: {exc.code}\n".encode(),
            status_code=exc.http_status,
            media_type="text/plain",
        )

    status_code = int(result.get("status_code", 502))
    raw_headers = result.get("headers") or {}
    # Debug : logger le type exact pour diagnostiquer les problèmes de format.
    logger.debug(
        "Tunnel %s response: status=%d, headers_type=%s, headers=%r",
        tunnel_id,
        status_code,
        type(raw_headers).__name__,
        raw_headers,
    )
    flat = _flatten_headers(raw_headers)
    # Drop hop-by-hop / forbidden headers from the response too.
    # Also drop content-length (Starlette will recompute it from the body)
    # and content-encoding (we serve raw decoded bytes).
    flat = [
        (k, v)
        for (k, v) in flat
        if k.lower() not in FORBIDDEN_FORWARD_HEADERS
        and k.lower() != "content-encoding"
        and k.lower() != "content-length"
    ]
    # Starlette's Response.headers expects a dict[str, str] (it calls .items()
    # then .encode() on each value). Multi-value headers are joined with ", "
    # per RFC 7230 §3.2.2 (e.g. "Set-Cookie" should ideally remain separate,
    # but for the common case a single comma-joined value works).
    headers_dict: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    for name, value in flat:
        lname = name.lower()
        if lname in headers_dict:
            # Already have one value → switch to multi mode.
            multi.setdefault(lname, [headers_dict[lname]]).append(value)
            headers_dict[lname] = ", ".join(multi[lname])
        elif lname in multi:
            multi[lname].append(value)
            headers_dict[lname] = ", ".join(multi[lname])
        else:
            headers_dict[lname] = value
    body_out: bytes = result.get("body") or b""
    logger.info(
        "Tunnel %s: %s %s → %d (%d bytes)",
        tunnel_id,
        method,
        path,
        status_code,
        len(body_out),
    )
    return Response(
        content=body_out,
        status_code=status_code,
        headers=headers_dict,
        media_type=None,
    )


async def proxy_request(request: Request, tunnel_id: str) -> Response:
    """Entry point called by the /tunnels/{username} route."""
    # Reconstruct sub-path: the route is mounted as /tunnels/{username}/{rest:path}
    # but we also support the bare form. Normalize path to start with "/".
    raw_path = request.url.path
    prefix = f"/tunnels/{tunnel_id}"
    if raw_path.startswith(prefix):
        sub = raw_path[len(prefix):]
    else:
        sub = raw_path
    if not sub.startswith("/"):
        sub = "/" + sub

    query_string = request.url.query
    headers = _strip_headers(request.headers.raw)
    body = await request.body()
    return await forward_request(
        tunnel_id=tunnel_id,
        method=request.method,
        path=sub,
        query_string=query_string,
        headers=headers,
        body=body,
        request=request,
    )
