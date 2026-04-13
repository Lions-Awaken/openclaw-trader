"""
Shared state and helpers for the OpenClaw dashboard.

All route modules import from here so that the single httpx.AsyncClient,
environment variables, and auth helpers are shared across the whole app
without creating circular imports.
"""

import hashlib
import hmac
import os
import re
import secrets
import time

import httpx
from fastapi import HTTPException, Request

# ============================================================================
# Environment variables
# ============================================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
RIDLEY_URL = os.environ.get("RIDLEY_URL", "http://ridley:9090")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE = "https://paper-api.alpaca.markets"
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SENTRY_AUTH_TOKEN = os.environ.get("SENTRY_AUTH_TOKEN", "")
SENTRY_ORG = os.environ.get("SENTRY_ORG", "lions-awaken")
SENTRY_PROJECT = os.environ.get("SENTRY_PROJECT", "openclaw-trader")

# ============================================================================
# Persistent HTTP client
# ============================================================================

_http: httpx.AsyncClient | None = None


def get_http() -> httpx.AsyncClient:
    """Get the shared async HTTP client."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=10.0)
    return _http


def set_http_client(client: httpx.AsyncClient) -> None:
    """Set the shared HTTP client (called from app startup)."""
    global _http
    _http = client


async def close_http_client() -> None:
    """Close the shared HTTP client (called from app shutdown)."""
    global _http
    if _http:
        await _http.aclose()


# ============================================================================
# Supabase headers
# ============================================================================


def sb_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


# ============================================================================
# Input validation helpers
# ============================================================================

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,60}$")
_SAFE_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clamp_days(days: int, max_days: int = 365) -> int:
    """Clamp days parameter to prevent unbounded queries."""
    return min(max(1, days), max_days)


def _validate_uuid(val: str) -> str:
    if not _UUID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid ID format")
    return val


def _validate_ticker(val: str) -> str:
    if not _SAFE_TICKER_RE.match(val.upper()):
        raise HTTPException(status_code=400, detail="Invalid ticker")
    return val.upper()


def _validate_date(val: str) -> str:
    from datetime import datetime

    if not _DATE_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid date format — expected YYYY-MM-DD")
    try:
        datetime.strptime(val, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date value")
    return val


def _validate_pipeline_name(val: str) -> str:
    if not _SAFE_NAME_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid pipeline name")
    return val


# ============================================================================
# Auth helpers
# ============================================================================

# Auth config
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_KEY", "")
if not DASHBOARD_PASSWORD:
    DASHBOARD_PASSWORD = secrets.token_urlsafe(24)
    print(
        "[Dashboard] WARNING: No DASHBOARD_KEY set. Using auto-generated password. "
        "Set DASHBOARD_KEY env var."
    )

PASSWORD_HASH = hashlib.sha256(DASHBOARD_PASSWORD.encode()).hexdigest()

_SESSION_SIGNING_SALT = os.environ.get("SESSION_SIGNING_SALT", "oc-session-stable-v1")
if _SESSION_SIGNING_SALT == "oc-session-stable-v1":
    print(
        "[Dashboard] WARNING: SESSION_SIGNING_SALT is using the default value. "
        "Set the SESSION_SIGNING_SALT environment variable for production security."
    )
_SIGNING_KEY = hashlib.sha256(_SESSION_SIGNING_SALT.encode()).digest()

SESSION_MAX_AGE = 86400 * 90  # 90 days


def _verify_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    issued_str, sig = parts
    try:
        issued = int(issued_str)
    except ValueError:
        return False
    expected = hmac.new(_SIGNING_KEY, issued_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    if time.time() - issued > SESSION_MAX_AGE:
        return False
    return True


def _is_authed(request: Request, session: str | None) -> bool:
    return _verify_session(session)


def _require_auth(request: Request, session: str | None) -> None:
    if not _is_authed(request, session):
        raise HTTPException(status_code=401, detail="Unauthorized")
