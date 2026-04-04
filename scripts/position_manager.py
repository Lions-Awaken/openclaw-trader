#!/usr/bin/env python3
"""
Position Manager — manages open position lifecycle.

Runs every 30 minutes during market hours (9:45 AM – 3:45 PM ET) via cron.
Also runs at 3:45 PM for EOD flatten of day-trade positions.

Responsibilities:
  - Trailing stop adjustment (only moves stops UP, never down)
  - Time stop: close positions held longer than max_hold_days
  - EOD flatten: close day-trade positions before market close
  - On every close: log to order_events, patch trade_decisions, log P&L,
    trigger post_trade_analysis.py

Uses httpx for ALL Alpaca calls (not alpaca-py SDK).
"""

import os
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    cancel_order,
    get_bars,
    get_open_orders,
    get_positions,
    load_strategy_profile,
    poll_for_fill,
    sb_get,
    slack_notify,
    submit_order,
)
from tracer import (
    PipelineTracer,
    _patch_supabase,
    _post_to_supabase,
    set_active_tracer,
    traced,
)

TODAY = date.today().isoformat()


@traced("trades")
def find_trade_decision(ticker: str) -> dict | None:
    """Find the open trade_decisions row for a ticker (no exit_price yet)."""
    rows = sb_get("trade_decisions", {
        "select": "*",
        "ticker": f"eq.{ticker}",
        "exit_price": "is.null",
        "order": "created_at.desc",
        "limit": "1",
    })
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# ATR computation
# ---------------------------------------------------------------------------
def compute_atr(bars: list, period: int = 14) -> float:
    """Compute ATR from bar data."""
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(max(1, len(bars) - period), len(bars)):
        h = float(bars[i]["h"])
        lo = float(bars[i]["l"])
        pc = float(bars[i - 1]["c"])
        tr = max(h - lo, abs(h - pc), abs(lo - pc))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


# ---------------------------------------------------------------------------
# Position close
# ---------------------------------------------------------------------------
@traced("trades")
def close_position(
    ticker: str,
    qty: int,
    current_price: float,
    reason: str,
    trade_decision: dict | None,
    open_orders: list,
    tracer: PipelineTracer,
):
    """Close a position: cancel related orders, sell, update records."""
    print(f"[position_mgr] Closing {ticker} ({qty} shares) — reason: {reason}")

    # Cancel any open stop/limit orders for this ticker
    for order in open_orders:
        if order.get("symbol") == ticker and order.get("side") == "sell":
            cancel_order(order["id"])
            print(f"[position_mgr]   Cancelled order {order['id'][:8]}...")

    # Submit market sell order
    sell_order = submit_order(ticker, qty, "sell", "market", "day")
    if not sell_order:
        print(f"[position_mgr] FAILED to close {ticker} — sell order rejected")
        return

    sell_order_id = sell_order.get("id", "")
    tracer.log_order_event(
        order_id=sell_order_id,
        ticker=ticker,
        event_type="submitted",
        side="sell",
        qty_ordered=qty,
        price=current_price,
        raw_event=sell_order,
    )

    # Poll for fill — market sells typically fill in seconds
    fill = poll_for_fill(sell_order_id, timeout_seconds=120)
    if fill:
        fill_status = fill.get("status", "unknown")
        filled_qty = float(fill.get("filled_qty", 0) or 0)
        avg_price = float(fill.get("filled_avg_price", 0) or 0)
        tracer.log_order_event(
            order_id=sell_order_id,
            ticker=ticker,
            event_type="filled" if fill_status == "filled" else fill_status,
            side="sell",
            qty_ordered=qty,
            qty_filled=filled_qty,
            avg_fill_price=avg_price if avg_price > 0 else None,
            raw_event=fill,
        )
        if avg_price > 0:
            current_price = avg_price  # Use actual fill price for P&L
        print(f"[position_mgr] {ticker} fill: {filled_qty} @ ${avg_price:.2f} ({fill_status})")
    else:
        print(f"[position_mgr] {ticker}: fill poll timed out, using quote price")
        tracer.log_order_event(
            order_id=sell_order_id,
            ticker=ticker,
            event_type="poll_timeout",
            side="sell",
            qty_ordered=qty,
            raw_event={"reason": "poll_for_fill timed out after 120s"},
        )

    # Update trade_decisions
    if trade_decision and trade_decision.get("id"):
        entry_price = float(trade_decision.get("entry_price", 0))
        pnl = (current_price - entry_price) * qty
        pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

        # Classify outcome
        if pnl_pct >= 5:
            outcome = "STRONG_WIN"
        elif pnl_pct >= 1:
            outcome = "WIN"
        elif pnl_pct >= -1:
            outcome = "SCRATCH"
        elif pnl_pct >= -3:
            outcome = "LOSS"
        else:
            outcome = "STRONG_LOSS"

        _patch_supabase("trade_decisions", trade_decision["id"], {
            "exit_price": round(current_price, 4),
            "exit_order_id": sell_order_id,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 3),
            "outcome": outcome,
            "exit_reason": reason,
            "exited_at": datetime.now(timezone.utc).isoformat(),
        })

        # Log P&L to cost_ledger
        _post_to_supabase("cost_ledger", {
            "ledger_date": TODAY,
            "category": "trade_pnl",
            "subcategory": f"trade_{ticker}",
            "amount": round(pnl, 2),
            "description": f"{ticker}: {outcome} ({pnl_pct:+.1f}%) — {reason}",
            "metadata": {
                "ticker": ticker,
                "entry_price": entry_price,
                "exit_price": current_price,
                "qty": qty,
                "hold_days": trade_decision.get("hold_days", 0),
            },
            "pipeline_run_id": tracer.root_id,
        })

        print(f"[position_mgr] {ticker}: {outcome} ({pnl_pct:+.1f}%, ${pnl:+.2f})")
        slack_notify(f"*Position closed* `{ticker}` — {outcome} ({pnl_pct:+.1f}%, ${pnl:+.2f}) | Reason: {reason}")

        # Trigger post-trade analysis (async subprocess)
        entry_date = trade_decision.get("created_at", "")[:10] or TODAY
        hold_days = (date.today() - date.fromisoformat(entry_date)).days if entry_date else 1
        chain_id = trade_decision.get("inference_chain_id", "")

        try:
            cmd = [
                sys.executable, os.path.join(os.path.dirname(__file__), "post_trade_analysis.py"),
                "--ticker", ticker,
                "--entry", str(entry_price),
                "--exit", str(current_price),
                "--hold_days", str(max(1, hold_days)),
            ]
            if chain_id:
                cmd.extend(["--chain_id", chain_id])
            if entry_date:
                cmd.extend(["--entry_date", entry_date])
            cmd.extend(["--pipeline_run_id", tracer.root_id])

            log_dir = os.path.expanduser("~/.openclaw/workspace/logs")
            os.makedirs(log_dir, exist_ok=True)
            log_file = open(f"{log_dir}/post_trade_{ticker}_{TODAY}.log", "a")
            subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
            print(f"[position_mgr] Triggered post_trade_analysis for {ticker}")
        except Exception as e:
            print(f"[position_mgr] post_trade_analysis launch failed: {e}")
    else:
        print(f"[position_mgr] No trade_decisions row found for {ticker} — logging order only")


# ---------------------------------------------------------------------------
# Trailing stop management
# ---------------------------------------------------------------------------
@traced("positions")
def manage_trailing_stop(
    ticker: str,
    current_price: float,
    atr: float,
    existing_stop_orders: list,
    tracer: PipelineTracer,
) -> bool:
    """Adjust trailing stop upward if warranted. Returns True if adjusted."""
    ideal_stop = round(current_price - (atr * 2.0), 2)
    if ideal_stop <= 0:
        return False

    # Find current stop order for this ticker
    current_stop = None
    for order in existing_stop_orders:
        if order.get("symbol") == ticker and order.get("side") == "sell" and order.get("type") == "stop":
            current_stop = order
            break

    if not current_stop:
        # No existing stop — place one
        qty = None
        # Get position qty
        positions = get_positions()
        for p in positions:
            if p.get("symbol") == ticker:
                qty = int(float(p.get("qty", 0)))
                break
        if qty and qty > 0:
            print(f"[position_mgr] {ticker}: placing missing stop @ ${ideal_stop:.2f}")
            stop_order = submit_order(ticker, qty, "sell", "stop", "gtc", stop_price=ideal_stop)
            if stop_order:
                tracer.log_order_event(
                    order_id=stop_order.get("id", ""),
                    ticker=ticker,
                    event_type="submitted",
                    side="sell",
                    qty_ordered=qty,
                    price=ideal_stop,
                    raw_event=stop_order,
                )
            return True
        return False

    current_stop_price = float(current_stop.get("stop_price", 0))
    qty = int(current_stop.get("qty", 0))

    # Only move stops UP, never down
    if ideal_stop <= current_stop_price:
        return False

    # Cancel old stop, place new one
    print(f"[position_mgr] {ticker}: raising stop ${current_stop_price:.2f} → ${ideal_stop:.2f}")
    cancel_order(current_stop["id"])
    time.sleep(0.2)  # Brief pause after cancel

    stop_order = submit_order(ticker, qty, "sell", "stop", "gtc", stop_price=ideal_stop)
    if stop_order:
        tracer.log_order_event(
            order_id=stop_order.get("id", ""),
            ticker=ticker,
            event_type="submitted",
            side="sell",
            qty_ordered=qty,
            price=ideal_stop,
            raw_event={"action": "trailing_stop_raised", "old_stop": current_stop_price, "new_stop": ideal_stop},
        )
    return True


# ---------------------------------------------------------------------------
# Main position management run
# ---------------------------------------------------------------------------
def run():
    """Evaluate and manage all open positions."""
    print(f"\n[position_mgr] Starting position check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    tracer = PipelineTracer("position_manager", metadata={"date": TODAY})
    set_active_tracer(tracer)

    try:
        # === Load profile ===
        with tracer.step("load_profile") as result:
            profile = load_strategy_profile()
            max_hold = int(profile.get("max_hold_days", 3))
            trade_style = profile.get("trade_style", "swing")
            result.set({"profile": profile.get("profile_name", "?"), "max_hold": max_hold, "style": trade_style})

        # === Get positions and orders ===
        with tracer.step("fetch_state") as result:
            positions = get_positions()
            open_orders = get_open_orders()
            result.set({"positions": len(positions), "open_orders": len(open_orders)})

        if not positions:
            print("[position_mgr] No open positions")
            tracer.complete({"positions": 0, "actions": []})
            return

        # Build stop orders lookup
        stop_orders = [o for o in open_orders if o.get("type") == "stop" and o.get("side") == "sell"]

        # Check current time for EOD flatten
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        is_eod = now_et.hour == 15 and now_et.minute >= 45

        # === Process each position ===
        actions = []
        with tracer.step("evaluate_positions", input_snapshot={
            "tickers": [p.get("symbol") for p in positions],
            "is_eod": is_eod,
        }) as result:
            for position in positions:
                ticker = position.get("symbol", "")
                qty = int(float(position.get("qty", 0)))
                current_price = float(position.get("current_price", 0))
                avg_entry = float(position.get("avg_entry_price", 0))
                unrealized_pnl_pct = float(position.get("unrealized_plpc", 0)) * 100

                print(f"\n[position_mgr] {ticker}: {qty} shares, "
                      f"entry=${avg_entry:.2f}, current=${current_price:.2f}, "
                      f"P&L={unrealized_pnl_pct:+.1f}%")

                # Look up trade_decisions row
                trade_decision = find_trade_decision(ticker)
                entry_date_str = ""
                if trade_decision:
                    entry_date_str = trade_decision.get("created_at", "")[:10]

                # Calculate hold days
                hold_days = 0
                if entry_date_str:
                    try:
                        entry_date = date.fromisoformat(entry_date_str)
                        hold_days = (date.today() - entry_date).days
                    except ValueError:
                        pass

                # --- Check 1: EOD flatten for day trades ---
                if is_eod and trade_style == "day_trade":
                    print(f"[position_mgr] {ticker}: EOD flatten (day_trade style)")
                    close_position(ticker, qty, current_price, "eod_flatten",
                                   trade_decision, open_orders, tracer)
                    actions.append({"ticker": ticker, "action": "eod_flatten"})
                    continue

                # --- Check 2: Time stop ---
                if hold_days >= max_hold and max_hold > 0:
                    print(f"[position_mgr] {ticker}: time stop ({hold_days}d >= {max_hold}d)")
                    close_position(ticker, qty, current_price, f"time_stop_{hold_days}d",
                                   trade_decision, open_orders, tracer)
                    actions.append({"ticker": ticker, "action": "time_stop", "hold_days": hold_days})
                    continue

                # --- Check 3: Trailing stop adjustment ---
                bars = get_bars(ticker, days=30)
                atr = compute_atr(bars)
                if atr > 0:
                    adjusted = manage_trailing_stop(ticker, current_price, atr, stop_orders, tracer)
                    if adjusted:
                        actions.append({"ticker": ticker, "action": "stop_adjusted"})
                else:
                    print(f"[position_mgr] {ticker}: no ATR data, skipping stop adjustment")

                time.sleep(0.15)  # Rate limit courtesy

            result.set({"actions": actions})

        # === Done ===
        summary = {
            "date": TODAY,
            "positions_checked": len(positions),
            "actions": actions,
            "is_eod": is_eod,
        }
        tracer.complete(summary)

        print(f"\n[position_mgr] Complete — {len(actions)} actions taken")
        for a in actions:
            print(f"  {a['ticker']}: {a['action']}")

    except Exception as e:
        tracer.fail(str(e), traceback.format_exc())
        print(f"[position_mgr] FATAL: {e}")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    from loki_logger import get_logger
    _logger = get_logger("position_manager")
    run()
