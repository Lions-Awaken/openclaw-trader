#!/usr/bin/env python3
"""
OpenClaw Trader Dashboard — Local web UI for monitoring the trading agent.
Serves the dashboard HTML and proxies data from Supabase + Alpaca.
Authentication via DASHBOARD_KEY cookie or ?key= query param on first visit.
"""

import hashlib
import json
import os
import secrets
from pathlib import Path

import httpx
from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

app = FastAPI(title="OpenClaw Trader Dashboard")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE = "https://paper-api.alpaca.markets"

# Dashboard auth — set DASHBOARD_KEY env var, or a random one is generated on startup
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "")
if not DASHBOARD_KEY:
    DASHBOARD_KEY = secrets.token_urlsafe(24)
    print(f"[Dashboard] Generated auth key: {DASHBOARD_KEY}")
    print(f"[Dashboard] Access at: http://localhost:8090/?key={DASHBOARD_KEY}")

DASHBOARD_KEY_HASH = hashlib.sha256(DASHBOARD_KEY.encode()).hexdigest()


def verify_auth(request: Request, dashboard_auth: str | None = Cookie(None)):
    """Check cookie or query param for dashboard key."""
    # Check cookie first
    if dashboard_auth and hashlib.sha256(dashboard_auth.encode()).hexdigest() == DASHBOARD_KEY_HASH:
        return True
    # Check query param
    key = request.query_params.get("key", "")
    if key and hashlib.sha256(key.encode()).hexdigest() == DASHBOARD_KEY_HASH:
        return True
    return False


def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, response: Response, dashboard_auth: str | None = Cookie(None)):
    key = request.query_params.get("key", "")
    # If key in query param, set cookie and redirect to clean URL
    if key and hashlib.sha256(key.encode()).hexdigest() == DASHBOARD_KEY_HASH:
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("dashboard_auth", key, httponly=True, samesite="strict", max_age=86400 * 30)
        return resp
    if not verify_auth(request, dashboard_auth):
        return HTMLResponse(
            "<html><body style='background:#0a0a0f;color:#6b6b80;font-family:sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'>"
            "<h1 style='color:#22d3ee'>OpenClaw Trader</h1>"
            "<p>Access key required. Append <code>?key=YOUR_KEY</code> to the URL.</p>"
            "</div></body></html>",
            status_code=401,
        )
    return FileResponse(Path(__file__).parent / "index.html")


def require_auth(request: Request, dashboard_auth: str | None):
    if not verify_auth(request, dashboard_auth):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/api/account")
async def get_account(request: Request, dashboard_auth: str | None = Cookie(None)):
    require_auth(request, dashboard_auth)
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
async def get_positions(request: Request, dashboard_auth: str | None = Cookie(None)):
    require_auth(request, dashboard_auth)
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
async def get_trades(request: Request, dashboard_auth: str | None = Cookie(None)):
    require_auth(request, dashboard_auth)
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
async def get_performance(request: Request, dashboard_auth: str | None = Cookie(None)):
    require_auth(request, dashboard_auth)
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
async def get_regime(request: Request, dashboard_auth: str | None = Cookie(None)):
    require_auth(request, dashboard_auth)
    regime_file = Path.home() / ".openclaw/workspace/memory/regime-current.json"
    if regime_file.exists():
        return json.loads(regime_file.read_text())
    return {"regime": "UNKNOWN", "action": "No regime data — run regime.py first"}


@app.get("/api/regime-history")
async def get_regime_history(request: Request, dashboard_auth: str | None = Cookie(None)):
    require_auth(request, dashboard_auth)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
