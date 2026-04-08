#!/usr/bin/env python3
"""
Options Flow Ingest — loads unusual options activity into options_flow_signals.

Two modes:
  1. Manual CSV (data/options_flow.csv) — default when UNUSUAL_WHALES_API_KEY is not set.
  2. Unusual Whales API — stub; enabled when UNUSUAL_WHALES_API_KEY env var is present.

Scoring: score_options_signal() returns 1–10 based on premium size,
signal type, IV rank, and sentiment alignment.

Runs as a standalone script (cron 7 AM PST weekdays) or imported for its
scoring function by the scanner enrichment layer.
"""

import csv
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import slack_notify
from tracer import PipelineTracer, _post_to_supabase, set_active_tracer, traced

# ===========================================================================
# Config
# ===========================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
UNUSUAL_WHALES_KEY = os.environ.get("UNUSUAL_WHALES_API_KEY", "")

# Path to the manual CSV (relative to repo root, resolved at runtime)
_REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = _REPO_ROOT / "data" / "options_flow.csv"

# Expected CSV columns (in any order — we use DictReader)
CSV_COLUMNS = {
    "ticker",
    "signal_date",
    "signal_type",
    "strike",
    "expiry",
    "premium",
    "open_interest",
    "volume",
    "implied_volatility",
    "sentiment",
}

# Valid enum values (must match DB CHECK constraints)
VALID_SIGNAL_TYPES = {"unusual_call", "unusual_put", "sweep", "block", "darkpool"}
VALID_SENTIMENTS = {"bullish", "bearish", "neutral"}


# ===========================================================================
# Scoring
# ===========================================================================
def score_options_signal(row: dict) -> int:
    """Score an options flow signal 1–10.

    Args:
        row: dict with keys: premium (float), signal_type (str),
             implied_volatility (float), sentiment (str).

    Returns:
        Integer score 1–10. Returns 0 if the row is clearly invalid.
    """
    score = 1  # base

    # Premium size
    try:
        premium = float(row.get("premium") or 0)
    except (ValueError, TypeError):
        premium = 0.0

    if premium >= 1_000_000:
        score += 3
    elif premium >= 500_000:
        score += 2
    elif premium >= 100_000:
        score += 1

    # Signal type
    signal_type = str(row.get("signal_type") or "").lower().strip()
    if signal_type in ("sweep", "block"):
        score += 2
    elif signal_type == "darkpool":
        score += 1

    # IV rank
    try:
        iv = float(row.get("implied_volatility") or 0)
    except (ValueError, TypeError):
        iv = 0.0

    if iv >= 0.70:
        score += 2
    elif iv >= 0.50:
        score += 1

    return min(score, 10)


# ===========================================================================
# Data sources
# ===========================================================================
def load_csv() -> list[dict]:
    """Read options flow signals from data/options_flow.csv.

    Returns an empty list (no error raised) if the file does not exist.
    """
    if not CSV_PATH.exists():
        print(f"[options_flow] CSV not found at {CSV_PATH} — skipping manual load")
        return []

    rows: list[dict] = []
    with CSV_PATH.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for i, raw in enumerate(reader, start=1):
            # Normalise keys to lowercase strip
            row = {k.strip().lower(): v.strip() for k, v in raw.items()}

            # Validate required fields
            ticker = row.get("ticker", "").upper()
            signal_type = row.get("signal_type", "").lower()
            sentiment = row.get("sentiment", "").lower()

            if not ticker:
                print(f"[options_flow] Row {i}: missing ticker — skipped")
                continue
            if signal_type not in VALID_SIGNAL_TYPES:
                print(
                    f"[options_flow] Row {i} ({ticker}): invalid signal_type "
                    f"'{signal_type}' — skipped"
                )
                continue
            if sentiment and sentiment not in VALID_SENTIMENTS:
                print(
                    f"[options_flow] Row {i} ({ticker}): invalid sentiment "
                    f"'{sentiment}' — defaulting to neutral"
                )
                sentiment = "neutral"

            # Parse numeric fields (None if blank/invalid)
            def _num(key: str) -> float | None:
                val = row.get(key, "")
                try:
                    return float(val) if val else None
                except ValueError:
                    return None

            rows.append({
                "ticker": ticker,
                "signal_date": row.get("signal_date") or date.today().isoformat(),
                "signal_type": signal_type,
                "strike": _num("strike"),
                "expiry": row.get("expiry") or None,
                "premium": _num("premium"),
                "open_interest": int(_num("open_interest") or 0) or None,
                "volume": int(_num("volume") or 0) or None,
                "implied_volatility": _num("implied_volatility"),
                "sentiment": sentiment or "neutral",
                "source": "manual",
                "raw_data": dict(raw),
            })

    print(f"[options_flow] Loaded {len(rows)} rows from CSV")
    return rows


def fetch_from_unusual_whales(api_key: str) -> list[dict]:
    """Fetch unusual options activity from the Unusual Whales API.

    This is a stub — the full implementation requires a paid API subscription.
    When UNUSUAL_WHALES_API_KEY is not set, this function prints a warning and
    returns an empty list so the script can still run in CSV-only mode.

    Args:
        api_key: Unusual Whales API key from UNUSUAL_WHALES_API_KEY env var.

    Returns:
        List of normalised option flow dicts (empty until API is wired up).
    """
    if not api_key:
        print(
            "[options_flow] UNUSUAL_WHALES_API_KEY not configured — "
            "set env var to enable live options flow"
        )
        return []

    # Stub: API integration not yet implemented.
    # When ready, call https://api.unusualwhales.com/api/option-contracts/flow
    # with Authorization: Bearer {api_key} and map response to our schema.
    print("[options_flow] Unusual Whales API key found but integration is a stub — returning empty list")
    return []


# ===========================================================================
# DB insert
# ===========================================================================
@traced("ingest")
def insert_signals(signals: list[dict]) -> int:
    """Upsert options flow signals to Supabase. Returns inserted count."""
    if not signals:
        return 0

    inserted = 0
    for sig in signals:
        score = score_options_signal(sig)
        record = {**sig, "raw_data": {**sig.get("raw_data", {}), "score": score}}

        # Remove None values so Supabase uses column defaults
        record = {k: v for k, v in record.items() if v is not None}

        stored = _post_to_supabase("options_flow_signals", record)
        if stored:
            inserted += 1

    return inserted


# ===========================================================================
# Entry point
# ===========================================================================
def run() -> None:
    tracer = PipelineTracer(
        "ingest_options_flow",
        metadata={"time": datetime.now(timezone.utc).isoformat()},
    )
    set_active_tracer(tracer)

    try:
        # Step 1: collect signals
        signals: list[dict] = []

        with tracer.step("fetch_signals") as result:
            if UNUSUAL_WHALES_KEY:
                live = fetch_from_unusual_whales(UNUSUAL_WHALES_KEY)
                signals.extend(live)
                result.set({"source": "unusual_whales", "count": len(live)})
            else:
                csv_rows = load_csv()
                signals.extend(csv_rows)
                result.set({"source": "csv", "count": len(csv_rows)})

        # Step 2: score + insert
        with tracer.step("insert_signals", input_snapshot={"total": len(signals)}) as result:
            inserted = insert_signals(signals)
            result.set({"inserted": inserted, "total": len(signals)})

        tracer.complete({"inserted": inserted, "total": len(signals)})
        print(f"[options_flow] Complete — {inserted}/{len(signals)} signals inserted")

        if signals:
            slack_notify(
                f"*Options Flow Ingest* — `{inserted}` signals inserted "
                f"({'unusual_whales' if UNUSUAL_WHALES_KEY else 'csv'})"
            )

    except Exception as e:
        tracer.fail(str(e))
        print(f"[options_flow] Fatal: {e}")
        slack_notify(f"*Options Flow Ingest FATAL*: {e}")
        raise


if __name__ == "__main__":
    run()
