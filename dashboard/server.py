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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

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

# Session store: {token_hash: expiry_timestamp}
_sessions: dict[str, float] = {}
SESSION_MAX_AGE = 86400 * 7  # 7 days

# Rate limiting: {ip: [timestamps]}
_login_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 300  # 5 minutes

# CSRF tokens: {token_hash: expiry}
_csrf_tokens: dict[str, float] = {}


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
    """Create a session token, store its hash, return the raw token."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    _sessions[token_hash] = time.time() + SESSION_MAX_AGE
    # Prune expired sessions
    now = time.time()
    expired = [k for k, v in _sessions.items() if v < now]
    for k in expired:
        del _sessions[k]
    return token


def _verify_session(token: str | None) -> bool:
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expiry = _sessions.get(token_hash)
    if not expiry:
        return False
    if time.time() > expiry:
        del _sessions[token_hash]
        return False
    return True


def _create_csrf() -> str:
    """Create a CSRF token."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    _csrf_tokens[token_hash] = time.time() + 600  # 10 min expiry
    # Prune
    now = time.time()
    expired = [k for k, v in _csrf_tokens.items() if v < now]
    for k in expired:
        del _csrf_tokens[k]
    return token


def _verify_csrf(token: str | None) -> bool:
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expiry = _csrf_tokens.pop(token_hash, None)  # One-time use
    if not expiry:
        return False
    return time.time() <= expiry


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
        headers={"X-CSRF-Token": csrf},
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
    if oc_session:
        token_hash = hashlib.sha256(oc_session.encode()).hexdigest()
        _sessions.pop(token_hash, None)
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


# Dashboard Route
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, oc_session: str | None = Cookie(None)):
    if not _is_authed(request, oc_session):
        return RedirectResponse("/login", status_code=302)
    return FileResponse(Path(__file__).parent / "index.html")


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


@app.get("/api/llm/stats")
async def get_llm_stats(request: Request, oc_session: str | None = Cookie(None)):
    """LLM inference statistics."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/llm_stats",
        headers=sb_headers(),
    )
    stats = resp.json() if resp.status_code == 200 else []

    # Also get recent inferences
    resp2 = await client.get(
        f"{SUPABASE_URL}/rest/v1/llm_inferences",
        headers=sb_headers(),
        params={"order": "created_at.desc", "limit": "20"},
    )
    recent = resp2.json() if resp2.status_code == 200 else []

    return {"models": stats, "recent": recent}


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
        params={"order": "config_key.asc"},
    )
    configs = resp.json() if resp.status_code == 200 else []

    # Get today's spend
    resp2 = await client.get(
        f"{SUPABASE_URL}/rest/v1/cost_ledger",
        headers=sb_headers(),
        params={
            "select": "category,amount",
            "ledger_date": f"eq.{today}",
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
    # Activate selected FIRST (so we never have zero active profiles)
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=representation"},
        params={"id": f"eq.{profile_id}"},
        json={"active": True, "updated_at": now},
    )
    if resp.status_code != 200 or not resp.json():
        raise HTTPException(status_code=500, detail="Failed to activate profile")
    # Then deactivate all others
    await client.patch(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers={**sb_headers(), "Content-Type": "application/json"},
        params={"active": "eq.true", "id": f"neq.{profile_id}"},
        json={"active": False, "updated_at": now},
    )
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
                    json={"model": "claude-sonnet-4-6-20250514", "max_tokens": 1, "messages": []},
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
        params={"order": "version.desc"},
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
