#!/usr/bin/env python3
"""
Shadow Mark-to-Market — nightly update of open shadow positions.

Runs 6:00 PM PDT weekdays (after market close). Fetches current prices via
yfinance for all open shadow_positions, recalculates P&L, applies exit rules,
and patches Supabase.

Exit rules (checked in order):
  1. time_stop     — trading days since entry >= 10
  2. stop_loss     — current_pnl_pct <= -7.5%
  3. profit_target_2 — current_pnl_pct >= 25.0%
  4. profit_target_1 — current_pnl_pct >= 15.0%
"""

import os
import sys
import traceback
from collections import defaultdict
from datetime import date

import numpy as np

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore[assignment]

sys.path.insert(0, os.path.dirname(__file__))
from common import sb_get, slack_notify
from tracer import (
    PipelineTracer,
    _patch_supabase,
    set_active_tracer,
    traced,
)

TODAY = date.today()

# ---------------------------------------------------------------------------
# Exit thresholds
# ---------------------------------------------------------------------------
TIME_STOP_DAYS: int = 10
STOP_LOSS_PCT: float = -7.5
PROFIT_TARGET_1_PCT: float = 15.0
PROFIT_TARGET_2_PCT: float = 25.0


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------
@traced("shadow_mtm")
def fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Batch-fetch latest close prices for all tickers via yfinance.

    Returns a dict of {ticker: price}. Missing tickers are omitted.
    """
    if not tickers:
        return {}

    if yf is None:
        print("[shadow_mtm] yfinance not installed — cannot fetch prices")
        return {}

    prices: dict[str, float] = {}
    unique = list(set(tickers))

    try:
        raw = yf.download(
            unique,
            period="1d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        # yfinance returns multi-index columns when >1 ticker, flat when exactly 1
        if len(unique) == 1:
            ticker = unique[0]
            close_col = "Close"
            if close_col in raw.columns and not raw.empty:
                val = raw[close_col].iloc[-1]
                if not np.isnan(float(val)):
                    prices[ticker] = round(float(val), 4)
        else:
            # Multi-index: top level is field ("Close", "Open", …), second is ticker
            if "Close" in raw.columns.get_level_values(0):
                close = raw["Close"]
                for ticker in unique:
                    if ticker in close.columns:
                        val = close[ticker].dropna()
                        if not val.empty:
                            prices[ticker] = round(float(val.iloc[-1]), 4)
    except Exception as e:
        print(f"[shadow_mtm] yfinance batch fetch error: {e}")

    # Fallback: individually fetch any ticker that was not covered
    missing = [t for t in unique if t not in prices]
    for ticker in missing:
        try:
            single = yf.download(
                ticker,
                period="1d",
                interval="1d",
                progress=False,
                auto_adjust=True,
            )
            if not single.empty and "Close" in single.columns:
                val = single["Close"].dropna()
                if not val.empty:
                    prices[ticker] = round(float(val.iloc[-1]), 4)
        except Exception as e:
            print(f"[shadow_mtm] yfinance fallback error for {ticker}: {e}")

    print(f"[shadow_mtm] Fetched prices for {len(prices)}/{len(unique)} tickers")
    return prices


# ---------------------------------------------------------------------------
# Trading days calculation
# ---------------------------------------------------------------------------
def trading_days_since(entry_date_str: str) -> int:
    """Number of trading days (weekdays only) from entry_date to today (inclusive)."""
    try:
        entry = date.fromisoformat(entry_date_str)
        # numpy busday_count: end is exclusive, so add 1 to include today
        count = int(np.busday_count(entry.isoformat(), TODAY.isoformat()))
        return max(count, 0)
    except Exception as e:
        print(f"[shadow_mtm] trading_days_since error for {entry_date_str}: {e}")
        return 0


# ---------------------------------------------------------------------------
# Exit rule evaluation
# ---------------------------------------------------------------------------
def evaluate_exit(
    position: dict,
    current_price: float,
) -> tuple[bool, str]:
    """Return (should_close, close_reason) or (False, '') if staying open."""
    entry_price = float(position["entry_price"])
    if entry_price <= 0:
        return False, ""

    current_pnl_pct = ((current_price - entry_price) / entry_price) * 100.0

    # 1. Time stop
    days_held = trading_days_since(position["entry_date"])
    if days_held >= TIME_STOP_DAYS:
        return True, "time_stop"

    # 2. Stop loss
    if current_pnl_pct <= STOP_LOSS_PCT:
        return True, "stop_loss"

    # 3. Profit target 2 (higher threshold wins)
    if current_pnl_pct >= PROFIT_TARGET_2_PCT:
        return True, "profit_target_2"

    # 4. Profit target 1
    if current_pnl_pct >= PROFIT_TARGET_1_PCT:
        return True, "profit_target_1"

    return False, ""


# ---------------------------------------------------------------------------
# Supabase PATCH helpers
# ---------------------------------------------------------------------------
def patch_position_open(position_id: str, update: dict) -> bool:
    """PATCH an open shadow_position (live mark-to-market update, no exit)."""
    return _patch_supabase("shadow_positions", position_id, update)


def patch_position_close(position_id: str, update: dict) -> bool:
    """PATCH a shadow_position to closed status."""
    return _patch_supabase("shadow_positions", position_id, update)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------
@traced("shadow_mtm")
def load_open_positions() -> list[dict]:
    """Fetch all open shadow positions from Supabase."""
    rows = sb_get("shadow_positions", {
        "select": "*",
        "status": "eq.open",
    })
    print(f"[shadow_mtm] Found {len(rows)} open shadow positions")
    return rows


@traced("shadow_mtm")
def process_positions(
    positions: list[dict],
    prices: dict[str, float],
) -> dict:
    """Mark-to-market all positions and apply exit rules.

    Returns a summary dict for logging and Slack notification.
    """
    updated_count = 0
    closed_count = 0
    price_miss_count = 0
    close_reasons: dict[str, int] = defaultdict(int)

    for pos in positions:
        ticker = pos["ticker"]
        position_id = pos["id"]
        entry_price = float(pos["entry_price"])
        position_size_shares = float(pos.get("position_size_shares") or 0)
        existing_peak = float(pos.get("peak_pnl_pct") or 0)

        current_price = prices.get(ticker)
        if current_price is None or current_price <= 0:
            price_miss_count += 1
            print(f"[shadow_mtm] No price for {ticker} — skipping")
            continue

        if entry_price <= 0:
            print(f"[shadow_mtm] Invalid entry_price for {ticker} (id={position_id}) — skipping")
            continue

        # Recalculate P&L
        current_pnl = (current_price - entry_price) * position_size_shares
        current_pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
        peak_pnl_pct = max(existing_peak, current_pnl_pct)

        should_close, close_reason = evaluate_exit(pos, current_price)

        if should_close:
            shadow_was_right = current_pnl > 0
            ok = patch_position_close(position_id, {
                "status": "closed",
                "current_price": round(current_price, 4),
                "current_pnl": round(current_pnl, 4),
                "current_pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(peak_pnl_pct, 4),
                "exit_date": TODAY.isoformat(),
                "exit_price": round(current_price, 4),
                "final_pnl": round(current_pnl, 4),
                "final_pnl_pct": round(current_pnl_pct, 4),
                "close_reason": close_reason,
                "shadow_was_right": shadow_was_right,
            })
            if ok:
                closed_count += 1
                close_reasons[close_reason] += 1
                print(
                    f"[shadow_mtm] Closed {ticker} ({pos['shadow_profile']}) "
                    f"reason={close_reason} pnl_pct={current_pnl_pct:.2f}%"
                )
            else:
                print(f"[shadow_mtm] PATCH failed (close) for {ticker} id={position_id}")
        else:
            ok = patch_position_open(position_id, {
                "current_price": round(current_price, 4),
                "current_pnl": round(current_pnl, 4),
                "current_pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(peak_pnl_pct, 4),
            })
            if ok:
                updated_count += 1
            else:
                print(f"[shadow_mtm] PATCH failed (update) for {ticker} id={position_id}")

    return {
        "total": len(positions),
        "updated": updated_count,
        "closed": closed_count,
        "price_misses": price_miss_count,
        "close_reasons": dict(close_reasons),
    }


# ---------------------------------------------------------------------------
# Slack summary
# ---------------------------------------------------------------------------
def build_slack_message(summary: dict) -> str:
    total = summary["total"]
    updated = summary["updated"]
    closed = summary["closed"]
    price_misses = summary["price_misses"]
    close_reasons = summary["close_reasons"]
    today_str = TODAY.isoformat()

    if total == 0:
        return f"*Shadow MTM* ({today_str}) — no open shadow positions to update"

    reasons_str = ""
    if close_reasons:
        parts = [f"`{reason}` x{count}" for reason, count in sorted(close_reasons.items())]
        reasons_str = " · " + " · ".join(parts)

    msg = (
        f"*Shadow MTM* ({today_str}) — "
        f"`{total}` positions · `{updated}` updated · `{closed}` closed{reasons_str}"
    )
    if price_misses:
        msg += f" · `{price_misses}` price misses"
    return msg


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run() -> None:
    tracer = PipelineTracer(
        "shadow_mark_to_market",
        metadata={"run_date": TODAY.isoformat()},
    )
    set_active_tracer(tracer)

    try:
        # Step 1: Load open positions
        with tracer.step("load_positions") as result:
            positions = load_open_positions()
            result.set({"count": len(positions)})

        if not positions:
            tracer.complete({"positions": 0, "updated": 0, "closed": 0})
            slack_notify(
                f"*Shadow MTM* ({TODAY.isoformat()}) — no open shadow positions to update"
            )
            print("[shadow_mtm] No open positions. Done.")
            return

        # Step 2: Fetch prices (batch via yfinance)
        tickers = [p["ticker"] for p in positions]
        with tracer.step("fetch_prices") as result:
            prices = fetch_prices(tickers)
            result.set({"tickers_requested": len(set(tickers)), "prices_fetched": len(prices)})

        # Step 3: Mark-to-market + apply exit rules
        with tracer.step("process_positions") as result:
            summary = process_positions(positions, prices)
            result.set(summary)

        tracer.complete(summary)

        slack_msg = build_slack_message(summary)
        slack_notify(slack_msg)
        print(f"[shadow_mtm] {slack_msg}")

    except Exception as e:
        tb = traceback.format_exc()
        tracer.fail(str(e), tb)
        print(f"[shadow_mtm] FATAL: {e}\n{tb}")
        slack_notify(f"*Shadow MTM FATAL*: {e}")
        raise


if __name__ == "__main__":
    run()
