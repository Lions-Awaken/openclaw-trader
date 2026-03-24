#!/usr/bin/env python3
"""
Scanner — autonomous trading orchestrator.

Scans for candidates with recent catalysts, runs them through the
5-tumbler inference engine, and executes trades on Alpaca for
enter/strong_enter decisions.

Flow:
  1. Load active strategy profile
  2. Check circuit breakers (consecutive losses, drawdown, etc.)
  3. Get watchlist tickers with recent catalysts (last 24h)
  4. For each candidate: build signal snapshot, run inference engine
  5. For enter/strong_enter: calculate position size, submit Alpaca order
  6. Log everything to trade_decisions + order_events

Cron schedule: M-F 9:35 AM, 12:30 PM ET (after catalyst ingest + market open)
"""

import json
import os
import sys
from datetime import datetime, timezone

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from inference_engine import load_active_profile, run_inference, sb_get
from tracer import PipelineTracer, _post_to_supabase

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE = os.environ.get(
    "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
)
ALPACA_DATA_BASE = "https://data.alpaca.markets"

_client = httpx.Client(timeout=15.0)


# ---------------------------------------------------------------------------
# Alpaca helpers
# ---------------------------------------------------------------------------

def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }


def get_account() -> dict:
    """Fetch Alpaca account info (equity, buying_power, etc.)."""
    resp = _client.get(
        f"{ALPACA_BASE}/v2/account", headers=_alpaca_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_positions() -> list:
    """Fetch current open positions from Alpaca."""
    resp = _client.get(
        f"{ALPACA_BASE}/v2/positions", headers=_alpaca_headers()
    )
    resp.raise_for_status()
    return resp.json()


def get_latest_quote(ticker: str) -> dict:
    """Fetch latest quote for a ticker from Alpaca data API."""
    resp = _client.get(
        f"{ALPACA_DATA_BASE}/v2/stocks/{ticker}/quotes/latest",
        headers=_alpaca_headers(),
    )
    if resp.status_code == 200:
        return resp.json().get("quote", {})
    return {}


def get_bars(ticker: str, timeframe: str = "1Day", limit: int = 20) -> list:
    """Fetch historical bars for ATR calculation."""
    resp = _client.get(
        f"{ALPACA_DATA_BASE}/v2/stocks/{ticker}/bars",
        headers=_alpaca_headers(),
        params={"timeframe": timeframe, "limit": limit},
    )
    if resp.status_code == 200:
        return resp.json().get("bars", [])
    return []


def submit_order(
    ticker: str,
    qty: int,
    side: str = "buy",
    order_type: str = "market",
    time_in_force: str = "day",
    stop_price: float | None = None,
) -> dict | None:
    """Submit an order to Alpaca. Returns order dict or None."""
    payload = {
        "symbol": ticker,
        "qty": str(qty),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
    }
    if stop_price and order_type == "stop":
        payload["stop_price"] = str(round(stop_price, 2))

    resp = _client.post(
        f"{ALPACA_BASE}/v2/orders",
        headers=_alpaca_headers(),
        json=payload,
    )
    if resp.status_code in (200, 201):
        return resp.json()
    print(f"[scanner] Order submission failed: {resp.status_code} {resp.text}")
    return None


# ---------------------------------------------------------------------------
# Signal building
# ---------------------------------------------------------------------------

def build_signals(ticker: str) -> dict:
    """Build signal snapshot for a ticker using recent data.

    Uses Alpaca bars for technical indicators and catalyst_events for
    fundamental/sentiment.  This is a simplified signal builder — the
    full version would use a dedicated technical analysis library.
    """
    bars = get_bars(ticker, "1Day", 30)
    if len(bars) < 10:
        return {"total_score": 0, "signals": {}}

    closes = [float(b["c"]) for b in bars]
    volumes = [float(b["v"]) for b in bars]

    current = closes[-1]

    # Signal 1: Trend — price above 10-day and 20-day SMA
    sma10 = sum(closes[-10:]) / 10
    sma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else sma10
    trend_passed = current > sma10 and current > sma20

    # Signal 2: Momentum — RSI between 40 and 70 (healthy, not overbought)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d for d in deltas[-14:] if d > 0]
    losses = [-d for d in deltas[-14:] if d < 0]
    avg_gain = sum(gains) / 14 if gains else 0.001
    avg_loss = sum(losses) / 14 if losses else 0.001
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    momentum_passed = 40 <= rsi <= 70

    # Signal 3: Volume — today's volume > 1.5x 20-day average
    avg_vol = sum(volumes[-20:]) / min(len(volumes), 20)
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
    volume_passed = vol_ratio > 1.5

    # Signal 4: Fundamental — check for recent catalysts
    catalysts = sb_get("catalyst_events", {
        "select": "id,catalyst_type,direction,sentiment_score,magnitude",
        "ticker": f"eq.{ticker}",
        "event_time": f"gte.{(datetime.now(timezone.utc) - __import__('datetime').timedelta(hours=24)).isoformat()}",
        "order": "event_time.desc",
        "limit": "5",
    })
    bullish_catalysts = [c for c in catalysts if c.get("direction") == "bullish"]
    fundamental_passed = len(bullish_catalysts) > 0

    # Signal 5: Sentiment — average sentiment from recent catalysts
    sentiments = [
        float(c["sentiment_score"])
        for c in catalysts
        if c.get("sentiment_score") is not None
    ]
    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
    sentiment_passed = avg_sentiment > 0.1

    # Signal 6: Flow — relative strength vs SPY (price outperforming)
    spy_bars = get_bars("SPY", "1Day", 10)
    flow_passed = False
    if spy_bars and len(spy_bars) >= 5:
        spy_closes = [float(b["c"]) for b in spy_bars]
        spy_ret = (spy_closes[-1] - spy_closes[-5]) / spy_closes[-5]
        stock_ret = (closes[-1] - closes[-5]) / closes[-5] if len(closes) >= 5 else 0
        flow_passed = stock_ret > spy_ret

    signals = {
        "trend": {"passed": trend_passed, "sma10": round(sma10, 2), "sma20": round(sma20, 2), "price": current},
        "momentum": {"passed": momentum_passed, "rsi": round(rsi, 1)},
        "volume": {"passed": volume_passed, "ratio": round(vol_ratio, 2)},
        "fundamental": {"passed": fundamental_passed, "catalyst_count": len(catalysts), "bullish_count": len(bullish_catalysts)},
        "sentiment": {"passed": sentiment_passed, "score": round(avg_sentiment, 3)},
        "flow": {"passed": flow_passed},
    }

    total = sum(1 for s in signals.values() if s.get("passed"))
    return {"total_score": total, "signals": signals}


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def calculate_atr(ticker: str, period: int = 14) -> float:
    """Calculate Average True Range for position sizing."""
    bars = get_bars(ticker, "1Day", period + 1)
    if len(bars) < 2:
        return 0.0

    trs = []
    for i in range(1, len(bars)):
        h = float(bars[i]["h"])
        lo = float(bars[i]["l"])
        prev_c = float(bars[i - 1]["c"])
        tr = max(h - lo, abs(h - prev_c), abs(lo - prev_c))
        trs.append(tr)

    return sum(trs) / len(trs) if trs else 0.0


def calculate_position_size(
    ticker: str,
    profile: dict,
    account: dict,
    current_price: float,
) -> int:
    """Calculate position size based on strategy profile and risk limits."""
    equity = float(account.get("equity", 0))
    buying_power = float(account.get("buying_power", 0))

    max_risk_pct = float(profile.get("max_risk_per_trade_pct", 5.0))
    max_portfolio_risk = float(profile.get("max_portfolio_risk_pct", 15.0))
    method = profile.get("position_size_method", "atr")
    stop_atr_mult = float(profile.get("stop_loss_atr_multiple", 1.5))

    # Risk per trade in dollars
    risk_dollars = equity * (max_risk_pct / 100)

    if method == "aggressive_kelly":
        # More aggressive — use 8% of equity per position
        position_value = equity * 0.08
    elif method == "atr":
        atr = calculate_atr(ticker)
        if atr <= 0:
            return 0
        stop_distance = atr * stop_atr_mult
        # Position size = risk_dollars / stop_distance
        position_value = (risk_dollars / stop_distance) * current_price
    else:
        # Fixed fractional — 5% of equity
        position_value = equity * (max_risk_pct / 100)

    # Cap at buying power
    position_value = min(position_value, buying_power * 0.95)

    # Cap at portfolio risk limit
    max_position = equity * (max_portfolio_risk / 100)
    position_value = min(position_value, max_position)

    qty = int(position_value / current_price) if current_price > 0 else 0
    return max(qty, 0)


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------

def check_circuit_breakers(profile: dict) -> tuple[bool, str]:
    """Check circuit breakers. Returns (ok_to_trade, reason)."""
    if not profile.get("circuit_breakers_enabled", True):
        return True, "circuit_breakers_disabled"

    max_consec = int(profile.get("circuit_breaker_consecutive_losses", 3))
    max_weekly_loss = float(profile.get("circuit_breaker_weekly_loss_pct", 8.0))

    # Check consecutive losses
    recent_trades = sb_get("trade_decisions", {
        "select": "outcome",
        "order": "created_at.desc",
        "limit": str(max_consec),
    })
    consecutive_losses = 0
    for t in recent_trades:
        if t.get("outcome") in ("LOSS", "STRONG_LOSS"):
            consecutive_losses += 1
        else:
            break

    if consecutive_losses >= max_consec:
        return False, f"consecutive_losses={consecutive_losses}"

    # Check weekly P&L
    week_trades = sb_get("trade_decisions", {
        "select": "pnl",
        "created_at": f"gte.{(datetime.now(timezone.utc) - __import__('datetime').timedelta(days=7)).isoformat()}",
    })
    weekly_pnl = sum(float(t.get("pnl", 0) or 0) for t in week_trades)

    try:
        acct = get_account()
        equity = float(acct.get("equity", 100000))
    except Exception:
        equity = 100000

    weekly_loss_pct = abs(weekly_pnl) / equity * 100 if weekly_pnl < 0 else 0
    if weekly_loss_pct >= max_weekly_loss:
        return False, f"weekly_loss={weekly_loss_pct:.1f}%"

    return True, "ok"


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def get_watchlist() -> list[str]:
    """Get tickers with recent catalysts or strong signals.

    In UNLEASHED mode, also scans a broader universe of liquid names.
    """
    # Tickers with catalysts in last 24 hours
    catalysts = sb_get("catalyst_events", {
        "select": "ticker",
        "event_time": f"gte.{(datetime.now(timezone.utc) - __import__('datetime').timedelta(hours=24)).isoformat()}",
        "ticker": "not.is.null",
    })
    tickers = list({c["ticker"] for c in catalysts if c.get("ticker")})

    # Also include a base universe of liquid names for UNLEASHED mode
    profile = load_active_profile()
    if profile.get("scan_all_regimes") or profile.get("profile_name") == "UNLEASHED":
        base_universe = [
            "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
            "AMD", "PLTR", "SOFI", "COIN", "MARA", "RIOT", "SQ",
            "SNOW", "NET", "CRWD", "DDOG", "MDB", "ABNB",
        ]
        for t in base_universe:
            if t not in tickers:
                tickers.append(t)

    return tickers


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------

def run():
    """Main scanner loop — find candidates, run inference, execute trades."""
    tracer = PipelineTracer("scanner")

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("[scanner] ERROR: ALPACA_API_KEY and ALPACA_SECRET_KEY required")
        return

    if not SUPABASE_URL:
        print("[scanner] ERROR: SUPABASE_URL required")
        return

    try:
        # Load strategy profile
        with tracer.step("load_profile"):
            profile = load_active_profile()
            profile_name = profile.get("profile_name", "DEFAULT")
            auto_execute = profile.get("auto_execute_all", False)
            min_score = int(profile.get("min_signal_score", 4))
            print(f"[scanner] Profile: {profile_name}, auto_execute={auto_execute}, min_score={min_score}")

        # Check circuit breakers
        with tracer.step("circuit_breakers"):
            cb_ok, cb_reason = check_circuit_breakers(profile)
            if not cb_ok:
                print(f"[scanner] Circuit breaker tripped: {cb_reason}. Halting scan.")
                tracer.complete({"halted": True, "reason": cb_reason})
                return

        # Check account and positions
        with tracer.step("account_check"):
            account = get_account()
            positions = get_positions()
            max_positions = int(profile.get("max_concurrent_positions", 3))
            current_tickers = [p["symbol"] for p in positions]
            open_slots = max_positions - len(positions)
            equity = float(account.get("equity", 0))
            buying_power = float(account.get("buying_power", 0))
            print(
                f"[scanner] Account: equity=${equity:,.2f}, "
                f"buying_power=${buying_power:,.2f}, "
                f"positions={len(positions)}/{max_positions}"
            )

            if open_slots <= 0:
                print("[scanner] Max positions reached. Skipping scan.")
                tracer.complete({"halted": True, "reason": "max_positions"})
                return

        # Get watchlist
        with tracer.step("watchlist"):
            tickers = get_watchlist()
            # Remove tickers we already hold
            tickers = [t for t in tickers if t not in current_tickers]
            print(f"[scanner] Watchlist: {len(tickers)} candidates: {tickers[:10]}...")

        # Scan each candidate
        scanned = 0
        entries = 0
        orders_placed = 0

        for ticker in tickers:
            if open_slots <= 0:
                print("[scanner] No more open slots. Stopping scan.")
                break

            with tracer.step(f"scan_{ticker}"):
                # Build signals
                sig_data = build_signals(ticker)
                total_score = sig_data["total_score"]
                signals = sig_data["signals"]

                if total_score < min_score:
                    print(f"[scanner] {ticker}: score={total_score}/{min_score} — skip")
                    scanned += 1
                    continue

                # Run inference engine
                print(f"[scanner] {ticker}: score={total_score} — running inference...")
                result = run_inference(
                    ticker=ticker,
                    signals=signals,
                    total_score=total_score,
                    scan_type="pre_market",
                )

                decision = result.get("final_decision", "skip")
                confidence = result.get("final_confidence", 0)
                scanned += 1

                if decision not in ("enter", "strong_enter"):
                    print(f"[scanner] {ticker}: decision={decision} conf={confidence:.3f} — no trade")
                    continue

                entries += 1

                # Execute trade if auto_execute is on
                if not auto_execute:
                    print(f"[scanner] {ticker}: {decision} conf={confidence:.3f} — auto_execute OFF, logging only")
                    _log_trade_decision(ticker, signals, result, profile, entry_price=None, order_id=None)
                    continue

                # Get current price
                quote = get_latest_quote(ticker)
                ask_price = float(quote.get("ap", 0))
                bid_price = float(quote.get("bp", 0))
                current_price = ask_price if ask_price > 0 else bid_price

                if current_price <= 0:
                    print(f"[scanner] {ticker}: could not get price — skipping order")
                    continue

                # Calculate position size
                qty = calculate_position_size(ticker, profile, account, current_price)
                if qty <= 0:
                    print(f"[scanner] {ticker}: position size=0 — skipping")
                    continue

                # Submit market order
                print(
                    f"[scanner] {ticker}: EXECUTING {decision} — "
                    f"qty={qty}, price~${current_price:.2f}, "
                    f"value=${qty * current_price:,.2f}"
                )
                order = submit_order(ticker, qty, side="buy")

                if order:
                    order_id = order.get("id", "")
                    orders_placed += 1
                    open_slots -= 1

                    # Log order event
                    _post_to_supabase("order_events", {
                        "order_id": order_id,
                        "ticker": ticker,
                        "event_type": "submitted",
                        "side": "buy",
                        "qty_ordered": qty,
                        "price": current_price,
                        "raw_event": order,
                    })

                    # Log trade decision
                    _log_trade_decision(
                        ticker, signals, result, profile,
                        entry_price=current_price,
                        order_id=order_id,
                    )

                    # Submit stop-loss order
                    atr = calculate_atr(ticker)
                    stop_mult = float(profile.get("stop_loss_atr_multiple", 1.5))
                    if atr > 0:
                        stop_price = current_price - (atr * stop_mult)
                        stop_order = submit_order(
                            ticker, qty, side="sell",
                            order_type="stop",
                            stop_price=stop_price,
                            time_in_force="gtc",
                        )
                        if stop_order:
                            _post_to_supabase("order_events", {
                                "order_id": stop_order.get("id", ""),
                                "ticker": ticker,
                                "event_type": "submitted",
                                "side": "sell",
                                "qty_ordered": qty,
                                "price": stop_price,
                                "raw_event": stop_order,
                            })
                            print(f"[scanner] {ticker}: stop-loss set at ${stop_price:.2f} ({stop_mult}x ATR)")
                else:
                    print(f"[scanner] {ticker}: order submission FAILED")

        tracer.complete({
            "scanned": scanned,
            "entries": entries,
            "orders_placed": orders_placed,
            "profile": profile_name,
        })
        print(
            f"[scanner] Complete. Scanned: {scanned}, "
            f"Entries: {entries}, Orders: {orders_placed}"
        )

    except Exception as e:
        tracer.fail(str(e))
        print(f"[scanner] FAILED: {e}")
        raise


def _log_trade_decision(
    ticker: str,
    signals: dict,
    inference_result: dict,
    profile: dict,
    entry_price: float | None,
    order_id: str | None,
):
    """Log a trade decision to the trade_decisions table."""
    decision = inference_result.get("final_decision", "skip")
    confidence = inference_result.get("final_confidence", 0)
    chain_id = inference_result.get("inference_chain_id")
    total_score = sum(1 for s in signals.values() if s.get("passed"))

    reasoning_parts = []
    for name, sig in signals.items():
        if sig.get("passed"):
            reasoning_parts.append(f"{name}: {json.dumps({k: v for k, v in sig.items() if k != 'passed'})}")

    reasoning = (
        f"{decision.upper()} via {profile.get('profile_name', 'DEFAULT')} profile. "
        f"Confidence: {confidence:.3f}. "
        f"Signals ({total_score}/6): {', '.join(reasoning_parts)}"
    )

    content = (
        f"{'BUY' if entry_price else 'SIGNAL'} {ticker} "
        f"{total_score}/6 signals, {decision} conf={confidence:.3f}"
    )

    _post_to_supabase("trade_decisions", {
        "ticker": ticker,
        "action": "BUY",
        "entry_price": entry_price,
        "signals_fired": total_score,
        "reasoning": reasoning,
        "content": content,
        "metadata": {
            "inference_chain_id": chain_id,
            "order_id": order_id,
            "confidence": confidence,
            "decision": decision,
            "profile": profile.get("profile_name"),
            "signals": signals,
        },
    })


if __name__ == "__main__":
    run()
