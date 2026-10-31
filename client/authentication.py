"""BenTunnel authentication helpers (login + short-lived WS token fetch)."""

from __future__ import annotations

import httpx


class AuthError(Exception):
    """Raised when authentication or token-fetch fails."""


def _extract_error_message(resp: httpx.Response, default: str) -> str:
    """Try to extract a human-readable error message from a JSON error body."""
    try:
        data = resp.json()
    except Exception:
        return default
    if isinstance(data, dict):
        for key in ("detail", "message", "error", "msg"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
        # FastAPI style: {"detail": [{"msg": "..."}, ...]}
        detail = data.get("detail")
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict) and isinstance(first.get("msg"), str):
                return first["msg"]
    return default


async def login(server_url: str, email: str, password: str) -> dict:
    """Authenticate against the server and return credential + tunnel metadata.

    Returns a dict with keys:
        session_cookie, csrf_token, user_email, tunnel_id, public_url
    """
    if not email or not password:
        raise AuthError("Email and password are required")

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            try:
                resp = await client.post(
                    f"{server_url}/api/auth/login",
                    json={"email": email, "password": password},
                )
            except httpx.RequestError:
                raise AuthError("Cannot reach server")

            if resp.status_code != 200:
                msg = _extract_error_message(
                    resp, f"Login failed (HTTP {resp.status_code})"
                )
                raise AuthError(msg)

            # Cookies are now in the client's jar.
            session_cookie = client.cookies.get("bentunnel_session")
            csrf_token = client.cookies.get("bentunnel_csrf")
            if not session_cookie or not csrf_token:
                # Fall back to response cookies (some servers set them on resp only).
                session_cookie = resp.cookies.get("bentunnel_session")
                csrf_token = resp.cookies.get("bentunnel_csrf")

            if not session_cookie or not csrf_token:
                raise AuthError("Server did not return session cookies")

            # Fetch the user's tunnel info (tunnel_id + public_url).
            try:
                me_resp = await client.get(f"{server_url}/api/tunnels/me")
            except httpx.RequestError:
                raise AuthError("Cannot reach server")

            if me_resp.status_code != 200:
                raise AuthError(
                    f"Could not fetch tunnel info (HTTP {me_resp.status_code})"
                )

            try:
                me_data = me_resp.json()
            except Exception:
                raise AuthError("Server returned invalid tunnel info")

            tunnel_id = me_data.get("tunnel_id")
            if tunnel_id is None:
                # Some servers might nest it under a different key.
                tunnel_id = me_data.get("tunnel_id_str") or me_data.get("id")
            if tunnel_id is None:
                raise AuthError("No tunnel found for this account")

            public_url = me_data.get("public_url") or ""
    except AuthError:
        raise
    except httpx.RequestError:
        raise AuthError("Cannot reach server")

    return {
        "session_cookie": session_cookie,
        "csrf_token": csrf_token,
        "user_email": email,
        "tunnel_id": str(tunnel_id),
        "public_url": public_url,
    }


async def fetch_tunnel_token(
    server_url: str, session_cookie: str, csrf_token: str
) -> dict:
    """Fetch a fresh short-lived JWT for WS authentication.

    Returns a dict with keys: token, expires_in, ws_url.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(
                    f"{server_url}/api/tunnels/token",
                    cookies={
                        "bentunnel_session": session_cookie,
                        "bentunnel_csrf": csrf_token,
                    },
                    headers={"X-CSRF-Token": csrf_token},
                )
            except httpx.RequestError:
                raise AuthError("Cannot reach server")

            if resp.status_code != 200:
                msg = _extract_error_message(
                    resp, f"Token fetch failed (HTTP {resp.status_code})"
                )
                raise AuthError(msg)

            try:
                data = resp.json()
            except Exception:
                raise AuthError("Server returned invalid token response")
    except AuthError:
        raise
    except httpx.RequestError:
        raise AuthError("Cannot reach server")

    token = data.get("token")
    ws_url = data.get("ws_url")
    expires_in = data.get("expires_in", 300)

    if not token or not ws_url:
        raise AuthError("Server returned incomplete token response")

    try:
        expires_in_int = int(expires_in)
    except (TypeError, ValueError):
        expires_in_int = 300

    return {"token": token, "expires_in": expires_in_int, "ws_url": ws_url}
