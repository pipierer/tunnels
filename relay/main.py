"""BenTunnel Render server — FastAPI application entrypoint.

Wires up:
- DB engine + bootstrap on startup
- Static files at /static
- Web (HTML) and API (JSON) routers
- The /ws WebSocket endpoint that local Python clients connect to
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .auth import audit_log, verify_tunnel_token
from .config import get_settings
from .db import bootstrap_admin, dispose_engine, get_session_factory, init_db
from .models import Tunnel
from .routes.api import router as api_router
from .routes.web import router as web_router
from .ws_relay import ws_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bentunnel.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create tables, bootstrap admin. Shutdown: dispose engine."""
    logger.info("Starting BenTunnel server…")
    try:
        await init_db()
        await bootstrap_admin()
    except Exception:
        logger.exception("Startup init failed — continuing anyway (Render will retry)")
    yield
    logger.info("Shutting down BenTunnel server…")
    await dispose_engine()


app = FastAPI(
    title="BenTunnel",
    version="1.0.0",
    lifespan=lifespan,
    # Same-origin only — no CORS needed.
    docs_url=None,
    redoc_url=None,
)

# Trust Render's reverse proxy (X-Forwarded-Proto → https → secure cookies).
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

app.include_router(web_router)
app.include_router(api_router)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    token: str = Query(default="", description="Court JWT tunnel"),
    tunnel_id: str = Query(default="", description="Tunnel ciblé (vérifié vs JWT)"),
) -> None:
    """Local client → server WebSocket.

    Auth flow:
    1. Verify the JWT (signature, exp, jti, ownership, suspension).
    2. Register the connection in ws_manager.
    3. Loop on incoming frames, dispatch each to ws_manager.
    4. On disconnect, ws_manager.disconnect cleans up.
    """
    if not token or not tunnel_id:
        await ws.close(code=1008, reason="Missing token or tunnel_id")
        return

    factory = get_session_factory()
    async with factory() as db:
        try:
            user, tunnel = await verify_tunnel_token(token, db)
        except Exception as exc:
            await ws.close(code=1008, reason=str(exc)[:120])
            return

        # The spec is explicit: never trust client-sent tunnel_id. The JWT
        # is authoritative. If they don't match, reject.
        if tunnel.tunnel_id != tunnel_id:
            await ws.close(code=1008, reason="tunnel_id mismatch")
            return

        user_id = user.id

    # Connect (also marks tunnel.is_active=1 in DB and starts the ping loop).
    try:
        conn = await ws_manager.connect(ws, tunnel.tunnel_id, user_id)
    except RuntimeError:
        # Already closed inside connect() (capacity reached).
        return

    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            bytes_ = message.get("bytes")
            if text is not None:
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON on tunnel %s", tunnel.tunnel_id)
                    continue
                await ws_manager.handle_text_message(tunnel.tunnel_id, msg)
            elif bytes_ is not None:
                await ws_manager.handle_binary_message(tunnel.tunnel_id, bytes_)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WS receive loop crashed for tunnel %s", tunnel.tunnel_id)
    finally:
        await ws_manager.disconnect(tunnel.tunnel_id, "closed")


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
@app.exception_handler(500)
async def _internal_error(request: Request, exc: Exception):  # noqa: ARG001
    logger.exception("Unhandled server error")
    import traceback
    tb = traceback.format_exc()
    return JSONResponse(
        {
            "detail": "Erreur interne du serveur",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": tb,
        },
        status_code=500,
    )


@app.get("/robots.txt", include_in_schema=False)
async def robots() -> str:
    return "User-agent: *\nAllow: /\n"
