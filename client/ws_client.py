"""BenTunnel WebSocket client.

Connects to the BenTunnel server, fetches a fresh JWT on every (re)connection,
and forwards inbound HTTP requests to the user's local server.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from authentication import AuthError, fetch_tunnel_token
from config import CLIENT_VERSION
from http_forwarder import forward_to_local


LogCallback = Callable[[str, str], None]

# Reconnection backoff: 1s, 2s, 4s, 8s, 16s, 30s, 60s (max).
BACKOFF_SEQUENCE = [1, 2, 4, 8, 16, 30, 60]

# Client-initiated PING cadence (anti-spin-down).
CLIENT_PING_INTERVAL = 60.0

# Bodies <= this size are inlined as base64 in the JSON message; larger bodies
# are sent as a separate binary frame.
MAX_BODY_INLINE = 4096

# Hard cap on response body size (matches server default MAX_RESPONSE_BYTES).
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

# Hard cap on request body size (defensive; server should enforce too).
MAX_REQUEST_BYTES = 5 * 1024 * 1024

# WebSocket frame size cap (slightly above MAX_RESPONSE_BYTES to allow headers).
WS_MAX_SIZE = 10 * 1024 * 1024

# JWT-like pattern (three base64url segments separated by dots). Used to redact
# accidental leaks in log messages.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def _redact(message: str) -> str:
    """Strip JWT-like substrings from a log message."""
    if not message:
        return message
    return _JWT_RE.sub("[REDACTED]", message)


class TunnelClient:
    """Manages the WebSocket connection to the BenTunnel server."""

    def __init__(
        self,
        server_url: str,
        session_cookie: str,
        csrf_token: str,
        tunnel_id: str,
        local_port: int,
        public_url: str,
        on_log: LogCallback,
    ) -> None:
        self.server_url = server_url
        self.session_cookie = session_cookie
        self.csrf_token = csrf_token
        self.tunnel_id = str(tunnel_id)
        self.local_port = int(local_port)
        self.public_url = public_url
        self.on_log = on_log

        self.stopped = False
        self._ws: Optional[Any] = None
        self._last_pong = time.monotonic()
        self._pending_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        if self._ws is None:
            return False
        try:
            return bool(getattr(self._ws, "open", False))
        except Exception:
            return False

    async def run(self) -> None:
        """Main loop: connect, run, reconnect with backoff until stopped."""
        backoff_idx = 0
        while not self.stopped:
            was_connected = False
            try:
                self._log("INFO", f"Fetching tunnel token from {self.server_url}...")
                token_info = await fetch_tunnel_token(
                    self.server_url,
                    self.session_cookie,
                    self.csrf_token,
                )

                ws_url = token_info["ws_url"]
                params = urlencode(
                    {"tunnel_id": self.tunnel_id, "token": token_info["token"]}
                )
                sep = "&" if "?" in ws_url else "?"
                full_ws_url = f"{ws_url}{sep}{params}"

                self._log("INFO", "Connecting to tunnel server...")
                try:
                    async with websockets.connect(
                        full_ws_url,
                        ping_interval=None,
                        ping_timeout=None,
                        max_size=WS_MAX_SIZE,
                        close_timeout=5,
                    ) as ws:
                        self._ws = ws
                        await self._send_json(
                            ws,
                            "CLIENT_CONNECT",
                            {
                                "client_version": CLIENT_VERSION,
                                "local_port": self.local_port,
                            },
                        )

                        ping_task = asyncio.create_task(self._client_ping_loop(ws))
                        try:
                            was_connected = await self._message_loop(ws)
                        finally:
                            ping_task.cancel()
                            try:
                                await ping_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            self._ws = None
                except (
                    websockets.exceptions.InvalidHandshake,
                    websockets.exceptions.InvalidURI,
                    OSError,
                    httpx.RequestError,
                ) as e:
                    self._log(
                        "ERROR",
                        f"Connection failed: {type(e).__name__}: {_redact(str(e))[:200]}",
                    )
                except ConnectionClosed as e:
                    self._log(
                        "INFO",
                        f"WebSocket closed: code={getattr(e, 'code', '?')} reason={_redact(str(getattr(e, 'reason', '')))[:120]}",
                    )
                else:
                    self._log("INFO", "WebSocket disconnected")
            except asyncio.CancelledError:
                self._log("INFO", "Tunnel client cancelled")
                self.stopped = True
                await self._close_ws_gracefully()
                break
            except AuthError as e:
                # Session may have expired; retry with backoff (the next
                # fetch_tunnel_token will fail again, but eventually the user
                # will be told to re-login).
                self._log("ERROR", f"Auth error: {_redact(str(e))[:200]}")
            except Exception as e:  # noqa: BLE001
                self._log(
                    "ERROR",
                    f"Unexpected error: {type(e).__name__}: {_redact(str(e))[:200]}",
                )

            if was_connected:
                # We had a healthy session — reset the backoff.
                backoff_idx = 0

            if self.stopped:
                break

            delay = BACKOFF_SEQUENCE[
                min(backoff_idx, len(BACKOFF_SEQUENCE) - 1)
            ]
            self._log("INFO", f"Reconnecting in {delay}s...")
            backoff_idx += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self.stopped = True
                break

        # Best-effort graceful close on the way out.
        await self._close_ws_gracefully()
        self._log("INFO", "Tunnel client stopped")

    async def stop(self) -> None:
        """Signal the client to stop and close the current WebSocket."""
        if self.stopped:
            return
        self.stopped = True
        await self._close_ws_gracefully()

    # ------------------------------------------------------------------
    # WebSocket helpers
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        try:
            self.on_log(level, _redact(message))
        except Exception:
            pass

    async def _send_json(
        self,
        ws: Any,
        type_: str,
        payload: dict,
        request_id: Optional[str] = None,
        tunnel_id: Optional[str] = None,
    ) -> None:
        msg = {
            "type": type_,
            "request_id": request_id,
            "tunnel_id": tunnel_id if tunnel_id is not None else self.tunnel_id,
            "payload": payload,
        }
        await ws.send(json.dumps(msg))

    async def _send_http_response(
        self,
        ws: Any,
        request_id: str,
        status_code: int,
        headers: dict,
        body: bytes,
    ) -> None:
        body_size = len(body) if body else 0
        has_body = body_size > 0

        payload: dict = {
            "status_code": int(status_code),
            "headers": headers,
            "body_size": body_size,
            "has_body": has_body,
        }

        if body and body_size <= MAX_BODY_INLINE:
            payload["body_b64"] = base64.b64encode(body).decode("ascii")
        else:
            payload["body_b64"] = None

        await self._send_json(ws, "HTTP_RESPONSE", payload, request_id=request_id)

        if body and body_size > MAX_BODY_INLINE:
            # Send the body as a single binary frame right after the JSON.
            await ws.send(body)

    async def _close_ws_gracefully(self) -> None:
        ws = self._ws
        if ws is None:
            return
        self._ws = None
        try:
            if getattr(ws, "open", False):
                try:
                    await self._send_json(
                        ws, "CLIENT_DISCONNECT", {"reason": "client_stop"}
                    )
                except Exception:
                    pass
                await ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def _client_ping_loop(self, ws: Any) -> None:
        """Send a protocol-level PING every 60s (anti-spin-down)."""
        try:
            while not self.stopped:
                await asyncio.sleep(CLIENT_PING_INTERVAL)
                if self.stopped:
                    break
                try:
                    await self._send_json(ws, "PING", {"ts": time.time()})
                except Exception as e:
                    self._log(
                        "WARN",
                        f"Ping send failed: {type(e).__name__}: {_redact(str(e))[:120]}",
                    )
                    return
        except asyncio.CancelledError:
            return

    async def _message_loop(self, ws: Any) -> bool:
        """Process inbound WS messages until disconnect/stop.

        Returns True if we received ``CLIENT_CONNECTED`` (i.e. we had a healthy
        session, so the caller can reset the backoff).
        """
        received_connected = False
        while not self.stopped:
            try:
                msg = await ws.recv()
            except ConnectionClosed:
                self._log("INFO", "WebSocket closed by server")
                break
            except asyncio.CancelledError:
                raise

            if isinstance(msg, (bytes, bytearray)):
                # Unexpected binary frame outside an HTTP_REQUEST sequence.
                self._log("WARN", "Received unexpected binary frame; ignoring")
                continue

            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                self._log("WARN", "Received invalid JSON text frame; ignoring")
                continue
            if not isinstance(data, dict):
                continue

            mtype = data.get("type")
            payload = data.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            request_id = data.get("request_id")

            if mtype == "CLIENT_CONNECTED":
                received_connected = True
                max_req = payload.get("max_request_bytes")
                max_resp = payload.get("max_response_bytes")
                self._log(
                    "INFO",
                    f"Connected to server (max_request={max_req}, max_response={max_resp})",
                )

            elif mtype == "HTTP_REQUEST":
                await self._handle_http_request(ws, data)

            elif mtype == "PING":
                ts = payload.get("ts", time.time())
                try:
                    await self._send_json(ws, "PONG", {"ts": ts})
                except Exception as e:
                    self._log(
                        "WARN",
                        f"Failed to send PONG: {type(e).__name__}: {_redact(str(e))[:120]}",
                    )

            elif mtype == "PONG":
                self._last_pong = time.monotonic()

            elif mtype == "TUNNEL_STATUS":
                status = payload.get("status", "unknown")
                self._log("INFO", f"Tunnel status: {status}")
                if status == "suspended":
                    self._log("ERROR", "Tunnel has been suspended by an admin")

            elif mtype == "CLIENT_DISCONNECT":
                reason = payload.get("reason", "")
                self._log(
                    "INFO",
                    f"Server requested disconnect: {_redact(str(reason))[:120]}",
                )
                break

            elif mtype == "ERROR":
                code = payload.get("code", "")
                message = payload.get("message", "")
                self._log(
                    "ERROR",
                    f"Server error: code={code} message={_redact(str(message))[:200]}",
                )
                if code in ("AUTH_EXPIRED", "AUTH_INVALID", "TUNNEL_REVOKED"):
                    # No point retrying immediately with the same token; break
                    # so the outer loop fetches a fresh one.
                    break

            elif mtype == "HTTP_BODY_CHUNK":
                # Not used in v1 (server sends body inline as a single binary
                # frame after HTTP_REQUEST). Ignore.
                pass

            else:
                self._log("WARN", f"Unknown message type: {mtype}")

        return received_connected

    async def _handle_http_request(self, ws: Any, data: dict) -> None:
        """Forward a single HTTP_REQUEST to the local server and reply."""
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            return
        request_id = data.get("request_id")
        if not request_id:
            return

        method = payload.get("method", "GET")
        path = payload.get("path", "/")
        query_string = payload.get("query_string", "") or ""
        headers = payload.get("headers", {}) or {}
        body_size = int(payload.get("body_size", 0) or 0)
        body_b64 = payload.get("body_b64")

        body: Optional[bytes] = None

        if body_size > 0:
            if body_b64 is not None:
                try:
                    body = base64.b64decode(body_b64)
                except Exception:
                    body = b""
            else:
                # Binary frame follows immediately.
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                except asyncio.TimeoutError:
                    self._log(
                        "WARN",
                        f"Timeout waiting for binary body (request_id={request_id})",
                    )
                    raw = b""
                except ConnectionClosed:
                    return
                if isinstance(raw, (bytes, bytearray)):
                    body = bytes(raw)
                else:
                    body = b""

        # Defensive size cap (server should already enforce).
        if body is not None and len(body) > MAX_REQUEST_BYTES:
            try:
                await self._send_json(
                    ws,
                    "ERROR",
                    {
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": "Request body exceeds client max",
                    },
                    request_id=request_id,
                )
            except Exception:
                pass
            return

        # Spawn the forward+reply as a task so the receive loop keeps moving.
        task = asyncio.create_task(
            self._forward_and_reply(ws, request_id, method, path, query_string, headers, body)
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _forward_and_reply(
        self,
        ws: Any,
        request_id: str,
        method: str,
        path: str,
        query_string: str,
        headers: dict,
        body: Optional[bytes],
    ) -> None:
        try:
            status_code, resp_headers, resp_body = await forward_to_local(
                self.local_port, method, path, query_string, headers, body
            )
        except Exception as e:  # noqa: BLE001
            self._log(
                "ERROR",
                f"Forwarding failed (request_id={request_id}): "
                f"{type(e).__name__}: {_redact(str(e))[:150]}",
            )
            status_code = 502
            resp_headers = {"content-type": ["text/plain"]}
            resp_body = b"Local server not reachable"

        if resp_body is not None and len(resp_body) > MAX_RESPONSE_BYTES:
            try:
                await self._send_json(
                    ws,
                    "ERROR",
                    {
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": "Response body exceeds client max",
                    },
                    request_id=request_id,
                )
            except Exception as e:
                self._log(
                    "WARN",
                    f"Failed to send PAYLOAD_TOO_LARGE: {type(e).__name__}",
                )
            return

        try:
            await self._send_http_response(
                ws, request_id, status_code, resp_headers, resp_body or b""
            )
        except Exception as e:  # noqa: BLE001
            self._log(
                "WARN",
                f"Failed to send HTTP response (request_id={request_id}): "
                f"{type(e).__name__}: {_redact(str(e))[:150]}",
            )
