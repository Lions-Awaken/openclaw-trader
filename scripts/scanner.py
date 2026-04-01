#!/usr/bin/env python3
"""
Scanner — autonomous trading orchestrator.

Connects the analysis pipeline to order execution. Runs on cron 2x/day
(9:35 AM, 12:30 PM ET) during market hours.

Flow:
  1. Load active strategy profile from Supabase
  2. Check circuit breakers (consecutive losses, drawdown)
  3. Check Alpaca account (equity, buying power, open positions)
  4. Build watchlist: catalyst tickers (24h) + liquid universe
  5. Fetch 60-day daily bars per ticker via Alpaca data API (httpx)
  6. Compute 6 signals, filter by min_signal_score
  7. Run inference_engine.run_inference() on each candidate
  8. If decision ∈ {enter, strong_enter} and auto_execute → place orders
  9. Log everything to pipeline_runs, order_events, trade_decisions

Uses httpx for ALL Alpaca calls (not alpaca-py SDK).
"""

import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    check_market_open,
    get_account,
    get_bars,
    get_latest_quote,
    get_positions,
    load_strategy_profile,
    poll_for_fill,
    sb_get,
    slack_notify,
    submit_order,
)
from inference_engine import load_active_profile, run_inference
from tracer import PipelineTracer, _post_to_supabase

TODAY = date.today().isoformat()

# Liquid universe — broad coverage across sectors
LIQUID_UNIVERSE = [
    # AI / Semiconductor
    "NVDA", "AMD", "AVGO", "ARM", "TSM", "SMCI", "INTC", "QCOM", "MRVL", "MU",
    # Cloud / Hyperscalers
    "MSFT", "GOOGL", "META", "AMZN", "ORCL", "CRM", "SNOW", "NET",
    # Consumer Tech
    "AAPL", "TSLA", "NFLX", "UBER", "SHOP", "SQ",
    # Speculative / High Beta
    "PLTR", "MSTR", "DELL", "IONQ", "RGTI", "COIN", "HOOD", "SOFI",
    # Biotech / Pharma
    "MRNA", "CRSP", "DXCM",
    # Energy / Industrial
    "FSLR", "ENPH", "LNG",
]


# ---------------------------------------------------------------------------
# Circuit breaker check
# ---------------------------------------------------------------------------
def check_circuit_breakers() -> tuple[bool, str]:
    """Check for consecutive losses and drawdown. Returns (ok, reason)."""
    # Check last 5 trades for consecutive losses
    rows = sb_get("trade_decisions", {
        "select": "outcome",
        "exit_price": "not.is.null",
        "order": "created_at.desc",
        "limit": "5",
    })
    if rows:
        consecutive_losses = 0
        for r in rows:
            if r.get("outcome") in ("LOSS", "STRONG_LOSS"):
                consecutive_losses += 1
            else:
                break
        if consecutive_losses >= 3:
            return False, f"circuit_breaker: {consecutive_losses} consecutive losses"

    # Check daily drawdown from account
    account = get_account()
    if account:
        equity = float(account.get("equity", 0))
        last_equity = float(account.get("last_equity", 0))
        if last_equity > 0:
            daily_pnl_pct = (equity - last_equity) / last_equity * 100
            if daily_pnl_pct < -2.0:
                return False, f"circuit_breaker: daily drawdown {daily_pnl_pct:.1f}%"

    return True, ""


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------
def compute_signals(ticker: str, bars: list, spy_bars: list | None = None) -> dict | None:
    """Compute 6 signals from daily bar data. Returns signal dict or None."""
    if len(bars) < 20:
        return None

    closes = [float(b["c"]) for b in bars]
    volumes = [float(b["v"]) for b in bars]

    price = closes[-1]
    if price <= 0:
        return None

    # SMA(10), SMA(20)
    sma10 = sum(closes[-10:]) / 10
    sma20 = sum(closes[-20:]) / 20

    # RSI(14)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0, d) for d in deltas[-14:]]
    losses = [max(0, -d) for d in deltas[-14:]]
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14 if sum(losses) > 0 else 1e-9
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    # 20-day average volume
    avg_vol_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes)
    today_volume = volumes[-1]

    # ATR(14)
    trs = []
    for i in range(max(1, len(bars) - 14), len(bars)):
        h = float(bars[i]["h"])
        lo = float(bars[i]["l"])
        pc = float(bars[i - 1]["c"]) if i > 0 else lo
        tr = max(h - lo, abs(h - pc), abs(lo - pc))
        trs.append(tr)
    atr = sum(trs) / len(trs) if trs else 0

    signals = {}
    total_score = 0

    # Signal 1: Trend — price > SMA10 and price > SMA20
    trend = price > sma10 and price > sma20
    signals["trend"] = {"passed": trend, "price": round(price, 2), "sma10": round(sma10, 2), "sma20": round(sma20, 2)}
    if trend:
        total_score += 1

    # Signal 2: Momentum — 40 < RSI < 70
    momentum = 40 < rsi < 70
    signals["momentum"] = {"passed": momentum, "rsi": round(rsi, 1)}
    if momentum:
        total_score += 1

    # Signal 3: Volume — today_volume > 1.5 × avg_vol_20
    vol_surge = today_volume > 1.5 * avg_vol_20 if avg_vol_20 > 0 else False
    signals["volume"] = {"passed": vol_surge, "today_vol": today_volume, "avg_vol_20": round(avg_vol_20, 0), "ratio": round(today_volume / avg_vol_20, 2) if avg_vol_20 > 0 else 0}
    if vol_surge:
        total_score += 1

    # Signal 4: Catalyst — bullish catalyst_events in last 24h
    catalyst_found = False
    catalyst_rows = sb_get("catalyst_events", {
        "select": "direction,sentiment_score,headline",
        "ticker": f"eq.{ticker}",
        "event_time": f"gte.{(datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()}",
        "order": "event_time.desc",
        "limit": "5",
    })
    bullish_catalysts = [c for c in catalyst_rows if c.get("direction") == "bullish"]
    catalyst_found = len(bullish_catalysts) > 0
    signals["fundamental"] = {"passed": catalyst_found, "catalyst_count": len(catalyst_rows), "bullish_count": len(bullish_catalysts)}
    if catalyst_found:
        total_score += 1

    # Signal 5: Sentiment — avg sentiment_score > 0.1 from recent catalysts
    sentiment_scores = [float(c.get("sentiment_score", 0)) for c in catalyst_rows if c.get("sentiment_score") is not None]
    avg_sentiment = sum(sentiment_scores) / len(sentiment_scores) if sentiment_scores else 0.0
    sentiment_ok = avg_sentiment > 0.1
    signals["sentiment"] = {"passed": sentiment_ok, "avg_sentiment": round(avg_sentiment, 3), "sample_count": len(sentiment_scores)}
    if sentiment_ok:
        total_score += 1

    # Signal 6: Flow — 5-day relative strength vs SPY > 0
    flow_ok = False
    if spy_bars and len(spy_bars) >= 5 and len(closes) >= 5:
        spy_closes = [float(b["c"]) for b in spy_bars]
        ticker_5d_ret = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] > 0 else 0
        spy_5d_ret = (spy_closes[-1] - spy_closes[-5]) / spy_closes[-5] if spy_closes[-5] > 0 else 0
        flow_ok = ticker_5d_ret > spy_5d_ret
        signals["flow"] = {"passed": flow_ok, "ticker_5d_pct": round(ticker_5d_ret * 100, 2), "spy_5d_pct": round(spy_5d_ret * 100, 2)}
    else:
        signals["flow"] = {"passed": False, "reason": "insufficient_data"}
    if flow_ok:
        total_score += 1

    return {
        "ticker": ticker,
        "price": round(price, 2),
        "atr": round(atr, 2),
        "rsi": round(rsi, 1),
        "signals": signals,
        "total_score": total_score,
    }


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
def calculate_position_size(
    method: str,
    equity: float,
    price: float,
    atr: float,
    max_risk_pct: float = 5.0,
) -> int:
    """Calculate number of shares to buy."""
    if price <= 0:
        return 0

    if method == "aggressive_kelly":
        # 8% of equity per position
        allocation = equity * 0.08
        qty = int(allocation / price)
    elif method == "atr":
        # Risk $25 per ATR unit (or max_risk_pct of equity)
        risk_dollars = min(25.0, equity * max_risk_pct / 100)
        stop_distance = atr * 2.0
        if stop_distance <= 0:
            return 0
        qty = int(risk_dollars / stop_distance)
    else:
        # Conservative: 5% of equity
        allocation = equity * 0.05
        qty = int(allocation / price)

    # Floor: at least 1 share
    return max(1, qty)


# ---------------------------------------------------------------------------
# Build watchlist
# ---------------------------------------------------------------------------
def build_watchlist() -> list[str]:
    """Combine catalyst tickers (24h) with liquid universe, deduplicated."""
    tickers = set(LIQUID_UNIVERSE)

    # Add tickers with recent catalysts
    catalyst_rows = sb_get("catalyst_events", {
        "select": "ticker",
        "event_time": f"gte.{(datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()}",
        "direction": "eq.bullish",
    })
    for row in catalyst_rows:
        t = row.get("ticker")
        if t and len(t) <= 5 and t.isalpha():
            tickers.add(t)

    # Add tickers from extended watchlist file
    wl_file = os.path.expanduser("~/.openclaw/workspace/memory/watchlist-extended.json")
    if os.path.exists(wl_file):
        try:
            with open(wl_file) as f:
                extended = json.load(f).get("tickers", [])
                tickers.update(t for t in extended if isinstance(t, str) and len(t) <= 5 and t.isalpha() and t.isupper())
        except (json.JSONDecodeError, OSError):
            pass

    # Remove SPY/QQQ — they're regime tickers, not trade targets
    tickers.discard("SPY")
    tickers.discard("QQQ")

    return sorted(tickers)


# ---------------------------------------------------------------------------
# Core execution
# ---------------------------------------------------------------------------
def execute_trade(
    ticker: str,
    inference_result: dict,
    price: float,
    atr: float,
    equity: float,
    profile: dict,
    tracer: PipelineTracer,
) -> dict | None:
    """Place entry order + stop-loss, log to order_events and trade_decisions."""
    method = profile.get("position_size_method", "atr")
    max_risk = float(profile.get("max_risk_per_trade_pct", 5.0))
    qty = calculate_position_size(method, equity, price, atr, max_risk)
    if qty <= 0:
        print(f"[scanner] {ticker}: position size = 0, skipping")
        return None

    stop_price = round(price - (atr * 2.0), 2)
    if stop_price <= 0:
        print(f"[scanner] {ticker}: stop price <= 0, skipping")
        return None

    print(f"[scanner] {ticker}: placing market buy {qty} shares @ ~${price:.2f}, stop ${stop_price:.2f}")

    # Place market buy order
    entry_order = submit_order(ticker, qty, "buy", "market", "day")
    if not entry_order:
        print(f"[scanner] {ticker}: entry order failed")
        return None

    order_id = entry_order.get("id", "")
    tracer.log_order_event(
        order_id=order_id,
        ticker=ticker,
        event_type="submitted",
        side="buy",
        qty_ordered=qty,
        price=price,
        raw_event=entry_order,
    )

    # Poll for fill — market orders typically fill in seconds
    fill = poll_for_fill(order_id, timeout_seconds=120)
    if fill:
        fill_status = fill.get("status", "unknown")
        filled_qty = float(fill.get("filled_qty", 0) or 0)
        avg_price = float(fill.get("filled_avg_price", 0) or 0)
        tracer.log_order_event(
            order_id=order_id,
            ticker=ticker,
            event_type="filled" if fill_status == "filled" else fill_status,
            side="buy",
            qty_ordered=qty,
            qty_filled=filled_qty,
            avg_fill_price=avg_price if avg_price > 0 else None,
            raw_event=fill,
        )
        if avg_price > 0:
            price = avg_price  # Use actual fill price for stop calc + trade record
        if filled_qty > 0:
            qty = int(filled_qty)
        print(f"[scanner] {ticker} fill: {filled_qty} @ ${avg_price:.2f} ({fill_status})")
    else:
        print(f"[scanner] {ticker}: fill poll timed out, using quote price")
        tracer.log_order_event(
            order_id=order_id,
            ticker=ticker,
            event_type="poll_timeout",
            side="buy",
            qty_ordered=qty,
            raw_event={"reason": "poll_for_fill timed out after 120s"},
        )

    # Recalculate stop with actual fill price
    stop_price = round(price - (atr * 2.0), 2)

    # Place stop-loss GTC order (retry up to 3 times, close position if all fail)
    stop_order = None
    for stop_attempt in range(3):
        stop_order = submit_order(ticker, qty, "sell", "stop", "gtc", stop_price=stop_price)
        if stop_order:
            tracer.log_order_event(
                order_id=stop_order.get("id", ""),
                ticker=ticker,
                event_type="submitted",
                side="sell",
                qty_ordered=qty,
                price=stop_price,
                raw_event=stop_order,
            )
            break
        print(f"[scanner] {ticker}: stop-loss attempt {stop_attempt + 1}/3 failed")
        time.sleep(2)

    if not stop_order:
        print(f"[scanner] {ticker}: ALL stop-loss attempts failed — closing position for safety")
        close_order = submit_order(ticker, qty, "sell", "market", "day")
        if close_order:
            tracer.log_order_event(
                order_id=close_order.get("id", ""),
                ticker=ticker,
                event_type="submitted",
                side="sell",
                qty_ordered=qty,
                raw_event={"reason": "stop_loss_failed_safety_close"},
            )
        return None

    # Log to trade_decisions
    trade_decision = {
        "ticker": ticker,
        "decision": inference_result["final_decision"],
        "confidence": inference_result["final_confidence"],
        "entry_price": price,
        "stop_price": stop_price,
        "qty": qty,
        "side": "long",
        "trade_style": profile.get("trade_style", "swing"),
        "inference_chain_id": inference_result.get("inference_chain_id"),
        "entry_order_id": order_id,
        "stop_order_id": stop_order.get("id") if stop_order else None,
        "profile_name": profile.get("profile_name", "DEFAULT"),
        "signals_score": inference_result.get("tumblers", [{}])[0].get("confidence_before", 0) if inference_result.get("tumblers") else 0,
        "max_depth_reached": inference_result.get("max_depth_reached", 0),
        "metadata": {
            "position_size_method": method,
            "atr": atr,
            "equity_at_entry": equity,
        },
    }
    _post_to_supabase("trade_decisions", trade_decision)

    print(f"[scanner] {ticker}: TRADE PLACED — {qty} shares, entry ~${price:.2f}, stop ${stop_price:.2f}")
    return {
        "ticker": ticker,
        "qty": qty,
        "entry_price": price,
        "stop_price": stop_price,
        "order_id": order_id,
    }


# ---------------------------------------------------------------------------
# Main scanner run
# ---------------------------------------------------------------------------
def run():
    """Execute the full scan → analyze → trade pipeline."""
    print(f"\n{'='*60}")
    print(f"[scanner] Starting autonomous scan — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    tracer = PipelineTracer("scanner", metadata={"date": TODAY})

    try:
        # === Step 1: Load strategy profile ===
        with tracer.step("load_profile") as result:
            profile = load_strategy_profile()
            # Also load the inference engine's copy
            load_active_profile()
            result.set({"profile": profile.get("profile_name", "?")})

        min_signal = int(profile.get("min_signal_score", 4))
        max_positions = int(profile.get("max_concurrent_positions", 5))
        auto_execute = profile.get("auto_execute_all", False)
        circuit_breakers_on = profile.get("circuit_breakers_enabled", True)

        # === Step 1b: Market hours gate ===
        with tracer.step("market_hours_check") as result:
            is_open, reason = check_market_open()
            result.set({"is_open": is_open, "reason": reason})
            if not is_open:
                print(f"[scanner] Market not open: {reason}. Exiting.")
                tracer.complete({"stopped": reason, "trades_placed": 0})
                return

        # === Step 2: Circuit breaker check ===
        with tracer.step("circuit_breaker_check") as result:
            if circuit_breakers_on:
                cb_ok, cb_reason = check_circuit_breakers()
                result.set({"ok": cb_ok, "reason": cb_reason})
                if not cb_ok:
                    print(f"[scanner] HALTED: {cb_reason}")
                    tracer.complete({"halted": cb_reason})
                    return
            else:
                result.set({"ok": True, "reason": "circuit_breakers_disabled"})

        # === Step 3: Check Alpaca account ===
        with tracer.step("account_check") as result:
            account = get_account()
            if not account:
                print("[scanner] Cannot reach Alpaca API — aborting")
                tracer.fail("alpaca_unreachable")
                return

            equity = float(account.get("equity", 0))
            buying_power = float(account.get("buying_power", 0))
            open_positions = get_positions()
            open_tickers = {p.get("symbol") for p in open_positions}

            result.set({
                "equity": equity,
                "buying_power": buying_power,
                "open_positions": len(open_positions),
                "open_tickers": sorted(open_tickers),
            })

            if max_positions >= 999:
                slots_available = 999  # Unlimited
                print(f"[scanner] Account: equity=${equity:,.0f}, buying_power=${buying_power:,.0f}, "
                      f"positions={len(open_positions)} (unlimited mode)")
            else:
                slots_available = max_positions - len(open_positions)
                if slots_available <= 0:
                    print(f"[scanner] Max positions reached ({len(open_positions)}/{max_positions}) — scan only, no new trades")
                print(f"[scanner] Account: equity=${equity:,.0f}, buying_power=${buying_power:,.0f}, "
                      f"positions={len(open_positions)}/{max_positions}")

        # === Step 4: Build watchlist and fetch SPY bars ===
        with tracer.step("build_watchlist") as result:
            watchlist = build_watchlist()
            spy_bars = get_bars("SPY", days=30)

            if len(spy_bars) < 15:
                print(f"[scanner] Only {len(spy_bars)} SPY bars available. Waiting 90s for data...")
                time.sleep(90)
                spy_bars = get_bars("SPY", days=30)
                if len(spy_bars) < 15:
                    print(f"[scanner] Still only {len(spy_bars)} SPY bars after retry. Aborting.")
                    tracer.complete({"stopped": "insufficient_spy_data", "spy_bars": len(spy_bars)})
                    return

            result.set({"watchlist_size": len(watchlist), "spy_bars": len(spy_bars)})
            print(f"[scanner] Watchlist: {len(watchlist)} tickers, SPY bars: {len(spy_bars)}")

        # === Step 5: Compute signals for all tickers ===
        candidates = []
        with tracer.step("signal_scan", input_snapshot={"tickers": watchlist}) as result:
            for ticker in watchlist:
                if ticker in open_tickers:
                    continue  # Skip tickers we already hold

                bars = get_bars(ticker, days=60)
                if not bars or len(bars) < 20:
                    continue

                sig = compute_signals(ticker, bars, spy_bars)
                if sig and sig["total_score"] >= min_signal:
                    candidates.append(sig)
                    print(f"[scanner]   {ticker}: score={sig['total_score']}/6, "
                          f"price=${sig['price']:.2f}, atr=${sig['atr']:.2f}")

                time.sleep(0.15)  # Rate limit courtesy

            candidates.sort(key=lambda x: x["total_score"], reverse=True)
            result.set({
                "scanned": len(watchlist),
                "candidates": len(candidates),
                "top": [{"ticker": c["ticker"], "score": c["total_score"]} for c in candidates[:5]],
            })
            print(f"[scanner] Candidates: {len(candidates)} tickers pass signal threshold ({min_signal}+)")

        # === Step 6: Run inference on candidates ===
        inference_results = []
        with tracer.step("inference", input_snapshot={"candidates": [c["ticker"] for c in candidates]}) as result:
            for cand in candidates:
                ticker = cand["ticker"]
                print(f"\n[scanner] Running inference on {ticker} (score={cand['total_score']})...")

                inf_result = run_inference(
                    ticker=ticker,
                    signals=cand["signals"],
                    total_score=cand["total_score"],
                    scan_type="scanner",
                    pipeline_run_id=tracer.root_id,
                )
                inf_result["_price"] = cand["price"]
                inf_result["_atr"] = cand["atr"]
                inf_result["_score"] = cand["total_score"]
                inference_results.append(inf_result)

                # Log signal evaluation
                tracer.log_signal_evaluation(
                    ticker=ticker,
                    signals=cand["signals"],
                    total_score=cand["total_score"],
                    decision=inf_result["final_decision"],
                    reasoning=f"confidence={inf_result['final_confidence']:.3f}, "
                              f"depth={inf_result['max_depth_reached']}, "
                              f"stop={inf_result.get('stopping_reason', '?')}",
                    scan_type="scanner",
                )

            actionable = [r for r in inference_results if r["final_decision"] in ("enter", "strong_enter")]
            result.set({
                "total_inferred": len(inference_results),
                "actionable": len(actionable),
                "decisions": {r["ticker"]: r["final_decision"] for r in inference_results},
            })
            print(f"\n[scanner] Inference complete: {len(actionable)} actionable / {len(inference_results)} total")

        # === Step 7: Execute trades ===
        trades_placed = []
        with tracer.step("execution") as result:
            if not auto_execute:
                print("[scanner] auto_execute_all is OFF — logging decisions only, no orders placed")
                result.set({"auto_execute": False, "trades": 0})
            else:
                # Sort by confidence descending, take top N available slots
                actionable = sorted(
                    [r for r in inference_results if r["final_decision"] in ("enter", "strong_enter")],
                    key=lambda x: x["final_confidence"],
                    reverse=True,
                )

                for inf_result in actionable:
                    if slots_available <= 0:
                        print("[scanner] No more position slots available")
                        break
                    if buying_power < equity * 0.05:
                        print(f"[scanner] Insufficient buying power (${buying_power:,.0f})")
                        break

                    ticker = inf_result["ticker"]
                    quote = get_latest_quote(ticker)
                    price = quote["price"]
                    if price <= 0:
                        print(f"[scanner] {ticker}: no valid quote, skipping")
                        continue

                    trade = execute_trade(
                        ticker=ticker,
                        inference_result=inf_result,
                        price=price,
                        atr=inf_result["_atr"],
                        equity=equity,
                        profile=profile,
                        tracer=tracer,
                    )
                    if trade:
                        trades_placed.append(trade)
                        slots_available -= 1
                        buying_power -= price * trade["qty"]

                result.set({
                    "auto_execute": True,
                    "trades_placed": len(trades_placed),
                    "tickers": [t["ticker"] for t in trades_placed],
                })

        # === Done ===
        summary = {
            "date": TODAY,
            "profile": profile.get("profile_name", "?"),
            "watchlist_size": len(watchlist),
            "candidates": len(candidates),
            "inferred": len(inference_results),
            "actionable": len([r for r in inference_results if r["final_decision"] in ("enter", "strong_enter")]),
            "trades_placed": len(trades_placed),
            "tickers_traded": [t["ticker"] for t in trades_placed],
        }
        tracer.complete(summary)

        print(f"\n{'='*60}")
        print(f"[scanner] Complete — {len(trades_placed)} trades placed")
        for t in trades_placed:
            print(f"  {t['ticker']}: {t['qty']} shares @ ${t['entry_price']:.2f}, stop ${t['stop_price']:.2f}")
        print(f"{'='*60}\n")

        # Slack summary
        lines = [f"*Scanner complete* — {profile.get('profile_name', '?')} profile"]
        lines.append(f"Watchlist: {len(watchlist)} | Candidates: {len(candidates)} | Inferred: {len(inference_results)}")
        if trades_placed:
            lines.append(f"*Trades placed: {len(trades_placed)}*")
            for t in trades_placed:
                lines.append(f"  `{t['ticker']}` {t['qty']} shares @ ${t['entry_price']:.2f}, stop ${t['stop_price']:.2f}")
        else:
            actionable_count = len([r for r in inference_results if r["final_decision"] in ("enter", "strong_enter")])
            lines.append(f"No trades placed ({actionable_count} actionable, {len(inference_results)} inferred)")
        slack_notify("\n".join(lines))

    except Exception as e:
        tracer.fail(str(e), traceback.format_exc())
        print(f"[scanner] FATAL: {e}")
        traceback.print_exc()
        slack_notify(f"*Scanner FATAL*: {e}")
        raise


if __name__ == "__main__":
    from loki_logger import get_logger
    _logger = get_logger("scanner")
    run()
