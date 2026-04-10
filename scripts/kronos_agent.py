#!/usr/bin/env python3
"""
Kronos inference agent — pure price pattern forecasting using the Kronos
financial time series foundation model.

Memory lifecycle: unload Ollama -> load Kronos -> infer -> unload Kronos -> cleanup
This ensures Kronos and Ollama never compete for GPU memory on the Jetson.

Real Kronos API (verified from /home/ridley/Kronos/model/kronos.py):
  - KronosPredictor.__init__(model, tokenizer, device=None, max_context=512, clip=5)
  - KronosPredictor.predict(df, x_timestamp, y_timestamp, pred_len, T, top_k, top_p, sample_count, verbose)
    * x_timestamp: pd.Series of historical datetime values (MUST be Series, not DatetimeIndex)
    * y_timestamp: pd.Series of future prediction datetime values (length == pred_len)
    * Returns pd.DataFrame with columns: open, high, low, close, volume, amount
    * sample_count > 1 returns a single internally-averaged DataFrame (not a distribution)
  - For Monte Carlo: call predict() NUM_PATHS times with sample_count=1, collect close[HORIZON_BAR]
"""

import gc
import os
import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd

# Add Kronos repo to path (ridley deployment path)
KRONOS_REPO_PATH = "/home/ridley/Kronos"
sys.path.insert(0, KRONOS_REPO_PATH)

TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
MODEL_ID = "NeoQuasar/Kronos-small"
PREDICTION_LENGTH = 15
NUM_PATHS = 50          # Monte Carlo paths (each is a separate predict() call)
HORIZON_BAR = 10        # Evaluate bullish/bearish direction at this bar index (0-based)
BULLISH_THRESHOLD = 0.60
BEARISH_THRESHOLD = 0.40
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


def unload_ollama() -> None:
    """Tell Ollama to drop all loaded models from GPU memory.

    Sends keep_alive=0 for the primary model used by the inference engine.
    Safe to call even if Ollama is not running — errors are silently swallowed.
    """
    # Import here to avoid pulling in common.py's full init at module load time
    try:
        import httpx

        httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": "qwen2.5:3b", "keep_alive": 0},
            timeout=10.0,
        )
        time.sleep(1)  # Give Ollama a moment to release VRAM
    except Exception:
        pass  # Ollama may not be running — that's fine


def get_ohlcv_bars(ticker: str, days: int = 252) -> pd.DataFrame:
    """Fetch daily OHLCV bars via yfinance.

    Returns a DataFrame with lowercase columns: open, high, low, close, volume, amount.
    Raises ValueError if data is unavailable or required columns are missing.
    """
    import yfinance as yf

    data = yf.download(ticker, period=f"{days}d", interval="1d", progress=False, auto_adjust=True)
    if data.empty:
        raise ValueError(f"No yfinance data for {ticker}")

    # yfinance may return MultiIndex columns — flatten to lowercase strings
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [c[0].lower() for c in data.columns]
    else:
        data.columns = [str(c).lower() for c in data.columns]

    for col in ("open", "high", "low", "close", "volume"):
        if col not in data.columns:
            raise ValueError(f"Missing required column '{col}' for {ticker}")

    # Kronos expects an 'amount' column (turnover = volume * typical_price)
    if "amount" not in data.columns:
        typical = (data["open"] + data["high"] + data["low"] + data["close"]) / 4.0
        data["amount"] = data["volume"] * typical

    # Drop any rows with NaN in the key columns
    key_cols = ["open", "high", "low", "close", "volume", "amount"]
    data = data[key_cols].dropna()

    if len(data) < PREDICTION_LENGTH + 1:
        raise ValueError(
            f"Insufficient clean bars for {ticker}: "
            f"need {PREDICTION_LENGTH + 1}, got {len(data)}"
        )

    return data


def _make_future_timestamps(last_date: pd.Timestamp, n: int) -> pd.Series:
    """Generate n future business-day timestamps starting the day after last_date.

    Uses pandas bdate_range (Mon–Fri, no holiday calendar) to stay consistent
    with how the model was trained on exchange trading days.
    """
    future_dates = pd.bdate_range(start=last_date + timedelta(days=1), periods=n)
    # Kronos needs timestamps normalized to midnight (no intraday offset)
    future_dates = future_dates.normalize()
    return pd.Series(future_dates)


def run_kronos_inference(
    ticker: str,
    bars_df: pd.DataFrame | None = None,
) -> dict:
    """Run Kronos Monte Carlo inference on a ticker.

    Memory lifecycle:
      1. Unload Ollama from GPU (keep_alive=0)
      2. Load Kronos model + tokenizer (auto-dispatches to CUDA on Jetson)
      3. Run NUM_PATHS independent prediction paths (sample_count=1 each)
      4. Compute bullish probability: fraction of paths where close[HORIZON_BAR] > current_price
      5. Mandatory cleanup: del objects, gc.collect(), torch.cuda.empty_cache()

    Args:
        ticker:   Stock ticker symbol (e.g. "NVDA").
        bars_df:  Optional pre-fetched OHLCV DataFrame. If None, fetches via yfinance.

    Returns:
        dict with keys:
          ticker, bullish_prob, bearish_prob, direction, current_price,
          mean_predicted_price, horizon, paths, elapsed_ms
        On error: adds 'error' key and sets bullish_prob=0.5, direction='neutral'.
    """
    # Defer heavy imports so this module is importable without torch/yfinance installed
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer

    t0 = time.time()
    predictor: KronosPredictor | None = None
    model: Kronos | None = None
    tokenizer: KronosTokenizer | None = None

    try:
        # --- Step 1: Unload Ollama ---
        unload_ollama()

        # --- Step 2: Load Kronos (auto-detects cuda:0 on Jetson) ---
        model = Kronos.from_pretrained(MODEL_ID)
        tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_ID)
        predictor = KronosPredictor(model, tokenizer, max_context=512)

        # --- Step 3: Prepare data ---
        if bars_df is None:
            bars_df = get_ohlcv_bars(ticker, days=252)

        current_price = float(bars_df["close"].iloc[-1])
        last_date = pd.Timestamp(bars_df.index[-1])

        # x_timestamp MUST be pd.Series (not DatetimeIndex) for .dt accessor in calc_time_stamps
        x_timestamp = pd.Series(pd.DatetimeIndex(bars_df.index).normalize())

        # y_timestamp: NUM future business-day dates
        y_timestamp = _make_future_timestamps(last_date, PREDICTION_LENGTH)

        # Kronos input DataFrame — ensure correct column order
        ohlcva_df = bars_df[["open", "high", "low", "close", "volume", "amount"]].copy()

        # --- Step 4: Monte Carlo paths ---
        horizon_closes: list[float] = []
        for _ in range(NUM_PATHS):
            try:
                pred = predictor.predict(
                    df=ohlcva_df,
                    x_timestamp=x_timestamp,
                    y_timestamp=y_timestamp,
                    pred_len=PREDICTION_LENGTH,
                    T=1.0,
                    top_k=0,
                    top_p=0.9,
                    sample_count=1,
                    verbose=False,
                )
                # pred is a DataFrame indexed by y_timestamp with HORIZON_BAR rows
                if isinstance(pred, pd.DataFrame) and len(pred) > HORIZON_BAR:
                    horizon_closes.append(float(pred["close"].iloc[HORIZON_BAR]))
            except Exception as path_err:
                # A single path failure is non-fatal — log and continue
                print(f"[kronos] Path error for {ticker}: {path_err}")
                continue

        elapsed_ms = int((time.time() - t0) * 1000)

        if not horizon_closes:
            return {
                "ticker": ticker,
                "bullish_prob": 0.5,
                "bearish_prob": 0.5,
                "direction": "neutral",
                "current_price": current_price,
                "mean_predicted_price": current_price,
                "horizon": HORIZON_BAR,
                "paths": 0,
                "elapsed_ms": elapsed_ms,
                "error": "No valid prediction paths completed",
            }

        # --- Step 5: Compute probabilities ---
        bullish_count = sum(1 for c in horizon_closes if c > current_price)
        bullish_prob = round(bullish_count / len(horizon_closes), 4)
        bearish_prob = round(1.0 - bullish_prob, 4)
        mean_predicted = round(float(np.mean(horizon_closes)), 2)

        if bullish_prob >= BULLISH_THRESHOLD:
            direction = "bullish"
        elif bullish_prob <= BEARISH_THRESHOLD:
            direction = "bearish"
        else:
            direction = "neutral"

        return {
            "ticker": ticker,
            "bullish_prob": bullish_prob,
            "bearish_prob": bearish_prob,
            "direction": direction,
            "current_price": current_price,
            "mean_predicted_price": mean_predicted,
            "horizon": HORIZON_BAR,
            "paths": len(horizon_closes),
            "elapsed_ms": elapsed_ms,
        }

    finally:
        # --- Step 6: Mandatory cleanup — always runs even on exception ---
        del predictor, model, tokenizer
        gc.collect()
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
        print(f"[kronos] Inference complete for {ticker} — GPU memory freed")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Kronos inference agent — Monte Carlo price forecasting")
    parser.add_argument("ticker", nargs="?", default="NVDA", help="Ticker to forecast (default: NVDA)")
    parser.add_argument(
        "--paths",
        type=int,
        default=NUM_PATHS,
        help=f"Number of Monte Carlo paths (default: {NUM_PATHS})",
    )
    args = parser.parse_args()

    print(f"[kronos] Starting inference for {args.ticker} ({args.paths} paths)...")
    result = run_kronos_inference(args.ticker)
    print("\n[kronos] Result:")
    for k, v in result.items():
        print(f"  {k}: {v}")
