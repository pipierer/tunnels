#!/usr/bin/env python3
"""BenTunnel client CLI.

Exposes a local port (e.g. ``localhost:3000``) to the public internet via the
BenTunnel server.

Usage:
    python bentunnel.py login [--server URL]
    python bentunnel.py start <port> [--server URL]
    python bentunnel.py stop
    python bentunnel.py status
    python bentunnel.py logout
    python bentunnel.py --version
    python bentunnel.py --help
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

# Make sibling modules importable when this file is run directly
# (e.g. ``python client/bentunnel.py``) or imported as ``bentunnel`` after
# inserting ``client/`` on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from authentication import AuthError, login  # noqa: E402
from config import (  # noqa: E402
    CLIENT_VERSION,
    CONFIG_DIR,
    PID_FILE,
    clear_session,
    get_server_url,
    load_config,
    store_session,
)
from ws_client import TunnelClient  # noqa: E402


# ---------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------

class C:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"


_USE_COLORS = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def colorize(text: str, *colors: str) -> str:
    if not _USE_COLORS or not colors:
        return text
    return "".join(colors) + text + C.RESET


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log(level: str, message: str) -> None:
    color_map = {
        "INFO": C.CYAN,
        "ERROR": C.RED,
        "WARN": C.YELLOW,
        "SUCCESS": C.GREEN,
    }
    color = color_map.get(level, "")
    level_str = colorize(f"[{level}]", color) if color else f"[{level}]"
    print(f"[{_ts()}] {level_str} {message}")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def resolve_server_url(override: Optional[str]) -> str:
    if override:
        return override.rstrip("/")
    return get_server_url()


def validate_server_url(url: str) -> tuple[bool, str]:
    """Return (ok, message). message is a warning if ok, an error if not."""
    if not url:
        return False, "Server URL is empty"
    if url.startswith("https://"):
        return True, ""
    if url.startswith("http://"):
        if "localhost" in url or "127.0.0.1" in url:
            return True, "Warning: using insecure http:// for local development"
        return False, f"Refusing to use insecure http:// URL '{url}' in production"
    return False, f"Server URL must start with http:// or https:// (got '{url}')"


def validate_port(port_str: str) -> tuple[bool, int, str]:
    try:
        port = int(port_str)
    except (TypeError, ValueError):
        return False, 0, f"Invalid port '{port_str}' (must be an integer)"
    if port < 1 or port > 65535:
        return False, 0, f"Port must be between 1 and 65535 (got {port})"
    return True, port, ""


def write_pid_file() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass
        fd = os.open(str(PID_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        finally:
            try:
                os.chmod(PID_FILE, 0o600)
            except OSError:
                pass
    except OSError as e:
        log("WARN", f"Could not write PID file: {e}")


def remove_pid_file() -> None:
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------

def cmd_login(args: argparse.Namespace) -> int:
    server_url = resolve_server_url(args.server)
    ok, msg = validate_server_url(server_url)
    if not ok:
        print(colorize(f"Error: {msg}", C.RED))
        return 1
    if msg:
        print(colorize(msg, C.YELLOW))

    print(colorize("BenTunnel — Login", C.BOLD + C.CYAN))
    print(f"Server: {server_url}")
    print()

    try:
        email = input("Email: ").strip()
    except EOFError:
        print()
        print(colorize("Error: Email is required", C.RED))
        return 1
    if not email:
        print(colorize("Error: Email is required", C.RED))
        return 1

    try:
        password = getpass.getpass("Password: ")
    except (EOFError, KeyboardInterrupt):
        print()
        print(colorize("Error: Password is required", C.RED))
        return 1
    if not password:
        print(colorize("Error: Password is required", C.RED))
        return 1

    try:
        result = asyncio.run(login(server_url, email, password))
    except AuthError as e:
        print(colorize(f"Login failed: {e}", C.RED))
        return 1
    except Exception as e:  # noqa: BLE001
        print(colorize(f"Unexpected error: {type(e).__name__}: {e}", C.RED))
        return 1

    store_session(
        session_cookie=result["session_cookie"],
        csrf_token=result["csrf_token"],
        user_email=result["user_email"],
        tunnel_id=result["tunnel_id"],
        public_url=result["public_url"],
    )

    print(colorize("Login successful!", C.GREEN + C.BOLD))
    print(f"  Email:      {result['user_email']}")
    print(f"  Tunnel ID:  {result['tunnel_id']}")
    print(f"  Public URL: {result['public_url']}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    server_url = resolve_server_url(args.server)

    ok, msg = validate_server_url(server_url)
    if not ok:
        print(colorize(f"Error: {msg}", C.RED))
        return 1
    if msg:
        print(colorize(msg, C.YELLOW))

    ok, port, msg = validate_port(args.port)
    if not ok:
        print(colorize(f"Error: {msg}", C.RED))
        return 1

    cfg = load_config()
    session_cookie = cfg.get("session_cookie")
    csrf_token = cfg.get("csrf_token")
    tunnel_id = cfg.get("tunnel_id")
    public_url = cfg.get("public_url")
    user_email = cfg.get("user_email")

    if not session_cookie or not csrf_token or not tunnel_id:
        print(
            colorize(
                "Not logged in. Run: python bentunnel.py login",
                C.RED,
            )
        )
        return 1

    # Banner
    print()
    print(colorize("  BenTunnel — Tunnel started", C.BOLD + C.GREEN))
    print(colorize(f"  Public URL:    {public_url}", C.CYAN))
    print(f"  Forwarding to: http://127.0.0.1:{port}")
    print(f"  User:          {user_email}")
    print(f"  Server:        {server_url}")
    print(colorize("  Press Ctrl+C to stop", C.YELLOW))
    print()

    write_pid_file()

    client = TunnelClient(
        server_url=server_url,
        session_cookie=session_cookie,
        csrf_token=csrf_token,
        tunnel_id=tunnel_id,
        local_port=port,
        public_url=public_url or "",
        on_log=lambda level, message: log(level, message),
    )

    # Ctrl+C handling — install explicit handlers per the spec.
    def _sigint_handler(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    old_sigint = signal.signal(signal.SIGINT, _sigint_handler)
    old_sigbreak = None
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        try:
            old_sigbreak = signal.signal(signal.SIGBREAK, _sigint_handler)
        except (OSError, ValueError):
            old_sigbreak = None

    exit_code = 0
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print()
        log("INFO", "Stopping tunnel...")
        try:
            asyncio.run(client.stop())
        except Exception as e:  # noqa: BLE001
            log("WARN", f"Error during stop: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        log("ERROR", f"Fatal error: {type(e).__name__}: {e}")
        exit_code = 1
    finally:
        try:
            signal.signal(signal.SIGINT, old_sigint)
        except (OSError, ValueError):
            pass
        if old_sigbreak is not None:
            try:
                signal.signal(signal.SIGBREAK, old_sigbreak)
            except (OSError, ValueError):
                pass
        remove_pid_file()

    return exit_code


def cmd_stop(args: argparse.Namespace) -> int:  # noqa: ARG001
    if not PID_FILE.exists():
        print(colorize("No running tunnel found (no PID file).", C.YELLOW))
        print(
            "If a tunnel is running in another terminal, use Ctrl+C in that "
            "terminal to stop it."
        )
        return 1

    try:
        pid_str = PID_FILE.read_text().strip()
        pid = int(pid_str)
    except (OSError, ValueError) as e:
        print(colorize(f"Invalid PID file: {e}", C.RED))
        remove_pid_file()
        return 1

    if pid == os.getpid():
        print(colorize("PID file points to this process; nothing to stop.", C.YELLOW))
        remove_pid_file()
        return 0

    print(f"Stopping tunnel process (PID {pid})...")
    try:
        if sys.platform == "win32":
            import subprocess

            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                text=True,
                shell=False,
            )
            if result.returncode != 0:
                msg = (result.stderr or result.stdout or "").strip()
                print(colorize(f"Failed to stop process: {msg}", C.RED))
                return 1
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                print(
                    colorize(
                        "Process not found (already stopped?).",
                        C.YELLOW,
                    )
                )
                remove_pid_file()
                return 0
        # Give it a moment, then verify.
        removed = _wait_for_exit(pid, timeout=5.0)
        if removed:
            print(colorize("Tunnel stopped.", C.GREEN))
        else:
            print(
                colorize(
                    "Signal sent but process may still be exiting. "
                    "Verify with `python bentunnel.py status`.",
                    C.YELLOW,
                )
            )
    except Exception as e:  # noqa: BLE001
        print(colorize(f"Error stopping process: {type(e).__name__}: {e}", C.RED))
        return 1
    finally:
        remove_pid_file()

    return 0


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """Best-effort check that a process has exited (Unix only)."""
    if sys.platform == "win32":
        # On Windows, taskkill /F is immediate; nothing to poll.
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.1)
    return False


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    session_cookie = cfg.get("session_cookie")

    if not session_cookie:
        print(colorize("Not logged in", C.YELLOW))
        print("Run: python bentunnel.py login")
        return 1

    print(colorize("BenTunnel — Status", C.BOLD + C.CYAN))
    print(f"  Email:      {cfg.get('user_email', 'unknown')}")
    print(f"  Tunnel ID:  {cfg.get('tunnel_id', 'unknown')}")
    print(f"  Public URL: {cfg.get('public_url', 'unknown')}")

    # Live status (optional, best-effort).
    server_url = resolve_server_url(args.server)
    csrf_token = cfg.get("csrf_token", "")

    async def _fetch_live() -> tuple[bool, str, Optional[int]]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.get(
                    f"{server_url}/api/tunnels/me",
                    cookies={
                        "bentunnel_session": session_cookie,
                        "bentunnel_csrf": csrf_token,
                    },
                )
        except httpx.RequestError:
            return False, "unreachable (network error)", None
        if resp.status_code != 200:
            return False, f"unavailable (HTTP {resp.status_code})", None
        try:
            data = resp.json()
        except Exception:
            return False, "unavailable (invalid response)", None
        is_active = bool(data.get("is_active"))
        local_port = data.get("local_port")
        status = "online" if is_active else "offline"
        return is_active, status, local_port

    try:
        is_active, status_str, live_port = asyncio.run(_fetch_live())
        color = C.GREEN if is_active else C.YELLOW
        port_part = f", local_port={live_port}" if live_port else ""
        print(colorize(f"  Live status: {status_str}{port_part}", color))
    except Exception as e:  # noqa: BLE001
        print(colorize(f"  Live status: error ({type(e).__name__})", C.YELLOW))

    return 0


def cmd_logout(args: argparse.Namespace) -> int:  # noqa: ARG001
    clear_session()
    print(colorize("Logged out.", C.GREEN))
    return 0


# ---------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bentunnel",
        description="BenTunnel — expose your localhost to the public internet.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"BenTunnel v{CLIENT_VERSION}",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--server",
        default=None,
        help="Override the server URL (default: $BENTUNNEL_SERVER or built-in)",
    )

    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser(
        "login", parents=[common], help="Log in to BenTunnel"
    )
    p_login.set_defaults(func=cmd_login)

    p_start = sub.add_parser(
        "start", parents=[common], help="Start a tunnel exposing a local port"
    )
    p_start.add_argument("port", help="Local port to expose (1-65535)")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", parents=[common], help="Stop the running tunnel")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser(
        "status", parents=[common], help="Show tunnel status"
    )
    p_status.set_defaults(func=cmd_status)

    p_logout = sub.add_parser(
        "logout", parents=[common], help="Log out and clear stored credentials"
    )
    p_logout.set_defaults(func=cmd_logout)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    func = args.func
    try:
        return int(func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
