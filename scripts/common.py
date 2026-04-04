#!/usr/bin/env python3
"""
Common — shared config, HTTP clients, and helpers for all OpenClaw scripts.

Centralizes env vars, Alpaca API helpers, Supabase helpers, and Ollama
embedding calls. Eliminates duplication across scanner, position_manager,
inference_engine, catalyst_ingest, calibrator, meta_daily, meta_weekly,
and post_trade_analysis.
"""

import atexit
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
from tracer import traced

# ==========================================================================
# Sentry — initialize before anything else so all errors are captured
# ==========================================================================
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=0,
            send_default_pii=False,
            environment="paper",
        )
    except ImportError:
        pass

# ==========================================================================
# Config — env vars loaded once, shared by all importers
# ==========================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_API_KEY_2 = os.environ.get("ANTHROPIC_API_KEY_2", "")
PERPLEXITY_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C0ANK2A0M7G")  # #all-lions-awaken

ALPACA_PAPER = "https://paper-api.alpaca.markets"
ALPACA_DATA = "https://data.alpaca.markets"


# ==========================================================================
# HTTP Clients — reusable, closed at exit
# ==========================================================================
_client = httpx.Client(timeout=15.0)
_claude_client = httpx.Client(timeout=45.0)
atexit.register(_client.close)
atexit.register(_claude_client.close)


# ==========================================================================
# Supabase helpers
# ==========================================================================
def sb_headers() -> dict:
    """Standard Supabase REST headers."""
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def sb_get(table: str, params: dict | None = None) -> list:
    """GET from Supabase REST API."""
    try:
        resp = _client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=sb_headers(),
            params=params or {},
        )
        return resp.json() if resp.status_code == 200 else []
    except Exception:
        return []


def sb_rpc(fn_name: str, params: dict) -> list:
    """Call a Supabase RPC function."""
    try:
        resp = _client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{fn_name}",
            headers=sb_headers(),
            json=params,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return []


# ==========================================================================
# Alpaca helpers
# ==========================================================================
def alpaca_headers() -> dict:
    """Standard Alpaca REST headers."""
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


@traced("sitrep")
def check_market_open() -> tuple[bool, str]:
    """Check Alpaca /v2/clock to see if market is currently open."""
    try:
        resp = _client.get(
            f"{ALPACA_PAPER}/v2/clock",
            headers=alpaca_headers(),
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("is_open"):
                return True, "market_open"
            return False, f"market_closed_until_{data.get('next_open', 'unknown')}"
    except Exception as e:
        return False, f"clock_check_failed_{e}"
    return False, "clock_check_failed"


@traced("sitrep")
def get_account() -> dict | None:
    """GET /v2/account from Alpaca paper."""
    try:
        resp = _client.get(
            f"{ALPACA_PAPER}/v2/account",
            headers=alpaca_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[alpaca] Account error: {e}")
    return None


@traced("positions")
def get_positions() -> list:
    """GET /v2/positions from Alpaca paper."""
    try:
        resp = _client.get(
            f"{ALPACA_PAPER}/v2/positions",
            headers=alpaca_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[alpaca] Positions error: {e}")
    return []


@traced("positions")
def get_open_orders() -> list:
    """GET /v2/orders?status=open from Alpaca paper."""
    try:
        resp = _client.get(
            f"{ALPACA_PAPER}/v2/orders",
            headers=alpaca_headers(),
            params={"status": "open"},
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[alpaca] Orders error: {e}")
    return []


def get_bars(ticker: str, days: int = 60) -> list:
    """GET /v2/stocks/{ticker}/bars — 1Day bars for the last N days."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        resp = _client.get(
            f"{ALPACA_DATA}/v2/stocks/{ticker}/bars",
            headers=alpaca_headers(),
            params={
                "timeframe": "1Day",
                "start": start.strftime("%Y-%m-%dT00:00:00Z"),
                "end": end.strftime("%Y-%m-%dT23:59:59Z"),
                "limit": "200",
                "adjustment": "split",
                "feed": "iex",
            },
        )
        if resp.status_code == 200:
            return resp.json().get("bars", [])
        else:
            print(f"[alpaca] Bars HTTP {resp.status_code} for {ticker}: {resp.text[:200]}")
    except Exception as e:
        print(f"[alpaca] Bars error for {ticker}: {e}")
    return []


def get_latest_quote(ticker: str) -> dict:
    """GET /v2/stocks/{ticker}/quotes/latest from Alpaca data API."""
    try:
        resp = _client.get(
            f"{ALPACA_DATA}/v2/stocks/{ticker}/quotes/latest",
            headers=alpaca_headers(),
            params={"feed": "iex"},
        )
        if resp.status_code == 200:
            q = resp.json().get("quote", {})
            bid = float(q.get("bp", 0))
            ask = float(q.get("ap", 0))
            mid = round((bid + ask) / 2, 2) if bid and ask else 0
            return {"price": mid, "bid": bid, "ask": ask}
    except Exception as e:
        print(f"[alpaca] Quote error for {ticker}: {e}")
    return {"price": 0, "bid": 0, "ask": 0}


@traced("trades")
def submit_order(
    ticker: str,
    qty: int,
    side: str,
    order_type: str = "market",
    time_in_force: str = "day",
    stop_price: float | None = None,
) -> dict | None:
    """POST /v2/orders to Alpaca paper."""
    body: dict = {
        "symbol": ticker,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
    }
    if stop_price is not None:
        body["stop_price"] = str(round(stop_price, 2))

    try:
        resp = _client.post(
            f"{ALPACA_PAPER}/v2/orders",
            headers={**alpaca_headers(), "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code in (200, 201):
            return resp.json()
        else:
            print(f"[alpaca] Order rejected for {ticker}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[alpaca] Order submit error for {ticker}: {e}")
    return None


@traced("trades")
def cancel_order(order_id: str) -> bool:
    """DELETE /v2/orders/{order_id}."""
    try:
        resp = _client.delete(
            f"{ALPACA_PAPER}/v2/orders/{order_id}",
            headers=alpaca_headers(),
        )
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"[alpaca] Cancel order error: {e}")
    return False


@traced("trades")
def poll_for_fill(order_id: str, timeout_seconds: int = 120) -> dict | None:
    """Poll Alpaca for order fill status. Returns final order state or None on timeout."""
    TERMINAL = {"filled", "partially_filled", "cancelled", "rejected", "expired", "done_for_day"}
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            resp = _client.get(
                f"{ALPACA_PAPER}/v2/orders/{order_id}",
                headers=alpaca_headers(),
                timeout=5.0,
            )
            if resp.status_code == 200:
                order = resp.json()
                if order.get("status") in TERMINAL:
                    return order
        except Exception as e:
            print(f"[alpaca] Fill poll error: {e}")
        time.sleep(4)
    print(f"[alpaca] Fill poll timeout for order {order_id}")
    return None


# ==========================================================================
# Ollama embedding
# ==========================================================================
def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding via Ollama nomic-embed-text."""
    try:
        resp = _client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text, "keep_alive": "0"},
            timeout=60.0,
        )
        if resp.status_code == 200:
            return resp.json().get("embedding")
    except Exception:
        pass
    return None


# ==========================================================================
# Strategy profile
# ==========================================================================
def load_strategy_profile() -> dict:
    """Load active strategy profile from Supabase."""
    rows = sb_get("strategy_profiles", {
        "select": "*",
        "active": "eq.true",
        "limit": "1",
    })
    if rows:
        profile = rows[0]
        print(f"[common] Active profile: {profile.get('profile_name', '?')}")
        return profile
    print("[common] No active profile found, using defaults")
    return {
        "profile_name": "DEFAULT",
        "min_signal_score": 4,
        "min_confidence": 0.60,
        "max_concurrent_positions": 5,
        "max_hold_days": 3,
        "position_size_method": "atr",
        "trade_style": "swing",
        "circuit_breakers_enabled": True,
        "bypass_regime_gate": False,
        "auto_execute_all": False,
    }


# ==========================================================================
# Slack notifications
# ==========================================================================
def slack_notify(text: str, thread_ts: str | None = None) -> bool:
    """Post a message to the configured Slack channel. Returns True on success."""
    if not SLACK_BOT_TOKEN:
        return False
    try:
        payload = {
            "channel": SLACK_CHANNEL,
            "text": text,
            "unfurl_links": False,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        resp = _client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json=payload,
            timeout=5.0,
        )
        return resp.status_code == 200 and resp.json().get("ok", False)
    except Exception:
        return False
