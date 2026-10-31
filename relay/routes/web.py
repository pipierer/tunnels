"""HTML routes: landing, auth pages, dashboard, admin, health, and the
catch-all reverse proxy at ``/tunnels/{username}``."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user, require_admin, require_auth
from ..config import get_settings
from ..db import get_db
from ..http_proxy import proxy_request
from ..models import User
from ..ws_relay import ws_manager

logger = logging.getLogger("bentunnel.web")

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Process start time for /health uptime.
_START_TIME = time.time()


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"request": request, "public_base_url": get_settings().PUBLIC_BASE_URL},
    )


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(
    request: Request,
    user: User | None = Depends(get_current_user),
) -> Response:
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"request": request})


@router.get("/register", response_class=HTMLResponse, response_model=None)
async def register_page(
    request: Request,
    user: User | None = Depends(get_current_user),
) -> Response:
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "register.html", {"request": request})


# ---------------------------------------------------------------------------
# Authenticated pages
# ---------------------------------------------------------------------------
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(
    request: Request,
    user: User = Depends(require_auth),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "public_base_url": get_settings().PUBLIC_BASE_URL,
        },
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    user: User = Depends(require_admin),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"request": request, "user": user},
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "uptime": int(time.time() - _START_TIME),
            "tunnels_connected": len(ws_manager.tunnels),
        }
    )


# ---------------------------------------------------------------------------
# Reverse proxy: /tunnels/{username} — accepts ANY method and sub-path.
# ---------------------------------------------------------------------------
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@router.api_route(
    "/tunnels/{username}",
    methods=PROXY_METHODS,
    include_in_schema=False,
)
async def tunnel_proxy_root(request: Request, username: str):
    return await proxy_request(request, username)


@router.api_route(
    "/tunnels/{username}/{rest:path}",
    methods=PROXY_METHODS,
    include_in_schema=False,
)
async def tunnel_proxy_sub(request: Request, username: str, rest: str):
    return await proxy_request(request, username)
