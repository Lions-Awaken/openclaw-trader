#!/usr/bin/env python3
"""
shadow_position_opener.py — Open virtual shadow positions after each scanner run.

Queries inference_chains for today's shadow-profile entries with enter/strong_enter
decisions, deduplicates against shadow_positions, fetches current price, and inserts
one row per new position.

Schedule (PDT weekdays):
  7:15 AM  — after scanner_morning (6:35 AM) + inference buffer
  10:30 AM — after scanner_midday  (9:30 AM) + inference buffer

Flow:
  1. Query inference_chains for today's shadow entries (enter/strong_enter)
  2. For each chain, skip if shadow_positions already has a row for profile+ticker+today
  3. Fetch close price via yfinance; fall back to Alpaca latest quote on failure
  4. Calculate position_size_shares = 10000 / entry_price
  5. Check shadow_divergences for a matching row (was_divergent)
  6. Get CONGRESS_MIRROR's final_decision for the same ticker+today (vs_live_decision)
  7. INSERT into shadow_positions with status='open'
  8. Slack summary of how many positions were opened
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    get_latest_quote,
    sb_get,
    slack_notify,
)
from tracer import PipelineTracer, _post_to_supabase, set_active_tracer, traced

POSITION_SIZE_USD = 10_000.0


# ---------------------------------------------------------------------------
# Price fetch — yfinance primary, Alpaca fallback
# ---------------------------------------------------------------------------
def _fetch_price_yfinance(ticker: str) -> float | None:
    """Fetch today's close (or last available close) via yfinance. Returns None on failure."""
    try:
        import yfinance as yf

        data = yf.download(ticker, period="1d", interval="1d", progress=False, auto_adjust=True)
        if data.empty:
            return None
        # Flatten MultiIndex if present
        if hasattr(data.columns, "levels"):
            data.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in data.columns]
        else:
            data.columns = [str(c).lower() for c in data.columns]
        if "close" not in data.columns:
            return None
        price = float(data["close"].iloc[-1])
        return price if price > 0 else None
    except Exception as exc:
        print(f"[shadow_opener] yfinance error for {ticker}: {exc}")
        return None


def _fetch_price(ticker: str) -> float | None:
    """Fetch entry price — yfinance first, Alpaca quote fallback."""
    price = _fetch_price_yfinance(ticker)
    if price is not None:
        print(f"[shadow_opener] {ticker}: yfinance price = {price:.2f}")
        return price
    # Alpaca fallback
    quote = get_latest_quote(ticker)
    alpaca_price = quote.get("price", 0)
    if alpaca_price and alpaca_price > 0:
        print(f"[shadow_opener] {ticker}: Alpaca fallback price = {alpaca_price:.2f}")
        return float(alpaca_price)
    print(f"[shadow_opener] {ticker}: no price available — skipping")
    return None


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def _position_exists(profile: str, ticker: str, entry_date: str) -> bool:
    """Return True if shadow_positions already has a row for this profile+ticker+date."""
    rows = sb_get(
        "shadow_positions",
        {
            "select": "id",
            "shadow_profile": f"eq.{profile}",
            "ticker": f"eq.{ticker}",
            "entry_date": f"eq.{entry_date}",
            "limit": "1",
        },
    )
    return len(rows) > 0


def _get_divergence_id(chain_id: str) -> str | None:
    """Return shadow_divergences.id if a matching row exists for this chain (was_divergent=true)."""
    rows = sb_get(
        "shadow_divergences",
        {
            "select": "id",
            "shadow_chain_id": f"eq.{chain_id}",
            "limit": "1",
        },
    )
    return rows[0]["id"] if rows else None


def _get_congress_mirror_decision(ticker: str, today_start: str) -> str | None:
    """Return CONGRESS_MIRROR's final_decision for this ticker today, or None."""
    rows = sb_get(
        "inference_chains",
        {
            "select": "final_decision",
            "profile_name": "eq.CONGRESS_MIRROR",
            "ticker": f"eq.{ticker}",
            "created_at": f"gte.{today_start}",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    return rows[0]["final_decision"] if rows else None


@traced("pipeline")
def fetch_shadow_chains(today_start: str) -> list[dict]:
    """Query inference_chains for today's shadow entries with enter/strong_enter decision."""
    rows = sb_get(
        "inference_chains",
        {
            "select": "id,ticker,profile_name,final_decision,final_confidence,created_at",
            "profile_name": "neq.CONGRESS_MIRROR",
            "final_decision": "in.(enter,strong_enter)",
            "created_at": f"gte.{today_start}",
            "order": "created_at.asc",
        },
    )
    print(f"[shadow_opener] Found {len(rows)} shadow chain(s) with enter/strong_enter today")
    return rows


@traced("pipeline")
def open_shadow_positions(chains: list[dict], today: str, today_start: str) -> dict:
    """Process each chain and insert shadow_positions rows as needed."""
    opened = 0
    skipped_duplicate = 0
    skipped_no_price = 0
    errors = 0

    for chain in chains:
        chain_id: str = chain["id"]
        ticker: str = chain["ticker"]
        profile: str = chain["profile_name"]
        decision: str = chain["final_decision"]
        confidence: float = chain.get("final_confidence") or 0.0

        # Deduplicate: skip if already opened for this profile+ticker+today
        if _position_exists(profile, ticker, today):
            print(f"[shadow_opener] {profile}/{ticker}: already open for {today} — skip")
            skipped_duplicate += 1
            continue

        # Fetch price
        price = _fetch_price(ticker)
        if price is None:
            skipped_no_price += 1
            continue

        # Calculate shares
        shares = round(POSITION_SIZE_USD / price, 4)

        # Check for matching divergence
        divergence_id = _get_divergence_id(chain_id)
        was_divergent = divergence_id is not None

        # Get CONGRESS_MIRROR's live decision for this ticker today
        vs_live_decision = _get_congress_mirror_decision(ticker, today_start)

        # Build the row
        row: dict = {
            "shadow_profile": profile,
            "ticker": ticker,
            "entry_date": today,
            "entry_price": price,
            "position_size_usd": POSITION_SIZE_USD,
            "position_size_shares": shares,
            "shadow_chain_id": chain_id,
            "shadow_divergence_id": divergence_id,
            "was_divergent": was_divergent,
            "vs_live_decision": vs_live_decision,
            "status": "open",
        }

        result = _post_to_supabase("shadow_positions", row)
        if result is not None:
            opened += 1
            print(
                f"[shadow_opener] OPENED {profile}/{ticker}: "
                f"${price:.2f} x {shares:.2f}sh "
                f"decision={decision} conf={confidence:.3f} "
                f"divergent={was_divergent} vs_live={vs_live_decision}"
            )
        else:
            errors += 1
            print(f"[shadow_opener] ERROR inserting {profile}/{ticker} — see tracer output")

    return {
        "opened": opened,
        "skipped_duplicate": skipped_duplicate,
        "skipped_no_price": skipped_no_price,
        "errors": errors,
        "total_chains": len(chains),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run() -> None:
    tracer = PipelineTracer("shadow_position_opener")
    set_active_tracer(tracer)

    today = date.today().isoformat()
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    print(f"[shadow_opener] Starting — date={today}")

    try:
        with tracer.step("fetch_chains", input_snapshot={"date": today}) as result:
            chains = fetch_shadow_chains(today_start)
            result.set({"chains_found": len(chains)})

        if not chains:
            print("[shadow_opener] No shadow entries today — nothing to open")
            tracer.complete({"opened": 0, "reason": "no_shadow_entries"})
            slack_notify(
                f":shadow-position: Shadow position opener: no enter/strong_enter chains today ({today})"
            )
            return

        with tracer.step(
            "open_positions",
            input_snapshot={"chains": len(chains), "date": today},
        ) as result:
            stats = open_shadow_positions(chains, today, today_start)
            result.set(stats)

        tracer.complete(stats)

        # Slack summary
        lines = [
            f":chart_with_upwards_trend: *Shadow Position Opener* — {today}",
            f"  Chains evaluated: {stats['total_chains']}",
            f"  Positions opened: {stats['opened']}",
        ]
        if stats["skipped_duplicate"] > 0:
            lines.append(f"  Skipped (duplicate): {stats['skipped_duplicate']}")
        if stats["skipped_no_price"] > 0:
            lines.append(f"  Skipped (no price): {stats['skipped_no_price']}")
        if stats["errors"] > 0:
            lines.append(f"  :warning: Insert errors: {stats['errors']}")
        slack_notify("\n".join(lines))

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[shadow_opener] FATAL: {exc}\n{tb}")
        tracer.fail(str(exc), tb)
        slack_notify(f":x: Shadow position opener FAILED ({today}): {exc}")
        raise


if __name__ == "__main__":
    run()
