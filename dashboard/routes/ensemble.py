"""
Ensemble Intelligence API — /api/shadow/* and /api/signals/*

Shadow profile fitness, divergences, unanimous dissent, Kronos latest,
options flow signals, and Form 4 insider signals.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Request
from shared import (
    SUPABASE_URL,
    _require_auth,
    get_http,
    sb_headers,
)

router = APIRouter()


# ============================================================================
# Shadow Intelligence Routes
# ============================================================================


@router.get("/api/shadow/profiles")
async def get_shadow_profiles(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers=sb_headers(),
        params={
            "select": "profile_name,shadow_type,fitness_score,dwm_weight,"
                      "conditional_brier,times_correct,times_dissented,"
                      "divergence_rate,last_graded_at",
            "is_shadow": "eq.true",
            "order": "fitness_score.desc",
            "limit": "20",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/shadow/divergences")
async def get_shadow_divergences(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 30,
):
    _require_auth(request, oc_session)
    days = min(days, 90)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_divergences",
        headers=sb_headers(),
        params={
            "select": "id,ticker,divergence_date,live_profile,live_decision,live_confidence,"
                      "shadow_profile,shadow_type,shadow_decision,shadow_confidence,"
                      "shadow_stopping_reason,first_diverged_at_tumbler,"
                      "shadow_was_right,save_value,actual_outcome",
            "divergence_date": f"gte.{cutoff}",
            "order": "divergence_date.desc,created_at.desc",
            "limit": "200",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/shadow/unanimous")
async def get_shadow_unanimous(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 30,
):
    _require_auth(request, oc_session)
    days = min(days, 90)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_divergences",
        headers=sb_headers(),
        params={
            "select": "ticker,divergence_date,live_decision,live_confidence,"
                      "shadow_profile,shadow_decision,shadow_confidence,"
                      "shadow_stopping_reason,shadow_was_right,actual_outcome,save_value",
            "divergence_date": f"gte.{cutoff}",
            "order": "divergence_date.desc",
            "limit": "500",
        },
    )
    rows = resp.json() if resp.status_code == 200 else []

    # Group by ticker+date, find where ALL shadows dissented
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_key[(r["ticker"], r["divergence_date"])].append(r)

    unanimous = []
    for (ticker, div_date), divs in by_key.items():
        live_was_entry = any(d["live_decision"] in ("enter", "strong_enter") for d in divs)
        all_shadows_dissented = all(
            d["shadow_decision"] not in ("enter", "strong_enter") for d in divs
        )
        if live_was_entry and all_shadows_dissented and len(divs) >= 2:
            unanimous.append(
                {
                    "ticker": ticker,
                    "date": div_date,
                    "live_confidence": max((d.get("live_confidence") or 0) for d in divs),
                    "shadows": [
                        {
                            "profile": d["shadow_profile"],
                            "decision": d["shadow_decision"],
                            "confidence": d.get("shadow_confidence"),
                            "reason": d.get("shadow_stopping_reason"),
                        }
                        for d in divs
                    ],
                    "outcome": divs[0].get("actual_outcome"),
                    "save_value": sum(float(d.get("save_value") or 0) for d in divs),
                }
            )
    return sorted(unanimous, key=lambda x: x["date"], reverse=True)


@router.get("/api/shadow/kronos/latest")
async def get_shadow_kronos_latest(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_divergences",
        headers=sb_headers(),
        params={
            "select": "ticker,shadow_decision,shadow_confidence,live_decision,divergence_date,created_at",
            "shadow_profile": "eq.KRONOS_TECHNICALS",
            "order": "created_at.desc",
            "limit": "10",
        },
    )
    if resp.status_code != 200:
        return []
    return resp.json()


# ============================================================================
# Signal Feed Routes
# ============================================================================


@router.get("/api/signals/options-flow")
async def get_signals_options_flow(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
) -> list:
    _require_auth(request, oc_session)
    days = min(days, 90)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/options_flow_signals",
        headers=sb_headers(),
        params={
            "select": "id,ticker,signal_date,signal_type,strike,expiry,premium,"
                      "open_interest,volume,implied_volatility,sentiment,source,created_at",
            "signal_date": f"gte.{cutoff}",
            "order": "signal_date.desc,created_at.desc",
            "limit": "100",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/signals/form4")
async def get_signals_form4(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 30,
) -> list:
    _require_auth(request, oc_session)
    days = min(days, 180)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/form4_signals",
        headers=sb_headers(),
        params={
            "select": "id,ticker,signal_date,filing_date,filer_name,filer_title,"
                      "transaction_type,shares,price_per_share,total_value,"
                      "shares_owned_after,ownership_pct_change,days_since_last_filing,"
                      "cluster_count,source,created_at",
            "signal_date": f"gte.{cutoff}",
            "order": "signal_date.desc,created_at.desc",
            "limit": "100",
        },
    )
    return resp.json() if resp.status_code == 200 else []
