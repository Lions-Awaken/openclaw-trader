#!/usr/bin/env python3
"""Unified signal ingest — Form 4 insider filings and options flow.

Usage:
    python scripts/ingest_signals.py form4      # SEC EDGAR Form 4 insider filings
    python scripts/ingest_signals.py options    # Options flow (CSV or Unusual Whales API)

Form 4 mode (replaces ingest_form4.py):
    Fetches SEC EDGAR Form 4 filings for the active watchlist + AI infra tickers.
    Scores purchase transactions 1–10 and writes to form4_signals.
    Runs at 6:00 AM PDT weekdays.

Options mode (replaces ingest_options_flow.py):
    Loads unusual options activity from data/options_flow.csv (manual) or
    the Unusual Whales API (when UNUSUAL_WHALES_API_KEY is set).
    Scores signals 1–10 and writes to options_flow_signals.
    Runs at 7:00 AM PDT weekdays.
"""

import argparse
import csv
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import _client, load_strategy_profile, sb_get, slack_notify
from tracer import PipelineTracer, _post_to_supabase, set_active_tracer, traced

# ===========================================================================
# Shared config
# ===========================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")

# ===========================================================================
# Form 4 — config
# ===========================================================================
AI_INFRA_TICKERS = ["NVDA", "AMD", "AVGO", "SMCI", "MRVL", "DELL", "PLTR", "ARM"]

# SEC EDGAR EFTS full-text search endpoint
EDGAR_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

# User-Agent required by SEC EDGAR
SEC_USER_AGENT = "OpenClaw-Trader/1.0 (research; github.com/Lions-Awaken)"

LOOKBACK_DAYS = 3

SENIOR_TITLES = {"ceo", "cfo", "coo", "president", "chairman", "chief executive", "chief financial"}
MID_TITLES = {"vp", "vice president", "director", "svp", "evp", "general counsel", "treasurer"}

# ===========================================================================
# Options Flow — config
# ===========================================================================
UNUSUAL_WHALES_KEY = os.environ.get("UNUSUAL_WHALES_API_KEY", "")

_REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = _REPO_ROOT / "data" / "options_flow.csv"

VALID_SIGNAL_TYPES = {"unusual_call", "unusual_put", "sweep", "block", "darkpool"}
VALID_SENTIMENTS = {"bullish", "bearish", "neutral"}


# ===========================================================================
# Form 4 — scoring
# ===========================================================================
def score_form4_signal(row: dict) -> int:
    """Score a Form 4 filing signal 1–10.

    Args:
        row: dict with keys: transaction_type (str), total_value (float),
             ownership_pct_change (float), cluster_count (int),
             filer_title (str).

    Returns:
        Integer score 1–10. Returns 0 for non-purchase transactions.
    """
    transaction_type = str(row.get("transaction_type") or "").lower().strip()
    if transaction_type != "purchase":
        return 0  # Only purchases carry bullish signal

    score = 1  # base

    # Total value of the purchase
    try:
        total_value = float(row.get("total_value") or 0)
    except (ValueError, TypeError):
        total_value = 0.0

    if total_value >= 1_000_000:
        score += 3
    elif total_value >= 500_000:
        score += 2
    elif total_value >= 100_000:
        score += 1

    # Ownership percentage change
    try:
        pct_change = float(row.get("ownership_pct_change") or 0)
    except (ValueError, TypeError):
        pct_change = 0.0

    if pct_change >= 0.10:
        score += 3
    elif pct_change >= 0.05:
        score += 2
    elif pct_change >= 0.01:
        score += 1

    # Cluster count (additional buyers beyond the first, capped at +4)
    try:
        cluster_count = int(row.get("cluster_count") or 1)
    except (ValueError, TypeError):
        cluster_count = 1

    cluster_bonus = min((cluster_count - 1) * 2, 4)
    score += cluster_bonus

    # Filer title seniority
    filer_title = str(row.get("filer_title") or "").lower()
    if any(t in filer_title for t in SENIOR_TITLES):
        score += 2
    elif any(t in filer_title for t in MID_TITLES):
        score += 1

    return min(score, 10)


# ===========================================================================
# Form 4 — watchlist
# ===========================================================================
def get_target_tickers() -> list[str]:
    """Combine active profile watchlist with AI infra hardcoded list."""
    tickers: set[str] = set(AI_INFRA_TICKERS)

    try:
        profile = load_strategy_profile()
        watchlist = profile.get("watchlist", [])
        if isinstance(watchlist, list):
            tickers.update(t.upper() for t in watchlist if isinstance(t, str))
    except Exception as e:
        print(f"[form4] Could not load strategy profile watchlist: {e}")

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
        evals = sb_get("signal_evaluations", {
            "select": "ticker",
            "signal_date": f"gte.{cutoff}",
        })
        for row in evals:
            t = row.get("ticker", "")
            if t:
                tickers.add(t.upper())
    except Exception as e:
        print(f"[form4] Could not query signal_evaluations for tickers: {e}")

    result = sorted(tickers)
    print(f"[form4] Target tickers ({len(result)}): {result}")
    return result


# ===========================================================================
# Form 4 — EDGAR fetcher
# ===========================================================================
@traced("ingest")
def fetch_edgar_form4(start_dt: date, end_dt: date) -> list[dict]:
    """Query SEC EDGAR EFTS for Form 4 filings in the given date range."""
    try:
        resp = _client.get(
            EDGAR_EFTS_URL,
            headers={"User-Agent": SEC_USER_AGENT},
            params={
                "q": '"4"',
                "forms": "4",
                "dateRange": "custom",
                "startdt": start_dt.isoformat(),
                "enddt": end_dt.isoformat(),
            },
            timeout=20.0,
        )
    except Exception as e:
        print(f"[form4] EDGAR EFTS request failed: {e}")
        return []

    if resp.status_code != 200:
        print(f"[form4] EDGAR EFTS HTTP {resp.status_code}: {resp.text[:200]}")
        return []

    try:
        data = resp.json()
    except Exception as e:
        print(f"[form4] EDGAR EFTS JSON parse error: {e}")
        return []

    hits = data.get("hits", {}).get("hits", [])
    filings: list[dict] = []
    for hit in hits:
        src = hit.get("_source", {})
        filings.append(src)

    print(f"[form4] EDGAR returned {len(filings)} Form 4 filings for {start_dt} – {end_dt}")
    return filings


# ===========================================================================
# Form 4 — parse + filter
# ===========================================================================
def _extract_ticker(filing: dict) -> str | None:
    """Best-effort extraction of the primary ticker from an EDGAR filing record."""
    ticker = (
        filing.get("ticker_symbol")
        or filing.get("ticker")
        or filing.get("entity_ticker")
    )
    if ticker:
        return str(ticker).upper().strip()
    return None


def _extract_float(filing: dict, *keys: str) -> float | None:
    for key in keys:
        val = filing.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return None


def _extract_int(filing: dict, *keys: str) -> int | None:
    for key in keys:
        val = filing.get(key)
        if val is not None:
            try:
                return int(float(val))
            except (ValueError, TypeError):
                continue
    return None


def parse_filings(filings: list[dict], target_tickers: set[str]) -> list[dict]:
    """Filter to target tickers, normalise fields, skip sales."""
    records: list[dict] = []
    today = date.today().isoformat()

    for filing in filings:
        ticker = _extract_ticker(filing)
        if not ticker or ticker not in target_tickers:
            continue

        filing_date = filing.get("file_date") or filing.get("period_of_report") or today
        signal_date = filing.get("period_of_report") or filing_date

        filer_name = str(filing.get("display_date_filed") or filing.get("entity_name") or "")
        filer_title = str(filing.get("officer_title") or "")

        transaction_type_raw = str(filing.get("transaction_code") or "").upper()
        transaction_type_map = {
            "P": "purchase",
            "S": "sale",
            "G": "gift",
            "M": "exercise",
            "A": "purchase",  # Grant/award treated as purchase for scoring
        }
        transaction_type = transaction_type_map.get(transaction_type_raw, "sale")

        if transaction_type == "sale":
            continue

        shares = _extract_int(filing, "shares", "transaction_shares")
        price_per_share = _extract_float(filing, "price_per_share", "transaction_price_per_share")
        total_value = _extract_float(filing, "total_value", "transaction_total_value")

        if total_value is None and shares is not None and price_per_share is not None:
            total_value = round(shares * price_per_share, 2)

        shares_owned_after = _extract_int(filing, "shares_owned_after", "shares_owned_following_transaction")
        ownership_pct_change = _extract_float(filing, "ownership_pct_change")

        records.append({
            "ticker": ticker,
            "signal_date": signal_date,
            "filing_date": filing_date,
            "filer_name": filer_name[:200] if filer_name else None,
            "filer_title": filer_title[:100] if filer_title else None,
            "transaction_type": transaction_type,
            "shares": shares,
            "price_per_share": price_per_share,
            "total_value": total_value,
            "shares_owned_after": shares_owned_after,
            "ownership_pct_change": ownership_pct_change,
            "days_since_last_filing": None,
            "cluster_count": 1,
            "source": "sec_edgar",
            "raw_data": filing,
        })

    return records


def _detect_clusters(records: list[dict]) -> list[dict]:
    """Update cluster_count for tickers with multiple buyers in the batch."""
    ticker_counts: Counter = Counter(r["ticker"] for r in records)
    for rec in records:
        rec["cluster_count"] = ticker_counts[rec["ticker"]]
    return records


# ===========================================================================
# Form 4 — DB insert
# ===========================================================================
@traced("ingest")
def insert_form4_signals(records: list[dict]) -> int:
    """Score and insert Form 4 signals into Supabase. Returns inserted count."""
    if not records:
        return 0

    inserted = 0
    for rec in records:
        score = score_form4_signal(rec)
        if score == 0:
            continue

        row = {**rec, "raw_data": {**rec.get("raw_data", {}), "score": score}}
        row = {k: v for k, v in row.items() if v is not None}

        stored = _post_to_supabase("form4_signals", row)
        if stored:
            inserted += 1

    return inserted


# ===========================================================================
# Options Flow — scoring
# ===========================================================================
def score_options_signal(sig: dict) -> int:
    """Score an options flow signal 1–10.

    Args:
        sig: dict with keys: premium (float), signal_type (str),
             implied_volatility (float), sentiment (str).

    Returns:
        Integer score 1–10. Returns 0 if the row is clearly invalid.
    """
    score = 1  # base

    # Premium size
    try:
        premium = float(sig.get("premium") or 0)
    except (ValueError, TypeError):
        premium = 0.0

    if premium >= 1_000_000:
        score += 3
    elif premium >= 500_000:
        score += 2
    elif premium >= 100_000:
        score += 1

    # Signal type
    signal_type = str(sig.get("signal_type") or "").lower().strip()
    if signal_type in ("sweep", "block"):
        score += 2
    elif signal_type == "darkpool":
        score += 1

    # IV rank
    try:
        iv = float(sig.get("implied_volatility") or 0)
    except (ValueError, TypeError):
        iv = 0.0

    if iv >= 0.70:
        score += 2
    elif iv >= 0.50:
        score += 1

    return min(score, 10)


# ===========================================================================
# Options Flow — data sources
# ===========================================================================
def load_options_csv(path: str) -> list[dict]:
    """Read options flow signals from the given CSV path.

    Returns an empty list (no error raised) if the file does not exist.
    """
    csv_path = Path(path)
    if not csv_path.exists():
        print(f"[options_flow] CSV not found at {csv_path} — skipping manual load")
        return []

    rows: list[dict] = []
    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for i, raw in enumerate(reader, start=1):
            row = {k.strip().lower(): v.strip() for k, v in raw.items()}

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
    When UNUSUAL_WHALES_API_KEY is not set this function returns an empty list
    so the script can run in CSV-only mode.

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
# Options Flow — DB insert
# ===========================================================================
@traced("ingest")
def insert_options_signals(signals: list[dict]) -> int:
    """Score and upsert options flow signals to Supabase. Returns inserted count."""
    if not signals:
        return 0

    inserted = 0
    for sig in signals:
        score = score_options_signal(sig)
        record = {**sig, "raw_data": {**sig.get("raw_data", {}), "score": score}}
        record = {k: v for k, v in record.items() if v is not None}

        stored = _post_to_supabase("options_flow_signals", record)
        if stored:
            inserted += 1

    return inserted


# ===========================================================================
# Mode runners
# ===========================================================================
def run_form4() -> None:
    """Run the Form 4 insider filing ingest (replaces ingest_form4.py)."""
    tracer = PipelineTracer(
        "ingest_form4",
        metadata={"time": datetime.now(timezone.utc).isoformat()},
    )
    set_active_tracer(tracer)

    try:
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)

        with tracer.step("load_watchlist") as result:
            target_tickers = get_target_tickers()
            result.set({"ticker_count": len(target_tickers)})

        with tracer.step("fetch_edgar", input_snapshot={"start": start_dt.isoformat(), "end": end_dt.isoformat()}) as result:
            filings = fetch_edgar_form4(start_dt, end_dt)
            result.set({"raw_filings": len(filings)})
            time.sleep(1)

        with tracer.step("parse_filings", input_snapshot={"filings": len(filings)}) as result:
            records = parse_filings(filings, set(target_tickers))
            records = _detect_clusters(records)
            result.set({"parsed": len(records)})

        with tracer.step("insert_signals", input_snapshot={"records": len(records)}) as result:
            inserted = insert_form4_signals(records)
            result.set({"inserted": inserted, "total": len(records)})

        tracer.complete({"inserted": inserted, "total": len(records)})
        print(f"[form4] Complete — {inserted}/{len(records)} signals inserted")

        if inserted:
            slack_notify(
                f"*Form 4 Ingest* — `{inserted}` insider purchase signals inserted "
                f"(EDGAR lookback {LOOKBACK_DAYS}d, {len(target_tickers)} tickers)"
            )

    except Exception as e:
        tracer.fail(str(e))
        print(f"[form4] Fatal: {e}")
        slack_notify(f"*Form 4 Ingest FATAL*: {e}")
        raise


def run_options() -> None:
    """Run the options flow ingest (replaces ingest_options_flow.py)."""
    tracer = PipelineTracer(
        "ingest_options_flow",
        metadata={"time": datetime.now(timezone.utc).isoformat()},
    )
    set_active_tracer(tracer)

    try:
        signals: list[dict] = []

        with tracer.step("fetch_signals") as result:
            if UNUSUAL_WHALES_KEY:
                live = fetch_from_unusual_whales(UNUSUAL_WHALES_KEY)
                signals.extend(live)
                result.set({"source": "unusual_whales", "count": len(live)})
            else:
                csv_rows = load_options_csv(str(CSV_PATH))
                signals.extend(csv_rows)
                result.set({"source": "csv", "count": len(csv_rows)})

        with tracer.step("insert_signals", input_snapshot={"total": len(signals)}) as result:
            inserted = insert_options_signals(signals)
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


# ===========================================================================
# Entry point
# ===========================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Unified signal ingest — Form 4 insider filings and options flow."
    )
    parser.add_argument(
        "mode",
        choices=["form4", "options"],
        help="form4: SEC EDGAR insider filings | options: unusual options activity",
    )
    args = parser.parse_args()

    if args.mode == "form4":
        run_form4()
    else:
        run_options()
