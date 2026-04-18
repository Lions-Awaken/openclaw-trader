#!/usr/bin/env python3
"""
OpenClaw Trader — Backtest Harness for the 6-Signal System

Simulates the OpenClaw swing-trading strategy against historical daily bars.
Signals 4-6 (fundamental, sentiment, flow) cannot be backtested reliably,
so we evaluate the 3 technical signals (trend, momentum, volume) plus the
SPY regime filter.

Usage:
    python backtest.py --ticker NVDA --days 180 --starting-capital 500
    python backtest.py --ticker AAPL --days 365 --json
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ---------------------------------------------------------------------------
# Technical indicator helpers
# ---------------------------------------------------------------------------

def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def calc_sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing)."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return atr


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """Record of a single completed trade."""
    entry_date: str
    exit_date: str
    ticker: str
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    hold_days: int
    exit_reason: str

    # Partial exit tracking (for multi-tranche exits)
    partial_exits: list = field(default_factory=list)

    @property
    def r_multiple(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return self.pnl / 25.0  # $25 risk per trade


@dataclass
class OpenPosition:
    """Tracks a position that has not yet been fully closed."""
    entry_date: str
    entry_price: float
    shares: float
    stop_price: float
    atr_at_entry: float
    risk_per_share: float  # 1.5 * ATR
    days_held: int = 0

    # Tranche tracking: shares remaining in each bucket
    tranche_1_shares: float = 0.0  # 40% — exit at 1.5R
    tranche_2_shares: float = 0.0  # 40% — exit at 2.5R
    tranche_3_shares: float = 0.0  # 20% — trailing stop

    # State flags
    tranche_1_exited: bool = False
    tranche_2_exited: bool = False
    trailing_stop: float = 0.0  # Updated once tranche 2 exits

    partial_exits: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_bars(client: StockHistoricalDataClient, ticker: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Fetch daily bars from Alpaca and return a clean DataFrame."""
    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
    )
    bars = client.get_stock_bars(request)
    df = bars.df

    # If multi-index (symbol, timestamp), drop the symbol level
    if isinstance(df.index, pd.MultiIndex):
        df = df.droplevel("symbol")

    # Ensure index is timezone-naive date for easy alignment
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df[~df.index.duplicated(keep="first")]
    df.sort_index(inplace=True)

    return df


def enrich_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators needed for signal evaluation."""
    df = df.copy()
    df["ema_21"] = calc_ema(df["close"], 21)
    df["ema_50"] = calc_ema(df["close"], 50)
    df["sma_50"] = calc_sma(df["close"], 50)
    df["rsi_14"] = calc_rsi(df["close"], 14)
    df["atr_14"] = calc_atr(df["high"], df["low"], df["close"], 14)
    df["vol_sma_20"] = calc_sma(df["volume"].astype(float), 20)
    return df


# ---------------------------------------------------------------------------
# Signal evaluation
# ---------------------------------------------------------------------------

def check_regime(spy_row: pd.Series) -> str:
    """Return 'UP' if SPY is above its 50 SMA, else 'DOWN'."""
    if pd.isna(spy_row.get("sma_50")):
        return "DOWN"  # Not enough data yet
    return "UP" if spy_row["close"] > spy_row["sma_50"] else "DOWN"


def check_signals(row: pd.Series) -> dict:
    """
    Evaluate the 3 backtestable technical signals.
    Returns a dict with signal names and boolean results.
    """
    signals = {}

    # Signal 1 — Trend: Price > 21 EMA AND 21 EMA > 50 EMA
    if not pd.isna(row.get("ema_21")) and not pd.isna(row.get("ema_50")):
        signals["trend"] = bool(row["close"] > row["ema_21"] and row["ema_21"] > row["ema_50"])
    else:
        signals["trend"] = False

    # Signal 2 — Momentum: RSI(14) between 35 and 65
    if not pd.isna(row.get("rsi_14")):
        signals["momentum"] = bool(35 <= row["rsi_14"] <= 65)
    else:
        signals["momentum"] = False

    # Signal 3 — Volume: Current volume > 1.5x 20-day average
    if not pd.isna(row.get("vol_sma_20")) and row["vol_sma_20"] > 0:
        signals["volume"] = bool(float(row["volume"]) > 1.5 * row["vol_sma_20"])
    else:
        signals["volume"] = False

    return signals


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------

def calculate_position_size(
    entry_price: float, atr: float, risk_dollars: float = 25.0
) -> tuple[float, float, float]:
    """
    Given entry price and ATR, compute shares and stop price.
    Risk per share = 1.5 * ATR.  Shares = risk_dollars / risk_per_share.
    Returns (shares, stop_price, risk_per_share).
    """
    risk_per_share = 1.5 * atr
    if risk_per_share <= 0:
        return 0.0, 0.0, 0.0

    shares = risk_dollars / risk_per_share

    # Ensure we can afford at least a fractional share
    if shares < 0.001:
        return 0.0, 0.0, 0.0

    stop_price = entry_price - risk_per_share
    return shares, stop_price, risk_per_share


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest(
    ticker: str,
    days: int,
    starting_capital: float,
) -> tuple[list[Trade], dict]:
    """
    Run the backtest and return (trades, summary_dict).
    """

    # --- Setup Alpaca client (no auth needed for historical data) ----------
    api_key = os.environ.get("ALPACA_API_KEY", "")
    api_secret = os.environ.get("ALPACA_SECRET_KEY", "")

    if not api_key or not api_secret:
        print("ERROR: ALPACA_API_KEY and ALPACA_SECRET_KEY env vars must be set.")
        sys.exit(1)

    client = StockHistoricalDataClient(api_key, api_secret)

    # Fetch extra history for indicator warm-up (need ~60 bars before our window)
    warmup_days = 80
    end_date = datetime.now() - timedelta(days=1)  # Yesterday
    start_date = end_date - timedelta(days=days + warmup_days)

    print(f"Fetching {ticker} bars from {start_date.date()} to {end_date.date()}...")
    ticker_df = fetch_bars(client, ticker, start_date, end_date)
    print(f"  -> {len(ticker_df)} bars for {ticker}")

    print("Fetching SPY bars for regime detection...")
    spy_df = fetch_bars(client, "SPY", start_date, end_date)
    print(f"  -> {len(spy_df)} bars for SPY")

    # Enrich both DataFrames with indicators
    ticker_df = enrich_dataframe(ticker_df)
    spy_df = enrich_dataframe(spy_df)

    # Align dates — only trade on days where we have both ticker and SPY data
    common_dates = ticker_df.index.intersection(spy_df.index)
    common_dates = common_dates.sort_values()

    # Skip warmup period — start trading after enough bars for indicators
    if len(common_dates) <= warmup_days:
        print("ERROR: Not enough data after warmup period.")
        sys.exit(1)

    # The actual backtest window starts after warmup
    trading_dates = common_dates[warmup_days:]
    print(f"Backtesting {len(trading_dates)} trading days "
          f"({trading_dates[0].date()} to {trading_dates[-1].date()})\n")

    # --- Simulation state ---------------------------------------------------
    capital = starting_capital
    position: OpenPosition | None = None
    trades: list[Trade] = []
    equity_curve: list[float] = []
    daily_returns: list[float] = []

    for date in trading_dates:
        ticker_row = ticker_df.loc[date]
        spy_row = spy_df.loc[date]
        today_close = float(ticker_row["close"])
        today_low = float(ticker_row["low"])

        # Track equity (capital + mark-to-market of open position)
        if position is not None:
            mtm = capital + position.shares * today_close
        else:
            mtm = capital
        equity_curve.append(mtm)

        if len(equity_curve) >= 2:
            prev = equity_curve[-2]
            daily_returns.append((mtm - prev) / prev if prev > 0 else 0.0)

        # --- Manage open position -------------------------------------------
        if position is not None:
            position.days_held += 1
            realized_today = 0.0
            exit_reason = None

            # Check regime shift — close entire position
            regime = check_regime(spy_row)
            if regime == "DOWN":
                realized_today = position.shares * (today_close - position.entry_price)
                exit_reason = "regime_shift"
                position.partial_exits.append({
                    "reason": "regime_shift",
                    "price": today_close,
                    "shares": position.shares,
                })
                position.shares = 0.0

            # Check stop loss (intraday — use low)
            elif today_low <= position.stop_price:
                exit_price = position.stop_price  # Assume fill at stop
                realized_today = position.shares * (exit_price - position.entry_price)
                exit_reason = "stop_loss"
                position.partial_exits.append({
                    "reason": "stop_loss",
                    "price": exit_price,
                    "shares": position.shares,
                })
                position.shares = 0.0

            # Check trailing stop (only active after tranche 2 exits)
            elif position.tranche_2_exited and today_low <= position.trailing_stop:
                exit_price = position.trailing_stop
                remaining = position.tranche_3_shares
                realized_today = remaining * (exit_price - position.entry_price)
                exit_reason = "trailing_stop"
                position.partial_exits.append({
                    "reason": "trailing_stop",
                    "price": exit_price,
                    "shares": remaining,
                })
                position.tranche_3_shares = 0.0
                position.shares = 0.0

            else:
                # Check take-profit levels
                price_move = today_close - position.entry_price
                r_value = position.risk_per_share  # 1.5 * ATR = 1R

                # Tranche 1: exit 40% at 1.5R
                if not position.tranche_1_exited and price_move >= 1.5 * r_value:
                    shares_to_exit = position.tranche_1_shares
                    realized_today += shares_to_exit * (today_close - position.entry_price)
                    position.partial_exits.append({
                        "reason": "take_profit_1.5R",
                        "price": today_close,
                        "shares": shares_to_exit,
                    })
                    position.shares -= shares_to_exit
                    position.tranche_1_shares = 0.0
                    position.tranche_1_exited = True

                    # Move stop to breakeven after first take-profit
                    position.stop_price = position.entry_price

                # Tranche 2: exit 40% at 2.5R
                if not position.tranche_2_exited and price_move >= 2.5 * r_value:
                    shares_to_exit = position.tranche_2_shares
                    realized_today += shares_to_exit * (today_close - position.entry_price)
                    position.partial_exits.append({
                        "reason": "take_profit_2.5R",
                        "price": today_close,
                        "shares": shares_to_exit,
                    })
                    position.shares -= shares_to_exit
                    position.tranche_2_shares = 0.0
                    position.tranche_2_exited = True

                    # Activate trailing stop for remaining 20%
                    position.trailing_stop = today_close - (1.0 * position.atr_at_entry)

                # Update trailing stop if active (ratchets up only)
                if position.tranche_2_exited and position.tranche_3_shares > 0:
                    new_trail = today_close - (1.0 * position.atr_at_entry)
                    if new_trail > position.trailing_stop:
                        position.trailing_stop = new_trail

                # Time stop: 10 trading days max
                if position.days_held >= 10 and position.shares > 0:
                    realized_today += position.shares * (today_close - position.entry_price)
                    exit_reason = "time_stop"
                    position.partial_exits.append({
                        "reason": "time_stop",
                        "price": today_close,
                        "shares": position.shares,
                    })
                    position.shares = 0.0

                # If all tranches exited by take-profit but no explicit exit reason
                if position.shares <= 0.001 and exit_reason is None:
                    exit_reason = "take_profit_all"

            # Close out position if fully exited
            if position.shares <= 0.001:
                total_pnl = sum(
                    pe["shares"] * (pe["price"] - position.entry_price)
                    for pe in position.partial_exits
                )

                # Determine average exit price
                total_exit_shares = sum(pe["shares"] for pe in position.partial_exits)
                avg_exit = (
                    sum(pe["shares"] * pe["price"] for pe in position.partial_exits) / total_exit_shares
                    if total_exit_shares > 0 else position.entry_price
                )

                trade = Trade(
                    entry_date=position.entry_date,
                    exit_date=str(date.date()),
                    ticker=ticker,
                    entry_price=round(position.entry_price, 2),
                    exit_price=round(avg_exit, 2),
                    shares=round(total_exit_shares, 4),
                    pnl=round(total_pnl, 2),
                    hold_days=position.days_held,
                    exit_reason=exit_reason or "unknown",
                    partial_exits=position.partial_exits,
                )
                trades.append(trade)
                capital += total_pnl
                position = None

        # --- Entry logic (only if flat) --------------------------------------
        if position is None:
            regime = check_regime(spy_row)
            if regime != "UP":
                continue  # Skip — regime is DOWN

            signals = check_signals(ticker_row)
            fired = sum(signals.values())

            if fired < 3:
                continue  # Need all 3 technical signals

            atr = float(ticker_row["atr_14"])
            if pd.isna(atr) or atr <= 0:
                continue

            entry_price = today_close
            shares, stop_price, risk_per_share = calculate_position_size(
                entry_price, atr, risk_dollars=25.0
            )

            if shares <= 0:
                continue

            # Check if we can afford this position
            cost = shares * entry_price
            if cost > capital:
                # Scale down to what we can afford
                shares = capital / entry_price
                if shares < 0.001:
                    continue

            position = OpenPosition(
                entry_date=str(date.date()),
                entry_price=entry_price,
                shares=shares,
                stop_price=stop_price,
                atr_at_entry=atr,
                risk_per_share=risk_per_share,
                tranche_1_shares=round(shares * 0.40, 6),
                tranche_2_shares=round(shares * 0.40, 6),
                tranche_3_shares=round(shares * 0.20, 6),
            )

    # --- Close any position still open at end of backtest --------------------
    if position is not None:
        final_close = float(ticker_df.loc[trading_dates[-1]]["close"])
        total_pnl = position.shares * (final_close - position.entry_price)
        trade = Trade(
            entry_date=position.entry_date,
            exit_date=str(trading_dates[-1].date()),
            ticker=ticker,
            entry_price=round(position.entry_price, 2),
            exit_price=round(final_close, 2),
            shares=round(position.shares, 4),
            pnl=round(total_pnl, 2),
            hold_days=position.days_held,
            exit_reason="backtest_end",
        )
        trades.append(trade)
        capital += total_pnl

    # --- Compute summary statistics ------------------------------------------
    summary = compute_summary(trades, starting_capital, capital, equity_curve, daily_returns)
    return trades, summary


def compute_summary(
    trades: list[Trade],
    starting_capital: float,
    final_capital: float,
    equity_curve: list[float],
    daily_returns: list[float],
) -> dict:
    """Compute backtest summary statistics."""
    total_trades = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)

    # Max drawdown from equity curve
    max_drawdown = 0.0
    peak = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd

    # Sharpe ratio (annualized, assuming 252 trading days)
    sharpe = 0.0
    if daily_returns:
        ret_arr = np.array(daily_returns)
        if ret_arr.std() > 0:
            sharpe = (ret_arr.mean() / ret_arr.std()) * np.sqrt(252)

    pnls = [t.pnl for t in trades]
    hold_days = [t.hold_days for t in trades]

    return {
        "ticker": trades[0].ticker if trades else "N/A",
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total_trades * 100, 1) if total_trades > 0 else 0.0,
        "starting_capital": round(starting_capital, 2),
        "final_capital": round(final_capital, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_pnl / starting_capital * 100, 2) if starting_capital > 0 else 0.0,
        "avg_pnl_per_trade": round(total_pnl / total_trades, 2) if total_trades > 0 else 0.0,
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "avg_hold_days": round(sum(hold_days) / len(hold_days), 1) if hold_days else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_report(trades: list[Trade], summary: dict) -> None:
    """Print a formatted backtest report to stdout."""
    print("=" * 80)
    print("  PARALLAX — BACKTEST REPORT")
    print("=" * 80)
    print()

    # Trade log
    if trades:
        print(f"{'DATE':>12}  {'EXIT':>12}  {'TICKER':<6}  {'ENTRY':>8}  {'EXIT':>8}  "
              f"{'P&L':>8}  {'DAYS':>5}  {'REASON'}")
        print("-" * 80)
        for t in trades:
            pnl_str = f"${t.pnl:+.2f}"
            print(f"{t.entry_date:>12}  {t.exit_date:>12}  {t.ticker:<6}  "
                  f"${t.entry_price:>7.2f}  ${t.exit_price:>7.2f}  "
                  f"{pnl_str:>8}  {t.hold_days:>5}  {t.exit_reason}")
    else:
        print("  No trades executed during backtest period.")

    print()
    print("=" * 80)
    print("  SUMMARY")
    print("=" * 80)
    print()
    print(f"  Ticker:              {summary['ticker']}")
    print(f"  Total Trades:        {summary['total_trades']}")
    print(f"  Wins / Losses:       {summary['wins']} / {summary['losses']}")
    print(f"  Win Rate:            {summary['win_rate']}%")
    print()
    print(f"  Starting Capital:    ${summary['starting_capital']:,.2f}")
    print(f"  Final Capital:       ${summary['final_capital']:,.2f}")
    print(f"  Total P&L:           ${summary['total_pnl']:+,.2f}")
    print(f"  Total Return:        {summary['total_return_pct']:+.2f}%")
    print(f"  Avg P&L / Trade:     ${summary['avg_pnl_per_trade']:+,.2f}")
    print()
    print(f"  Best Trade:          ${summary['best_trade']:+,.2f}")
    print(f"  Worst Trade:         ${summary['worst_trade']:+,.2f}")
    print(f"  Avg Hold Time:       {summary['avg_hold_days']} days")
    print()
    print(f"  Max Drawdown:        {summary['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe Ratio (ann):  {summary['sharpe_ratio']:.2f}")
    print()
    print("=" * 80)
    print("  NOTE: Signals 4-6 (fundamental, sentiment, flow) are not modeled.")
    print("  Real performance will differ. This tests the technical edge only.")
    print("=" * 80)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Parallax — Backtest the 6-signal swing trading system"
    )
    parser.add_argument(
        "--ticker", default="NVDA",
        help="Ticker symbol to backtest (default: NVDA)"
    )
    parser.add_argument(
        "--days", type=int, default=180,
        help="Number of calendar days to look back (default: 180)"
    )
    parser.add_argument(
        "--starting-capital", type=float, default=500.0,
        help="Starting capital in USD (default: 500)"
    )
    parser.add_argument(
        "--json", action="store_true", dest="output_json",
        help="Also output a JSON summary to stdout"
    )

    args = parser.parse_args()

    trades, summary = run_backtest(
        ticker=args.ticker.upper(),
        days=args.days,
        starting_capital=args.starting_capital,
    )

    print_report(trades, summary)

    if args.output_json:
        print()
        print("--- JSON SUMMARY ---")
        json_output = {
            "summary": summary,
            "trades": [
                {
                    "entry_date": t.entry_date,
                    "exit_date": t.exit_date,
                    "ticker": t.ticker,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "shares": t.shares,
                    "pnl": t.pnl,
                    "hold_days": t.hold_days,
                    "exit_reason": t.exit_reason,
                }
                for t in trades
            ],
        }
        print(json.dumps(json_output, indent=2))


if __name__ == "__main__":
    main()
