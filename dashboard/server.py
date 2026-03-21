#!/usr/bin/env python3
"""
OpenClaw Trader Dashboard — Local web UI for monitoring the trading agent.
Serves the dashboard HTML and proxies data from Supabase + Alpaca.

Authentication: password login form with session tokens, rate limiting,
and CSRF protection. No query-param key exposure.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import httpx
from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

app = FastAPI(title="OpenClaw Trader Dashboard", docs_url=None, redoc_url=None)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE = "https://paper-api.alpaca.markets"

# Auth config
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_KEY", "")
if not DASHBOARD_PASSWORD:
    DASHBOARD_PASSWORD = secrets.token_urlsafe(24)
    print(f"[Dashboard] Generated password: {DASHBOARD_PASSWORD}")

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
    ip = request.client.host if request.client else "unknown"

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
    html_path = Path(__file__).parent / "login.html"
    html = html_path.read_text()
    # Inject error and CSRF token
    html = html.replace("<!-- ERROR_PLACEHOLDER -->", f'<div class="error">{error}</div>')
    html = html.replace("CSRF_TOKEN_PLACEHOLDER", csrf)
    return html


# ============================================================================
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/prediction_accuracy",
            headers=sb_headers(),
        )
        if resp.status_code == 200:
            rows = resp.json()
            return rows[0] if rows else {}
        return {}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
