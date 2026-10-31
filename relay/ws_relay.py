"""Core WebSocket relay.

The :class:`WebSocketManager` keeps track of:
- ``tunnels``: connected clients keyed by ``tunnel_id``.
- ``pending_requests``: ``request_id -> asyncio.Future`` for in-flight HTTP
  requests forwarded through the tunnel.
- per-tunnel response-assembly state used to glue HTTP_RESPONSE text frames
  and the binary frames that carry their bodies (PROTOCOL.md §2.3).

All public methods are async and safe to call from FastAPI handlers. The
internal dicts are guarded by a single :class:`asyncio.Lock` to avoid races
between the receive loop and disconnect.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import WebSocket
from sqlalchemy import select, update

from .config import get_settings
from .db import get_session_factory
from .models import Tunnel

logger = logging.getLogger("bentunnel.ws")

# PING/PONG tuning (PROTOCOL.md §5).
PING_INTERVAL_SECONDS = 30
PONG_TIMEOUT_SECONDS = 10
WS_CLOSE_CODE_PING_TIMEOUT = 4001
WS_CLOSE_CODE_TOO_MANY = 1013
WS_CLOSE_CODE_PROTOCOL_ERROR = 1002

# 4 KB threshold (PROTOCOL.md §2.3).
INLINE_BODY_LIMIT = 4096


@dataclass
class WebSocketConnection:
    """An active tunnel WebSocket."""

    ws: WebSocket
    tunnel_id: str
    user_id: int
    last_pong: float = field(default_factory=time.time)
    last_ping: float = 0.0
    ping_task: Optional[asyncio.Task] = None


class _PendingResponse:
    """Per-request state used to assemble chunked/binary responses."""

    __slots__ = ("status_code", "headers", "body_size", "chunks", "is_last")

    def __init__(self, status_code: int, headers: dict, body_size: int) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body_size = body_size
        self.chunks: list[bytes] = []
        self.is_last: bool = True  # default: single-frame mode


class WebSocketManager:
    """Singleton that owns all live tunnels and in-flight requests."""

    def __init__(self) -> None:
        self.tunnels: dict[str, WebSocketConnection] = {}
        self.pending_requests: dict[str, asyncio.Future[dict]] = {}
        # request_id -> tunnel_id (so disconnect can fail in-flight reqs)
        self.request_tunnel: dict[str, str] = {}
        # request_id -> _PendingResponse (response body assembly)
        self.pending_responses: dict[str, _PendingResponse] = {}
        # tunnel_id -> request_id currently awaiting a binary frame
        self.active_response: dict[str, str] = {}
        # tunnel_id -> request_id currently in chunked mode
        self.chunked_mode: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    async def connect(
        self, ws: WebSocket, tunnel_id: str, user_id: int
    ) -> WebSocketConnection:
        """Accept the WS, register it, mark the tunnel active, kick pings."""
        settings = get_settings()

        # Reject if we are at capacity.
        async with self._lock:
            if len(self.tunnels) >= settings.MAX_WS_CONNECTIONS:
                await ws.close(code=WS_CLOSE_CODE_TOO_MANY)
                raise RuntimeError("Max WS connections reached")

        # If a previous connection exists for this tunnel, replace it
        # (the new client wins). The old one will be cleaned up by its
        # own receive loop.
        async with self._lock:
            old = self.tunnels.get(tunnel_id)
        if old is not None:
            try:
                await old.ws.close(code=4000, reason="Replaced by new connection")
            except Exception:
                pass
            if old.ping_task is not None:
                old.ping_task.cancel()

        await ws.accept()
        conn = WebSocketConnection(ws=ws, tunnel_id=tunnel_id, user_id=user_id)
        async with self._lock:
            self.tunnels[tunnel_id] = conn

        # Update DB: is_active=1, last_seen_at=now.
        await self._mark_active(tunnel_id, True)

        # Send CLIENT_CONNECTED ack (PROTOCOL.md §2.2).
        await self._send_json(
            conn,
            {
                "type": "CLIENT_CONNECTED",
                "request_id": None,
                "tunnel_id": tunnel_id,
                "payload": {
                    "tunnel_id": tunnel_id,
                    "server_time": int(datetime.now(timezone.utc).timestamp()),
                    "max_request_bytes": settings.MAX_REQUEST_BYTES,
                    "max_response_bytes": settings.MAX_RESPONSE_BYTES,
                },
            },
        )

        # Start the ping task.
        conn.ping_task = asyncio.create_task(self._ping_loop(conn))
        logger.info("Tunnel %s connected (user=%s)", tunnel_id, user_id)
        return conn

    async def disconnect(self, tunnel_id: str, reason: str = "unknown") -> None:
        """Remove a tunnel from the registry and fail in-flight requests."""
        async with self._lock:
            conn = self.tunnels.pop(tunnel_id, None)
            self.active_response.pop(tunnel_id, None)
            self.chunked_mode.pop(tunnel_id, None)

        if conn is None:
            # Already gone (e.g. replaced by a newer connection). Do not
            # touch the DB so we don't clobber the new client's state.
            return

        if conn.ping_task is not None:
            conn.ping_task.cancel()
            try:
                await conn.ping_task
            except (asyncio.CancelledError, Exception):
                pass

        # Fail any in-flight requests whose response was being assembled by
        # this tunnel (their _PendingResponse is still registered). Other
        # in-flight requests will simply time out via ``send_request``'s
        # ``asyncio.wait_for`` — we don't need to track per-request tunnel
        # ownership for those.
        await self._fail_pending_for_tunnel(tunnel_id)

        # Mark the tunnel inactive in DB.
        await self._mark_active(tunnel_id, False)

        # Try to close the WS gracefully.
        try:
            await conn.ws.close(code=1000, reason=reason)
        except Exception:
            pass
        logger.info("Tunnel %s disconnected (%s)", tunnel_id, reason)

    # ------------------------------------------------------------------
    # Sending requests through the tunnel
    # ------------------------------------------------------------------
    async def send_request(
        self,
        tunnel_id: str,
        method: str,
        path: str,
        query_string: str,
        headers: dict[str, list[str]],
        body: bytes | None,
    ) -> dict[str, Any]:
        """Forward an HTTP request through the tunnel and await the response.

        Returns ``{"status_code", "headers", "body"}`` on success.
        Raises :class:`TunnelError` on protocol/timeout errors.
        """
        settings = get_settings()
        conn = self.tunnels.get(tunnel_id)
        if conn is None:
            raise TunnelError("Tunnel not connected", code="NOT_CONNECTED")

        if body is None:
            body = b""
        if len(body) > settings.MAX_REQUEST_BYTES:
            raise TunnelError(
                "Request body too large", code="PAYLOAD_TOO_LARGE"
            )

        request_id = uuid.uuid4().hex
        body_b64: Optional[str]
        if len(body) > INLINE_BODY_LIMIT:
            body_b64 = None
        elif body:
            body_b64 = base64.b64encode(body).decode("ascii")
        else:
            body_b64 = None

        payload = {
            "method": method.upper(),
            "path": path,
            "query_string": query_string,
            "headers": headers,
            "body_b64": body_b64,
            "body_size": len(body),
        }

        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict] = loop.create_future()
        async with self._lock:
            self.pending_requests[request_id] = future
            self.request_tunnel[request_id] = tunnel_id

        try:
            await self._send_json(
                conn,
                {
                    "type": "HTTP_REQUEST",
                    "request_id": request_id,
                    "tunnel_id": tunnel_id,
                    "payload": payload,
                },
            )
            # If the body is large, send it as a single binary frame
            # right after the JSON text frame (PROTOCOL.md §2.3).
            if body_b64 is None and body:
                await conn.ws.send_bytes(body)

            try:
                result = await asyncio.wait_for(
                    future, timeout=settings.HTTP_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError as exc:
                raise TunnelError(
                    "Upstream timeout", code="TIMEOUT", http_status=504
                ) from exc
            return result

        finally:
            async with self._lock:
                self.pending_requests.pop(request_id, None)
                self.request_tunnel.pop(request_id, None)
                self.pending_responses.pop(request_id, None)

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------
    async def handle_text_message(
        self, tunnel_id: str, msg: dict[str, Any]
    ) -> None:
        """Dispatch a single JSON text frame received from the client."""
        msg_type = msg.get("type")
        request_id = msg.get("request_id")
        payload = msg.get("payload") or {}

        if msg_type == "HTTP_RESPONSE":
            await self._on_http_response(tunnel_id, request_id, payload)
        elif msg_type == "HTTP_BODY_CHUNK":
            await self._on_body_chunk(tunnel_id, request_id, payload)
        elif msg_type == "PONG":
            conn = self.tunnels.get(tunnel_id)
            if conn is not None:
                conn.last_pong = time.time()
        elif msg_type == "PING":
            # Client-initiated PING (anti-spin-down) → respond PONG.
            conn = self.tunnels.get(tunnel_id)
            if conn is not None:
                await self._send_json(
                    conn,
                    {
                        "type": "PONG",
                        "request_id": request_id,
                        "tunnel_id": tunnel_id,
                        "payload": {"ts": payload.get("ts")},
                    },
                )
        elif msg_type == "CLIENT_DISCONNECT":
            logger.info(
                "Client announced disconnect for tunnel %s: %s",
                tunnel_id,
                payload.get("reason"),
            )
            asyncio.create_task(self.disconnect(tunnel_id, "client_disconnect"))
        elif msg_type == "ERROR":
            await self._on_error(tunnel_id, request_id, payload)
        else:
            logger.warning("Unknown WS message type %r for tunnel %s", msg_type, tunnel_id)

    async def handle_binary_message(self, tunnel_id: str, data: bytes) -> None:
        """Dispatch a binary frame: either a chunk or a full body."""
        async with self._lock:
            chunked_rid = self.chunked_mode.get(tunnel_id)
            active_rid = self.active_response.get(tunnel_id)

        if chunked_rid is not None:
            # Append the chunk and possibly resolve.
            async with self._lock:
                self.chunked_mode.pop(tunnel_id, None)
                pending = self.pending_responses.get(chunked_rid)
            if pending is None:
                return
            pending.chunks.append(data)
            if pending.is_last:
                await self._resolve_response(chunked_rid, pending)
        elif active_rid is not None:
            # Single-frame binary body (PROTOCOL.md §2.3 point 2).
            async with self._lock:
                self.active_response.pop(tunnel_id, None)
                pending = self.pending_responses.pop(active_rid, None)
            if pending is None:
                return
            pending.chunks.append(data)
            await self._resolve_response(active_rid, pending)
        else:
            logger.warning(
                "Stray binary frame on tunnel %s (%d bytes)", tunnel_id, len(data)
            )

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------
    async def _on_http_response(
        self, tunnel_id: str, request_id: Optional[str], payload: dict
    ) -> None:
        if not request_id:
            logger.warning("HTTP_RESPONSE without request_id on tunnel %s", tunnel_id)
            return

        status_code = int(payload.get("status_code", 502))
        headers = payload.get("headers") or {}
        body_size = int(payload.get("body_size", 0))
        has_body = bool(payload.get("has_body", False))
        body_b64 = payload.get("body_b64")

        settings = get_settings()
        if body_size > settings.MAX_RESPONSE_BYTES:
            await self._resolve_error(
                request_id,
                TunnelError("Response too large", code="PAYLOAD_TOO_LARGE", http_status=413),
            )
            return

        # If body_b64 is present we are done immediately.
        if body_b64:
            body = base64.b64decode(body_b64)
            await self._resolve_response_with_body(request_id, status_code, headers, body)
            return

        if not has_body or body_size == 0:
            await self._resolve_response_with_body(request_id, status_code, headers, b"")
            return

        # Need to wait for the binary frame (single or chunked).
        async with self._lock:
            self.pending_responses[request_id] = _PendingResponse(
                status_code=status_code, headers=headers, body_size=body_size
            )
            self.active_response[tunnel_id] = request_id

    async def _on_body_chunk(
        self, tunnel_id: str, request_id: Optional[str], payload: dict
    ) -> None:
        seq = int(payload.get("seq", 0))
        total = int(payload.get("total", 1))
        is_last = bool(payload.get("is_last", False))

        async with self._lock:
            # The chunk belongs to whichever request is currently active.
            rid = request_id or self.active_response.get(tunnel_id)
            if rid is None:
                logger.warning("HTTP_BODY_CHUNK without active request on tunnel %s", tunnel_id)
                return
            pending = self.pending_responses.get(rid)
            if pending is None:
                logger.warning("HTTP_BODY_CHUNK for unknown request %s", rid)
                return
            # Switch to chunked mode: subsequent binary frame goes here.
            self.active_response[tunnel_id] = rid
            self.chunked_mode[tunnel_id] = rid
            pending.is_last = is_last
            # total is informational; we rely on is_last.
            _ = seq, total

    async def _on_error(
        self, tunnel_id: str, request_id: Optional[str], payload: dict
    ) -> None:
        code = payload.get("code", "INTERNAL")
        message = payload.get("message", "Tunnel error")
        http_status = 502
        if code == "PAYLOAD_TOO_LARGE":
            http_status = 413
        elif code == "TIMEOUT":
            http_status = 504
        elif code == "NOT_CONNECTED":
            http_status = 503
        exc = TunnelError(message, code=code, http_status=http_status)
        if request_id:
            await self._resolve_error(request_id, exc)
        else:
            logger.warning("Tunnel %s protocol error: %s (%s)", tunnel_id, code, message)

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------
    async def _resolve_response_with_body(
        self,
        request_id: str,
        status_code: int,
        headers: dict,
        body: bytes,
    ) -> None:
        async with self._lock:
            fut = self.pending_requests.get(request_id)
        if fut is None or fut.done():
            return
        fut.set_result(
            {"status_code": status_code, "headers": headers, "body": body}
        )

    async def _resolve_response(
        self, request_id: str, pending: _PendingResponse
    ) -> None:
        body = b"".join(pending.chunks)
        await self._resolve_response_with_body(
            request_id, pending.status_code, pending.headers, body
        )

    async def _resolve_error(self, request_id: str, exc: "TunnelError") -> None:
        async with self._lock:
            fut = self.pending_requests.get(request_id)
            self.pending_responses.pop(request_id, None)
        if fut is None or fut.done():
            return
        fut.set_exception(exc)

    async def _fail_pending_for_tunnel(self, tunnel_id: str) -> None:
        """Fail every unresolved future belonging to *tunnel_id*.

        We use the ``request_tunnel`` mapping (populated by
        :meth:`send_request`) to identify which in-flight requests were
        routed through this tunnel, then resolve each future with a 502
        :class:`TunnelError`.
        """
        async with self._lock:
            rids_to_fail = [
                rid
                for rid, tid in self.request_tunnel.items()
                if tid == tunnel_id
            ]
        for rid in rids_to_fail:
            await self._resolve_error(
                rid,
                TunnelError(
                    "Tunnel disconnected", code="NOT_CONNECTED", http_status=502
                ),
            )

    # ------------------------------------------------------------------
    # Ping loop
    # ------------------------------------------------------------------
    async def _ping_loop(self, conn: WebSocketConnection) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SECONDS)
                if conn.tunnel_id not in self.tunnels:
                    break
                if self.tunnels[conn.tunnel_id] is not conn:
                    break
                conn.last_ping = time.time()
                await self._send_json(
                    conn,
                    {
                        "type": "PING",
                        "request_id": None,
                        "tunnel_id": conn.tunnel_id,
                        "payload": {"ts": int(time.time())},
                    },
                )
                # Give the client 10s to PONG.
                await asyncio.sleep(PONG_TIMEOUT_SECONDS)
                if conn.tunnel_id not in self.tunnels:
                    break
                if self.tunnels[conn.tunnel_id] is not conn:
                    break
                if conn.last_pong < conn.last_ping:
                    logger.warning(
                        "Tunnel %s PONG timeout — closing", conn.tunnel_id
                    )
                    try:
                        await conn.ws.close(
                            code=WS_CLOSE_CODE_PING_TIMEOUT,
                            reason="PONG timeout",
                        )
                    except Exception:
                        pass
                    asyncio.create_task(
                        self.disconnect(conn.tunnel_id, "ping_timeout")
                    )
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            logger.exception("Ping loop crashed for tunnel %s", conn.tunnel_id)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    async def _mark_active(self, tunnel_id: str, active: bool) -> None:
        """Flip ``is_active`` and bump ``last_seen_at`` for the tunnel."""
        factory = get_session_factory()
        try:
            async with factory() as session:
                await session.execute(
                    update(Tunnel)
                    .where(Tunnel.tunnel_id == tunnel_id)
                    .values(
                        is_active=1 if active else 0,
                        last_seen_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("Failed to mark tunnel %s active=%s", tunnel_id, active)

    # ------------------------------------------------------------------
    # Low-level send helper
    # ------------------------------------------------------------------
    async def _send_json(self, conn: WebSocketConnection, msg: dict) -> None:
        import json

        await conn.ws.send_text(json.dumps(msg))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def is_connected(self, tunnel_id: str) -> bool:
        return tunnel_id in self.tunnels

    def connection_for(self, tunnel_id: str) -> Optional[WebSocketConnection]:
        return self.tunnels.get(tunnel_id)

    async def list_active_tunnels(self) -> list[dict]:
        """Return a snapshot of all connected tunnels (for admin views)."""
        async with self._lock:
            return [
                {
                    "tunnel_id": c.tunnel_id,
                    "user_id": c.user_id,
                    "last_pong": c.last_pong,
                }
                for c in self.tunnels.values()
            ]


class TunnelError(Exception):
    """Raised by :meth:`WebSocketManager.send_request` on protocol failures."""

    def __init__(
        self,
        message: str,
        code: str = "INTERNAL",
        http_status: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


# Singleton instance used everywhere in the app.
ws_manager = WebSocketManager()
