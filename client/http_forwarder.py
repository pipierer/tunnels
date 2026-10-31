"""Forward inbound HTTP requests (from the tunnel) to the user's local server.

Security: this module will **only** ever connect to ``127.0.0.1``. Any Host
header the server may have leaked is ignored, and ``x-bentunnel-*`` headers are
stripped before the request is dispatched.
"""

from __future__ import annotations

from typing import Optional

import httpx


ALLOWED_HOST = "127.0.0.1"
HTTP_TIMEOUT = 30.0

# Hop-by-hop / encoding headers we strip from the local server's response.
# httpx already de-chunks and decompresses the body, so passing these through
# would confuse the visitor's client. Content-Length is handled separately
# (we recompute it from the actual body we forward, except for HEAD requests
# where the local server's value reflects the would-be resource size).
_STRIP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
}

# Status codes that MUST NOT carry a Content-Length header (RFC 7230 3.3.2).
_NO_CONTENT_LENGTH_STATUS = {204, 304}

# Request headers we always strip (anti-SSRF / protocol hygiene).
_STRIP_REQUEST_HEADERS = {
    "host",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-real-ip",
}


def _clean_request_headers(headers: Optional[dict], local_port: int) -> list:
    """Strip dangerous headers and force ``Host: 127.0.0.1:port``.

    Returns a list of ``(name, value)`` tuples (httpx accepts this form and it
    preserves multi-valued headers correctly).
    """
    cleaned: list = []
    if headers:
        for name, values in headers.items():
            if name is None:
                continue
            lname = str(name).lower()
            if lname in _STRIP_REQUEST_HEADERS:
                continue
            if lname.startswith("x-bentunnel-"):
                continue
            if isinstance(values, (list, tuple)):
                for v in values:
                    cleaned.append((lname, str(v)))
            else:
                cleaned.append((lname, str(values)))
    # Force Host to the local loopback address (anti-SSRF).
    cleaned.append(("host", f"{ALLOWED_HOST}:{local_port}"))
    return cleaned


def _build_response_headers(resp: httpx.Response, is_head: bool) -> dict:
    """Convert an httpx response's headers into a ``{name: [values]}`` dict.

    For HEAD requests we preserve the local server's ``Content-Length`` (it
    reflects the would-be resource size). For other methods we recompute
    ``Content-Length`` from the actual body we're forwarding (which may have
    been decompressed by httpx).
    """
    out: dict = {}
    for name, value in resp.headers.multi_items():
        lname = name.lower()
        if lname in _STRIP_RESPONSE_HEADERS:
            continue
        if lname == "content-length":
            # Handled below.
            continue
        if lname in out:
            out[lname].append(value)
        else:
            out[lname] = [value]

    status_code = resp.status_code
    if 100 <= status_code < 200 or status_code in _NO_CONTENT_LENGTH_STATUS:
        # RFC 7230 3.3.2: no Content-Length for 1xx / 204 / 304.
        out.pop("content-length", None)
    elif is_head:
        # Preserve the local server's Content-Length (would-be body size).
        original_cl = resp.headers.get("content-length")
        if original_cl is not None:
            out["content-length"] = [original_cl]
    else:
        # Recompute from the actual body we forward (httpx already decoded).
        out["content-length"] = [str(len(resp.content))]
    return out


async def forward_to_local(
    local_port: int,
    method: str,
    path: str,
    query_string: str,
    headers: dict,
    body: Optional[bytes],
) -> tuple[int, dict, bytes]:
    """Forward a single HTTP request to ``http://127.0.0.1:{local_port}``.

    Returns ``(status_code, response_headers, response_body)``. On connection
    failure returns a synthetic ``502`` with a plain-text body.
    """
    if not path.startswith("/"):
        path = "/" + path
    url = f"http://{ALLOWED_HOST}:{local_port}{path}"
    if query_string:
        url = url + "?" + query_string

    cleaned_headers = _clean_request_headers(headers, local_port)
    method_upper = (method or "GET").upper()

    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,
            # Anti-SSRF: do not allow redirects to take us off 127.0.0.1.
            trust_env=False,
        ) as client:
            kwargs: dict = {"headers": cleaned_headers}
            if body:
                kwargs["content"] = body
            # For HEAD requests httpx does not download a body.
            resp = await client.request(method_upper, url, **kwargs)
    except httpx.ConnectError:
        return (
            502,
            {"content-type": ["text/plain"]},
            b"Local server not reachable",
        )
    except httpx.RequestError:
        return (
            502,
            {"content-type": ["text/plain"]},
            b"Local server not reachable",
        )

    status_code = resp.status_code
    response_headers = _build_response_headers(resp, is_head=(method_upper == "HEAD"))
    # For HEAD requests, resp.content is already empty (httpx handles this).
    response_body = resp.content

    return status_code, response_headers, response_body
