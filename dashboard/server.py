#!/usr/bin/env python3
"""
OpenClaw Trader Dashboard — Local web UI for monitoring the trading agent.
Serves the dashboard HTML and proxies data from Supabase + Alpaca.

Authentication: password login form with session tokens, rate limiting,
and CSRF protection. No query-param key exposure.
"""

import hashlib
import hmac
import os
import re
import secrets
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

# Route modules
from routes.chat import router as chat_router
from routes.ensemble import router as ensemble_router
from routes.health import router as health_router
from routes.replay import router as replay_router
from routes.system import router as system_router
from routes.trading import router as trading_router

# Shared helpers (also used by route modules via their own import)
from shared import (
    PASSWORD_HASH,
    SUPABASE_URL,
    _is_authed,
    _require_auth,
    _validate_uuid,
    close_http_client,
    get_http,
    sb_headers,
    set_http_client,
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

# Register route modules
app.include_router(replay_router)
app.include_router(ensemble_router)
app.include_router(health_router)
app.include_router(system_router)
app.include_router(trading_router)
app.include_router(chat_router)


# ============================================================================
# Persistent HTTP client lifecycle
# ============================================================================


@app.on_event("startup")
async def startup():
    set_http_client(httpx.AsyncClient(timeout=10.0))


@app.on_event("shutdown")
async def shutdown():
    await close_http_client()


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
# Input validation helpers (local aliases for routes that stay in this file)
# ============================================================================

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ALLOWED_BUDGET_KEYS = {"daily_claude_budget", "daily_perplexity_budget"}

# ============================================================================
# Auth config (signing key, sessions, rate limiting)
# ============================================================================

# PASSWORD_HASH imported from shared.py — single source of truth

_SESSION_SIGNING_SALT = os.environ.get("SESSION_SIGNING_SALT", "oc-session-stable-v1")
if _SESSION_SIGNING_SALT == "oc-session-stable-v1":
    print(
        "[Dashboard] WARNING: SESSION_SIGNING_SALT is using the default value. "
        "Set the SESSION_SIGNING_SALT environment variable for production security."
    )
_SIGNING_KEY = hashlib.sha256(_SESSION_SIGNING_SALT.encode()).digest()

SESSION_MAX_AGE = 86400 * 90  # 90 days

_login_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 300  # 5 minutes

# Magic link email config
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER

MAGIC_LINK_DURATIONS = {"1h": 3600, "24h": 86400, "7d": 86400 * 7}


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
    issued = str(int(time.time()))
    sig = hmac.new(_SIGNING_KEY, issued.encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{sig}"


def _verify_session_local(token: str | None) -> bool:
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
    try:
        issued = int(payload.split(".")[1])
    except (IndexError, ValueError):
        return False
    return time.time() - issued <= 600


# ============================================================================
# Auth Routes
# ============================================================================


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, oc_session: str | None = Cookie(None)):
    if _is_authed(request, oc_session):
        return RedirectResponse("/", status_code=302)
    csrf = _create_csrf()
    html = (Path(__file__).parent / "login.html").read_text()
    html = html.replace("CSRF_TOKEN_PLACEHOLDER", csrf)
    return HTMLResponse(
        html,
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
    ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )

    if _check_rate_limit(ip):
        return HTMLResponse(
            _login_error_page("Too many attempts. Wait 5 minutes.", _create_csrf()),
            status_code=429,
        )

    if not _verify_csrf(csrf_token):
        return HTMLResponse(
            _login_error_page("Session expired. Please try again.", _create_csrf()),
            status_code=403,
        )

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
    from html import escape

    html_path = Path(__file__).parent / "login.html"
    html = html_path.read_text()
    html = html.replace("<!-- ERROR_PLACEHOLDER -->", f'<div class="error">{escape(error)}</div>')
    html = html.replace("CSRF_TOKEN_PLACEHOLDER", csrf)
    return html


# ============================================================================
# Magic Link System
# ============================================================================


def _send_magic_email(email: str, link: str, expires_label: str) -> bool:
    if not SMTP_USER or not SMTP_PASS:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "OpenClaw Trader — Access Link"
    msg["From"] = SMTP_FROM
    msg["To"] = email

    text = (
        f"Your OpenClaw Trader access link (expires in {expires_label}):\n\n{link}\n\n"
        "This link is one-time use. It will stop working after you click it or after the timer expires."
    )
    html = (
        f'<div style="font-family:monospace;background:#050508;color:#e8e8f0;padding:40px;'
        f'border-radius:12px;max-width:500px">'
        f'<h2 style="color:#22d3ee;letter-spacing:3px;margin:0 0 20px">OPENCLAW TRADER</h2>'
        f'<p style="color:#a0a0b0;margin:0 0 20px">You have been granted access to the trading dashboard.</p>'
        f'<a href="{link}" style="display:inline-block;padding:14px 32px;background:transparent;'
        f'border:2px solid #22d3ee;border-radius:10px;color:#22d3ee;text-decoration:none;'
        f'font-weight:bold;letter-spacing:2px;font-family:monospace">ACCESS DASHBOARD</a>'
        f'<p style="color:#666;margin:20px 0 0;font-size:12px">This link expires in {expires_label}. '
        f"One-time use only.</p></div>"
    )

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

    client = get_http()
    resp = await client.post(
        f"{SUPABASE_URL}/rest/v1/magic_link_tokens",
        headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=representation"},
        json={"token_hash": token_hash, "email": email, "expires_at": expires_at.isoformat()},
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=500, detail="Failed to create token")

    host = request.headers.get("host", "openclaw-trader-dash.fly.dev")
    scheme = (
        "https"
        if "fly.dev" in host or "https" in request.headers.get("x-forwarded-proto", "")
        else "http"
    )
    link = f"{scheme}://{host}/auth/link?t={token}"
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
    if not t:
        return RedirectResponse("/login", status_code=302)

    token_hash = hashlib.sha256(t.encode()).hexdigest()
    client = get_http()
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
            '<html><body style="background:#050508;color:#ff3344;font-family:monospace;'
            'display:flex;align-items:center;justify-content:center;height:100vh;text-align:center">'
            "<div><h1 style=\"letter-spacing:4px\">LINK EXPIRED</h1>"
            '<p style="color:#666;margin-top:12px">This link has been used or has expired.</p>'
            '<a href="/login" style="color:#22d3ee;margin-top:20px;display:inline-block">'
            "GO TO LOGIN</a></div></body></html>",
            status_code=403,
        )

    token_row = resp.json()[0]
    token_id = token_row["id"]
    expires_at = token_row["expires_at"]

    if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) < datetime.now(timezone.utc):
        return HTMLResponse(
            '<html><body style="background:#050508;color:#ff3344;font-family:monospace;'
            'display:flex;align-items:center;justify-content:center;height:100vh;text-align:center">'
            "<div><h1 style=\"letter-spacing:4px\">FUSE BURNED</h1>"
            '<p style="color:#666;margin-top:12px">This link has expired.</p>'
            '<a href="/login" style="color:#22d3ee;margin-top:20px;display:inline-block">'
            "GO TO LOGIN</a></div></body></html>",
            status_code=403,
        )

    await client.patch(
        f"{SUPABASE_URL}/rest/v1/magic_link_tokens",
        headers={**sb_headers(), "Content-Type": "application/json"},
        params={"id": f"eq.{token_id}"},
        json={"used_at": datetime.now(timezone.utc).isoformat()},
    )

    session_token = _create_session()
    resp_out = RedirectResponse("/", status_code=302)
    resp_out.set_cookie(
        "oc_session",
        session_token,
        httponly=True,
        samesite="strict",
        secure=True,
        max_age=SESSION_MAX_AGE,
    )
    return resp_out


@app.get("/api/magic-link/list")
async def list_magic_links(request: Request, oc_session: str | None = Cookie(None)):
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
    _require_auth(request, oc_session)
    body = await request.json()
    link_id = _validate_uuid(body.get("id", ""))
    client = get_http()
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/magic_link_tokens",
        headers={**sb_headers(), "Content-Type": "application/json"},
        params={"id": f"eq.{link_id}"},
        json={"revoked": True},
    )
    return {"ok": resp.status_code in (200, 204)}


# ============================================================================
# Static Assets + Page Routes
# ============================================================================


@app.get("/theme.css")
async def theme_css():
    return FileResponse(Path(__file__).parent / "theme.css", media_type="text/css")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
