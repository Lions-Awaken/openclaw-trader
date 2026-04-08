#!/usr/bin/env python3
"""
Form 4 Ingest — fetches SEC EDGAR Form 4 insider filings and writes to
form4_signals table.

Target tickers: active strategy profile watchlist + hardcoded AI
infrastructure names (NVDA, AMD, AVGO, SMCI, MRVL, DELL, PLTR, ARM).

Scoring: score_form4_signal() returns 1–10. Sales are skipped (score 0).
Buys are scored on total value, ownership % change, cluster count, and
filer seniority.

Runs as a standalone script (cron 6 AM PST weekdays) or imported for its
scoring function by the scanner enrichment layer.
"""

import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import _client, load_strategy_profile, sb_get, slack_notify
from tracer import PipelineTracer, _post_to_supabase, set_active_tracer, traced

# ===========================================================================
# Config
# ===========================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")

# Hardcoded AI infrastructure watchlist — always included
AI_INFRA_TICKERS = ["NVDA", "AMD", "AVGO", "SMCI", "MRVL", "DELL", "PLTR", "ARM"]

# SEC EDGAR EFTS full-text search endpoint
EDGAR_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

# User-Agent required by SEC EDGAR — must identify the application
SEC_USER_AGENT = "OpenClaw-Trader/1.0 (research; github.com/Lions-Awaken)"

# Lookback window for Form 4 filings
LOOKBACK_DAYS = 3

# Titles that earn senior credit in scoring
SENIOR_TITLES = {"ceo", "cfo", "coo", "president", "chairman", "chief executive", "chief financial"}
MID_TITLES = {"vp", "vice president", "director", "svp", "evp", "general counsel", "treasurer"}


# ===========================================================================
# Scoring
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
# Watchlist
# ===========================================================================
def get_target_tickers() -> list[str]:
    """Combine active profile watchlist with AI infra hardcoded list."""
    tickers: set[str] = set(AI_INFRA_TICKERS)

    # Load from active strategy profile watchlist (signal_evaluations recent scans)
    try:
        profile = load_strategy_profile()
        watchlist = profile.get("watchlist", [])
        if isinstance(watchlist, list):
            tickers.update(t.upper() for t in watchlist if isinstance(t, str))
    except Exception as e:
        print(f"[form4] Could not load strategy profile watchlist: {e}")

    # Also pull from recent signal_evaluations as a secondary source
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
# EDGAR fetcher
# ===========================================================================
@traced("ingest")
def fetch_edgar_form4(start_dt: date, end_dt: date) -> list[dict]:
    """Query SEC EDGAR EFTS for Form 4 filings in the given date range.

    Returns a list of raw filing dicts extracted from the EDGAR response.
    """
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
# Parse + filter
# ===========================================================================
def _extract_ticker(filing: dict) -> str | None:
    """Best-effort extraction of the primary ticker from an EDGAR filing record."""
    # EDGAR EFTS uses 'entity_name' but not always a ticker — try common fields
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


def parse_filings(
    filings: list[dict],
    target_tickers: set[str],
) -> list[dict]:
    """Filter to target tickers, normalise fields, skip sales.

    Returns normalised records ready for scoring and DB insert.
    """
    records: list[dict] = []
    today = date.today().isoformat()

    for filing in filings:
        ticker = _extract_ticker(filing)
        if not ticker or ticker not in target_tickers:
            continue

        # EDGAR EFTS does not always carry structured transaction fields —
        # we capture what's available and leave None for the rest.
        # The DB columns are nullable to accommodate this.

        filing_date = filing.get("file_date") or filing.get("period_of_report") or today
        signal_date = filing.get("period_of_report") or filing_date

        filer_name = str(filing.get("display_date_filed") or filing.get("entity_name") or "")
        filer_title = str(filing.get("officer_title") or "")

        transaction_type_raw = str(filing.get("transaction_code") or "").upper()
        # EDGAR transaction codes: P = purchase, S = sale, G = gift, M = exercise
        transaction_type_map = {
            "P": "purchase",
            "S": "sale",
            "G": "gift",
            "M": "exercise",
            "A": "purchase",  # Grant/award treated as purchase for scoring
        }
        transaction_type = transaction_type_map.get(transaction_type_raw, "sale")

        # Skip sales immediately — score_form4_signal() would return 0 anyway
        if transaction_type == "sale":
            continue

        shares = _extract_int(filing, "shares", "transaction_shares")
        price_per_share = _extract_float(filing, "price_per_share", "transaction_price_per_share")
        total_value = _extract_float(filing, "total_value", "transaction_total_value")

        # Derive total_value if not directly available
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
            "days_since_last_filing": None,  # populated below if derivable
            "cluster_count": 1,              # updated by cluster detection
            "source": "sec_edgar",
            "raw_data": filing,
        })

    return records


def _detect_clusters(records: list[dict]) -> list[dict]:
    """Update cluster_count for tickers with multiple buyers in the batch."""
    from collections import Counter

    ticker_counts: Counter = Counter(r["ticker"] for r in records)
    for rec in records:
        rec["cluster_count"] = ticker_counts[rec["ticker"]]
    return records


# ===========================================================================
# DB insert
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
            continue  # Sales filtered here as a safety net

        row = {**rec, "raw_data": {**rec.get("raw_data", {}), "score": score}}
        # Strip None values to let DB defaults apply
        row = {k: v for k, v in row.items() if v is not None}

        stored = _post_to_supabase("form4_signals", row)
        if stored:
            inserted += 1

    return inserted


# ===========================================================================
# Entry point
# ===========================================================================
def run() -> None:
    tracer = PipelineTracer(
        "ingest_form4",
        metadata={"time": datetime.now(timezone.utc).isoformat()},
    )
    set_active_tracer(tracer)

    try:
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)

        # Step 1: resolve target tickers
        with tracer.step("load_watchlist") as result:
            target_tickers = get_target_tickers()
            result.set({"ticker_count": len(target_tickers)})

        # Step 2: fetch from EDGAR
        with tracer.step("fetch_edgar", input_snapshot={"start": start_dt.isoformat(), "end": end_dt.isoformat()}) as result:
            filings = fetch_edgar_form4(start_dt, end_dt)
            result.set({"raw_filings": len(filings)})

            # Brief pause to be polite to SEC servers
            time.sleep(1)

        # Step 3: parse, filter, cluster
        with tracer.step("parse_filings", input_snapshot={"filings": len(filings)}) as result:
            records = parse_filings(filings, set(target_tickers))
            records = _detect_clusters(records)
            result.set({"parsed": len(records)})

        # Step 4: score + insert
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


if __name__ == "__main__":
    run()
