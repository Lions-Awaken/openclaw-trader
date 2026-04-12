#!/usr/bin/env python3
"""
OpenClaw Trader Dashboard — Local web UI for monitoring the trading agent.
Serves the dashboard HTML and proxies data from Supabase + Alpaca.

Authentication: password login form with session tokens, rate limiting,
and CSRF protection. No query-param key exposure.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import httpx
import yfinance as yf
from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

app = FastAPI(title="OpenClaw Trader Dashboard", docs_url=None, redoc_url=None)

# CORS — restrict to our own origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://openclaw-dashboard.fly.dev",
        "http://localhost:8090",
    ],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ============================================================================
# Persistent HTTP clients — reuse connections across requests
# ============================================================================

_http: httpx.AsyncClient | None = None


def get_http() -> httpx.AsyncClient:
    """Get the shared async HTTP client."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=10.0)
    return _http


@app.on_event("startup")
async def startup():
    global _http
    _http = httpx.AsyncClient(timeout=10.0)


@app.on_event("shutdown")
async def shutdown():
    if _http:
        await _http.aclose()


# ============================================================================
# Security Headers Middleware
# ============================================================================


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ============================================================================
# Security: Global exception handler — never leak stack traces
# ============================================================================

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse({"error": "Internal server error"}, status_code=500)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ============================================================================
# Input validation helpers
# ============================================================================

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_SAFE_KEY_RE = re.compile(r'^[a-z][a-z0-9_]{1,60}$')
_SAFE_NAME_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{0,60}$')
_SAFE_TICKER_RE = re.compile(r'^[A-Z]{1,5}$')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def clamp_days(days: int, max_days: int = 365) -> int:
    """Clamp days parameter to prevent unbounded queries."""
    return min(max(1, days), max_days)

ALLOWED_BUDGET_KEYS = {"daily_claude_budget", "daily_perplexity_budget"}


def _validate_uuid(val: str) -> str:
    if not _UUID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid ID format")
    return val


def _validate_pipeline_name(val: str) -> str:
    if not _SAFE_NAME_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid pipeline name")
    return val


def _validate_ticker(val: str) -> str:
    if not _SAFE_TICKER_RE.match(val.upper()):
        raise HTTPException(status_code=400, detail="Invalid ticker")
    return val.upper()


def _validate_date(val: str) -> str:
    if not _DATE_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid date format — expected YYYY-MM-DD")
    try:
        datetime.strptime(val, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date value")
    return val

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE = "https://paper-api.alpaca.markets"
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SENTRY_AUTH_TOKEN = os.environ.get("SENTRY_AUTH_TOKEN", "")
SENTRY_ORG = os.environ.get("SENTRY_ORG", "lions-awaken")
SENTRY_PROJECT = os.environ.get("SENTRY_PROJECT", "openclaw-trader")

# Magic link email config
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")  # Gmail app password
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER

# Auth config
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_KEY", "")
if not DASHBOARD_PASSWORD:
    DASHBOARD_PASSWORD = secrets.token_urlsafe(24)
    print("[Dashboard] WARNING: No DASHBOARD_KEY set. Using auto-generated password. Set DASHBOARD_KEY env var.")

PASSWORD_HASH = hashlib.sha256(DASHBOARD_PASSWORD.encode()).hexdigest()

# Signing key for session cookies — stable across password rotations.
# Uses a fixed salt so sessions survive DASHBOARD_KEY changes.
_SESSION_SIGNING_SALT = os.environ.get("SESSION_SIGNING_SALT", "oc-session-stable-v1")
if _SESSION_SIGNING_SALT == "oc-session-stable-v1":
    print("[Dashboard] WARNING: SESSION_SIGNING_SALT is using the default value. Set the SESSION_SIGNING_SALT environment variable for production security.")
_SIGNING_KEY = hashlib.sha256(_SESSION_SIGNING_SALT.encode()).digest()

SESSION_MAX_AGE = 86400 * 90  # 90 days

# Rate limiting: {ip: [timestamps]}
_login_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 300  # 5 minutes





def _check_rate_limit(ip: str) -> bool:
    """Returns True if rate limited."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < ATTEMPT_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) >= MAX_ATTEMPTS


def _record_attempt(ip: str):
    _login_attempts.setdefault(ip, []).append(time.time())


def _create_session() -> str:
    """Create a signed session cookie. Stateless — survives machine restarts."""
    issued = str(int(time.time()))
    sig = hmac.new(_SIGNING_KEY, issued.encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{sig}"


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


def _create_csrf() -> str:
    """Create a signed CSRF token. Stateless — works across machines."""
    nonce = secrets.token_urlsafe(16)
    issued = str(int(time.time()))
    payload = f"{nonce}.{issued}"
    sig = hmac.new(_SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_csrf(token: str | None) -> bool:
    if not token:
        return False
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        return False
    payload, sig = parts
    expected = hmac.new(_SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    # Check expiry (10 min)
    try:
        issued = int(payload.split(".")[1])
    except (IndexError, ValueError):
        return False
    return time.time() - issued <= 600


def _is_authed(request: Request, session: str | None) -> bool:
    return _verify_session(session)


def _require_auth(request: Request, session: str | None):
    if not _is_authed(request, session):
        raise HTTPException(status_code=401, detail="Unauthorized")


def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


# ============================================================================
# Auth Routes
# ============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, oc_session: str | None = Cookie(None)):
    # Already authenticated? Redirect to dashboard
    if _is_authed(request, oc_session):
        return RedirectResponse("/", status_code=302)
    csrf = _create_csrf()
    return FileResponse(
        Path(__file__).parent / "login.html",
        headers={
            "X-CSRF-Token": csrf,
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")

    # Rate limit check
    if _check_rate_limit(ip):
        return HTMLResponse(
            _login_error_page("Too many attempts. Wait 5 minutes.", _create_csrf()),
            status_code=429,
        )

    # CSRF check
    if not _verify_csrf(csrf_token):
        return HTMLResponse(
            _login_error_page("Session expired. Please try again.", _create_csrf()),
            status_code=403,
        )

    # Password check (constant-time comparison)
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    if not hmac.compare_digest(password_hash, PASSWORD_HASH):
        _record_attempt(ip)
        remaining = MAX_ATTEMPTS - len(_login_attempts.get(ip, []))
        return HTMLResponse(
            _login_error_page(
                f"Invalid password. {remaining} attempts remaining.",
                _create_csrf(),
            ),
            status_code=401,
        )

    # Success — create session
    token = _create_session()
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        "oc_session",
        token,
        httponly=True,
        samesite="strict",
        secure=True,
        max_age=SESSION_MAX_AGE,
    )
    return resp


@app.get("/logout")
async def logout(oc_session: str | None = Cookie(None)):
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("oc_session")
    return resp


def _login_error_page(error: str, csrf: str) -> str:
    """Return the login page HTML with an error message injected."""
    from html import escape
    html_path = Path(__file__).parent / "login.html"
    html = html_path.read_text()
    # Inject error (escaped) and CSRF token
    html = html.replace("<!-- ERROR_PLACEHOLDER -->", f'<div class="error">{escape(error)}</div>')
    html = html.replace("CSRF_TOKEN_PLACEHOLDER", csrf)
    return html


# ============================================================================
# ============================================================================
# Magic Link System
# ============================================================================

MAGIC_LINK_DURATIONS = {
    "1h": 3600,
    "24h": 86400,
    "7d": 86400 * 7,
}


def _send_magic_email(email: str, link: str, expires_label: str) -> bool:
    """Send magic link via SMTP. Returns True on success."""
    if not SMTP_USER or not SMTP_PASS:
        return False

    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "OpenClaw Trader — Access Link"
    msg["From"] = SMTP_FROM
    msg["To"] = email

    text = f"Your OpenClaw Trader access link (expires in {expires_label}):\n\n{link}\n\nThis link is one-time use. It will stop working after you click it or after the timer expires."

    html = f"""<div style="font-family:monospace;background:#050508;color:#e8e8f0;padding:40px;border-radius:12px;max-width:500px">
<h2 style="color:#22d3ee;letter-spacing:3px;margin:0 0 20px">OPENCLAW TRADER</h2>
<p style="color:#a0a0b0;margin:0 0 20px">You have been granted access to the trading dashboard.</p>
<a href="{link}" style="display:inline-block;padding:14px 32px;background:transparent;border:2px solid #22d3ee;border-radius:10px;color:#22d3ee;text-decoration:none;font-weight:bold;letter-spacing:2px;font-family:monospace">ACCESS DASHBOARD</a>
<p style="color:#666;margin:20px 0 0;font-size:12px">This link expires in {expires_label}. One-time use only.</p>
</div>"""

    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"[magic-link] SMTP error: {e}")
        return False


@app.post("/api/magic-link/create")
async def create_magic_link(request: Request, oc_session: str | None = Cookie(None)):
    """Generate a magic login link. Only authenticated users can create them."""
    _require_auth(request, oc_session)

    body = await request.json()
    email = body.get("email", "").strip().lower()
    duration = body.get("duration", "24h")

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")

    if duration not in MAGIC_LINK_DURATIONS:
        raise HTTPException(status_code=400, detail="Invalid duration")

    ttl = MAGIC_LINK_DURATIONS[duration]
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    # Store in Supabase
    client = get_http()
    resp = await client.post(
        f"{SUPABASE_URL}/rest/v1/magic_link_tokens",
        headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=representation"},
        json={
            "token_hash": token_hash,
            "email": email,
            "expires_at": expires_at.isoformat(),
        },
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail="Failed to create token")

    # Build the link
    host = request.headers.get("host", "openclaw-trader-dash.fly.dev")
    scheme = "https" if "fly.dev" in host or "https" in request.headers.get("x-forwarded-proto", "") else "http"
    link = f"{scheme}://{host}/auth/link?t={token}"

    # Try to send email
    email_sent = _send_magic_email(email, link, duration)

    return {
        "link": link,
        "email": email,
        "expires_at": expires_at.isoformat(),
        "duration": duration,
        "email_sent": email_sent,
    }


@app.get("/auth/link")
async def consume_magic_link(t: str = ""):
    """Validate and consume a magic link token."""
    if not t:
        return RedirectResponse("/login", status_code=302)

    token_hash = hashlib.sha256(t.encode()).hexdigest()

    client = get_http()
    # Find the token
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/magic_link_tokens",
        headers=sb_headers(),
        params={
            "token_hash": f"eq.{token_hash}",
            "used_at": "is.null",
            "revoked": "eq.false",
            "select": "id,expires_at,email",
        },
    )

    if resp.status_code != 200 or not resp.json():
        return HTMLResponse(
            '<html><body style="background:#050508;color:#ff3344;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;text-align:center">'
            '<div><h1 style="letter-spacing:4px">LINK EXPIRED</h1><p style="color:#666;margin-top:12px">This link has been used or has expired.</p>'
            '<a href="/login" style="color:#22d3ee;margin-top:20px;display:inline-block">GO TO LOGIN</a></div></body></html>',
            status_code=403,
        )

    token_row = resp.json()[0]
    token_id = token_row["id"]
    expires_at = token_row["expires_at"]

    # Check expiry
    if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) < datetime.now(timezone.utc):
        return HTMLResponse(
            '<html><body style="background:#050508;color:#ff3344;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;text-align:center">'
            '<div><h1 style="letter-spacing:4px">FUSE BURNED</h1><p style="color:#666;margin-top:12px">This link has expired.</p>'
            '<a href="/login" style="color:#22d3ee;margin-top:20px;display:inline-block">GO TO LOGIN</a></div></body></html>',
            status_code=403,
        )

    # Consume the token (mark as used)
    await client.patch(
        f"{SUPABASE_URL}/rest/v1/magic_link_tokens",
        headers={**sb_headers(), "Content-Type": "application/json"},
        params={"id": f"eq.{token_id}"},
        json={"used_at": datetime.now(timezone.utc).isoformat()},
    )

    # Create session
    session_token = _create_session()
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(
        "oc_session",
        session_token,
        httponly=True,
        samesite="strict",
        secure=True,
        max_age=SESSION_MAX_AGE,
    )
    return resp


@app.get("/api/magic-link/list")
async def list_magic_links(request: Request, oc_session: str | None = Cookie(None)):
    """List all magic links (for management UI)."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []

    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/magic_link_tokens",
        headers=sb_headers(),
        params={
            "select": "id,email,expires_at,used_at,revoked,created_at",
            "order": "created_at.desc",
            "limit": "20",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.post("/api/magic-link/revoke")
async def revoke_magic_link(request: Request, oc_session: str | None = Cookie(None)):
    """Revoke a magic link."""
    _require_auth(request, oc_session)
    body = await request.json()
    link_id = body.get("id", "")
    link_id = _validate_uuid(link_id)

    client = get_http()
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/magic_link_tokens",
        headers={**sb_headers(), "Content-Type": "application/json"},
        params={"id": f"eq.{link_id}"},
        json={"revoked": True},
    )
    return {"ok": resp.status_code in (200, 204)}


# ============================================================================
# Static Assets
# ============================================================================

@app.get("/theme.css")
async def theme_css():
    return FileResponse(Path(__file__).parent / "theme.css", media_type="text/css")


# Static files — workflow widget, standalone HTML tools, etc.
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# Dashboard Route
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, oc_session: str | None = Cookie(None)):
    if not _is_authed(request, oc_session):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/systems", response_class=HTMLResponse)
async def systems_console(request: Request, oc_session: str | None = Cookie(None)):
    if not _is_authed(request, oc_session):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(Path(__file__).parent / "systems-console.html")


# ============================================================================
# API Routes (all require auth)
# ============================================================================

@app.get("/api/account")
async def get_account(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    client = get_http()
    resp = await client.get(
        f"{ALPACA_BASE}/v2/account",
        headers={
            "APCA-API-KEY-ID": ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        },
    )
    if resp.status_code == 200:
        data = resp.json()
        return {
            "equity": float(data.get("equity", 0)),
            "cash": float(data.get("cash", 0)),
            "buying_power": float(data.get("buying_power", 0)),
            "portfolio_value": float(data.get("portfolio_value", 0)),
            "account_number": data.get("account_number", ""),
            "status": data.get("status", ""),
            "paper": data.get("account_number", "").startswith("PA"),
        }
    return {"error": f"Alpaca {resp.status_code}"}


@app.get("/api/positions")
async def get_positions(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    client = get_http()
    resp = await client.get(
        f"{ALPACA_BASE}/v2/positions",
        headers={
            "APCA-API-KEY-ID": ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        },
    )
    if resp.status_code == 200:
        positions = []
        for p in resp.json():
            positions.append({
                "symbol": p.get("symbol"),
                "qty": float(p.get("qty", 0)),
                "avg_entry": float(p.get("avg_entry_price", 0)),
                "current_price": float(p.get("current_price", 0)),
                "market_value": float(p.get("market_value", 0)),
                "unrealized_pl": float(p.get("unrealized_pl", 0)),
                "unrealized_plpc": float(p.get("unrealized_plpc", 0)) * 100,
                "side": p.get("side", ""),
            })
        return positions
    return []


@app.get("/api/trades")
async def get_trades(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_decisions",
        headers=sb_headers(),
        params={
            "select": "id,ticker,action,entry_price,exit_price,pnl,outcome,signals_fired,hold_days,reasoning,what_worked,improvement,created_at",
            "order": "created_at.desc",
            "limit": "50",
        },
    )
    if resp.status_code == 200:
        return resp.json()
    return []


@app.get("/api/performance")
async def get_performance(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/account_performance",
        headers=sb_headers(),
    )
    if resp.status_code == 200:
        rows = resp.json()
        return rows[0] if rows else {}
    return {}


@app.get("/api/regime")
async def get_regime(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    regime_file = Path.home() / ".openclaw/workspace/memory/regime-current.json"
    if regime_file.exists():
        return json.loads(regime_file.read_text())
    return {"regime": "UNKNOWN", "action": "No regime data — run regime.py first"}


@app.get("/api/regime-history")
async def get_regime_history(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/regime_log",
        headers=sb_headers(),
        params={"order": "logged_at.desc", "limit": "30"},
    )
    if resp.status_code == 200:
        return resp.json()
    return []


@app.get("/api/predictions")
async def get_predictions(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/predictions",
        headers=sb_headers(),
        params={
            "select": "id,ticker,prediction_type,thesis,predicted_direction,predicted_target,entry_price,confidence,timeframe,regime_at_time,actual_price,actual_direction,accuracy_score,correct,post_mortem,lessons_learned,status,expires_at,graded_at,created_at",
            "order": "created_at.desc",
            "limit": "50",
        },
    )
    if resp.status_code == 200:
        return resp.json()
    return []


@app.get("/api/prediction-accuracy")
async def get_prediction_accuracy(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/prediction_accuracy",
        headers=sb_headers(),
    )
    if resp.status_code == 200:
        rows = resp.json()
        return rows[0] if rows else {}
    return {}


@app.get("/api/system/current")
async def get_system_current(request: Request, oc_session: str | None = Cookie(None)):
    """Latest system stats from Jetson."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_stats",
        headers=sb_headers(),
        params={"order": "collected_at.desc", "limit": "1"},
    )
    if resp.status_code == 200:
        rows = resp.json()
        return rows[0] if rows else {}
    return {}


@app.get("/api/system/history")
async def get_system_history(request: Request, oc_session: str | None = Cookie(None), minutes: int = 30):
    """System stats history for charts."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_stats",
        headers=sb_headers(),
        params={
            "select": "cpu_percent,mem_percent,gpu_load_pct,gpu_temp_c,cpu_temp_c,collected_at",
            "collected_at": f"gte.{cutoff}",
            "order": "collected_at.asc",
        },
    )
    if resp.status_code == 200:
        return resp.json()
    return []


# ============================================================================
# Systems Console API — transforms system_stats for the 3D console
# ============================================================================

@app.get("/api/system/info")
async def system_info(request: Request, oc_session: str | None = Cookie(None)):
    """Static hardware info for the systems console header."""
    _require_auth(request, oc_session)
    return {
        "hostname": "ridley",
        "hardware": {
            "device": "Jetson Orin Nano Super",
            "cpu": "6x ARM Cortex-A78AE",
            "gpu": "Orin (Ampere)",
            "ram_mb": 7620,
            "power_mode": "MAXN_SUPER",
        },
    }


def _status(value: float, normal_lt: float, warning_lt: float) -> str:
    """Compute normal/warning/critical status. normal < normal_lt, warning < warning_lt."""
    if value < normal_lt:
        return "normal"
    if value < warning_lt:
        return "warning"
    return "critical"


def _status_gt(value: float, normal_gt: float, warning_gt: float) -> str:
    """Inverted thresholds — higher is better (e.g. pipeline_health)."""
    if value > normal_gt:
        return "normal"
    if value > warning_gt:
        return "warning"
    return "critical"


def _build_metrics(
    row: dict,
    pipeline_runs: list[dict],
    cron_rows: list[dict],
    stack_services: dict[str, bool],
    inference_rows: list[dict],
    network_ms: float,
    ollama_heartbeat: dict | None,
) -> dict:
    """Assemble the full metrics dict from raw DB data."""
    cpu_pct = float(row.get("cpu_percent", 0) or 0)
    mem_pct = float(row.get("mem_percent", 0) or 0)
    gpu_pct = float(row.get("gpu_load_pct", 0) or 0)
    cpu_temp = float(row.get("cpu_temp_c", 0) or 0)
    gpu_temp = float(row.get("gpu_temp_c", 0) or 0)
    tj = max(cpu_temp, gpu_temp)
    cores = int(row.get("cpu_cores", 6) or 6)

    # pipeline_health
    total_runs = len(pipeline_runs)
    success_runs = sum(1 for r in pipeline_runs if r.get("status") == "success")
    pipeline_pct = round(success_runs / total_runs * 100, 1) if total_runs else 0.0

    # inference_latency from pipeline_runs
    durations = [
        float(r.get("duration_ms") or 0)
        for r in inference_rows
        if r.get("duration_ms") is not None
    ]
    durations.sort()
    n = len(durations)
    lat_value = durations[n // 2] if n else 0.0
    lat_p95 = durations[int(n * 0.95)] if n > 1 else lat_value

    # cron_health — latest root run per pipeline
    _cron_seen: dict[str, dict] = {}
    for cr in cron_rows:
        name = cr.get("pipeline_name", "")
        if name not in _cron_seen:
            _cron_seen[name] = cr
    def _cron_entry(pipeline_key: str, max_age_h: float) -> dict:
        row_c = _cron_seen.get(pipeline_key)
        if not row_c:
            return {"last_run": None, "status": "unknown", "stale": True}
        last_run_str = row_c.get("started_at") or ""
        try:
            last_run_dt = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - last_run_dt).total_seconds() / 3600
            stale = age_h > max_age_h
        except (ValueError, AttributeError):
            stale = True
        return {"last_run": last_run_str or None, "status": row_c.get("status", "unknown"), "stale": stale}

    cron_pipelines = {
        "scanner": _cron_entry("scanner", 13),
        "catalyst_ingest": _cron_entry("catalyst_ingest", 9),
        "position_manager": _cron_entry("position_manager", 1),
        "meta_daily": _cron_entry("meta_daily", 25),
        "meta_weekly": _cron_entry("meta_weekly", 170),
        "calibrator": _cron_entry("calibrator", 170),
        "heartbeat": _cron_entry("heartbeat", 2),
    }
    fresh_count = sum(1 for v in cron_pipelines.values() if not v["stale"])

    # ollama_status — prefer heartbeat, fall back to system_stats
    if ollama_heartbeat:
        hb_meta = ollama_heartbeat.get("metadata") or {}
        ollama_val = "loaded" if hb_meta.get("alive") else "down"
        models_loaded = hb_meta.get("models", [])
        vram_mb = int(row.get("ollama_vram_mb", 0) or 0)
    else:
        running = row.get("ollama_running", False)
        ollama_val = "loaded" if running else "down"
        raw_models = row.get("ollama_models") or []
        models_loaded = raw_models if isinstance(raw_models, list) else []
        vram_mb = int(row.get("ollama_vram_mb", 0) or 0)

    # stack_health
    svc_count = sum(1 for v in stack_services.values() if v)

    # disk_root
    disk_root_pct = float(row.get("disk_root_pct", 0) or 0)
    total_gb = 60.0  # Jetson Orin Nano eMMC
    used_gb = round(disk_root_pct * total_gb / 100, 1)

    return {
        "cpu_usage": {
            "value": cpu_pct,
            "status": _status(cpu_pct, 70, 90),
            "per_core": [cpu_pct] * cores,
            "freq_mhz": float(row.get("cpu_freq_mhz", 0) or 0),
        },
        "mem_usage": {
            "value": mem_pct,
            "status": _status(mem_pct, 75, 90),
            "total_mb": float(row.get("mem_total_mb", 0) or 0),
            "used_mb": float(row.get("mem_used_mb", 0) or 0),
            "available_mb": float(row.get("mem_available_mb", 0) or 0),
            "breakdown": {
                "ollama_mb": float(row.get("ollama_mem_mb", 0) or 0),
                "gateway_mb": 0.0,
                "openclaw_mb": float(row.get("openclaw_mem_mb", 0) or 0),
            },
        },
        "gpu_load": {
            "value": gpu_pct,
            "status": _status(gpu_pct, 70, 90),
            "freq_mhz": 1020,
        },
        "tj_temp": {
            "value": tj,
            "status": _status(tj, 70, 85),
            "zones": {
                "cpu": cpu_temp,
                "gpu": gpu_temp,
                "cv0": None,
                "cv1": None,
                "cv2": None,
                "soc0": None,
                "soc1": None,
                "soc2": None,
                "tj": tj,
            },
        },
        "inference_latency": {
            "value": lat_value,
            "status": _status(lat_value, 5000, 15000),
            "p50": lat_value,
            "p95": lat_p95,
            "sample_count": n,
        },
        "ollama_tokens_per_sec": {
            "value": 0.0,
            "status": "normal",
            "min": 0.0,
            "max": 0.0,
        },
        "pipeline_health": {
            "value": pipeline_pct,
            "status": _status_gt(pipeline_pct, 95, 80),
            "total": total_runs,
            "successes": success_runs,
            "failures": total_runs - success_runs,
        },
        "cron_health": {
            "value": fresh_count,
            "status": "normal" if fresh_count >= 5 else "warning" if fresh_count >= 3 else "critical",
            "pipelines": cron_pipelines,
        },
        "swap_usage": {
            "value": 0.0,
            "status": "normal",
            "total_mb": 0,
        },
        "disk_root_usage": {
            "value": disk_root_pct,
            "status": _status(disk_root_pct, 75, 90),
            "used_gb": used_gb,
            "total_gb": total_gb,
        },
        "power_draw": {
            "value": 0.0,
            "status": "normal",
            "rails": {"vdd_in": 0, "vdd_cpu_gpu_cv": 0, "vdd_soc": 0},
        },
        "ollama_status": {
            "value": ollama_val,
            "status": "normal" if ollama_val != "down" else "critical",
            "models_loaded": models_loaded,
            "vram_mb": vram_mb,
        },
        "stack_health": {
            "value": svc_count,
            "status": "normal" if svc_count >= 7 else "warning" if svc_count >= 5 else "critical",
            "services": {k: stack_services.get(k, False) for k in [
                "supabase", "alpaca", "ollama", "finnhub",
                "sentry", "pgvector", "tumbler", "claude",
            ]},
        },
        "network_latency": {
            "value": network_ms,
            "status": _status(network_ms, 100, 250) if network_ms > 0 else "normal",
        },
    }


async def _fetch_system_data(client: httpx.AsyncClient) -> dict:
    """Fetch all raw data needed to build the metrics dict. Returns dict of raw results."""
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    async def _get_stats() -> dict:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/system_stats",
                headers=sb_headers(),
                params={"order": "collected_at.desc", "limit": "1"},
            )
            rows = r.json() if r.status_code == 200 else []
            return rows[0] if rows else {}
        except Exception:
            return {}

    async def _get_pipeline_runs() -> list[dict]:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/pipeline_runs",
                headers=sb_headers(),
                params={"select": "status", "step_name": "eq.root", "started_at": f"gte.{cutoff_24h}", "limit": "500"},
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _get_cron_rows() -> list[dict]:
        """Get latest root run per pipeline. Use 7-day window + high limit to catch weekly jobs."""
        try:
            cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/pipeline_runs",
                headers=sb_headers(),
                params={
                    "select": "pipeline_name,status,started_at",
                    "step_name": "eq.root",
                    "started_at": f"gte.{cutoff_7d}",
                    "order": "started_at.desc",
                    "limit": "500",
                },
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _get_inference_rows() -> list[dict]:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/pipeline_runs",
                headers=sb_headers(),
                params={
                    "select": "duration_ms",
                    "or": "step_name.like.*call_claude*,step_name.like.*call_ollama*",
                    "started_at": f"gte.{cutoff_24h}",
                    "limit": "200",
                },
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _get_heartbeats() -> list[dict]:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/stack_heartbeats",
                headers=sb_headers(),
                params={"select": "service,last_seen,metadata"},
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _get_network_ms() -> float:
        try:
            t0 = asyncio.get_event_loop().time()
            r = await client.get(
                f"{ALPACA_BASE}/v2/clock",
                headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
                timeout=5.0,
            )
            elapsed = (asyncio.get_event_loop().time() - t0) * 1000
            return round(elapsed, 1) if r.status_code in (200, 403) else 9999.0
        except Exception:
            return 9999.0

    async def _get_stack_live() -> dict[str, bool]:
        """Live-ping all 8 services for stack health."""
        results: dict[str, bool] = {}
        try:
            r = await client.get(f"{SUPABASE_URL}/rest/v1/budget_config", headers=sb_headers(), params={"select": "id", "limit": "1"})
            results["supabase"] = r.status_code == 200
        except Exception:
            results["supabase"] = False
        try:
            r = await client.get(f"{ALPACA_BASE}/v2/account", headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET})
            results["alpaca"] = r.status_code == 200
        except Exception:
            results["alpaca"] = False
        results["finnhub"] = bool(FINNHUB_KEY)
        try:
            r = await client.post(f"{SUPABASE_URL}/rest/v1/rpc/match_meta_reflections", headers=sb_headers(),
                                  json={"query_embedding": [0.0] * 768, "match_threshold": 0.0, "match_count": 1})
            results["pgvector"] = r.status_code in (200, 406)
        except Exception:
            results["pgvector"] = False
        results["claude"] = bool(ANTHROPIC_API_KEY and len(ANTHROPIC_API_KEY) > 10)
        results["sentry"] = bool(SENTRY_AUTH_TOKEN or os.environ.get("SENTRY_DSN", ""))
        return results

    (stats_row, pipeline_runs, cron_rows, inference_rows, heartbeats, network_ms) = (
        await asyncio.gather(
            _get_stats(),
            _get_pipeline_runs(),
            _get_cron_rows(),
            _get_inference_rows(),
            _get_heartbeats(),
            _get_network_ms(),
        )
    )

    # Build stack_services from live checks + heartbeats
    stack_services = await _get_stack_live()
    ollama_heartbeat: dict | None = None
    stale_cutoff_str = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    for hb in heartbeats:
        svc = hb.get("service", "")
        last_seen = hb.get("last_seen", "")
        alive = last_seen > stale_cutoff_str if last_seen else False
        meta = hb.get("metadata") or {}
        if svc == "ollama":
            stack_services["ollama"] = alive and meta.get("alive", False)
            ollama_heartbeat = hb if alive else None
        elif svc == "tumbler":
            stack_services["tumbler"] = alive and meta.get("alive", False)

    return {
        "stats_row": stats_row,
        "pipeline_runs": pipeline_runs,
        "cron_rows": cron_rows,
        "inference_rows": inference_rows,
        "stack_services": stack_services,
        "ollama_heartbeat": ollama_heartbeat,
        "network_ms": float(network_ms),
        "collected_at": (stats_row or {}).get("collected_at"),
    }


@app.get("/api/system/metrics")
async def system_metrics(request: Request, oc_session: str | None = Cookie(None)):
    """Full snapshot of all system metrics for initial load and reconnection."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"timestamp": None, "metrics": {}}

    client = get_http()
    raw = await _fetch_system_data(client)
    metrics = _build_metrics(
        row=raw["stats_row"],
        pipeline_runs=raw["pipeline_runs"],
        cron_rows=raw["cron_rows"],
        stack_services=raw["stack_services"],
        inference_rows=raw["inference_rows"],
        network_ms=raw["network_ms"],
        ollama_heartbeat=raw["ollama_heartbeat"],
    )
    return {"timestamp": raw["collected_at"], "metrics": metrics}


@app.get("/api/system/metrics/{metric_name}/history")
async def system_metric_history(
    metric_name: str, request: Request, oc_session: str | None = Cookie(None), window: int = 300,
) -> dict:
    """Historical datapoints for a single metric (sparklines). Window in seconds."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"datapoints": []}

    col_map = {
        "cpu_usage": "cpu_percent",
        "mem_usage": "mem_percent",
        "gpu_load": "gpu_load_pct",
        "tj_temp": "gpu_temp_c",
    }
    col = col_map.get(metric_name)
    # Metrics with no system_stats column return empty — SSE will populate them live
    if not col:
        return {"datapoints": []}

    window = min(max(60, window), 3600)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_stats",
        headers=sb_headers(),
        params={
            "select": f"{col},collected_at",
            "collected_at": f"gte.{cutoff}",
            "order": "collected_at.asc",
            "limit": "150",
        },
    )
    rows = resp.json() if resp.status_code == 200 else []
    return {
        "datapoints": [
            {"value": float(r.get(col, 0) or 0), "ts": r.get("collected_at")} for r in rows
        ]
    }


# SSE tier intervals (seconds)
_FAST_INTERVAL = 2
_MED_INTERVAL = 5
_SLOW_INTERVAL = 30

# Track last known status per metric to emit alert events on transitions
_sse_prev_status: dict[str, str] = {}


@app.get("/api/system/stream")
async def system_stream(request: Request, oc_session: str | None = Cookie(None)):
    """Server-Sent Events stream — real-time metric updates at three tiers."""
    _require_auth(request, oc_session)

    client = get_http()

    async def generate():
        last_fast = 0.0
        last_med = 0.0
        last_slow = 0.0
        prev_status: dict[str, str] = {}

        while True:
            if await request.is_disconnected():
                break

            now = asyncio.get_event_loop().time()
            send_fast = now - last_fast >= _FAST_INTERVAL
            send_med = now - last_med >= _MED_INTERVAL
            send_slow = now - last_slow >= _SLOW_INTERVAL

            if not (send_fast or send_med or send_slow):
                await asyncio.sleep(0.5)
                continue

            # Fetch raw data for any tier that's due
            try:
                raw = await _fetch_system_data(client)
            except Exception:
                await asyncio.sleep(2)
                continue

            all_metrics = _build_metrics(
                row=raw["stats_row"],
                pipeline_runs=raw["pipeline_runs"],
                cron_rows=raw["cron_rows"],
                stack_services=raw["stack_services"],
                inference_rows=raw["inference_rows"],
                network_ms=raw["network_ms"],
                ollama_heartbeat=raw["ollama_heartbeat"],
            )

            updates: dict = {}
            alerts: list[dict] = []

            def _maybe_add(key: str) -> None:
                m = all_metrics.get(key)
                if m is None:
                    return
                updates[key] = m
                status = m.get("status", "normal")
                if prev_status.get(key) not in (None, status):
                    alerts.append({
                        "metric": key,
                        "value": m.get("value"),
                        "status": status,
                        "message": f"{key} transitioned to {status}",
                    })
                prev_status[key] = status

            if send_fast:
                last_fast = now
                for k in ("cpu_usage", "mem_usage", "gpu_load", "tj_temp"):
                    _maybe_add(k)

            if send_med:
                last_med = now
                for k in ("ollama_status", "swap_usage", "power_draw"):
                    _maybe_add(k)

            if send_slow:
                last_slow = now
                for k in (
                    "inference_latency",
                    "ollama_tokens_per_sec",
                    "pipeline_health",
                    "cron_health",
                    "stack_health",
                    "network_latency",
                    "disk_root_usage",
                ):
                    _maybe_add(k)

            if updates:
                ts = raw.get("collected_at") or datetime.now(timezone.utc).isoformat()
                payload = json.dumps({"timestamp": ts, "updates": updates})
                yield f"event: metrics\ndata: {payload}\n\n"

            for alert in alerts:
                yield f"event: alert\ndata: {json.dumps(alert)}\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/llm/stats")
async def get_llm_stats(request: Request, oc_session: str | None = Cookie(None)):
    """LLM inference statistics derived from pipeline_runs."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"models": [], "recent": []}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={
            "select": "id,step_name,status,duration_ms,started_at,input_snapshot,output_snapshot",
            "or": "step_name.like.*call_ollama*,step_name.like.*call_claude*",
            "started_at": f"gte.{cutoff}",
            "order": "started_at.desc",
            "limit": "200",
        },
    )
    rows: list[dict] = resp.json() if resp.status_code == 200 else []

    # Group by model — extract model name from step_name
    model_stats: dict[str, dict] = {}
    for row in rows:
        step = row.get("step_name", "")
        if "call_claude" in step:
            model = "claude"
        elif "call_ollama" in step:
            # e.g. "predictions:call_ollama_qwen" → "qwen2.5:3b"
            model = "qwen2.5:3b"
        else:
            model = step
        entry = model_stats.setdefault(
            model,
            {"model": model, "total_calls": 0, "total_duration_ms": 0, "avg_duration_ms": 0},
        )
        entry["total_calls"] += 1
        entry["total_duration_ms"] += int(row.get("duration_ms") or 0)

    for entry in model_stats.values():
        calls = entry["total_calls"]
        entry["avg_duration_ms"] = round(entry["total_duration_ms"] / calls) if calls else 0

    # Recent: top 20 rows already sorted by started_at desc
    recent = rows[:20]

    return {"models": list(model_stats.values()), "recent": recent}


# ============================================================================
# Pipeline & Meta-Learning API Routes
# ============================================================================

@app.get("/api/pipeline/runs")
async def get_pipeline_runs(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
    pipeline: str = "",
):
    """Recent pipeline runs (top-level only)."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    params = {
        "select": "id,pipeline_name,step_name,status,started_at,completed_at,duration_ms,error_message,metadata",
        "step_name": "eq.root",
        "started_at": f"gte.{cutoff}",
        "order": "started_at.desc",
        "limit": "100",
    }
    if pipeline:
        pipeline = _validate_pipeline_name(pipeline)
        params["pipeline_name"] = f"eq.{pipeline}"
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params=params,
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/pipeline/health")
async def get_pipeline_health(request: Request, oc_session: str | None = Cookie(None)):
    """Rolling 7-day pipeline health score."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"score": 0, "total": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={
            "select": "status",
            "step_name": "neq.root",
            "started_at": f"gte.{cutoff}",
            "limit": "2000",
        },
    )
    if resp.status_code != 200:
        return {"score": 0, "total": 0}
    runs = resp.json()
    total = len(runs)
    if total == 0:
        return {"score": 100, "total": 0, "successes": 0, "failures": 0}
    successes = sum(1 for r in runs if r["status"] == "success")
    failures = sum(1 for r in runs if r["status"] == "failure")
    return {
        "score": round(successes / total * 100, 1) if total else 0,
        "total": total,
        "successes": successes,
        "failures": failures,
    }


@app.get("/api/pipeline/run/{run_id}")
async def get_pipeline_run_detail(run_id: str, request: Request, oc_session: str | None = Cookie(None)):
    """Single pipeline run with all child steps."""
    _require_auth(request, oc_session)
    run_id = _validate_uuid(run_id)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    # Get root run
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={"id": f"eq.{run_id}"},
    )
    root = resp.json()[0] if resp.status_code == 200 and resp.json() else None
    if not root:
        return {}

    # Get all children (recursive via parent_run_id)
    resp2 = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={
            "select": "id,step_name,status,started_at,completed_at,duration_ms,input_snapshot,output_snapshot,error_message,parent_run_id",
            "or": f"(id.eq.{run_id},parent_run_id.eq.{run_id})",
            "order": "started_at.asc",
        },
    )
    steps = resp2.json() if resp2.status_code == 200 else []
    return {"root": root, "steps": steps}


@app.get("/api/signals/accuracy")
async def get_signal_accuracy(request: Request, oc_session: str | None = Cookie(None)):
    """Signal accuracy heatmap data from the view."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/signal_accuracy_report",
        headers=sb_headers(),
        params={"order": "week_start.desc", "limit": "12"},
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/signals/evaluations")
async def get_signal_evaluations(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
):
    """Recent signal evaluations."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/signal_evaluations",
        headers=sb_headers(),
        params={
            "select": "id,ticker,scan_date,scan_type,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision,reasoning",
            "scan_date": f"gte.{cutoff}",
            "order": "created_at.desc",
            "limit": "100",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/meta/reflections")
async def get_meta_reflections(request: Request, oc_session: str | None = Cookie(None)):
    """Recent meta reflections."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/meta_reflections",
        headers=sb_headers(),
        params={
            "select": "id,reflection_date,reflection_type,patterns_observed,signal_assessment,operational_issues,counterfactuals,adjustments,pipeline_summary,signal_accuracy,created_at",
            "order": "reflection_date.desc",
            "limit": "20",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/meta/adjustments")
async def get_meta_adjustments(request: Request, oc_session: str | None = Cookie(None)):
    """Strategy adjustments with status."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/strategy_adjustments",
        headers=sb_headers(),
        params={
            "select": "id,parameter_name,previous_value,new_value,reason,status,impact_assessment,trades_since_applied,pnl_since_applied,applied_at,created_at",
            "order": "created_at.desc",
            "limit": "30",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/predictions/live")
async def get_predictions_live(request: Request, oc_session: str | None = Cookie(None)):
    """Fetch current prices for all open predictions — polled every 30s by dashboard."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []

    client = get_http()
    # Get open predictions
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/predictions",
        headers=sb_headers(),
        params={
            "select": "id,ticker,predicted_direction,entry_price,confidence,timeframe,thesis,expires_at,created_at",
            "status": "eq.open",
            "order": "created_at.desc",
            "limit": "50",
        },
    )
    if resp.status_code != 200:
        return []

    predictions = resp.json()
    if not predictions:
        return []

    # Get unique tickers
    tickers = list(set(p["ticker"] for p in predictions))

    # Fetch latest quotes from Alpaca in parallel
    async def _fetch_quote(t: str):
        try:
            qr = await client.get(
                f"https://data.alpaca.markets/v2/stocks/{t}/quotes/latest",
                headers={
                    "APCA-API-KEY-ID": ALPACA_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET,
                },
            )
            if qr.status_code == 200:
                data = qr.json()
                quote = data.get("quote", data)
                bid = float(quote.get("bp", 0))
                ask = float(quote.get("ap", 0))
                mid = round((bid + ask) / 2, 2) if bid and ask else 0
                return t, {
                    "price": mid,
                    "bid": bid,
                    "ask": ask,
                    "spread": round(ask - bid, 4),
                }
        except Exception:
            pass
        return t, None

    quote_results = await asyncio.gather(*[_fetch_quote(t) for t in tickers])
    quotes = {t: q for t, q in quote_results if q is not None}

    # Enrich predictions with live data
    results = []
    for p in predictions:
        ticker = p["ticker"]
        entry = float(p["entry_price"])
        q = quotes.get(ticker, {})
        current = q.get("price", 0)
        change = current - entry if current else 0
        pct = (change / entry * 100) if entry and current else 0
        direction = p["predicted_direction"]

        # Is the prediction currently on track?
        on_track = (direction == "bullish" and change > 0) or \
                   (direction == "bearish" and change < 0) or \
                   (direction == "neutral" and abs(pct) < 1)

        results.append({
            "id": p["id"],
            "ticker": ticker,
            "direction": direction,
            "entry_price": entry,
            "current_price": current,
            "change": round(change, 2),
            "change_pct": round(pct, 2),
            "on_track": on_track,
            "confidence": float(p.get("confidence", 0)),
            "timeframe": p.get("timeframe"),
            "thesis": p.get("thesis", ""),
            "expires_at": p.get("expires_at"),
            "bid": q.get("bid", 0),
            "ask": q.get("ask", 0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    return results


# ============================================================================
# Prediction Deep Context API
# ============================================================================

@app.get("/api/predictions/context/{prediction_id}")
async def get_prediction_context(prediction_id: str, request: Request, oc_session: str | None = Cookie(None)):
    """Full decision-making context for a prediction: inference chain, catalysts, signals, reflections."""
    _require_auth(request, oc_session)
    prediction_id = _validate_uuid(prediction_id)
    if not SUPABASE_URL:
        return {}

    client = get_http()
    # 1. Get the prediction itself
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/predictions",
        headers=sb_headers(),
        params={"id": f"eq.{prediction_id}"},
    )
    pred = resp.json()[0] if resp.status_code == 200 and resp.json() else None
    if not pred:
        return {}

    ticker = pred["ticker"]
    pred_date = pred["created_at"][:10]

    # 2. Get inference chains for this ticker around prediction date (±1 day)
    chains_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={
            "select": "id,ticker,chain_date,max_depth_reached,final_confidence,final_decision,stopping_reason,tumblers,reasoning_summary,catalyst_event_ids,pattern_template_ids,created_at",
            "ticker": f"eq.{ticker}",
            "chain_date": f"gte.{pred_date}",
            "order": "chain_date.desc",
            "limit": "3",
        },
    )
    chains = chains_resp.json() if chains_resp.status_code == 200 else []

    # 3. Get signal evaluations for this ticker around prediction date
    signals_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/signal_evaluations",
        headers=sb_headers(),
        params={
            "select": "id,ticker,scan_date,scan_type,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision,reasoning,created_at",
            "ticker": f"eq.{ticker}",
            "scan_date": f"gte.{pred_date}",
            "order": "created_at.desc",
            "limit": "3",
        },
    )
    signals = signals_resp.json() if signals_resp.status_code == 200 else []

    # 4. Get catalyst events for this ticker (last 7 days from prediction)
    catalyst_cutoff = (datetime.fromisoformat(pred["created_at"].replace("Z", "+00:00")) - timedelta(days=7)).isoformat()
    catalysts_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/catalyst_events",
        headers=sb_headers(),
        params={
            "select": "id,ticker,catalyst_type,headline,source,source_url,event_time,magnitude,direction,sentiment_score,actual_impact_pct",
            "or": f"(ticker.eq.{ticker},affected_tickers.cs.{{{ticker}}})",
            "event_time": f"gte.{catalyst_cutoff}",
            "order": "event_time.desc",
            "limit": "10",
        },
    )
    catalysts = catalysts_resp.json() if catalysts_resp.status_code == 200 else []

    # 5. Get matched pattern templates if any chain has them
    pattern_ids = set()
    for c in chains:
        for pid in (c.get("pattern_template_ids") or []):
            if pid:
                pattern_ids.add(pid)

    patterns = []
    if pattern_ids:
        # Fetch each pattern (small set, typically 0-3)
        for pid in list(pattern_ids)[:5]:
            pt_resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/pattern_templates",
                headers=sb_headers(),
                params={
                    "select": "id,pattern_name,pattern_description,pattern_category,success_rate,times_matched,avg_return_pct",
                    "id": f"eq.{pid}",
                },
            )
            if pt_resp.status_code == 200 and pt_resp.json():
                patterns.extend(pt_resp.json())

    # 6. Get latest meta reflection that mentions this ticker (or just latest daily)
    ref_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/meta_reflections",
        headers=sb_headers(),
        params={
            "select": "reflection_date,reflection_type,patterns_observed,signal_assessment,counterfactuals",
            "reflection_date": f"gte.{pred_date}",
            "reflection_type": "eq.daily",
            "order": "reflection_date.desc",
            "limit": "1",
        },
    )
    reflections = ref_resp.json() if ref_resp.status_code == 200 else []

    # 7. Get latest calibration
    cal_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/confidence_calibration",
        headers=sb_headers(),
        params={
            "select": "calibration_week,brier_score,overconfidence_bias,active_factors",
            "order": "calibration_week.desc",
            "limit": "1",
        },
    )
    calibration = cal_resp.json()[0] if cal_resp.status_code == 200 and cal_resp.json() else None

    return {
        "prediction": pred,
        "inference_chains": chains,
        "signal_evaluations": signals,
        "catalysts": catalysts,
        "patterns": patterns,
        "reflections": reflections,
        "calibration": calibration,
    }


# ============================================================================
# Inference Engine & Tumbler Architecture API Routes
# ============================================================================

@app.get("/api/inference/depth-distribution")
async def get_inference_depth_distribution(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
):
    """Chain depth stats by day."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={
            "select": "chain_date,max_depth_reached,final_decision,final_confidence,stopping_reason",
            "chain_date": f"gte.{cutoff}",
            "order": "chain_date.asc",
            "limit": "500",
        },
    )
    if resp.status_code != 200:
        return []

    chains = resp.json()
    # Group by date
    by_date: dict = {}
    for c in chains:
        d = c["chain_date"]
        if d not in by_date:
            by_date[d] = {"date": d, "depths": {}, "decisions": {}, "total": 0, "avg_confidence": 0}
        by_date[d]["total"] += 1
        by_date[d]["avg_confidence"] += float(c.get("final_confidence", 0))

        depth = str(c.get("max_depth_reached", 0))
        by_date[d]["depths"][depth] = by_date[d]["depths"].get(depth, 0) + 1

        dec = c.get("final_decision", "skip")
        by_date[d]["decisions"][dec] = by_date[d]["decisions"].get(dec, 0) + 1

    for d in by_date.values():
        if d["total"] > 0:
            d["avg_confidence"] = round(d["avg_confidence"] / d["total"], 3)

    return sorted(by_date.values(), key=lambda x: x["date"])


@app.get("/api/inference/chain/{chain_id}")
async def get_inference_chain_detail(chain_id: str, request: Request, oc_session: str | None = Cookie(None)):
    """Full chain with tumblers."""
    _require_auth(request, oc_session)
    chain_id = _validate_uuid(chain_id)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={"id": f"eq.{chain_id}"},
    )
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return {}


@app.get("/api/calibration/latest")
async def get_calibration_latest(request: Request, oc_session: str | None = Cookie(None)):
    """Latest calibration buckets."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/confidence_calibration",
        headers=sb_headers(),
        params={
            "order": "calibration_week.desc",
            "limit": "1",
        },
    )
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return {}


@app.get("/api/catalysts/recent")
async def get_catalysts_recent(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
    ticker: str = "",
):
    """Catalyst feed."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    params = {
        "select": "id,ticker,catalyst_type,headline,source,event_time,magnitude,direction,sentiment_score,affected_tickers,sector,actual_impact_pct",
        "event_time": f"gte.{cutoff}",
        "order": "event_time.desc",
        "limit": "100",
    }
    if ticker:
        ticker = _validate_ticker(ticker)
        params["ticker"] = f"eq.{ticker}"
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/catalyst_events",
        headers=sb_headers(),
        params=params,
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/catalysts/stats")
async def get_catalyst_stats(request: Request, oc_session: str | None = Cookie(None)):
    """Catalyst type distribution for last 30 days."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/catalyst_events",
        headers=sb_headers(),
        params={
            "select": "catalyst_type,direction,magnitude",
            "event_time": f"gte.{cutoff}",
            "limit": "1000",
        },
    )
    if resp.status_code != 200:
        return {}

    events = resp.json()
    by_type: dict = {}
    by_direction: dict = {}
    by_magnitude: dict = {}

    for e in events:
        ct = e.get("catalyst_type", "other")
        by_type[ct] = by_type.get(ct, 0) + 1
        d = e.get("direction", "neutral")
        by_direction[d] = by_direction.get(d, 0) + 1
        m = e.get("magnitude", "medium")
        by_magnitude[m] = by_magnitude.get(m, 0) + 1

    return {
        "total": len(events),
        "by_type": by_type,
        "by_direction": by_direction,
        "by_magnitude": by_magnitude,
    }


@app.get("/api/patterns/active")
async def get_active_patterns(request: Request, oc_session: str | None = Cookie(None)):
    """Pattern template gallery."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pattern_templates",
        headers=sb_headers(),
        params={
            "select": "id,pattern_name,pattern_description,pattern_category,times_matched,times_correct,success_rate,avg_return_pct,template_confidence,status,last_matched_at,created_at",
            "order": "times_matched.desc",
            "limit": "50",
        },
    )
    return resp.json() if resp.status_code == 200 else []


# ============================================================================
# Congress API Routes
# ============================================================================


@app.get("/api/congress/politicians")
async def get_congress_politicians(
    request: Request, oc_session: str | None = Cookie(None),
):
    """Politician leaderboard sorted by signal score."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/politician_intel",
        headers=sb_headers(),
        params={
            "select": "full_name,chamber,party,state,leadership_role,"
                      "signal_score,trailing_12m_return_pct,"
                      "trailing_12m_vs_spy_pct,sector_expertise,"
                      "tracks_spouse,chronic_late_filer,last_trade_date",
            "order": "signal_score.desc",
            "limit": "50",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/congress/signals")
async def get_congress_signals(
    request: Request, oc_session: str | None = Cookie(None),
):
    """High-signal congressional buys from the last 21 days."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/catalyst_events",
        headers=sb_headers(),
        params={
            "select": "ticker,politician_signal_score,"
                      "disclosure_freshness_score,"
                      "disclosure_days_since_trade,"
                      "in_jurisdiction,filer_type,"
                      "event_time,metadata",
            "catalyst_type": "eq.congressional_trade",
            "direction": "eq.bullish",
            "created_at": f"gte.{cutoff}",
            "politician_signal_score": "gte.0.25",
            "order": "politician_signal_score.desc",
            "limit": "50",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/congress/clusters")
async def get_congress_clusters(
    request: Request, oc_session: str | None = Cookie(None),
):
    """Recent cluster detections (multi-member buys)."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=21)
    ).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/congress_clusters",
        headers=sb_headers(),
        params={
            "select": "ticker,cluster_date,member_count,"
                      "cross_chamber,members,confidence_boost,"
                      "avg_signal_score",
            "cluster_date": f"gte.{cutoff}",
            "order": "confidence_boost.desc",
            "limit": "20",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/congress/calendar")
async def get_congress_calendar(
    request: Request, oc_session: str | None = Cookie(None),
):
    """Upcoming legislative events (next 30 days)."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    today = datetime.now(timezone.utc).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/legislative_calendar",
        headers=sb_headers(),
        params={
            "select": "event_date,event_type,chamber,committee,"
                      "bill_title,affected_sectors,significance",
            "event_date": f"gte.{today}",
            "order": "event_date.asc",
            "limit": "30",
        },
    )
    return resp.json() if resp.status_code == 200 else []


# ============================================================================
# Economics & Budget API Routes
# ============================================================================

@app.get("/api/economics/summary")
async def get_economics_summary(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 30,
):
    """Project P&L summary (costs vs trading profit)."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/cost_ledger",
        headers=sb_headers(),
        params={
            "select": "category,amount",
            "ledger_date": f"gte.{cutoff}",
            "limit": "1000",
        },
    )
    if resp.status_code != 200:
        return {}

    entries = resp.json()
    total_costs = 0.0
    total_pnl = 0.0
    by_category: dict = {}

    for e in entries:
        amt = float(e.get("amount", 0))
        cat = e.get("category", "other")
        by_category[cat] = by_category.get(cat, 0) + amt
        if cat == "trade_pnl":
            total_pnl += amt
        else:
            total_costs += amt

    net = total_pnl + total_costs  # costs are negative
    roi = round(total_pnl / abs(total_costs) * 100, 1) if total_costs != 0 else 0

    return {
        "total_costs": round(abs(total_costs), 2),
        "total_pnl": round(total_pnl, 2),
        "net": round(net, 2),
        "roi_pct": roi,
        "by_category": {k: round(v, 4) for k, v in by_category.items()},
        "days": days,
    }


@app.get("/api/economics/breakdown")
async def get_economics_breakdown(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 30,
):
    """Cost breakdown by category and subcategory."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/cost_ledger",
        headers=sb_headers(),
        params={
            "select": "category,subcategory,amount,ledger_date",
            "ledger_date": f"gte.{cutoff}",
            "order": "ledger_date.desc",
            "limit": "1000",
        },
    )
    if resp.status_code != 200:
        return []

    entries = resp.json()
    # Group by category + subcategory
    breakdown: dict = {}
    for e in entries:
        key = f"{e.get('category', 'other')}|{e.get('subcategory', '')}"
        if key not in breakdown:
            breakdown[key] = {"category": e["category"], "subcategory": e.get("subcategory", ""), "total": 0, "count": 0}
        breakdown[key]["total"] += float(e.get("amount", 0))
        breakdown[key]["count"] += 1

    return sorted(breakdown.values(), key=lambda x: x["total"])


@app.get("/api/economics/history")
async def get_economics_history(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 90,
):
    """Daily P&L time series for chart."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/cost_ledger",
        headers=sb_headers(),
        params={
            "select": "ledger_date,category,amount",
            "ledger_date": f"gte.{cutoff}",
            "order": "ledger_date.asc",
            "limit": "1000",
        },
    )
    if resp.status_code != 200:
        return []

    entries = resp.json()
    by_date: dict = {}
    for e in entries:
        d = e["ledger_date"]
        if d not in by_date:
            by_date[d] = {"date": d, "costs": 0, "pnl": 0}
        amt = float(e.get("amount", 0))
        if e.get("category") == "trade_pnl":
            by_date[d]["pnl"] += amt
        else:
            by_date[d]["costs"] += amt

    # Compute cumulative
    result = sorted(by_date.values(), key=lambda x: x["date"])
    cum_costs = 0
    cum_pnl = 0
    for row in result:
        cum_costs += row["costs"]
        cum_pnl += row["pnl"]
        row["cum_costs"] = round(cum_costs, 2)
        row["cum_pnl"] = round(cum_pnl, 2)
        row["cum_net"] = round(cum_pnl + cum_costs, 2)
        row["costs"] = round(row["costs"], 4)
        row["pnl"] = round(row["pnl"], 4)

    return result


@app.get("/api/budget/config")
async def get_budget_config(request: Request, oc_session: str | None = Cookie(None)):
    """Current budget caps with today's spend."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []

    today = datetime.now(timezone.utc).date().isoformat()
    client = get_http()
    # Get config
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/budget_config",
        headers=sb_headers(),
        params={"order": "config_key.asc", "limit": "50"},
    )
    configs = resp.json() if resp.status_code == 200 else []

    # Get today's spend
    resp2 = await client.get(
        f"{SUPABASE_URL}/rest/v1/cost_ledger",
        headers=sb_headers(),
        params={
            "select": "category,amount",
            "ledger_date": f"eq.{today}",
            "limit": "500",
        },
    )
    today_costs = resp2.json() if resp2.status_code == 200 else []

    spend_by_cat: dict = {}
    for c in today_costs:
        cat = c.get("category", "")
        spend_by_cat[cat] = spend_by_cat.get(cat, 0) + abs(float(c.get("amount", 0)))

    # Enrich configs with today's spend
    for cfg in configs:
        key = cfg.get("config_key", "")
        if "claude" in key:
            cfg["today_spend"] = round(spend_by_cat.get("claude_api", 0), 4)
        elif "perplexity" in key:
            cfg["today_spend"] = round(spend_by_cat.get("perplexity_api", 0), 4)
        else:
            cfg["today_spend"] = 0

    return configs


@app.post("/api/budget/config")
async def update_budget_config(
    request: Request,
    oc_session: str | None = Cookie(None),
):
    """Update a budget cap from UI."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        raise HTTPException(status_code=500, detail="No Supabase connection")

    body = await request.json()
    config_key = body.get("config_key")
    value = body.get("value")

    if not config_key or value is None:
        raise HTTPException(status_code=400, detail="Missing config_key or value")

    if config_key not in ALLOWED_BUDGET_KEYS:
        raise HTTPException(status_code=400, detail="Invalid config key")

    try:
        value = float(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Value must be a number")

    if value < 0 or value > 100:
        raise HTTPException(status_code=400, detail="Value must be between 0 and 100")

    client = get_http()
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/budget_config",
        params={"config_key": f"eq.{config_key}"},
        headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=representation"},
        json={"value": value, "updated_at": datetime.now(timezone.utc).isoformat(), "updated_by": "dashboard_ui"},
    )
    if resp.status_code in (200, 204):
        return {"ok": True}
    raise HTTPException(status_code=resp.status_code, detail="Failed to update")


# ============================================================================
# RAG Status API Routes
# ============================================================================

@app.get("/api/rag/status")
async def get_rag_status(request: Request, oc_session: str | None = Cookie(None)):
    """RAG system health — embedding counts per table."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}

    tables = ["signal_evaluations", "meta_reflections", "catalyst_events", "inference_chains", "pattern_templates", "trade_learnings"]
    result = {}

    client = get_http()
    for table in tables:
        # Get total count
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**sb_headers(), "Prefer": "count=exact"},
            params={"select": "id", "limit": "0"},
        )
        total = int(resp.headers.get("content-range", "0/0").split("/")[-1]) if resp.status_code == 200 else 0

        # Get count with embeddings
        resp2 = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**sb_headers(), "Prefer": "count=exact"},
            params={"select": "id", "embedding": "not.is.null", "limit": "0"},
        )
        with_embedding = int(resp2.headers.get("content-range", "0/0").split("/")[-1]) if resp2.status_code == 200 else 0

        coverage = round(with_embedding / total * 100, 1) if total > 0 else 0
        result[table] = {
            "total": total,
            "with_embedding": with_embedding,
            "coverage_pct": coverage,
        }

    return result


@app.get("/api/rag/coverage")
async def get_rag_coverage(request: Request, oc_session: str | None = Cookie(None)):
    """Embedding coverage per table."""
    _require_auth(request, oc_session)
    return await get_rag_status(request, oc_session)


@app.get("/api/rag/activity")
async def get_rag_activity(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
):
    """Recent RAG queries from pipeline runs."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={
            "select": "pipeline_name,step_name,output_snapshot,duration_ms,started_at",
            "step_name": "like.*rag*",
            "started_at": f"gte.{cutoff}",
            "order": "started_at.desc",
            "limit": "20",
        },
    )
    return resp.json() if resp.status_code == 200 else []


# ============================================================================
# ============================================================================
# SIT-REP: Decision Intelligence Briefing
# ============================================================================

@app.get("/api/sitrep")
async def get_sitrep(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 30,
):
    """Trade decisions enriched with inference chains, catalysts, and signal data."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    client = get_http()
    # Fetch trades, chains, signals, and catalysts in parallel
    trades_resp, chains_resp, signals_resp, catalysts_resp = await asyncio.gather(
        client.get(
            f"{SUPABASE_URL}/rest/v1/trade_decisions",
            headers=sb_headers(),
            params={
                "select": "id,ticker,action,entry_price,exit_price,pnl,outcome,signals_fired,hold_days,reasoning,what_worked,improvement,created_at",
                "created_at": f"gte.{cutoff}",
                "order": "created_at.desc",
                "limit": "50",
            },
        ),
        client.get(
            f"{SUPABASE_URL}/rest/v1/inference_chains",
            headers=sb_headers(),
            params={
                "select": "id,ticker,chain_date,max_depth_reached,final_confidence,final_decision,stopping_reason,tumblers,catalyst_event_ids,reasoning_summary,actual_outcome,actual_pnl,created_at",
                "chain_date": f"gte.{cutoff[:10]}",
                "order": "created_at.desc",
                "limit": "100",
            },
        ),
        client.get(
            f"{SUPABASE_URL}/rest/v1/signal_evaluations",
            headers=sb_headers(),
            params={
                "select": "id,ticker,scan_date,scan_type,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision,reasoning,created_at",
                "scan_date": f"gte.{cutoff[:10]}",
                "order": "created_at.desc",
                "limit": "100",
            },
        ),
        client.get(
            f"{SUPABASE_URL}/rest/v1/catalyst_events",
            headers=sb_headers(),
            params={
                "select": "id,ticker,catalyst_type,headline,direction,magnitude,sentiment_score,event_time",
                "event_time": f"gte.{cutoff}",
                "order": "event_time.desc",
                "limit": "100",
            },
        ),
    )
    trades = trades_resp.json() if trades_resp.status_code == 200 else []
    chains = chains_resp.json() if chains_resp.status_code == 200 else []
    signals = signals_resp.json() if signals_resp.status_code == 200 else []
    catalysts = catalysts_resp.json() if catalysts_resp.status_code == 200 else []

    # Index chains and signals by ticker for matching
    chains_by_ticker: dict = {}
    for c in chains:
        t = c.get("ticker", "")
        if t not in chains_by_ticker:
            chains_by_ticker[t] = []
        chains_by_ticker[t].append(c)

    signals_by_ticker: dict = {}
    for s in signals:
        t = s.get("ticker", "")
        if t not in signals_by_ticker:
            signals_by_ticker[t] = []
        signals_by_ticker[t].append(s)

    catalysts_by_ticker: dict = {}
    for cat in catalysts:
        t = cat.get("ticker", "")
        if t:
            if t not in catalysts_by_ticker:
                catalysts_by_ticker[t] = []
            catalysts_by_ticker[t].append(cat)

    # Build enriched results
    results = []

    # Include trades with their matched chains/signals/catalysts
    for trade in trades:
        ticker = trade.get("ticker", "")
        entry = {
            "type": "trade",
            "trade": trade,
            "chains": chains_by_ticker.get(ticker, [])[:3],
            "signals": signals_by_ticker.get(ticker, [])[:3],
            "catalysts": catalysts_by_ticker.get(ticker, [])[:5],
        }
        results.append(entry)

    # Also include inference chains that didn't result in trades (watch/skip/veto)
    for chain in chains:
        if chain.get("final_decision") in ("watch", "skip", "veto"):
            ticker = chain.get("ticker", "")
            entry = {
                "type": "analysis",
                "chain": chain,
                "signals": signals_by_ticker.get(ticker, [])[:2],
                "catalysts": catalysts_by_ticker.get(ticker, [])[:3],
            }
            results.append(entry)

    return results[:60]


# ============================================================================
# Strategy Profiles
# ============================================================================

@app.get("/api/strategy/profiles")
async def get_strategy_profiles(request: Request, oc_session: str | None = Cookie(None)):
    """All strategy profiles."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers=sb_headers(),
        params={
            "select": "id,profile_name,description,active,annual_target_pct,daily_target_pct,weekly_target_pct,min_signal_score,min_tumbler_depth,min_confidence,max_risk_per_trade_pct,max_concurrent_positions,max_portfolio_risk_pct,position_size_method,trade_style,max_hold_days,circuit_breakers_enabled,self_modify_enabled,self_modify_requires_approval,prefer_high_beta,created_at",
            "order": "created_at.asc",
            "limit": "50",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/strategy/active")
async def get_active_strategy(request: Request, oc_session: str | None = Cookie(None)):
    """Currently active strategy profile."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers=sb_headers(),
        params={
            "select": "id,profile_name,active,annual_target_pct,daily_target_pct",
            "active": "eq.true",
            "limit": "1",
        },
    )
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return {}


@app.post("/api/strategy/activate")
async def activate_strategy(request: Request, oc_session: str | None = Cookie(None)):
    """Switch active strategy profile."""
    _require_auth(request, oc_session)
    body = await request.json()
    profile_id = body.get("id", "")
    profile_id = _validate_uuid(profile_id)

    client = get_http()
    now = datetime.now(timezone.utc).isoformat()
    # Deactivate all FIRST (unique constraint on active=true allows only one)
    await client.patch(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers={**sb_headers(), "Content-Type": "application/json"},
        params={"active": "eq.true"},
        json={"active": False, "updated_at": now},
    )
    # Then activate the selected one
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=representation"},
        params={"id": f"eq.{profile_id}"},
        json={"active": True, "updated_at": now},
    )
    if resp.status_code != 200 or not resp.json():
        raise HTTPException(status_code=500, detail="Failed to activate profile")
    return resp.json()[0]


# ============================================================================
# Latency Monitor
# ============================================================================

@app.get("/api/health/latency")
async def get_latency(request: Request, oc_session: str | None = Cookie(None)):
    """Measure round-trip latency to Alpaca (NYSE data feed)."""
    _require_auth(request, oc_session)
    result = {"nyse_ms": None, "timestamp": datetime.now(timezone.utc).isoformat()}

    # Ping Alpaca's market data endpoint — this is the actual path to NYSE quotes
    try:
        client = get_http()
        start = time.monotonic()
        r = await client.get(
            "https://data.alpaca.markets/v2/stocks/SPY/quotes/latest",
            headers={
                "APCA-API-KEY-ID": ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
        )
        elapsed = (time.monotonic() - start) * 1000
        if r.status_code == 200:
            result["nyse_ms"] = round(elapsed)
    except Exception:
        pass

    return result


# ============================================================================
# Real Stack Health Checks
# ============================================================================

@app.get("/api/health/stack")
async def get_stack_health(request: Request, oc_session: str | None = Cookie(None)):
    """Real health checks against every service in the tech stack."""
    _require_auth(request, oc_session)

    client = get_http()

    async def _check_supabase():
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/budget_config",
                headers=sb_headers(),
                params={"select": "id", "limit": "1"},
            )
            return r.status_code == 200
        except Exception:
            return False

    async def _check_alpaca():
        try:
            r = await client.get(
                f"{ALPACA_BASE}/v2/account",
                headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            )
            return r.status_code == 200
        except Exception:
            return False

    async def _check_ollama():
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/stack_heartbeats",
                headers=sb_headers(),
                params={"service": "eq.ollama", "select": "last_seen", "limit": "1"},
            )
            if r.status_code == 200 and r.json():
                last_seen = r.json()[0].get("last_seen", "")
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
                return last_seen > cutoff
            return False
        except Exception:
            return False

    async def _check_finnhub():
        try:
            if FINNHUB_KEY:
                r = await client.get(
                    "https://finnhub.io/api/v1/quote",
                    params={"symbol": "AAPL", "token": FINNHUB_KEY},
                )
                return r.status_code == 200 and r.json().get("c", 0) > 0
            return False
        except Exception:
            return False

    async def _check_sentry():
        try:
            if SENTRY_AUTH_TOKEN:
                r = await client.get(
                    f"https://sentry.io/api/0/projects/{SENTRY_ORG}/{SENTRY_PROJECT}/",
                    headers={"Authorization": f"Bearer {SENTRY_AUTH_TOKEN}"},
                )
                return r.status_code == 200
            return False
        except Exception:
            return False

    async def _check_pgvector():
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/signal_evaluations",
                headers={**sb_headers(), "Prefer": "count=exact"},
                params={"select": "id", "embedding": "not.is.null", "limit": "0"},
            )
            return r.status_code == 200
        except Exception:
            return False

    async def _check_tumbler():
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/stack_heartbeats",
                headers=sb_headers(),
                params={"service": "eq.tumbler", "select": "last_seen", "limit": "1"},
            )
            if r.status_code == 200 and r.json():
                last_seen = r.json()[0].get("last_seen", "")
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
                return last_seen > cutoff
            return False
        except Exception:
            return False

    async def _check_claude():
        try:
            if ANTHROPIC_API_KEY:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={"model": "claude-sonnet-4-6", "max_tokens": 1, "messages": []},
                )
                return r.status_code in (200, 400, 429, 529)
            return False
        except Exception:
            return False

    (
        supabase_ok, alpaca_ok, ollama_ok, finnhub_ok,
        sentry_ok, pgvector_ok, tumbler_ok, claude_ok,
    ) = await asyncio.gather(
        _check_supabase(),
        _check_alpaca(),
        _check_ollama(),
        _check_finnhub(),
        _check_sentry(),
        _check_pgvector(),
        _check_tumbler(),
        _check_claude(),
    )

    return {
        "supabase": supabase_ok,
        "alpaca": alpaca_ok,
        "ollama": ollama_ok,
        "finnhub": finnhub_ok,
        "sentry": sentry_ok,
        "pgvector": pgvector_ok,
        "tumbler": tumbler_ok,
        "claude": claude_ok,
    }


# ============================================================================
# Tuning System API Routes
# ============================================================================

@app.get("/api/tuning/profiles")
async def get_tuning_profiles(request: Request, oc_session: str | None = Cookie(None)):
    """All tuning profiles with performance summary."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/tuning_profile_performance",
        headers=sb_headers(),
        params={"order": "version.desc", "limit": "50"},
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/tuning/active")
async def get_active_tuning_profile(request: Request, oc_session: str | None = Cookie(None)):
    """Currently active tuning profile."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/tuning_profiles",
        headers=sb_headers(),
        params={
            "or": "(status.eq.active,status.eq.testing)",
            "order": "status.asc",
            "limit": "1",
        },
    )
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return {}


@app.get("/api/tuning/telemetry")
async def get_tuning_telemetry(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
    profile_id: str = "",
):
    """Recent telemetry data, optionally filtered by profile."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    params = {
        "select": "pipeline_name,wall_clock_ms,ram_peak_mb,avg_gpu_pct,gpu_temp_max_c,ollama_avg_tokens_per_sec,embedding_avg_ms,embedding_count,claude_call_count,step_count,thermal_throttle_events,power_draw_avg_watts,created_at",
        "created_at": f"gte.{cutoff}",
        "order": "created_at.desc",
        "limit": "200",
    }
    if profile_id:
        profile_id = _validate_uuid(profile_id)
        params["tuning_profile_id"] = f"eq.{profile_id}"

    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/tuning_telemetry",
        headers=sb_headers(),
        params=params,
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/tuning/compare")
async def compare_tuning_profiles(
    request: Request,
    oc_session: str | None = Cookie(None),
):
    """Side-by-side profile comparison from the view."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/tuning_profile_performance",
        headers=sb_headers(),
        params={"total_runs": "gt.0", "order": "version.desc"},
    )
    return resp.json() if resp.status_code == 200 else []


# ============================================================================
# Trade Learnings API Routes
# ============================================================================

@app.get("/api/trade-learnings")
async def get_trade_learnings(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 60,
    ticker: str = "",
    outcome: str = "",
):
    """Recent trade post-mortems from the RAG learning pipeline."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    params = {
        "select": "id,ticker,trade_date,entry_price,exit_price,pnl,pnl_pct,outcome,hold_days,"
                  "expected_direction,expected_confidence,actual_direction,actual_move_pct,"
                  "expectation_accuracy,catalyst_match,key_variance,what_worked,what_failed,"
                  "key_lesson,tumbler_depth,inference_chain_id,created_at",
        "trade_date": f"gte.{cutoff}",
        "order": "trade_date.desc",
        "limit": "50",
    }
    if ticker:
        ticker = _validate_ticker(ticker)
        params["ticker"] = f"eq.{ticker}"
    if outcome:
        params["outcome"] = f"eq.{outcome}"
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_learnings",
        headers=sb_headers(),
        params=params,
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/trade-learnings/stats")
async def get_trade_learnings_stats(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 60,
):
    """Aggregated stats from post-mortem analysis."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_learnings",
        headers=sb_headers(),
        params={
            "select": "outcome,pnl_pct,expectation_accuracy,tumbler_depth,expected_confidence",
            "trade_date": f"gte.{cutoff}",
            "limit": "500",
        },
    )
    if resp.status_code != 200:
        return {}

    rows = resp.json()
    if not rows:
        return {"total": 0}

    outcomes: dict = {}
    accuracy_counts: dict = {}
    total_pnl = 0.0
    depth_by_outcome: dict = {}

    for r in rows:
        o = r.get("outcome", "SCRATCH")
        outcomes[o] = outcomes.get(o, 0) + 1
        a = r.get("expectation_accuracy", "missed")
        accuracy_counts[a] = accuracy_counts.get(a, 0) + 1
        total_pnl += float(r.get("pnl_pct", 0) or 0)

        depth = str(r.get("tumbler_depth", 0) or 0)
        if depth not in depth_by_outcome:
            depth_by_outcome[depth] = {"wins": 0, "losses": 0, "total": 0}
        depth_by_outcome[depth]["total"] += 1
        if o in ("STRONG_WIN", "WIN"):
            depth_by_outcome[depth]["wins"] += 1
        elif o in ("LOSS", "STRONG_LOSS"):
            depth_by_outcome[depth]["losses"] += 1

    total = len(rows)
    wins = outcomes.get("STRONG_WIN", 0) + outcomes.get("WIN", 0)
    losses = outcomes.get("LOSS", 0) + outcomes.get("STRONG_LOSS", 0)

    # Expectation calibration: % of trades where direction was met or exceeded
    well_called = accuracy_counts.get("met", 0) + accuracy_counts.get("exceeded", 0)

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "scratches": outcomes.get("SCRATCH", 0),
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "avg_pnl_pct": round(total_pnl / total, 2) if total else 0,
        "expectation_accuracy_pct": round(well_called / total * 100, 1) if total else 0,
        "outcomes": outcomes,
        "accuracy_distribution": accuracy_counts,
        "depth_performance": depth_by_outcome,
    }


@app.get("/api/trade-learnings/{learning_id}")
async def get_trade_learning_detail(
    learning_id: str,
    request: Request,
    oc_session: str | None = Cookie(None),
):
    """Full trade learning record including market context and catalysts."""
    _require_auth(request, oc_session)
    learning_id = _validate_uuid(learning_id)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_learnings",
        headers=sb_headers(),
        params={"id": f"eq.{learning_id}"},
    )
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return {}


# ============================================================================
# AI Chat — Claude with full trading context via tool use
# ============================================================================

CHAT_TOOLS = [
    {
        "name": "get_account",
        "description": "Get current Alpaca account: equity, cash, buying power, portfolio value, paper/live status.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_positions",
        "description": "Get all open positions with entry price, current price, unrealized P&L, and quantity.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_trades",
        "description": "Get recent trade decisions with entry/exit prices, P&L, outcome, signals, reasoning.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max trades to return (default 20)"}},
            "required": [],
        },
    },
    {
        "name": "get_inference_chains",
        "description": "Get tumbler-by-tumbler inference chains: depth reached, confidence, decision (enter/watch/skip/veto), stopping reason, reasoning summary. This is how the system decides whether to trade.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Filter by ticker symbol (e.g. AAPL)"},
                "days": {"type": "integer", "description": "Lookback days (default 7)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_signal_evaluations",
        "description": "Get per-ticker signal scores: trend, momentum, volume, fundamental, sentiment, flow — each scored 0 or 1, with total score and reasoning.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Filter by ticker"},
                "days": {"type": "integer", "description": "Lookback days (default 7)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_catalysts",
        "description": "Get recent catalyst events: market-moving news with ticker, type, headline, direction (bullish/bearish), magnitude, sentiment score.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Filter by ticker"},
                "days": {"type": "integer", "description": "Lookback days (default 7)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_meta_reflections",
        "description": "Get daily/weekly meta-analysis reflections: AI-generated strategy reviews with patterns observed, pipeline health, adjustments proposed.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Lookback days (default 14)"}},
            "required": [],
        },
    },
    {
        "name": "get_trade_learnings",
        "description": "Get post-trade analysis (post-mortems): what worked, what failed, key lessons, tumbler depth, expectation accuracy, catalyst match analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Filter by ticker"},
                "days": {"type": "integer", "description": "Lookback days (default 60)"},
                "outcome": {"type": "string", "description": "Filter by outcome: WIN, STRONG_WIN, LOSS, STRONG_LOSS, SCRATCH"},
            },
            "required": [],
        },
    },
    {
        "name": "get_economics",
        "description": "Get economics summary: trading P&L, API costs (Claude, Perplexity), budget usage, cost breakdown by category.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Lookback days (default 30)"}},
            "required": [],
        },
    },
    {
        "name": "get_pipeline_health",
        "description": "Get pipeline health: success rate, recent run status per pipeline (scanner, catalyst_ingest, position_manager, etc), failure details.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_regime",
        "description": "Get current market regime (UP_LOWVOL, UP_HIGHVOL, DOWN_ANY, SIDEWAYS) and recent regime history.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_calibration",
        "description": "Get confidence calibration: Brier score, overconfidence bias, stated vs actual confidence buckets.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_strategy_profiles",
        "description": "Get all strategy profiles (CONSERVATIVE, UNLEASHED, etc) with parameters: min confidence, max risk, position sizing, trade style, hold days.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_sitrep",
        "description": "Get the full decision intelligence briefing: trades enriched with inference chains, signals, and catalysts. Best for comprehensive analysis.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Lookback days (default 30)"}},
            "required": [],
        },
    },
]


async def _chat_tool_dispatch(name: str, input_data: dict) -> str:
    """Execute a chat tool and return JSON string result."""
    client = get_http()
    try:
        if name == "get_account":
            resp = await client.get(
                f"{ALPACA_BASE}/v2/account",
                headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            )
            if resp.status_code == 200:
                d = resp.json()
                return json.dumps({
                    "equity": d.get("equity"), "cash": d.get("cash"),
                    "buying_power": d.get("buying_power"), "portfolio_value": d.get("portfolio_value"),
                    "status": d.get("status"), "paper": d.get("account_number", "").startswith("PA"),
                })
            return json.dumps({"error": f"Alpaca {resp.status_code}"})

        if name == "get_positions":
            resp = await client.get(
                f"{ALPACA_BASE}/v2/positions",
                headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            )
            if resp.status_code == 200:
                positions = []
                for p in resp.json():
                    positions.append({
                        "symbol": p.get("symbol"), "qty": p.get("qty"),
                        "avg_entry": p.get("avg_entry_price"), "current_price": p.get("current_price"),
                        "unrealized_pl": p.get("unrealized_pl"),
                        "unrealized_plpc": round(float(p.get("unrealized_plpc", 0)) * 100, 2),
                        "side": p.get("side"),
                    })
                return json.dumps(positions)
            return json.dumps([])

        if name == "get_trades":
            limit = min(input_data.get("limit", 20), 50)
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/trade_decisions", headers=sb_headers(),
                params={
                    "select": "id,ticker,action,entry_price,exit_price,pnl,outcome,signals_fired,hold_days,reasoning,what_worked,improvement,created_at",
                    "order": "created_at.desc", "limit": str(limit),
                },
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_inference_chains":
            days = clamp_days(input_data.get("days", 7), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            params: dict = {
                "select": "id,ticker,chain_date,max_depth_reached,final_confidence,final_decision,stopping_reason,tumblers,reasoning_summary,actual_outcome,actual_pnl,created_at",
                "chain_date": f"gte.{cutoff}", "order": "created_at.desc", "limit": "50",
            }
            ticker = input_data.get("ticker", "")
            if ticker:
                params["ticker"] = f"eq.{ticker.upper()}"
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/inference_chains", headers=sb_headers(), params=params,
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_signal_evaluations":
            days = clamp_days(input_data.get("days", 7), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            params = {
                "select": "id,ticker,scan_date,scan_type,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision,reasoning,created_at",
                "scan_date": f"gte.{cutoff}", "order": "created_at.desc", "limit": "50",
            }
            ticker = input_data.get("ticker", "")
            if ticker:
                params["ticker"] = f"eq.{ticker.upper()}"
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/signal_evaluations", headers=sb_headers(), params=params,
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_catalysts":
            days = clamp_days(input_data.get("days", 7), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            params = {
                "select": "id,ticker,catalyst_type,headline,direction,magnitude,sentiment_score,event_time",
                "event_time": f"gte.{cutoff}", "order": "event_time.desc", "limit": "50",
            }
            ticker = input_data.get("ticker", "")
            if ticker:
                params["ticker"] = f"eq.{ticker.upper()}"
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/catalyst_events", headers=sb_headers(), params=params,
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_meta_reflections":
            days = clamp_days(input_data.get("days", 14), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/meta_reflections", headers=sb_headers(),
                params={
                    "select": "id,reflection_type,reflection_date,patterns_observed,pipeline_health_score,adjustments_proposed,trade_count,win_rate,created_at",
                    "reflection_date": f"gte.{cutoff}", "order": "reflection_date.desc", "limit": "20",
                },
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_trade_learnings":
            days = clamp_days(input_data.get("days", 60), 180)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            params = {
                "select": "id,ticker,trade_date,entry_price,exit_price,pnl,pnl_pct,outcome,hold_days,"
                          "expected_direction,expected_confidence,actual_direction,actual_move_pct,"
                          "expectation_accuracy,catalyst_match,key_variance,what_worked,what_failed,"
                          "key_lesson,tumbler_depth,created_at",
                "trade_date": f"gte.{cutoff}", "order": "trade_date.desc", "limit": "50",
            }
            ticker = input_data.get("ticker", "")
            if ticker:
                params["ticker"] = f"eq.{ticker.upper()}"
            outcome = input_data.get("outcome", "")
            if outcome:
                params["outcome"] = f"eq.{outcome}"
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/trade_learnings", headers=sb_headers(), params=params,
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_economics":
            days = clamp_days(input_data.get("days", 30), 365)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/cost_ledger", headers=sb_headers(),
                params={
                    "select": "cost_type,source,amount,currency,description,created_at",
                    "created_at": f"gte.{cutoff}T00:00:00Z", "order": "created_at.desc", "limit": "100",
                },
            )
            rows = resp.json() if resp.status_code == 200 else []
            summary: dict = {}
            total = 0.0
            for r in rows:
                ct = r.get("cost_type", "other")
                amt = float(r.get("amount", 0))
                summary[ct] = summary.get(ct, 0.0) + amt
                total += amt
            return json.dumps({"total": round(total, 2), "by_type": {k: round(v, 2) for k, v in summary.items()}, "recent": rows[:20]})

        if name == "get_pipeline_health":
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/pipeline_runs", headers=sb_headers(),
                params={
                    "select": "pipeline_name,status,error_message,started_at,completed_at",
                    "order": "started_at.desc", "limit": "30",
                },
            )
            rows = resp.json() if resp.status_code == 200 else []
            by_pipeline: dict = {}
            for r in rows:
                name_ = r.get("pipeline_name", "unknown")
                if name_ not in by_pipeline:
                    by_pipeline[name_] = {"total": 0, "ok": 0, "failed": 0, "last_status": r.get("status"), "last_error": r.get("error_message")}
                by_pipeline[name_]["total"] += 1
                if r.get("status") == "completed":
                    by_pipeline[name_]["ok"] += 1
                elif r.get("status") == "failed":
                    by_pipeline[name_]["failed"] += 1
            return json.dumps(by_pipeline)

        if name == "get_regime":
            regime_file = Path.home() / ".openclaw/workspace/memory/regime-current.json"
            current = json.loads(regime_file.read_text()) if regime_file.exists() else {"regime": "UNKNOWN"}
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/regime_log", headers=sb_headers(),
                params={"order": "logged_at.desc", "limit": "10"},
            )
            history = resp.json() if resp.status_code == 200 else []
            return json.dumps({"current": current, "history": history})

        if name == "get_calibration":
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/confidence_calibration", headers=sb_headers(),
                params={"order": "week_start.desc", "limit": "8"},
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_strategy_profiles":
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/strategy_profiles", headers=sb_headers(),
                params={
                    "select": "id,profile_name,description,active,min_signal_score,min_tumbler_depth,min_confidence,max_risk_per_trade_pct,max_concurrent_positions,position_size_method,trade_style,max_hold_days,circuit_breakers_enabled,created_at",
                    "order": "created_at.asc",
                },
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_sitrep":
            days = clamp_days(input_data.get("days", 30), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            trades_resp, chains_resp = await asyncio.gather(
                client.get(
                    f"{SUPABASE_URL}/rest/v1/trade_decisions", headers=sb_headers(),
                    params={"select": "id,ticker,action,entry_price,exit_price,pnl,outcome,signals_fired,hold_days,reasoning,created_at", "created_at": f"gte.{cutoff}", "order": "created_at.desc", "limit": "30"},
                ),
                client.get(
                    f"{SUPABASE_URL}/rest/v1/inference_chains", headers=sb_headers(),
                    params={"select": "id,ticker,chain_date,max_depth_reached,final_confidence,final_decision,stopping_reason,reasoning_summary,created_at", "chain_date": f"gte.{cutoff[:10]}", "order": "created_at.desc", "limit": "50"},
                ),
            )
            return json.dumps({
                "trades": trades_resp.json() if trades_resp.status_code == 200 else [],
                "chains": chains_resp.json() if chains_resp.status_code == 200 else [],
            })

        return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


WORKFLOW_CONTEXT: dict[int, dict[str, str]] = {
    1: {
        "title": "HEALTH CHECK",
        "group": "pre-market",
        "description": "59 automated checks across 13 groups validating every integration point in the system. Runs at 5:00 AM PDT before anything else fires.",
        "data_in": "Supabase tables, Ollama API, Alpaca API, crontab, file system",
        "data_out": "system_health table rows (one per check, grouped by run_id)",
        "db_table": "system_health",
        "cost": "Free (one cheap Claude Haiku call for API canary)",
        "parameters": "Check groups: infrastructure, database, crons, signals, tumblers, ensemble, logging, dashboard, claude_api, crontab_drift, output_quality, data_freshness, historical_regression",
        "limitations": "Runs on ridley so it can test local services, but can't test the Fly.io dashboard deployment. Dashboard API checks (I-group) require the local dashboard to be running.",
        "improvements": "Could add a Fly.io health check that tests the production deployment. Could add network latency checks to external APIs.",
        "connections": "Runs before all other pipelines. If health check fails, Slack alert fires. Results visible in the dashboard Health tab and preflight simulator.",
    },
    2: {
        "title": "CATALYST INGEST",
        "group": "pre-market",
        "description": "5-source market catalyst detection. Fetches news, filings, congressional trades, price signals, and macro indicators. Embeds each event via Ollama for RAG retrieval in later tumblers.",
        "data_in": "Finnhub API (news + insiders), SEC EDGAR (filings), QuiverQuant (congressional trades), yfinance (price signals), FRED (macro indicators)",
        "data_out": "catalyst_events rows with embeddings, congress_clusters",
        "db_table": "catalyst_events",
        "cost": "Free (all sources are free-tier APIs, Ollama embedding is local)",
        "parameters": "Lookback hours: 8 (configurable). Watchlist: active profile tickers + recent signal_evaluations tickers. Duplicate detection: cosine similarity threshold 0.95.",
        "limitations": "QuiverQuant has been returning 0 events consistently — may be rate-limited or the API changed. SEC EDGAR returns matched=0 for most runs. Perplexity was removed to save cost.",
        "improvements": "Could add alternative congressional data sources. Could add earnings calendar integration. Could weight catalyst freshness more aggressively.",
        "connections": "Feeds T2 tumbler (catalyst boost). Must complete before scanner runs. 3x daily schedule (5:30, 9:00, 12:50 PDT).",
    },
    3: {
        "title": "FORM 4 INGEST",
        "group": "pre-market",
        "description": "SEC EDGAR Form 4 insider purchase filings. Scores by total value, ownership change, cluster count (multiple insiders buying same week), and filer title (CEO/CFO = strongest).",
        "data_in": "SEC EDGAR EFTS API, target tickers from active profile + AI infrastructure watchlist",
        "data_out": "form4_signals rows with score in raw_data",
        "db_table": "form4_signals",
        "cost": "Free (SEC EDGAR is public)",
        "parameters": "Scoring: total_value ($1M+=3, $500K+=2, $100K+=1), ownership_pct_change (>10%=3, >5%=2, >1%=1), cluster_count (per additional buyer: +2, max +4), filer_title (CEO/CFO/Chairman=+2, VP/Director=+1)",
        "limitations": "SEC EDGAR has rate limits (10 requests/sec with User-Agent). Filing delay: 2 business days from trade date. Sales are filtered out (only purchases).",
        "improvements": "Could track filing patterns over time (insiders who file fast = higher conviction). Could correlate with earnings dates.",
        "connections": "Feeds scanner enrichment (_enrich_with_form4). Scanner adds form4_insider_score and form4_purchase_count to each candidate's signals dict.",
    },
    4: {
        "title": "SCANNER SETUP",
        "group": "scanner",
        "description": "Loads the active trading profile (CONGRESS_MIRROR), checks circuit breakers (VIX, drawdown, consecutive losses), verifies Alpaca account equity and buying power, builds the 39-ticker watchlist.",
        "data_in": "strategy_profiles table, Alpaca account API, market clock",
        "data_out": "Profile config, equity/buying_power values, circuit breaker status",
        "db_table": "pipeline_runs",
        "cost": "Free",
        "parameters": "Circuit breakers: VIX > 28 (stand down), drawdown > 15%, 3 consecutive losses. Max concurrent positions: 3. Max risk per trade: 5%.",
        "limitations": "Watchlist is semi-static (39 tickers). Doesn't dynamically discover new tickers based on momentum or catalysts.",
        "improvements": "Dynamic watchlist expansion based on catalyst volume. Sector rotation detection to shift focus.",
        "connections": "Gates the entire scanner pipeline. If circuit breaker trips, scanner exits without scanning.",
    },
    5: {
        "title": "T1 — SIGNAL SCORING",
        "group": "scanner",
        "description": "39 watchlist tickers scored against 6 binary signals: Trend (SMA20 > SMA50), Momentum (RSI 30-70 + MACD), Volume (relative volume > 1.2), Catalyst (recent catalyst_events), Sentiment (positive catalyst sentiment), Flow (institutional flow indicators). Score 0-6, only tickers >= 3 advance.",
        "data_in": "Alpaca price bars (60 days), SPY bars for benchmark, catalyst_events for sentiment",
        "data_out": "signal_evaluations rows, candidate list with scores and signal details",
        "db_table": "signal_evaluations",
        "cost": "Free (pure computation, no API calls)",
        "parameters": "Min signal score: 3 (from active profile). SMA periods: 20, 50. RSI period: 14. Volume lookback: 20 days.",
        "limitations": "Binary signals (pass/fail) lose nuance. A ticker at RSI 31 scores the same as RSI 60. No weighting between signals.",
        "improvements": "Continuous signal scoring instead of binary. Signal weighting based on historical predictive power. Sector-relative scoring.",
        "connections": "First tumbler in the chain. Output feeds enrichment and T2. Writes to signal_evaluations for dashboard display.",
    },
    6: {
        "title": "SIGNAL ENRICHMENT",
        "group": "scanner",
        "description": "Candidates enriched with options flow data (3-day lookback, bullish/bearish/net from options_flow_signals) and Form 4 insider purchases (14-day lookback, score + count from form4_signals).",
        "data_in": "options_flow_signals table, form4_signals table, candidate list from T1",
        "data_out": "Candidates with additional signal keys: options_flow_bullish, options_flow_bearish, options_flow_net, form4_insider_score, form4_purchase_count",
        "db_table": "signal_evaluations",
        "cost": "Free (Supabase queries only)",
        "parameters": "Options flow lookback: 3 days. Form 4 lookback: 14 days. Options flow: count bullish/bearish sentiment. Form 4: tiered scoring by value + cluster.",
        "limitations": "Options flow data is currently empty (Unusual Whales API not connected). Form 4 data depends on ingest_signals.py running correctly.",
        "improvements": "Connect Unusual Whales API for live options flow. Add dark pool print detection. Correlate form4 timing with earnings.",
        "connections": "Runs after T1, before T2. Adds data that T2/T3 can reference in their analysis.",
    },
    7: {
        "title": "T2 — FUNDAMENTAL ANALYSIS",
        "group": "tumbler",
        "description": "RAG-powered fundamental analysis. Retrieves similar past inference chains and trade learnings via pgvector similarity search. Congressional trade disclosures get confidence boost if filed within 40 days. Hard veto if sentiment score < -0.5.",
        "data_in": "Candidate signals, pgvector embeddings from inference_chains and trade_learnings, catalyst_events, congress_clusters",
        "data_out": "Updated confidence (+-sentiment_adj), veto flag, catalyst_bonus, congress_boost",
        "db_table": "inference_chains",
        "cost": "Free (RAG retrieval only, no LLM call)",
        "parameters": "Sentiment adjustment: avg_sentiment * 0.15. Catalyst bonus: +-0.05. Congress boost: +-0.07 (if high-impact legislative event < 14 days). Veto threshold: sentiment < -0.5.",
        "limitations": "RAG quality depends on historical data volume. New tickers with no history get no RAG context. Congress data depends on QuiverQuant (currently returning 0).",
        "improvements": "Add earnings surprise correlation. Weight RAG results by recency. Add sector-level sentiment aggregation.",
        "connections": "Second tumbler. Receives confidence from T1. Can veto (kills the chain). Output feeds T3. Shadow context injected here for shadow profiles.",
    },
    8: {
        "title": "T3 — FLOW & CROSS-ASSET",
        "group": "tumbler",
        "description": "First LLM call — Ollama qwen2.5:3b running locally on ridley's Jetson GPU. Analyzes how this setup compares to past chains and outcomes. Shadow profile system prompts are injected here for adversarial analysis.",
        "data_in": "Candidate data, T1+T2 results, RAG context (past chains + trade learnings), shadow system prompt (if shadow profile)",
        "data_out": "Confidence adjustment +-0.10, qwen analysis text",
        "db_table": "inference_chains",
        "cost": "Free (local Ollama, no API cost)",
        "parameters": "Model: qwen2.5:3b. Temperature: 0.3. Max tokens (num_predict): 512. Adjustment range: +-0.10.",
        "limitations": "qwen2.5:3b is a 3B parameter model — limited reasoning depth compared to Claude. Can't do complex multi-step analysis. Response quality varies.",
        "improvements": "Could upgrade to qwen2.5:7b if RAM allows (would need to unload during Kronos runs). Could add structured output parsing for more reliable adjustments.",
        "connections": "Third tumbler. REGIME_WATCHER stops here (max_tumbler_depth=3). This is where shadow profiles diverge from live — the adversarial system prompts change qwen's analysis.",
    },
    9: {
        "title": "T4 — PATTERN MATCH",
        "group": "tumbler",
        "description": "Claude Haiku matches the current setup against known pattern templates with documented outcomes. Evaluates quality and coherence of the full trade thesis built by T1-T3.",
        "data_in": "Full tumbler chain context (T1-T3 results), pattern_templates from Supabase (with similarity matching)",
        "data_out": "Confidence adjustment +-0.10, matched pattern IDs, thesis quality assessment",
        "db_table": "inference_chains",
        "cost": "~$0.001 per call (Claude Haiku)",
        "parameters": "Model: claude-haiku-4-5-20251001. Max tokens: 256. Temperature: 0.3. Pattern match threshold: similarity >= 0.5.",
        "limitations": "Pattern template library is small — only patterns discovered by the weekly calibrator. Haiku has limited context window for complex pattern matching.",
        "improvements": "Could pre-compute pattern embeddings for faster matching. Could use a larger pattern library from historical backtesting. Could let the meta-learner create patterns from unanimous dissent events.",
        "connections": "Fourth tumbler. Only fires when Claude budget allows (budget gate). Shadow profiles get their adversarial prompts injected here too.",
    },
    10: {
        "title": "T5 — COUNTERFACTUAL",
        "group": "tumbler",
        "description": "Claude Sonnet as devil's advocate — constructs the strongest argument AGAINST the trade. Asymmetric adjustment: can drop confidence by 0.15 but raise it only 0.05. Applies calibration factor from weekly calibrator.",
        "data_in": "Full chain context (T1-T4), meta_reflections (RAG), trade_learnings (losses/misses)",
        "data_out": "Final calibrated confidence, risk factors, counterfactual analysis",
        "db_table": "inference_chains",
        "cost": "~$0.005 per call (Claude Sonnet)",
        "parameters": "Model: claude-sonnet-4-6-20250514. Max tokens: 512. Temperature: 0.3. Adjustment range: -0.15 to +0.05 (asymmetric bearish bias). Calibration factor applied after raw adjustment.",
        "limitations": "Most expensive tumbler. Budget gate may skip this at low budget. The asymmetric adjustment means T5 is structurally bearish — by design, but limits bullish conviction.",
        "improvements": "Could A/B test symmetric vs asymmetric adjustment. Could use Haiku for a cheaper counterfactual with less depth. Could add market regime awareness to the counterfactual prompt.",
        "connections": "Fifth and final tumbler. If all 5 complete: stopping_reason = 'all_tumblers_clear'. Calibration factor from calibrator.py applied to raw confidence. Final decision: strong_enter (>=0.75), enter (>=0.60), watch (>=0.45), skip (>=0.20), veto (<0.20).",
    },
    11: {
        "title": "EXECUTION GATE",
        "group": "execution",
        "description": "Trade executes ONLY if: decision = enter/strong_enter, confidence >= 0.60, and >= 3 tumblers completed. ATR-based position sizing with 5% max risk per trade. Market buy + stop-loss order.",
        "data_in": "Final inference result (decision, confidence, stopping_reason), Alpaca account (equity, buying_power, positions)",
        "data_out": "trade_decisions row, order_events rows (market buy + stop-loss)",
        "db_table": "trade_decisions",
        "cost": "Free (Alpaca paper trading)",
        "parameters": "Min confidence: 0.60. Min tumbler depth: 3. Max risk: 5% of equity. Position sizing: ATR-based (14-period ATR x 2 for stop distance). Max concurrent positions: 3.",
        "limitations": "Paper trading only — no real money at risk. Alpaca paper trading doesn't perfectly simulate real market conditions (fills are instant, no slippage).",
        "improvements": "Could implement limit orders instead of market orders. Could add time-of-day execution preferences. Could implement partial position sizing based on confidence level.",
        "connections": "End of the live inference chain. Only fires for the live profile (CONGRESS_MIRROR), never for shadow profiles. Writes to trade_decisions and order_events.",
    },
    12: {
        "title": "BUDGET GATE",
        "group": "shadow",
        "description": "Controls which shadow agents run based on remaining Claude API budget. Three tiers: >= 40% = all 6 shadows, 20-40% = cheap shadows only (Regime Watcher + Form 4 + Kronos), < 20% = Kronos only (zero API cost).",
        "data_in": "cost_ledger (today's Claude spend), budget_config (daily_claude_budget)",
        "data_out": "Filtered list of shadow profiles to run",
        "db_table": "cost_ledger",
        "cost": "Free",
        "parameters": "Tier 1 threshold: 40%. Tier 2 threshold: 20%. Cheap profiles: REGIME_WATCHER (Ollama only, stops at T3), FORM4_INSIDER, KRONOS_TECHNICALS (local GPU, zero API cost).",
        "limitations": "Binary tier system — doesn't partially reduce shadow depth. Could run all shadows but cap at T3 for budget savings.",
        "improvements": "Continuous budget allocation instead of tiers. Priority queue — run highest-DWM-weight shadows first. Adaptive tier thresholds based on time of day.",
        "connections": "Gates the shadow inference loop. Determines which of the 6 shadow profiles actually execute.",
    },
    13: {
        "title": "SKEPTIC",
        "group": "shadow",
        "description": "Maximally conservative adversarial reviewer. Requires overwhelming evidence to approve entry. Heavily penalizes momentum-chasing. If a stock moved > 3% in 3 days, demands additional justification.",
        "data_in": "Same candidates as live profile, full tumbler chain with SKEPTIC system prompt injected",
        "data_out": "shadow_divergences row if decision differs from live",
        "db_table": "shadow_divergences",
        "cost": "Claude API (runs full T1-T5 chain)",
        "parameters": "Grading metric: Conditional Brier Score. System prompt: immutable, never modified by meta-learner. Full tumbler depth (5).",
        "limitations": "Structurally bearish — almost always says skip. High dissent rate may dilute signal quality.",
        "improvements": "Could add a confidence-weighted dissent (strong skip vs weak skip). Could track which specific tumblers cause SKEPTIC to diverge.",
        "connections": "Runs after live inference. Divergences recorded for weekly calibrator grading. DWM weight determines how much the meta-learner listens to SKEPTIC's dissent.",
    },
    14: {
        "title": "CONTRARIAN",
        "group": "shadow",
        "description": "Assumes the trade is wrong. Overweights sector rotation signals, institutional distribution (volume without price progress), and divergence between price and fundamentals.",
        "data_in": "Same candidates, CONTRARIAN system prompt injected into tumblers",
        "data_out": "shadow_divergences row on disagreement",
        "db_table": "shadow_divergences",
        "cost": "Claude API (full T1-T5)",
        "parameters": "Grading metric: Regime-Conditional IC. System prompt: immutable. Full tumbler depth (5).",
        "limitations": "Expected to be wrong during strong trends (momentum carries). Most useful during regime transitions.",
        "improvements": "Could dynamically adjust contrarian weight based on market regime (higher weight in choppy markets, lower in trends).",
        "connections": "Same as SKEPTIC. Calibrator grades on regime-conditional information coefficient.",
    },
    15: {
        "title": "REGIME WATCHER",
        "group": "shadow",
        "description": "Ignores the ticker entirely. Only question: 'Is this a good time to enter ANY long position?' Evaluates SPY trend, VIX, yield curve, credit spreads, sector rotation breadth.",
        "data_in": "Macro data only — ignores individual ticker signals",
        "data_out": "shadow_divergences row on disagreement",
        "db_table": "shadow_divergences",
        "cost": "Free (stops at T3, Ollama only — no Claude API calls)",
        "parameters": "Max tumbler depth: 3 (T1 + T2 + T3 only). Grading metric: Detection Latency. System prompt: immutable.",
        "limitations": "No Claude analysis — limited to Ollama qwen's macro reasoning. Can't do deep counterfactual analysis of macro risks.",
        "improvements": "Could add FRED macro indicators as direct T2 input. Could track regime change detection accuracy over time.",
        "connections": "Cheapest LLM shadow — survives budget tier 2. High enter rate (bullish bias) — consistently wants to enter when others don't.",
    },
    16: {
        "title": "OPTIONS FLOW",
        "group": "shadow",
        "description": "Momentum-focused. Primary signal: unusual options activity — sweeps, blocks, dark pool prints. Alpha decay fast (1-5 day window). Graded on 5-day forward return.",
        "data_in": "Same candidates + options_flow_signals enrichment data",
        "data_out": "shadow_divergences row on disagreement",
        "db_table": "shadow_divergences",
        "cost": "Claude API (full T1-T5)",
        "parameters": "Alpha decay: 1-5 days. Grading metric: 5-day forward return. System prompt: immutable. Full tumbler depth (5).",
        "limitations": "Options flow data is currently empty (Unusual Whales API not connected). This shadow is making decisions without its primary signal source.",
        "improvements": "CRITICAL: Connect Unusual Whales API to actually feed options flow data. Without it, this shadow is essentially running blind on the options dimension.",
        "connections": "Depends on ingest_options_flow for data. Currently running without its key data source.",
    },
    17: {
        "title": "FORM 4 INSIDER",
        "group": "shadow",
        "description": "Fundamentals-anchored. Primary signal: Form 4 purchase filings by CEOs, CFOs, board members within 14 days. Cluster buys weighted heavily. CFOs = strongest signal.",
        "data_in": "Same candidates + form4_signals enrichment data",
        "data_out": "shadow_divergences row on disagreement",
        "db_table": "shadow_divergences",
        "cost": "Claude API (full T1-T5)",
        "parameters": "Holding period: up to 15 days. Grading metric: 15-day forward return. System prompt: immutable. Full tumbler depth (5).",
        "limitations": "Form 4 filing delay is 2 business days — signal is always slightly stale. Cluster detection is simple (same-week buys).",
        "improvements": "Could weight by insider's historical accuracy (some CFOs are consistently right). Could track filing speed anomalies (chronic late filers suddenly filing fast).",
        "connections": "Depends on ingest_form4 for data. Best when cluster buys are detected.",
    },
    18: {
        "title": "KRONOS TECHNICALS",
        "group": "shadow",
        "description": "Pure price pattern agent using Kronos financial time series foundation model. 252 daily OHLCV candles -> 50 Monte Carlo paths -> bullish probability at 10-day horizon. No news, no fundamentals — only price.",
        "data_in": "yfinance daily OHLCV bars (252 days), Kronos-small model weights",
        "data_out": "shadow_divergences row with bullish_prob in shadow_confidence",
        "db_table": "shadow_divergences",
        "cost": "Free (local Jetson GPU inference, ~25 seconds per ticker)",
        "parameters": "Model: NeoQuasar/Kronos-small (24.7M params). Prediction length: 15 bars. Monte Carlo paths: 50. Horizon bar: 10. Bullish threshold: 0.60. Bearish threshold: 0.40. Max candidates: top 5 by score.",
        "limitations": "25 seconds per ticker limits to top 5 candidates. Ollama must unload before Kronos loads (shared GPU memory). Model is price-only — blind to fundamental catalysts.",
        "improvements": "Could run overnight batch on all watchlist tickers. Could ensemble Kronos predictions with a trend-following model. Could fine-tune on the specific AI infrastructure sector.",
        "connections": "Survives all budget tiers (zero API cost). First live run: April 10, 2026. Graded on directional accuracy at 10-day horizon.",
    },
    19: {
        "title": "CALIBRATOR",
        "group": "calibration",
        "description": "Sunday weekly. Grades all ungraded shadow divergences from past 30 days. Each shadow type has its own grading metric. Updates fitness_score and DWM weight in strategy_profiles.",
        "data_in": "shadow_divergences (ungraded), inference_chains (actual outcomes), price history (for Kronos directional accuracy)",
        "data_out": "Updated fitness_score, dwm_weight, conditional_brier, times_correct, times_dissented in strategy_profiles. Updated shadow_was_right, actual_outcome, actual_pnl in shadow_divergences.",
        "db_table": "strategy_profiles",
        "cost": "Free (computation only)",
        "parameters": "DWM formula: new_weight = 1.0 x (1 + 0.5 x (fitness - median_fitness)), clamped [0.05, 3.0]. Alpha: 0.5. Lookback: 30 days. Grading metrics: SKEPTIC=Brier, CONTRARIAN=IC, REGIME_WATCHER=latency, KRONOS=directional_accuracy_10d.",
        "limitations": "Weekly cadence means DWM weights update slowly. 30-day window means old regime data can dilute current accuracy.",
        "improvements": "Could run daily for faster adaptation. Could use exponential decay instead of hard 30-day cutoff. Could weight recent divergences more heavily.",
        "connections": "Reads shadow_divergences, writes strategy_profiles. DWM weights influence meta daily reflection priority. Runs after meta_weekly.",
    },
    20: {
        "title": "META DAILY",
        "group": "meta",
        "description": "1:30 PM weekdays. Claude Sonnet reviews the day's shadow divergences. Identifies unanimous dissent (all 6 shadows disagreed with live on a ticker). Output becomes RAG context for future T2 tumbler calls.",
        "data_in": "shadow_divergences (today), pipeline_health, signal_accuracy, trades, catalysts, chain analysis",
        "data_out": "meta_reflections row with signal_assessment, operational_issues, adjustments, embedding",
        "db_table": "meta_reflections",
        "cost": "~$0.02 per call (Claude Sonnet + Ollama embedding)",
        "parameters": "Model: Claude Sonnet. Unanimous dissent: all active shadows disagreed on same ticker. RAG: retrieves similar past days for context.",
        "limitations": "Depends on Claude API budget being available. If budget exhausted, reflection fails with 'Unable to assess'. No automated adjustment execution — proposed adjustments require human approval.",
        "improvements": "Auto-execute adjustments within +-5% bounds. Add Kronos directional predictions as additional meta context. Track which adjustments were approved vs rejected.",
        "connections": "Final pipeline step each day. Writes to meta_reflections with embedding. Future T2 calls retrieve these reflections via RAG. The feedback loop: shadows -> divergences -> calibrator -> DWM weights -> meta daily -> RAG context -> future T2 analysis.",
    },
}


CHAT_SYSTEM_PROMPT = """You are the OpenClaw Trader AI co-pilot — deeply embedded in an autonomous swing trading system built on adversarial AI ensemble architecture.

## System Architecture

OpenClaw runs on ridley (NVIDIA Jetson Orin Nano 8GB) with these components:
- **Scanner**: Runs 2x daily (6:35 AM, 9:30 AM PDT). 39-ticker AI infrastructure watchlist. 5-tumbler inference chain (T1-T5).
- **Adversarial Ensemble**: 6 shadow agents run in parallel with the live profile, recording disagreements as training data.
- **DWM Calibrator**: Weekly grading of shadow divergences. Fitness = correct/dissented. Weight formula: 1.0 x (1 + 0.5 x (fitness - median)), clamped [0.05, 3.0].
- **Kronos**: Pure price pattern forecasting via Kronos-small (24.7M params) on Jetson GPU. 50 Monte Carlo paths, 15-bar horizon.
- **Meta Daily**: Claude Sonnet reflects on shadow divergences. Unanimous dissent = HIGH PRIORITY.

## The 5-Tumbler Chain
- T1: Signal scoring (6 binary signals, pure math, free)
- T2: Fundamental analysis (RAG + catalyst boost + Congress, free)
- T3: Flow & cross-asset (Ollama qwen2.5:3b, local GPU, free)
- T4: Pattern matching (Claude Haiku, ~$0.001)
- T5: Counterfactual synthesis (Claude Sonnet, ~$0.005, asymmetric bearish bias)

## The 6 Shadow Agents
1. SKEPTIC — maximally conservative, Conditional Brier Score
2. CONTRARIAN — assumes trade is wrong, Regime-Conditional IC
3. REGIME_WATCHER — macro only (stops at T3), Detection Latency
4. OPTIONS_FLOW — unusual options activity, 5-day forward return
5. FORM4_INSIDER — SEC insider purchases, 15-day forward return
6. KRONOS_TECHNICALS — pure OHLCV price patterns (local GPU, 10-day directional accuracy)

## Budget Gate
- >= 40% remaining: all 6 shadows
- 20-40%: Regime Watcher + Form 4 + Kronos
- < 20%: Kronos only (zero API cost)

## Execution Gate
Trade executes only if: decision = enter/strong_enter, confidence >= 0.60, >= 3 tumblers completed. ATR-based sizing, 5% max risk.

## Infrastructure
- ridley: Jetson Orin Nano 8GB (scanner, Ollama, Kronos GPU inference)
- motherbrain: Orchestrator (Picard)
- Supabase: PostgreSQL + pgvector (project: vpollvsbtushbiapoflr)
- Alpaca: Paper trading API
- Fly.io: Dashboard (openclaw-trader-dash.fly.dev)

## Your Persona
You are a knowledgeable co-pilot who:
- Answers with specific data — use your tools to look things up, don't guess
- Knows every detail of the architecture and can explain any component in depth
- Is willing to challenge design decisions and suggest improvements
- Thinks about edge cases, failure modes, and optimization opportunities
- Speaks like an engineering partner, not a tutorial
- When the user is viewing a specific workflow step, you have deep context about that step and can discuss its internals, limitations, and potential improvements

Keep responses concise and data-driven. Use tables for comparisons. The user built this system — speak as a peer."""


@app.post("/api/chat")
async def chat_endpoint(request: Request, oc_session: str | None = Cookie(None)):
    """Streaming AI chat with full trading context via tool use."""
    _require_auth(request, oc_session)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="Claude API key not configured")

    body = await request.json()
    messages = body.get("messages", [])
    current_step = body.get("current_step")  # dict or None
    current_step_index = body.get("current_step_index", 0)  # int
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    # Limit conversation history to prevent runaway context
    messages = messages[-20:]

    # Build dynamic system prompt with step context when available
    system_prompt = CHAT_SYSTEM_PROMPT

    if current_step and current_step_index is not None:
        step_num = current_step_index + 1  # 1-indexed
        step_title = current_step.get("title", "")

        # Look up deep context from WORKFLOW_CONTEXT
        deep_context = WORKFLOW_CONTEXT.get(step_num, {})

        if deep_context:
            step_context = f"""

## CURRENT WORKFLOW STEP — The user is viewing Step {step_num}: {step_title}

You have deep context about this step. When the user asks questions, prioritize this context:

- **Description**: {deep_context.get('description', '')}
- **Data In**: {deep_context.get('data_in', '')}
- **Data Out**: {deep_context.get('data_out', '')}
- **DB Table**: {deep_context.get('db_table', '')}
- **Cost**: {deep_context.get('cost', '')}
- **Parameters**: {deep_context.get('parameters', '')}
- **Known Limitations**: {deep_context.get('limitations', '')}
- **Potential Improvements**: {deep_context.get('improvements', '')}
- **Connections**: {deep_context.get('connections', '')}

If the user asks about this step, answer with specificity — reference parameters, limitations, and connections to adjacent steps. If they ask about improvements, be honest about what could be better and suggest concrete changes. If they ask a general question unrelated to this step, use your full system knowledge."""

            system_prompt = CHAT_SYSTEM_PROMPT + step_context

    claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    async def generate():
        conv = list(messages)
        max_tool_rounds = 5

        for _round in range(max_tool_rounds + 1):
            try:
                collected_text = ""
                tool_use_blocks: list = []

                async with claude.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=system_prompt,
                    messages=conv,
                    tools=CHAT_TOOLS,
                ) as stream:
                    async for event in stream:
                        if hasattr(event, "type"):
                            if event.type == "content_block_delta" and hasattr(event, "delta"):
                                if getattr(event.delta, "type", "") == "text_delta":
                                    chunk = event.delta.text
                                    collected_text += chunk
                                    yield f"data: {json.dumps({'type': 'text', 'text': chunk})}\n\n"

                    final_message = await stream.get_final_message()

                # Check if we need to handle tool calls
                if final_message.stop_reason == "tool_use":
                    # Collect tool use blocks from the response
                    for block in final_message.content:
                        if block.type == "tool_use":
                            tool_use_blocks.append(block)

                    # Notify client about tool calls
                    for tb in tool_use_blocks:
                        yield f"data: {json.dumps({'type': 'tool_call', 'name': tb.name, 'input': tb.input})}\n\n"

                    # Execute all tools
                    tool_results = []
                    for tb in tool_use_blocks:
                        result = await _chat_tool_dispatch(tb.name, tb.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tb.id,
                            "content": result,
                        })

                    # Add assistant response + tool results to conversation
                    # Strip extra fields (like parsed_output) that newer SDK versions add
                    # but the API rejects on re-submission
                    def _clean_block(b):
                        d = b.model_dump()
                        d.pop("parsed_output", None)
                        return d
                    conv.append({"role": "assistant", "content": [_clean_block(b) for b in final_message.content]})
                    conv.append({"role": "user", "content": tool_results})
                    continue  # Next round — Claude will respond to tool results
                else:
                    break  # Done — Claude finished with text

            except anthropic.APIError as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
                break

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ============================================================================
# Logging & Observability API Routes
# ============================================================================

# The 8 canonical pipeline domains from the @traced() decorator
_KNOWN_DOMAINS = frozenset([
    "pipeline", "trades", "positions", "predictions",
    "meta", "catalysts", "economics", "sitrep",
])

# Rate limiting for the reasoning endpoint: {trade_id: [timestamps]}
_reasoning_rate_tracker: dict[str, list[float]] = {}
_REASONING_MAX_PER_HOUR = 10
_REASONING_WINDOW = 3600  # 1 hour in seconds


def _check_reasoning_rate_limit() -> bool:
    """Returns True if the global reasoning call limit has been exceeded."""
    now = time.time()
    window_start = now - _REASONING_WINDOW
    # Use a single key for global rate limiting
    calls = _reasoning_rate_tracker.get("__global__", [])
    calls = [t for t in calls if t > window_start]
    _reasoning_rate_tracker["__global__"] = calls
    return len(calls) >= _REASONING_MAX_PER_HOUR


def _record_reasoning_call() -> None:
    calls = _reasoning_rate_tracker.get("__global__", [])
    calls.append(time.time())
    _reasoning_rate_tracker["__global__"] = calls


@app.get("/api/logs/domains")
async def get_logs_domains(request: Request, oc_session: str | None = Cookie(None)):
    """Aggregated success/failure counts per domain from the last 24 hours."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return _empty_domain_summary()

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={
            "select": "step_name,status,started_at",
            "started_at": f"gte.{cutoff}",
            "limit": "2000",
        },
    )
    if resp.status_code != 200:
        return _empty_domain_summary()

    rows = resp.json()

    # Aggregate per domain — only process rows with a "domain:function" step_name
    domain_data: dict[str, dict] = {}
    for domain in _KNOWN_DOMAINS:
        domain_data[domain] = {"success": 0, "failure": 0, "last_run": None}

    for row in rows:
        step_name = row.get("step_name") or ""
        if ":" not in step_name:
            continue
        domain = step_name.split(":", 1)[0]
        if domain not in _KNOWN_DOMAINS:
            continue

        status = row.get("status", "")
        started_at = row.get("started_at")

        if status == "success":
            domain_data[domain]["success"] += 1
        elif status in ("failure", "timeout"):
            domain_data[domain]["failure"] += 1

        # Track most recent run timestamp
        current_last = domain_data[domain]["last_run"]
        if started_at and (current_last is None or started_at > current_last):
            domain_data[domain]["last_run"] = started_at

    result = []
    for domain in sorted(_KNOWN_DOMAINS):
        d = domain_data[domain]
        total = d["success"] + d["failure"]
        result.append({
            "domain": domain,
            "success": d["success"],
            "failure": d["failure"],
            "total": total,
            "last_run": d["last_run"],
        })

    return result


def _empty_domain_summary() -> list:
    return [
        {"domain": domain, "success": 0, "failure": 0, "total": 0, "last_run": None}
        for domain in sorted(_KNOWN_DOMAINS)
    ]


@app.get("/api/logs/domain/{domain_name}")
async def get_logs_domain(
    domain_name: str,
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
):
    """Per-function run history for a specific domain."""
    _require_auth(request, oc_session)

    if domain_name not in _KNOWN_DOMAINS:
        return JSONResponse(
            {"error": f"Unknown domain '{domain_name}'. Valid domains: {sorted(_KNOWN_DOMAINS)}"},
            status_code=400,
        )

    days = clamp_days(days, 30)

    if not SUPABASE_URL:
        return {"domain": domain_name, "functions": []}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={
            "select": "id,step_name,status,duration_ms,started_at,error_message,input_snapshot,output_snapshot",
            "step_name": f"like.{domain_name}:*",
            "started_at": f"gte.{cutoff}",
            "order": "started_at.desc",
            "limit": "500",
        },
    )
    if resp.status_code != 200:
        return {"domain": domain_name, "functions": []}

    rows = resp.json()

    # Group by function name (strip domain prefix from step_name)
    funcs: dict[str, dict] = {}
    prefix = f"{domain_name}:"

    for row in rows:
        step_name = row.get("step_name") or ""
        if not step_name.startswith(prefix):
            continue
        fn_name = step_name[len(prefix):]

        if fn_name not in funcs:
            funcs[fn_name] = {
                "name": fn_name,
                "success_count": 0,
                "failure_count": 0,
                "_durations": [],
                "runs": [],
            }

        status = row.get("status", "")
        if status == "success":
            funcs[fn_name]["success_count"] += 1
        elif status in ("failure", "timeout"):
            funcs[fn_name]["failure_count"] += 1

        dur = row.get("duration_ms")
        if dur is not None:
            try:
                funcs[fn_name]["_durations"].append(float(dur))
            except (ValueError, TypeError):
                pass

        # Keep only the most recent 20 runs per function
        if len(funcs[fn_name]["runs"]) < 20:
            funcs[fn_name]["runs"].append({
                "id": row.get("id"),
                "status": status,
                "duration_ms": dur,
                "started_at": row.get("started_at"),
                "error_message": row.get("error_message"),
                "input_snapshot": row.get("input_snapshot"),
                "output_snapshot": row.get("output_snapshot"),
            })

    # Finalize — compute avg_duration_ms and remove internal _durations list
    functions_list = []
    for fn_name, fn_data in sorted(funcs.items()):
        durations = fn_data.pop("_durations")
        fn_data["avg_duration_ms"] = (
            round(sum(durations) / len(durations)) if durations else None
        )
        functions_list.append(fn_data)

    return {"domain": domain_name, "functions": functions_list}


@app.post("/api/trades/{trade_id}/reasoning")
async def get_trade_reasoning(
    trade_id: str,
    request: Request,
    oc_session: str | None = Cookie(None),
):
    """AI-powered trade reasoning analysis with caching and rate limiting."""
    _require_auth(request, oc_session)
    trade_id = _validate_uuid(trade_id)

    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="No Supabase connection")

    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="Claude API key not configured")

    client = get_http()

    # 1. Fetch the trade_decisions row
    trade_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_decisions",
        headers=sb_headers(),
        params={"id": f"eq.{trade_id}"},
    )
    if trade_resp.status_code != 200 or not trade_resp.json():
        return JSONResponse({"error": "Trade not found"}, status_code=404)

    trade = trade_resp.json()[0]

    # 2. Check for cached result
    metadata = trade.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, ValueError):
            metadata = {}

    if "ai_reasoning" in metadata:
        return {"reasoning": metadata["ai_reasoning"], "cached": True}

    # 3. Rate limit check
    if _check_reasoning_rate_limit():
        return JSONResponse(
            {"error": "Reasoning rate limit exceeded (10/hour). Try again later."},
            status_code=429,
        )

    # 4. Fetch linked data in parallel
    ticker = trade.get("ticker", "")
    inference_chain_id = trade.get("inference_chain_id")
    entry_order_id = trade.get("entry_order_id")
    stop_order_id = trade.get("stop_order_id")

    # Build date range for signal/catalyst lookups
    created_at_str = trade.get("created_at") or datetime.now(timezone.utc).isoformat()
    try:
        trade_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        trade_dt = datetime.now(timezone.utc)

    signal_start = (trade_dt - timedelta(days=1)).isoformat()
    catalyst_start = (trade_dt - timedelta(hours=48)).isoformat()

    async def _fetch_chain():
        if not inference_chain_id:
            return None
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/inference_chains",
                headers=sb_headers(),
                params={"id": f"eq.{inference_chain_id}"},
            )
            rows = r.json() if r.status_code == 200 else []
            return rows[0] if rows else None
        except Exception:
            return None

    async def _fetch_signals():
        if not ticker:
            return []
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/signal_evaluations",
                headers=sb_headers(),
                params={
                    "select": "ticker,scan_date,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision,reasoning",
                    "ticker": f"eq.{ticker}",
                    "created_at": f"gte.{signal_start}",
                    "order": "created_at.desc",
                    "limit": "3",
                },
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _fetch_catalysts():
        if not ticker:
            return []
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/catalyst_events",
                headers=sb_headers(),
                params={
                    "select": "catalyst_type,headline,magnitude,direction,sentiment_score,event_time",
                    "ticker": f"eq.{ticker}",
                    "event_time": f"gte.{catalyst_start}",
                    "order": "event_time.desc",
                    "limit": "10",
                },
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _fetch_orders():
        order_ids = [oid for oid in [entry_order_id, stop_order_id] if oid]
        if not order_ids:
            return []
        try:
            # Fetch each order separately to avoid complex filter syntax
            results = []
            for oid in order_ids:
                r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/order_events",
                    headers=sb_headers(),
                    params={"order_id": f"eq.{oid}", "limit": "5"},
                )
                if r.status_code == 200:
                    results.extend(r.json())
            return results
        except Exception:
            return []

    chain, signals, catalysts, orders = await asyncio.gather(
        _fetch_chain(),
        _fetch_signals(),
        _fetch_catalysts(),
        _fetch_orders(),
    )

    # 5. Build prompt
    action = trade.get("action", "UNKNOWN")
    qty = trade.get("qty") or trade.get("quantity") or "?"
    entry_price = trade.get("entry_price") or "?"
    pnl = trade.get("pnl")
    outcome = trade.get("outcome") or "UNKNOWN"
    confidence = trade.get("confidence") or "?"
    decision = trade.get("decision") or trade.get("reasoning") or "?"
    profile_name = trade.get("profile_name") or trade.get("tuning_profile_id") or "?"

    # Format inference chain tumblers
    chain_text = "No inference chain available."
    if chain:
        tumblers = chain.get("tumblers") or []
        if isinstance(tumblers, list) and tumblers:
            tumbler_lines = []
            for i, t in enumerate(tumblers, 1):
                if isinstance(t, dict):
                    name = t.get("name") or t.get("tumbler") or f"Tumbler {i}"
                    conf = t.get("confidence") or t.get("score") or "?"
                    summary = t.get("summary") or t.get("reasoning") or t.get("result") or ""
                    tumbler_lines.append(f"  [{i}] {name}: confidence={conf}  {summary}")
                else:
                    tumbler_lines.append(f"  [{i}] {t}")
            stopping = chain.get("stopping_reason") or "completed"
            max_depth = chain.get("max_depth_reached") or len(tumblers)
            chain_text = "\n".join(tumbler_lines) + f"\n  Stopping reason: {stopping}\n  Max depth reached: {max_depth}"
        elif chain.get("reasoning_summary"):
            chain_text = chain["reasoning_summary"]

    # Format signals
    signal_text = "No signal data available."
    if signals:
        sig = signals[0]
        signal_text = (
            f"Trend: {sig.get('trend', '?')}, Momentum: {sig.get('momentum', '?')}, "
            f"Volume: {sig.get('volume', '?')}\n"
            f"Fundamental: {sig.get('fundamental', '?')}, Sentiment: {sig.get('sentiment', '?')}, "
            f"Flow: {sig.get('flow', '?')}\n"
            f"Total: {sig.get('total_score', '?')}/6"
        )

    # Format catalysts
    catalyst_text = "No catalysts recorded in the 48h window."
    if catalysts:
        lines = []
        for cat in catalysts[:8]:
            lines.append(
                f"  - [{cat.get('catalyst_type', 'unknown')}] {cat.get('headline', '')} "
                f"| {cat.get('direction', '?')} | magnitude={cat.get('magnitude', '?')}"
            )
        catalyst_text = "\n".join(lines)

    pnl_str = f"${pnl}" if pnl is not None else "open/unknown"

    prompt = f"""You are analyzing a trade made by OpenClaw, an autonomous swing trading system.

TRADE DETAILS:
- Ticker: {ticker}
- Action: {action} {qty} shares
- Entry Price: ${entry_price} on {created_at_str[:10]}
- P&L: {pnl_str} ({outcome})
- Confidence: {confidence}
- Decision: {decision}
- Profile: {profile_name}

INFERENCE CHAIN (tumbler-by-tumbler reasoning):
{chain_text}

SIGNAL SCORES:
{signal_text}

CATALYSTS (48h before entry):
{catalyst_text}

Explain in plain language:
1. What was the primary thesis for this trade?
2. Which catalysts and signals were most influential?
3. How did each tumbler contribute to the final decision?
4. Was the reasoning sound given the available data?
5. If the trade lost money, what went wrong? If profitable, was it for the right reasons?"""

    # 6. Call Claude (non-streaming, single shot)
    _record_reasoning_call()
    try:
        claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        reasoning_text = message.content[0].text if message.content else "No reasoning generated."
    except anthropic.APIError as e:
        return JSONResponse({"error": f"Claude API error: {e}"}, status_code=502)
    except Exception:
        return JSONResponse({"error": "Failed to generate reasoning"}, status_code=500)

    # 7. Cache result in trade_decisions.metadata
    updated_metadata = dict(metadata)
    updated_metadata["ai_reasoning"] = reasoning_text

    try:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/trade_decisions",
            headers={**sb_headers(), "Content-Type": "application/json"},
            params={"id": f"eq.{trade_id}"},
            json={"metadata": updated_metadata},
        )
    except Exception:
        # Cache failure is non-fatal — return the reasoning anyway
        pass

    return {"reasoning": reasoning_text, "cached": False}


# ============================================================================
# Shadow Intelligence Routes
# ============================================================================


@app.get("/api/shadow/profiles")
async def get_shadow_profiles(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers=sb_headers(),
        params={
            "select": "profile_name,shadow_type,fitness_score,dwm_weight,"
                      "conditional_brier,times_correct,times_dissented,"
                      "divergence_rate,last_graded_at",
            "is_shadow": "eq.true",
            "order": "fitness_score.desc",
            "limit": "20",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/shadow/divergences")
async def get_shadow_divergences(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 30,
):
    _require_auth(request, oc_session)
    days = min(days, 90)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_divergences",
        headers=sb_headers(),
        params={
            "select": "id,ticker,divergence_date,live_profile,live_decision,live_confidence,"
                      "shadow_profile,shadow_type,shadow_decision,shadow_confidence,"
                      "shadow_stopping_reason,first_diverged_at_tumbler,"
                      "shadow_was_right,save_value,actual_outcome",
            "divergence_date": f"gte.{cutoff}",
            "order": "divergence_date.desc,created_at.desc",
            "limit": "200",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/shadow/unanimous")
async def get_shadow_unanimous(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 30,
):
    _require_auth(request, oc_session)
    days = min(days, 90)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_divergences",
        headers=sb_headers(),
        params={
            "select": "ticker,divergence_date,live_decision,live_confidence,"
                      "shadow_profile,shadow_decision,shadow_confidence,"
                      "shadow_stopping_reason,shadow_was_right,actual_outcome,save_value",
            "divergence_date": f"gte.{cutoff}",
            "order": "divergence_date.desc",
            "limit": "500",
        },
    )
    rows = resp.json() if resp.status_code == 200 else []
    # Group by ticker+date, find where ALL shadows dissented
    from collections import defaultdict

    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_key[(r["ticker"], r["divergence_date"])].append(r)

    unanimous = []
    for (ticker, div_date), divs in by_key.items():
        live_was_entry = any(d["live_decision"] in ("enter", "strong_enter") for d in divs)
        all_shadows_dissented = all(
            d["shadow_decision"] not in ("enter", "strong_enter") for d in divs
        )
        if live_was_entry and all_shadows_dissented and len(divs) >= 2:
            unanimous.append(
                {
                    "ticker": ticker,
                    "date": div_date,
                    "live_confidence": max((d.get("live_confidence") or 0) for d in divs),
                    "shadows": [
                        {
                            "profile": d["shadow_profile"],
                            "decision": d["shadow_decision"],
                            "confidence": d.get("shadow_confidence"),
                            "reason": d.get("shadow_stopping_reason"),
                        }
                        for d in divs
                    ],
                    "outcome": divs[0].get("actual_outcome"),
                    "save_value": sum(float(d.get("save_value") or 0) for d in divs),
                }
            )
    return sorted(unanimous, key=lambda x: x["date"], reverse=True)


@app.get("/api/shadow/kronos/latest")
async def get_shadow_kronos_latest(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    # Query shadow_divergences for the most recent KRONOS_TECHNICALS entries
    # Returns: ticker, shadow_decision, shadow_confidence (bullish_prob),
    #          divergence_date, live_decision
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_divergences",
        headers=sb_headers(),
        params={
            "select": "ticker,shadow_decision,shadow_confidence,live_decision,divergence_date,created_at",
            "shadow_profile": "eq.KRONOS_TECHNICALS",
            "order": "created_at.desc",
            "limit": "10",
        },
    )
    if resp.status_code != 200:
        return []
    return resp.json()


# ============================================================================
# System Health API Routes
# ============================================================================


@app.get("/api/health/latest")
async def get_health_latest(request: Request, oc_session: str = Cookie(None)):
    """Most recent health check run results, grouped by check_group."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"run_id": None, "checks": []}

    client = get_http()

    # Get the most recent run_id
    latest_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_health",
        headers=sb_headers(),
        params={
            "select": "run_id,run_type,created_at",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    if latest_resp.status_code != 200 or not latest_resp.json():
        return {"run_id": None, "checks": []}

    latest = latest_resp.json()[0]
    run_id = latest["run_id"]
    run_type = latest["run_type"]
    run_created_at = latest["created_at"]

    # Fetch all rows for that run_id
    rows_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_health",
        headers=sb_headers(),
        params={
            "run_id": f"eq.{run_id}",
            "order": "check_order.asc",
            "limit": "200",
        },
    )
    if rows_resp.status_code != 200:
        return {"run_id": run_id, "checks": []}

    rows = rows_resp.json()

    total_pass = sum(1 for r in rows if r.get("status") == "pass")
    total_fail = sum(1 for r in rows if r.get("status") == "fail")
    total_warn = sum(1 for r in rows if r.get("status") == "warn")
    total_skip = sum(1 for r in rows if r.get("status") == "skip")
    total_duration_ms = sum(int(r.get("duration_ms") or 0) for r in rows)

    return {
        "run_id": run_id,
        "run_type": run_type,
        "created_at": run_created_at,
        "total_pass": total_pass,
        "total_fail": total_fail,
        "total_warn": total_warn,
        "total_skip": total_skip,
        "duration_ms": total_duration_ms,
        "checks": rows,
    }


@app.post("/api/health/run")
async def trigger_health_run(request: Request, oc_session: str = Cookie(None)):
    """Trigger health_check.py as a subprocess with a new run_id."""
    _require_auth(request, oc_session)
    run_id = str(uuid.uuid4())
    scripts_dir = Path(__file__).parent.parent / "scripts" / "health_check.py"
    subprocess_env = {**os.environ, "HEALTH_RUN_ID": run_id}
    subprocess.Popen(
        [sys.executable, str(scripts_dir), "--notify-always"],
        env=subprocess_env,
        cwd=str(Path(__file__).parent.parent),
    )
    return {"status": "triggered", "run_id": run_id}


@app.get("/api/health/history")
async def get_health_history(request: Request, oc_session: str = Cookie(None)):
    """Last 7 health check runs as summary rows for the history strip."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []

    client = get_http()

    # Get enough rows to cover 7 distinct runs
    rows_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_health",
        headers=sb_headers(),
        params={
            "select": "run_id,run_type,status,created_at",
            "order": "created_at.desc",
            "limit": "500",
        },
    )
    if rows_resp.status_code != 200:
        return []

    rows = rows_resp.json()

    # Aggregate by run_id — preserve insertion order (desc by created_at)
    seen: dict[str, dict] = {}
    for r in rows:
        rid = r["run_id"]
        if rid not in seen:
            seen[rid] = {
                "run_id": rid,
                "run_type": r.get("run_type"),
                "created_at": r.get("created_at"),
                "pass": 0,
                "fail": 0,
                "warn": 0,
                "skip": 0,
                "worst": "pass",
            }
        entry = seen[rid]
        status = r.get("status", "skip")
        entry[status] = entry.get(status, 0) + 1
        # Track worst status for the run dot colour
        if status == "fail":
            entry["worst"] = "fail"
        elif status == "warn" and entry["worst"] != "fail":
            entry["worst"] = "warn"

    runs = list(seen.values())[:7]
    return runs


@app.get("/api/signals/options-flow")
async def get_signals_options_flow(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
) -> list:
    _require_auth(request, oc_session)
    days = min(days, 90)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/options_flow_signals",
        headers=sb_headers(),
        params={
            "select": "id,ticker,signal_date,signal_type,strike,expiry,premium,"
                      "open_interest,volume,implied_volatility,sentiment,source,created_at",
            "signal_date": f"gte.{cutoff}",
            "order": "signal_date.desc,created_at.desc",
            "limit": "100",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@app.get("/api/signals/form4")
async def get_signals_form4(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 30,
) -> list:
    _require_auth(request, oc_session)
    days = min(days, 180)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/form4_signals",
        headers=sb_headers(),
        params={
            "select": "id,ticker,signal_date,filing_date,filer_name,filer_title,"
                      "transaction_type,shares,price_per_share,total_value,"
                      "shares_owned_after,ownership_pct_change,days_since_last_filing,"
                      "cluster_count,source,created_at",
            "signal_date": f"gte.{cutoff}",
            "order": "signal_date.desc,created_at.desc",
            "limit": "100",
        },
    )
    return resp.json() if resp.status_code == 200 else []


# ============================================================================
# Simulator / Preflight API Routes
# ============================================================================

FLIGHT_MANIFEST = [
    {"name": "health_check", "pipeline_name": "health_check", "schedule": "5:00 AM weekdays", "criticality": "high", "freshness_hours": 26, "writes_pipeline_runs": False},
    {"name": "catalyst_ingest", "pipeline_name": "catalyst_ingest", "schedule": "5:30/9:15/12:50 weekdays", "criticality": "high", "freshness_hours": 26, "writes_pipeline_runs": True},
    {"name": "ingest_form4", "pipeline_name": "ingest", "schedule": "6:00 AM weekdays", "criticality": "medium", "freshness_hours": 26, "writes_pipeline_runs": True},
    {"name": "scanner", "pipeline_name": "scanner", "schedule": "6:35/9:30 weekdays", "criticality": "high", "freshness_hours": 26, "writes_pipeline_runs": True},
    {"name": "ingest_options_flow", "pipeline_name": "ingest", "schedule": "7:00 AM weekdays", "criticality": "medium", "freshness_hours": 26, "writes_pipeline_runs": True},
    {"name": "position_manager", "pipeline_name": "position_manager", "schedule": "Every 30m market hours", "criticality": "high", "freshness_hours": 2, "writes_pipeline_runs": True},
    {"name": "meta_daily", "pipeline_name": "meta_daily", "schedule": "1:30 PM weekdays", "criticality": "high", "freshness_hours": 26, "writes_pipeline_runs": True},
    {"name": "meta_weekly", "pipeline_name": "meta_weekly", "schedule": "4:00 PM Sundays", "criticality": "medium", "freshness_hours": 170, "writes_pipeline_runs": True},
    {"name": "calibrator", "pipeline_name": "calibrator", "schedule": "4:30 PM Sundays", "criticality": "medium", "freshness_hours": 170, "writes_pipeline_runs": True},
    {"name": "heartbeat", "pipeline_name": "heartbeat", "schedule": "Every 5 min", "criticality": "low", "freshness_hours": 1, "writes_pipeline_runs": True},
]


@app.post("/api/simulator/run")
async def trigger_simulator(request: Request, oc_session: str = Cookie(None)):
    """Write a trigger row to system_health; simulator_watcher on ridley will pick it up.

    The old approach (subprocess.Popen) only works when the dashboard is running locally
    on ridley. This Supabase-bridged approach works from Fly.io or anywhere else.

    Accepts optional JSON body: {"concurrency": 4} (1-10) to trigger a stress run.
    """
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")

    # Parse optional concurrency from request body
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    concurrency = min(max(int(body.get("concurrency", 1)), 1), 10)

    run_id = str(uuid.uuid4())
    client = get_http()
    resp = await client.post(
        f"{SUPABASE_URL}/rest/v1/system_health",
        headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
        json={
            "run_id": run_id,
            "run_type": "simulator",
            "check_group": "TRIGGER",
            "check_name": "_trigger",
            "check_order": 0,
            "status": "skip",
            "value": f"concurrency={concurrency}",
            "expected": "",
            "error_message": "",
            "duration_ms": 0,
        },
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="Failed to write trigger row to Supabase")
    return {"status": "triggered", "run_id": run_id, "concurrency": concurrency}


@app.get("/api/simulator/status")
async def get_simulator_status(
    request: Request,
    oc_session: str = Cookie(None),
    run_id: str = "",
):
    """Return all test results written so far for a given simulator run_id.

    If run_id is omitted, returns the most recent simulator run summary.
    """
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"run_id": None, "checks": [], "summary": {}}

    client = get_http()

    if not run_id:
        # Return the most recent simulator run_id
        latest_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/system_health",
            headers=sb_headers(),
            params={
                "select": "run_id,created_at",
                "run_type": "eq.simulator",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if latest_resp.status_code != 200 or not latest_resp.json():
            return {"run_id": None, "checks": [], "summary": {}}
        run_id = latest_resp.json()[0]["run_id"]

    run_id = _validate_uuid(run_id)

    rows_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_health",
        headers=sb_headers(),
        params={
            "run_id": f"eq.{run_id}",
            "run_type": "eq.simulator",
            "check_name": "neq._trigger",  # exclude the trigger sentinel row
            "order": "check_order.asc",
            "limit": "100",
        },
    )
    if rows_resp.status_code != 200:
        return {"run_id": run_id, "checks": [], "summary": {}}

    checks = rows_resp.json()
    total = len(checks)
    go_count = sum(1 for c in checks if c.get("status") == "pass")
    nogo_count = sum(1 for c in checks if c.get("status") == "fail")
    scrub_count = sum(1 for c in checks if c.get("status") == "skip")
    # The simulator has 64 base tests + 8 Mission Readiness tests (Group Q) = 72 total
    # In normal mode (concurrency=1) P tests report as GO/skipped so still count
    complete = total >= 72

    return {
        "run_id": run_id,
        "checks": checks,
        "summary": {
            "total": total,
            "go": go_count,
            "nogo": nogo_count,
            "scrub": scrub_count,
            "complete": complete,
        },
    }


@app.get("/api/health/flight-status")
async def get_flight_status(request: Request, oc_session: str = Cookie(None)):
    """Manifest vs reality for today's scheduled functions.

    Consolidated from N+1 per-entry queries into 2 total queries:
    one against pipeline_runs (all pipeline_names at once, latest per name
    resolved in Python), one against system_health (for health_check entry).
    """
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []

    client = get_http()
    now = datetime.now(timezone.utc)

    # Determine the maximum freshness window across all manifest entries so we
    # fetch far enough back to catch weekly jobs (170h = ~7 days).
    max_freshness_h = max(e["freshness_hours"] for e in FLIGHT_MANIFEST)
    cutoff = (now - timedelta(hours=max_freshness_h + 12)).isoformat()

    # Collect all distinct pipeline_names that use pipeline_runs.
    pipeline_names = list({
        e["pipeline_name"]
        for e in FLIGHT_MANIFEST
        if e["writes_pipeline_runs"]
    })

    # Single query: recent root rows for all relevant pipelines.
    # Results are ordered desc so the first occurrence per pipeline_name is
    # the most recent run — we reduce to latest_per_name in Python.
    latest_per_pipeline: dict[str, str] = {}
    try:
        # Build an OR filter: pipeline_name=eq.X,pipeline_name=eq.Y,...
        or_filter = ",".join(f"pipeline_name.eq.{pn}" for pn in pipeline_names)
        pr_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/pipeline_runs",
            headers=sb_headers(),
            params={
                "select": "pipeline_name,started_at",
                "or": f"({or_filter})",
                "step_name": "eq.root",
                "started_at": f"gte.{cutoff}",
                "order": "started_at.desc",
                "limit": "500",
            },
        )
        if pr_resp.status_code == 200:
            for row in pr_resp.json():
                pn = row.get("pipeline_name", "")
                if pn and pn not in latest_per_pipeline:
                    latest_per_pipeline[pn] = row["started_at"]
    except Exception:
        pass

    # Single query: most recent scheduled health_check run.
    health_check_last_run: str | None = None
    try:
        sh_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/system_health",
            headers=sb_headers(),
            params={
                "select": "created_at",
                "run_type": "eq.scheduled",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if sh_resp.status_code == 200 and sh_resp.json():
            health_check_last_run = sh_resp.json()[0].get("created_at")
    except Exception:
        pass

    def _compute_entry(entry: dict) -> dict:
        name = entry["name"]
        pipeline_name = entry["pipeline_name"]
        freshness_hours = entry["freshness_hours"]
        writes_pipeline_runs = entry["writes_pipeline_runs"]

        if writes_pipeline_runs:
            last_run_at = latest_per_pipeline.get(pipeline_name)
        else:
            last_run_at = health_check_last_run

        if last_run_at:
            try:
                last_dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                age_h = (now - last_dt).total_seconds() / 3600
                freshness_ok = age_h <= freshness_hours
                status = "ran" if freshness_ok else "stale"
            except (ValueError, AttributeError):
                status = "missing"
                freshness_ok = False
        else:
            status = "missing"
            freshness_ok = False

        return {
            "name": name,
            "schedule": entry["schedule"],
            "pipeline_name": pipeline_name,
            "criticality": entry["criticality"],
            "last_run_at": last_run_at,
            "status": status,
            "freshness_ok": freshness_ok,
            "freshness_hours": freshness_hours,
        }

    return [_compute_entry(e) for e in FLIGHT_MANIFEST]


# ============================================================================
# Trade Replay Viewer API
# ============================================================================

_ohlcv_cache: dict[str, list] = {}


@app.get("/api/replay/dates")
async def get_replay_dates(request: Request, oc_session: str | None = Cookie(None)):
    """Return distinct dates with CONGRESS_MIRROR scan data, most recent first."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={
            "select": "created_at,ticker,scan_type",
            "profile_name": "eq.CONGRESS_MIRROR",
            "order": "created_at.desc",
            "limit": "2000",
        },
    )
    if resp.status_code != 200:
        return []
    rows = resp.json()
    from collections import defaultdict
    by_date: dict[str, dict] = defaultdict(lambda: {"tickers": set(), "sessions": set()})
    for r in rows:
        d = r["created_at"][:10]
        by_date[d]["tickers"].add(r.get("ticker") or "")
        by_date[d]["sessions"].add(r.get("scan_type") or "")
    result = [
        {
            "date": d,
            "candidate_count": len(v["tickers"]),
            "session_count": len(v["sessions"]),
        }
        for d, v in sorted(by_date.items(), reverse=True)
    ]
    return result[:90]


@app.get("/api/replay/candidates")
async def get_replay_candidates(
    request: Request,
    date: str,
    session: str = "morning",
    oc_session: str | None = Cookie(None),
):
    """Return CONGRESS_MIRROR inference chains for a date, optionally filtered by session."""
    _require_auth(request, oc_session)
    date = _validate_date(date)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers={**sb_headers(), "Prefer": "count=exact"},
        params={
            "select": "id,ticker,final_decision,final_confidence,max_depth_reached,stopping_reason,profile_name,scan_type,created_at",
            "profile_name": "eq.CONGRESS_MIRROR",
            "created_at": f"gte.{date}T00:00:00Z",
            "order": "final_confidence.desc",
            "limit": "200",
        },
    )
    if resp.status_code != 200:
        return []
    chains = [c for c in resp.json() if c["created_at"][:10] == date]

    # Filter by session: morning = before 15:00 UTC (8 AM PT), midday = 15:00+
    # Scanner morning runs ~13:35 UTC (6:35 AM PDT), midday ~16:30 UTC (9:30 AM PDT)
    if session == "morning":
        chains = [c for c in chains if c["created_at"][11:13] < "15"]
    elif session == "midday":
        chains = [c for c in chains if c["created_at"][11:13] >= "15"]

    # Fetch shadow dissent counts for these chains
    chain_ids = [c["id"] for c in chains]
    dissent_counts: dict[str, int] = {}
    if chain_ids:
        id_list = ",".join(chain_ids)
        div_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/shadow_divergences",
            headers=sb_headers(),
            params={
                "select": "live_chain_id",
                "live_chain_id": f"in.({id_list})",
                "limit": "500",
            },
        )
        if div_resp.status_code == 200:
            for d in div_resp.json():
                cid = d.get("live_chain_id")
                if cid:
                    dissent_counts[cid] = dissent_counts.get(cid, 0) + 1

    result = []
    for c in chains:
        result.append(
            {
                "chain_id": c["id"],
                "ticker": c["ticker"],
                "total_score": c.get("total_score", 0),
                "final_decision": c["final_decision"],
                "final_confidence": c["final_confidence"],
                "max_depth_reached": c.get("max_depth_reached"),
                "stopping_reason": c.get("stopping_reason"),
                "profile_name": c["profile_name"],
                "shadow_dissent_count": dissent_counts.get(c["id"], 0),
                "date": date,
            }
        )
    return result


@app.get("/api/replay/chain")
async def get_replay_chain(
    request: Request,
    chain_id: str,
    oc_session: str | None = Cookie(None),
):
    """Return the full inference_chains row (including tumblers JSONB) for a chain ID."""
    _require_auth(request, oc_session)
    chain_id = _validate_uuid(chain_id)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={"select": "*", "id": f"eq.{chain_id}"},
    )
    if resp.status_code != 200 or not resp.json():
        return {}
    return resp.json()[0]


@app.get("/api/replay/shadows")
async def get_replay_shadows(
    request: Request,
    ticker: str,
    date: str,
    oc_session: str | None = Cookie(None),
):
    """Return all profile chains for a ticker on a date, with divergence metadata."""
    _require_auth(request, oc_session)
    ticker = _validate_ticker(ticker)
    date = _validate_date(date)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={
            "select": "id,profile_name,final_decision,final_confidence,max_depth_reached,stopping_reason,tumblers,created_at",
            "ticker": f"eq.{ticker}",
            "created_at": f"gte.{date}T00:00:00Z",
            "order": "profile_name.asc",
            "limit": "20",
        },
    )
    if resp.status_code != 200:
        return []
    chains = [c for c in resp.json() if c["created_at"][:10] == date]

    # Fetch divergence metadata keyed by shadow_chain_id
    chain_ids = [c["id"] for c in chains]
    div_map: dict[str, dict] = {}
    if chain_ids:
        id_list = ",".join(chain_ids)
        div_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/shadow_divergences",
            headers=sb_headers(),
            params={
                "select": "shadow_chain_id,first_diverged_at_tumbler,shadow_was_right",
                "shadow_chain_id": f"in.({id_list})",
                "limit": "50",
            },
        )
        if div_resp.status_code == 200:
            for d in div_resp.json():
                div_map[d["shadow_chain_id"]] = d

    result = []
    for c in chains:
        div = div_map.get(c["id"], {})
        result.append(
            {
                "profile_name": c["profile_name"],
                "final_decision": c["final_decision"],
                "final_confidence": c["final_confidence"],
                "max_depth_reached": c.get("max_depth_reached"),
                "stopping_reason": c.get("stopping_reason"),
                "tumblers": c.get("tumblers"),
                "first_diverged_at_tumbler": div.get("first_diverged_at_tumbler"),
                "shadow_was_right": div.get("shadow_was_right"),
            }
        )
    return result


@app.get("/api/replay/outcome")
async def get_replay_outcome(
    request: Request,
    ticker: str,
    date: str,
    oc_session: str | None = Cookie(None),
):
    """Return the trade_learnings row for a ticker on a date, if a trade was executed."""
    _require_auth(request, oc_session)
    ticker = _validate_ticker(ticker)
    date = _validate_date(date)
    if not SUPABASE_URL:
        return None
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_learnings",
        headers=sb_headers(),
        params={
            "select": "*",
            "ticker": f"eq.{ticker}",
            "created_at": f"gte.{date}T00:00:00Z",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    if resp.status_code != 200 or not resp.json():
        return None
    row = resp.json()[0]
    if row["created_at"][:10] != date:
        return None
    return row


@app.get("/api/replay/ohlcv")
async def get_replay_ohlcv(
    request: Request,
    ticker: str,
    date: str,
    oc_session: str | None = Cookie(None),
):
    """Return 90 days of daily OHLCV bars ending on the given date (yfinance, in-memory cache)."""
    _require_auth(request, oc_session)
    ticker = _validate_ticker(ticker)
    date = _validate_date(date)
    cache_key = f"{ticker}_{date}"
    if cache_key in _ohlcv_cache:
        return _ohlcv_cache[cache_key]

    try:
        end_date = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        df = yf.download(ticker, end=end_date, period="90d", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return []

        result = []
        for idx, row in df.iterrows():
            result.append(
                {
                    "time": idx.strftime("%Y-%m-%d"),
                    "open": round(float(row["Open"]), 2),
                    "high": round(float(row["High"]), 2),
                    "low": round(float(row["Low"]), 2),
                    "close": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]),
                }
            )

        _ohlcv_cache[cache_key] = result
        # Cap cache at 100 entries — evict oldest key
        if len(_ohlcv_cache) > 100:
            oldest = next(iter(_ohlcv_cache))
            del _ohlcv_cache[oldest]

        return result
    except Exception as exc:
        print(f"[replay/ohlcv] {ticker} {date}: {exc}")
        return []


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
