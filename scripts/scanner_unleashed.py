#!/usr/bin/env python3
"""
UNLEASHED Scanner — aggressive day-trader signal detection.

Unlike the conservative scanner (4/6 signals, trend-following only),
this scans for setups that work in ANY market regime:

  SETUP TYPES:
  1. Relative Strength Play    — stock up when SPY is down (buyers defending it)
  2. Pre-Market Gap Play       — gap up >1.5% on volume with catalyst
  3. Momentum Continuation     — RSI 55-72, high volume, trend intact
  4. Oversold Bounce           — RSI < 30, volume spike, fresh catalyst
  5. Breakout Play             — price breaching 20-day high on 2x+ volume
  6. High-Beta Mover           — stock moving >2x SPY's daily range

Minimum threshold: 2 signals (vs 4 in conservative mode).
Returns candidates sorted by composite score, with setup_type label.

Called by the UNLEASHED skill step when profile is UNLEASHED.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

try:
    import finnhub
    import pandas as pd
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
except ImportError:
    print(json.dumps({"error": "Run: pip install alpaca-py pandas numpy finnhub-python"}))
    sys.exit(1)

API_KEY    = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")

if not API_KEY or not SECRET_KEY:
    print(json.dumps({"error": "ALPACA_API_KEY and ALPACA_SECRET_KEY env vars must be set"}))
    sys.exit(1)

data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
fh_client   = finnhub.Client(api_key=FINNHUB_KEY) if FINNHUB_KEY else None

# Expanded watchlist — high-beta, liquid names where day-trade setups appear
CORE_WATCHLIST = [
    # AI / Semiconductor
    "NVDA", "AMD", "AVGO", "ARM", "TSM", "SMCI", "INTC", "QCOM",
    # Cloud / Hyperscalers
    "MSFT", "GOOGL", "META", "AMZN",
    # Speculative / High Beta
    "PLTR", "MSTR", "DELL", "IONQ", "RGTI",
    # SPY/QQQ for regime context
    "SPY", "QQQ",
]

REGIME_TICKERS = {"SPY", "QQQ"}


def load_extended_watchlist() -> list:
    wl_file = os.path.expanduser("~/.openclaw/workspace/memory/watchlist-extended.json")
    if os.path.exists(wl_file):
        try:
            with open(wl_file) as f:
                return json.load(f).get("tickers", [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def get_bars(ticker: str, days: int = 60) -> pd.DataFrame | None:
    try:
        end   = datetime.now()
        start = end - timedelta(days=days)
        req   = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars = data_client.get_stock_bars(req)
        df   = bars.df.reset_index()
        if "symbol" in df.columns:
            df = df[df["symbol"] == ticker].copy()
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df.reset_index(drop=True) if len(df) >= 20 else None
    except Exception:
        return None



def compute_indicators(df: pd.DataFrame) -> dict:
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # RSI(14)
    delta  = close.diff()
    gain   = delta.clip(lower=0).rolling(14).mean()
    loss   = (-delta.clip(upper=0)).rolling(14).mean()
    rs     = gain / loss.replace(0, 1e-9)
    rsi    = 100 - (100 / (1 + rs))

    # EMA
    ema21  = close.ewm(span=21).mean()
    ema50  = close.ewm(span=50).mean()
    sma200 = close.rolling(200).mean()

    # ATR(14)
    prev_cl = close.shift(1)
    tr     = pd.concat([
        high - low,
        (high - prev_cl).abs(),
        (low  - prev_cl).abs(),
    ], axis=1).max(axis=1)
    atr14  = tr.rolling(14).mean()

    # OBV
    obv    = (volume * ((close.diff() > 0).astype(int) * 2 - 1)).cumsum()
    obv_trend = float(obv.iloc[-1]) > float(obv.iloc[-4])

    # Relative volume
    avg_vol_20 = volume.rolling(20).mean()
    rel_vol    = float(volume.iloc[-1]) / float(avg_vol_20.iloc[-1]) if float(avg_vol_20.iloc[-1]) > 0 else 1.0

    # 20-day high
    high_20 = float(high.rolling(20).max().iloc[-1])

    price    = float(close.iloc[-1])
    prev_close = float(close.iloc[-2]) if len(close) >= 2 else price

    return {
        "price":        price,
        "prev_close":   prev_close,
        "rsi":          float(rsi.iloc[-1]),
        "ema21":        float(ema21.iloc[-1]),
        "ema50":        float(ema50.iloc[-1]),
        "sma200":       float(sma200.iloc[-1]) if len(close) >= 200 else 0,
        "atr":          float(atr14.iloc[-1]),
        "atr_pct":      float(atr14.iloc[-1]) / price * 100,
        "obv_trend":    obv_trend,
        "rel_vol":      round(rel_vol, 2),
        "high_20":      high_20,
        "volume_today": float(volume.iloc[-1]),
    }


def score_ticker_unleashed(ticker: str, spy_move_pct: float, spy_atr_pct: float) -> dict | None:
    """Score a ticker across all 6 UNLEASHED setup types."""
    df = get_bars(ticker)
    if df is None or len(df) < 5:
        return None

    ind  = compute_indicators(df)
    price = ind["price"]
    if price <= 0:
        return None

    score   = 0
    signals = {}
    setup_types = []

    # ── Signal 1: Trend (still counts, but not required) ──────────────────────
    trend_ok = (price > ind["ema21"]) and (ind["ema21"] > ind["ema50"])
    signals["trend"]      = trend_ok
    signals["rsi"]        = round(ind["rsi"], 1)
    signals["ema21"]      = round(ind["ema21"], 2)
    if trend_ok:
        score += 1

    # ── Signal 2: Momentum ────────────────────────────────────────────────────
    # Expanded: also count RSI 55-72 as momentum (continuation zone)
    momentum_ok = (35 <= ind["rsi"] <= 72)
    signals["momentum"] = momentum_ok
    if momentum_ok:
        score += 1

    # ── Signal 3: Volume ──────────────────────────────────────────────────────
    vol_ok = ind["obv_trend"] and ind["rel_vol"] >= 1.2
    signals["volume"]   = vol_ok
    signals["rel_vol"]  = ind["rel_vol"]
    if vol_ok:
        score += 1

    # ── Signal 4: Fundamental / Catalyst ─────────────────────────────────────
    fund_ok = False
    if fh_client:
        try:
            earnings = fh_client.company_earnings(ticker, limit=2)
            if earnings:
                surprise = earnings[0].get("surprise", 0) or 0
                fund_ok  = surprise > 0.03  # >3% (lower than conservative's 5%)
                signals["earnings_surprise"] = round(surprise, 3)
        except Exception:
            pass
    signals["fundamental"] = fund_ok
    if fund_ok:
        score += 1

    # ── Signal 5: Relative Strength vs SPY ────────────────────────────────────
    today_move_pct = ((price - ind["prev_close"]) / ind["prev_close"] * 100) if ind["prev_close"] else 0
    # RS: stock is beating SPY by at least 1% today
    rs_beat = today_move_pct - spy_move_pct
    rs_ok   = rs_beat >= 1.0
    signals["relative_strength"]     = rs_ok
    signals["today_move_pct"]        = round(today_move_pct, 2)
    signals["spy_move_pct"]          = round(spy_move_pct, 2)
    signals["rs_beat_pct"]           = round(rs_beat, 2)
    if rs_ok:
        score += 1
        setup_types.append("relative_strength")

    # ── Signal 6: Special Setup Detection ─────────────────────────────────────

    # Oversold bounce: RSI < 30 + volume spike
    oversold_bounce = ind["rsi"] < 30 and ind["rel_vol"] >= 2.0
    signals["oversold_bounce"] = oversold_bounce
    if oversold_bounce:
        score += 1
        setup_types.append("oversold_bounce")

    # Breakout: price breaking 20-day high on volume
    breakout = (price >= ind["high_20"] * 0.99) and ind["rel_vol"] >= 1.8
    signals["breakout"] = breakout
    if breakout:
        score += 1
        setup_types.append("breakout")

    # High-beta mover: stock moving >2x SPY's daily ATR
    high_beta_move = abs(today_move_pct) > (spy_atr_pct * 2.0)
    signals["high_beta_mover"] = high_beta_move
    if high_beta_move:
        score += 1
        setup_types.append("high_beta_mover")

    # Determine primary setup type
    if not setup_types:
        if trend_ok and momentum_ok and vol_ok:
            setup_types = ["momentum_continuation"]
        elif trend_ok:
            setup_types = ["trend_following"]
        else:
            setup_types = ["mixed"]

    # Flow signal from Finnhub
    flow_ok = False
    if fh_client:
        try:
            insider  = fh_client.stock_insider_transactions(ticker)
            txns     = insider.get("data", []) if insider else []
            cutoff   = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            buys     = [t for t in txns
                        if t.get("transactionDate", "") >= cutoff
                        and (t.get("share", 0) or 0) > 0
                        and t.get("transactionCode") == "P"]
            sellers  = [t for t in txns
                        if t.get("transactionDate", "") >= cutoff
                        and (t.get("share", 0) or 0) < 0]
            flow_ok  = len(buys) >= 1 and len(sellers) == 0
            signals["insider_buys"] = len(buys)
        except Exception:
            pass
    signals["flow"] = flow_ok
    if flow_ok:
        score += 1

    # Composite score out of possible 9 (extended signal set)
    # Normalize to base 6 for compatibility
    normalized_score = min(6, round(score * 6 / 9))

    return {
        "ticker":           ticker,
        "score":            normalized_score,
        "raw_score":        score,
        "price":            round(price, 2),
        "atr":              round(ind["atr"], 2),
        "atr_pct":          round(ind["atr_pct"], 2),
        "signals":          signals,
        "setup_types":      setup_types,
        "primary_setup":    setup_types[0] if setup_types else "mixed",
        "today_move_pct":   round(today_move_pct, 2),
        "is_discovery":     ind["rel_vol"] >= 3.0,
        "timestamp":        datetime.now().isoformat(),
    }


def get_spy_context() -> tuple[float, float]:
    """Get SPY's current day move % and ATR% for relative strength calcs."""
    df = get_bars("SPY", days=30)
    if df is None:
        return 0.0, 1.0
    ind = compute_indicators(df)
    spy_move = ((ind["price"] - ind["prev_close"]) / ind["prev_close"] * 100) if ind["prev_close"] else 0
    return round(spy_move, 2), round(ind["atr_pct"], 2)


if __name__ == "__main__":
    print("[scanner_unleashed] Loading SPY context...", file=sys.stderr)
    spy_move_pct, spy_atr_pct = get_spy_context()
    print(f"[scanner_unleashed] SPY today: {spy_move_pct:+.2f}%, ATR%: {spy_atr_pct:.2f}%", file=sys.stderr)

    # Build universe
    extended = load_extended_watchlist()
    universe = list(dict.fromkeys(
        [t for t in CORE_WATCHLIST if t not in REGIME_TICKERS] + extended
    ))

    results = []
    for ticker in universe:
        time.sleep(0.25)
        result = score_ticker_unleashed(ticker, spy_move_pct, spy_atr_pct)
        if result is not None:
            results.append(result)

    # Sort by raw_score descending, then by abs(today_move_pct)
    results.sort(key=lambda x: (x["raw_score"], abs(x.get("today_move_pct", 0))), reverse=True)

    # UNLEASHED threshold: 2 signals minimum
    candidates   = [r for r in results if r["raw_score"] >= 2]
    high_conv    = [r for r in results if r["raw_score"] >= 5]
    discovery    = [r for r in results if r.get("is_discovery") and r not in candidates]

    output = {
        "scan_time":         datetime.now().isoformat(),
        "mode":              "UNLEASHED",
        "universe_size":     len(universe),
        "spy_context":       {"move_pct": spy_move_pct, "atr_pct": spy_atr_pct},
        "candidates":        candidates,
        "high_conviction":   high_conv,
        "discovery":         discovery,
        "all_scores":        [{"ticker": r["ticker"], "score": r["raw_score"], "setup": r["primary_setup"]} for r in results],
    }

    print(json.dumps(output, indent=2))
