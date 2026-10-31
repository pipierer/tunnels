"""BenTunnel client configuration & credential storage.

Credentials are stored in ``~/.bentunnel/config.json`` with restrictive
permissions (directory mode 0700, file mode 0600).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

CLIENT_VERSION = "1.0.0"

DEFAULT_SERVER_URL = "https://bentunnel.onrender.com"

CONFIG_DIR = Path.home() / ".bentunnel"
CONFIG_FILE = CONFIG_DIR / "config.json"
PID_FILE = CONFIG_DIR / "tunnel.pid"


def load_config() -> dict:
    """Load the config file, returning ``{}`` if it doesn't exist or is invalid."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(cfg: dict) -> None:
    """Persist the config dict to disk with mode 0600 (dir 0700)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        # On some platforms chmod on a directory may be restricted; ignore.
        pass

    # Open with O_CREAT so the file is created with mode 0600 if missing.
    fd = os.open(str(CONFIG_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    finally:
        # Always enforce 0600 even if the file already existed with looser bits.
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass


def get_stored_session() -> Optional[str]:
    """Return the stored ``bentunnel_session`` cookie value, or None."""
    return load_config().get("session_cookie")


def store_session(
    session_cookie: str,
    csrf_token: str,
    user_email: str,
    tunnel_id: str,
    public_url: str,
) -> None:
    """Persist the session credentials & tunnel metadata."""
    cfg = load_config()
    cfg["session_cookie"] = session_cookie
    cfg["csrf_token"] = csrf_token
    cfg["user_email"] = user_email
    cfg["tunnel_id"] = str(tunnel_id)
    cfg["public_url"] = public_url
    save_config(cfg)


def clear_session() -> None:
    """Remove stored session credentials (keeps any other keys)."""
    cfg = load_config()
    for key in (
        "session_cookie",
        "csrf_token",
        "user_email",
        "tunnel_id",
        "public_url",
    ):
        cfg.pop(key, None)
    save_config(cfg)


def get_server_url() -> str:
    """Resolve the server URL from env or default, without a trailing slash."""
    url = os.environ.get("BENTUNNEL_SERVER", DEFAULT_SERVER_URL)
    return url.rstrip("/")
