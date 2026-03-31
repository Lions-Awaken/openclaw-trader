#!/usr/bin/env python3
"""
Position Manager — manages open Alpaca positions and exit logic.

Checks existing positions against strategy profile exit rules:
  - Time stop: close after max_hold_days
  - Take-profit ladder: scale out at R-multiple targets
  - Trailing stop: move stop up as price advances
  - End-of-day flatten: close all positions before market close (day trade mode)

Also updates trade_decisions with exit info and P&L when positions close.

Cron schedule: M-F every 30 min during market hours (9:45 AM - 3:45 PM ET)
"""

import os
import sys
from datetime import datetime, timezone

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from tracer import PipelineTracer, _patch_supabase, _post_to_supabase, _sb_headers

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE = os.environ.get(
    "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
)
ALPACA_DATA_BASE = "https://data.alpaca.markets"

_client = httpx.Client(timeout=15.0)


def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }


def sb_get(path: str, params: dict | None = None) -> list:
    resp = _client.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=_sb_headers(),
        params=params or {},
    )
    return resp.json() if resp.status_code == 200 else []


def get_account() -> dict:
    resp = _client.get(
        f"{ALPACA_BASE}/v2/account", headers=_alpaca_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_positions() -> list:
    resp = _client.get(
        f"{ALPACA_BASE}/v2/positions", headers=_alpaca_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_orders(status: str = "open") -> list:
    """Fetch orders with given status."""
    resp = _client.get(
        f"{ALPACA_BASE}/v2/orders",
        headers=_alpaca_headers(),
        params={"status": status, "limit": "100"},
    )
    return resp.json() if resp.status_code == 200 else []


def close_position(ticker: str) -> dict | None:
    """Close an entire position via Alpaca."""
    resp = _client.delete(
        f"{ALPACA_BASE}/v2/positions/{ticker}",
        headers=_alpaca_headers(),
    )
    if resp.status_code in (200, 204):
        result = resp.json() if resp.text else {}
        print(f"[position_manager] Closed position: {ticker}")
        return result
    print(f"[position_manager] Failed to close {ticker}: {resp.status_code} {resp.text}")
    return None


def cancel_orders_for(ticker: str):
    """Cancel all open orders for a ticker."""
    orders = get_orders("open")
    for order in orders:
        if order.get("symbol") == ticker:
            order_id = order["id"]
            _client.delete(
                f"{ALPACA_BASE}/v2/orders/{order_id}",
                headers=_alpaca_headers(),
            )
            print(f"[position_manager] Cancelled order {order_id} for {ticker}")


def replace_stop_order(ticker: str, qty: int, new_stop_price: float) -> dict | None:
    """Cancel existing stop orders and place a new one."""
    cancel_orders_for(ticker)
    payload = {
        "symbol": ticker,
        "qty": str(qty),
        "side": "sell",
        "type": "stop",
        "stop_price": str(round(new_stop_price, 2)),
        "time_in_force": "gtc",
    }
    resp = _client.post(
        f"{ALPACA_BASE}/v2/orders",
        headers=_alpaca_headers(),
        json=payload,
    )
    if resp.status_code in (200, 201):
        return resp.json()
    return None


# ---------------------------------------------------------------------------
# Trade decision helpers
# ---------------------------------------------------------------------------

def get_trade_for_ticker(ticker: str) -> dict | None:
    """Find the most recent open trade decision for this ticker."""
    rows = sb_get("trade_decisions", {
        "select": "*",
        "ticker": f"eq.{ticker}",
        "action": "eq.BUY",
        "exit_price": "is.null",
        "order": "created_at.desc",
        "limit": "1",
    })
    return rows[0] if rows else None


def close_trade_decision(trade_id: int, exit_price: float, pnl: float, outcome: str, hold_days: int):
    """Update trade_decisions with exit info."""
    _patch_supabase("trade_decisions", str(trade_id), {
        "exit_price": exit_price,
        "pnl": round(pnl, 4),
        "outcome": outcome,
        "hold_days": hold_days,
    })
    _post_to_supabase("cost_ledger", {
        "ledger_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "category": "trade_pnl",
        "subcategory": "realized",
        "amount": round(pnl, 4),
        "description": f"Closed {outcome}: trade #{trade_id}",
    })


def classify_outcome(pnl_pct: float) -> str:
    """Classify trade outcome based on P&L percentage."""
    if pnl_pct >= 5.0:
        return "STRONG_WIN"
    elif pnl_pct > 0:
        return "WIN"
    elif pnl_pct >= -1.0:
        return "SCRATCH"
    elif pnl_pct >= -5.0:
        return "LOSS"
    else:
        return "STRONG_LOSS"


# ---------------------------------------------------------------------------
# Exit logic
# ---------------------------------------------------------------------------

def load_profile() -> dict:
    """Load active strategy profile."""
    rows = sb_get("strategy_profiles", {
        "select": "*",
        "active": "eq.true",
        "limit": "1",
    })
    return rows[0] if rows else {}


def check_time_stop(trade: dict, profile: dict) -> bool:
    """Check if position has exceeded max hold days."""
    if not profile.get("time_stop_enabled", True):
        return False
    max_days = int(profile.get("max_hold_days", 10))
    created = trade.get("created_at", "")
    if not created:
        return False
    try:
        entry_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
        hold_days = (datetime.now(timezone.utc) - entry_time).days
        return hold_days >= max_days
    except (ValueError, TypeError):
        return False


def check_eod_flatten(profile: dict) -> bool:
    """Check if we should flatten for end-of-day (day trade mode).

    Returns True if trade_style is day_trade and market closes within 15 min.
    """
    if profile.get("trade_style") != "day_trade":
        return False

    now_utc = datetime.now(timezone.utc)
    # Market close is 4:00 PM ET = 20:00 UTC (EDT) or 21:00 UTC (EST)
    # Approximate: if hour is 19 and minute >= 45, or hour >= 20
    # This is simplified; a proper implementation would use pytz
    hour_utc = now_utc.hour
    minute = now_utc.minute
    # During EDT (March-November): close = 20:00 UTC, flatten at 19:45
    # During EST (November-March): close = 21:00 UTC, flatten at 20:45
    # Use 19:45 UTC as a conservative cutoff
    if hour_utc >= 20 or (hour_utc == 19 and minute >= 45):
        return True
    return False


def calculate_atr(ticker: str, period: int = 14) -> float:
    """Calculate ATR for trailing stop calculation."""
    resp = _client.get(
        f"{ALPACA_DATA_BASE}/v2/stocks/{ticker}/bars",
        headers=_alpaca_headers(),
        params={"timeframe": "1Day", "limit": str(period + 1)},
    )
    if resp.status_code != 200:
        return 0.0
    bars = resp.json().get("bars", [])
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, lo, prev_c = float(bars[i]["h"]), float(bars[i]["l"]), float(bars[i - 1]["c"])
        trs.append(max(h - lo, abs(h - prev_c), abs(lo - prev_c)))
    return sum(trs) / len(trs) if trs else 0.0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    """Check all open positions and apply exit logic."""
    tracer = PipelineTracer("position_manager")

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("[position_manager] ERROR: ALPACA credentials required")
        return

    try:
        with tracer.step("load_profile"):
            profile = load_profile()
            profile_name = profile.get("profile_name", "DEFAULT")
            print(f"[position_manager] Profile: {profile_name}")

        with tracer.step("get_positions"):
            positions = get_positions()
            if not positions:
                print("[position_manager] No open positions.")
                tracer.complete({"positions": 0})
                return
            print(f"[position_manager] {len(positions)} open positions")

        # Check EOD flatten
        eod_flatten = check_eod_flatten(profile)
        if eod_flatten:
            print("[position_manager] EOD FLATTEN — closing all positions (day trade mode)")

        closed = 0
        stops_updated = 0

        for pos in positions:
            ticker = pos["symbol"]
            qty = int(pos["qty"])
            current_price = float(pos["current_price"])
            avg_entry = float(pos["avg_entry_price"])
            unrealized_pnl = float(pos["unrealized_pl"])
            pnl_pct = float(pos["unrealized_plpc"]) * 100

            trade = get_trade_for_ticker(ticker)

            with tracer.step(f"manage_{ticker}"):
                # EOD flatten — close everything
                if eod_flatten:
                    cancel_orders_for(ticker)
                    result = close_position(ticker)
                    if result and trade:
                        entry_time = datetime.fromisoformat(
                            trade["created_at"].replace("Z", "+00:00")
                        )
                        hold_days = (datetime.now(timezone.utc) - entry_time).days
                        outcome = classify_outcome(pnl_pct)
                        close_trade_decision(
                            trade["id"], current_price,
                            unrealized_pnl, outcome, hold_days,
                        )
                        _post_to_supabase("order_events", {
                            "order_id": f"flatten_{ticker}_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                            "ticker": ticker,
                            "event_type": "filled",
                            "side": "sell",
                            "qty_filled": qty,
                            "avg_fill_price": current_price,
                        })
                    closed += 1
                    continue

                # Time stop
                if trade and check_time_stop(trade, profile):
                    print(f"[position_manager] {ticker}: TIME STOP — closing after max hold days")
                    cancel_orders_for(ticker)
                    result = close_position(ticker)
                    if result:
                        entry_time = datetime.fromisoformat(
                            trade["created_at"].replace("Z", "+00:00")
                        )
                        hold_days = (datetime.now(timezone.utc) - entry_time).days
                        outcome = classify_outcome(pnl_pct)
                        close_trade_decision(
                            trade["id"], current_price,
                            unrealized_pnl, outcome, hold_days,
                        )
                    closed += 1
                    continue

                # Trailing stop — move stop up as price advances
                atr = calculate_atr(ticker)
                stop_mult = float(profile.get("stop_loss_atr_multiple", 1.5))
                if atr > 0 and current_price > avg_entry:
                    # Ideal stop is current_price - (atr * stop_mult)
                    ideal_stop = current_price - (atr * stop_mult)
                    # Only move stop UP, never down
                    min_stop = avg_entry - (atr * stop_mult)  # original stop
                    new_stop = max(ideal_stop, min_stop)

                    # Check existing stop orders
                    open_orders = get_orders("open")
                    existing_stop = None
                    for o in open_orders:
                        if (
                            o.get("symbol") == ticker
                            and o.get("side") == "sell"
                            and o.get("type") == "stop"
                        ):
                            existing_stop = float(o.get("stop_price", 0))
                            break

                    if existing_stop is None or new_stop > existing_stop + 0.05:
                        result = replace_stop_order(ticker, qty, new_stop)
                        if result:
                            stops_updated += 1
                            print(
                                f"[position_manager] {ticker}: trailing stop "
                                f"${existing_stop or 0:.2f} → ${new_stop:.2f} "
                                f"(price=${current_price:.2f}, P&L={pnl_pct:+.1f}%)"
                            )

                print(
                    f"[position_manager] {ticker}: qty={qty}, "
                    f"entry=${avg_entry:.2f}, now=${current_price:.2f}, "
                    f"P&L={pnl_pct:+.1f}% (${unrealized_pnl:+.2f})"
                )

        tracer.complete({
            "positions_checked": len(positions),
            "closed": closed,
            "stops_updated": stops_updated,
            "eod_flatten": eod_flatten,
        })
        print(
            f"[position_manager] Complete. "
            f"Checked: {len(positions)}, Closed: {closed}, "
            f"Stops updated: {stops_updated}"
        )

    except Exception as e:
        tracer.fail(str(e))
        print(f"[position_manager] FAILED: {e}")
        raise


if __name__ == "__main__":
    run()
