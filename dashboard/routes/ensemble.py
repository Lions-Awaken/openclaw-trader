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
# Shadow P&L Routes
# ============================================================================


@router.get("/api/shadow/positions")
async def get_shadow_positions(
    request: Request,
    oc_session: str | None = Cookie(None),
    profile: str | None = None,
    status: str = "open",
) -> list:
    _require_auth(request, oc_session)
    valid_statuses = {"open", "closed", "stopped", "expired"}
    if status not in valid_statuses:
        status = "open"
    client = get_http()
    params: dict[str, str] = {
        "select": "id,shadow_profile,ticker,entry_date,entry_price,"
                  "position_size_usd,position_size_shares,"
                  "shadow_chain_id,shadow_divergence_id,"
                  "was_divergent,vs_live_decision,"
                  "current_price,current_pnl,current_pnl_pct,peak_pnl_pct,"
                  "status,exit_date,exit_price,final_pnl,final_pnl_pct,"
                  "close_reason,shadow_was_right,created_at",
        "status": f"eq.{status}",
        "order": "created_at.desc",
        "limit": "200",
    }
    if profile:
        params["shadow_profile"] = f"eq.{profile}"
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_positions",
        headers=sb_headers(),
        params=params,
    )
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/shadow/positions/{position_id}")
async def get_shadow_position_detail(
    position_id: str,
    request: Request,
    oc_session: str | None = Cookie(None),
):
    from shared import _validate_uuid

    _require_auth(request, oc_session)
    _validate_uuid(position_id)
    client = get_http()

    # Fetch the position row
    pos_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_positions",
        headers=sb_headers(),
        params={
            "select": "id,shadow_profile,ticker,entry_date,entry_price,"
                      "position_size_usd,position_size_shares,"
                      "shadow_chain_id,shadow_divergence_id,"
                      "was_divergent,vs_live_decision,"
                      "current_price,current_pnl,current_pnl_pct,peak_pnl_pct,"
                      "status,exit_date,exit_price,final_pnl,final_pnl_pct,"
                      "close_reason,shadow_was_right,created_at",
            "id": f"eq.{position_id}",
            "limit": "1",
        },
    )
    rows = pos_resp.json() if pos_resp.status_code == 200 else []
    if not rows:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Shadow position not found")

    position = rows[0]

    # Fetch linked inference chain if present
    chain_detail = None
    shadow_chain_id = position.get("shadow_chain_id")
    if shadow_chain_id:
        chain_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/inference_chains",
            headers=sb_headers(),
            params={
                "select": "id,ticker,chain_date,scan_type,profile_name,"
                          "tumblers,final_decision,final_confidence,"
                          "stopping_reason,total_duration_ms,created_at",
                "id": f"eq.{shadow_chain_id}",
                "limit": "1",
            },
        )
        chain_rows = chain_resp.json() if chain_resp.status_code == 200 else []
        if chain_rows:
            chain_detail = chain_rows[0]

    return {"position": position, "chain": chain_detail}


@router.get("/api/shadow/performance")
async def get_shadow_performance(
    request: Request,
    oc_session: str | None = Cookie(None),
    weeks: int = 12,
) -> list:
    _require_auth(request, oc_session)
    weeks = min(max(1, weeks), 52)
    # 5 profiles × weeks rows maximum
    limit = weeks * 5
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_performance",
        headers=sb_headers(),
        params={
            "select": "id,shadow_profile,week_start,"
                      "trades_opened,trades_closed,trades_won,trades_lost,"
                      "win_rate_pct,total_pnl,avg_pnl_per_trade,"
                      "best_trade_pnl,worst_trade_pnl,"
                      "divergent_trades,divergent_win_rate,"
                      "live_pnl_same_period,vs_live_delta,"
                      "dwm_weight_start,dwm_weight_end,created_at",
            "order": "week_start.desc",
            "limit": str(limit),
        },
    )
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/shadow/leaderboard")
async def get_shadow_leaderboard(
    request: Request,
    oc_session: str | None = Cookie(None),
) -> list:
    _require_auth(request, oc_session)
    client = get_http()

    # Fetch all shadow positions (closed for P&L math, open for open_count)
    pos_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/shadow_positions",
        headers=sb_headers(),
        params={
            "select": "shadow_profile,status,final_pnl,shadow_was_right,was_divergent",
            "limit": "5000",
        },
    )
    positions = pos_resp.json() if pos_resp.status_code == 200 else []

    # Fetch shadow profiles for dwm_weight
    prof_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers=sb_headers(),
        params={
            "select": "profile_name,dwm_weight,fitness_score",
            "is_shadow": "eq.true",
        },
    )
    profiles = prof_resp.json() if prof_resp.status_code == 200 else []
    dwm_by_profile: dict[str, float] = {
        p["profile_name"]: float(p.get("dwm_weight") or 1.0) for p in profiles
    }
    fitness_by_profile: dict[str, float] = {
        p["profile_name"]: float(p.get("fitness_score") or 0.0) for p in profiles
    }

    # Aggregate per profile
    stats: dict[str, dict] = {}
    for row in positions:
        prof = row["shadow_profile"]
        if prof not in stats:
            stats[prof] = {
                "shadow_profile": prof,
                "total_pnl": 0.0,
                "closed_count": 0,
                "open_count": 0,
                "wins": 0,
                "divergent_closed": 0,
                "divergent_wins": 0,
            }
        s = stats[prof]
        if row["status"] == "open":
            s["open_count"] += 1
        else:
            s["closed_count"] += 1
            s["total_pnl"] += float(row.get("final_pnl") or 0.0)
            if row.get("shadow_was_right"):
                s["wins"] += 1
            if row.get("was_divergent"):
                s["divergent_closed"] += 1
                if row.get("shadow_was_right"):
                    s["divergent_wins"] += 1

    # Build result list — include profiles with no positions yet
    all_profiles: set[str] = set(stats.keys()) | set(dwm_by_profile.keys())
    result = []
    for prof in all_profiles:
        s = stats.get(prof, {
            "shadow_profile": prof,
            "total_pnl": 0.0,
            "closed_count": 0,
            "open_count": 0,
            "wins": 0,
            "divergent_closed": 0,
            "divergent_wins": 0,
        })
        closed = s["closed_count"]
        div_closed = s["divergent_closed"]
        result.append({
            "shadow_profile": prof,
            "total_pnl": round(s["total_pnl"], 2),
            "win_rate": round(s["wins"] / closed, 4) if closed > 0 else None,
            "open_count": s["open_count"],
            "closed_count": closed,
            "divergent_win_rate": (
                round(s["divergent_wins"] / div_closed, 4) if div_closed > 0 else None
            ),
            "dwm_weight": dwm_by_profile.get(prof),
            "fitness_score": fitness_by_profile.get(prof),
        })

    return sorted(result, key=lambda x: x["total_pnl"], reverse=True)


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
