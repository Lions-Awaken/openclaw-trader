"""
Trading Data API — /api/account, /api/positions, /api/trades, /api/performance,
/api/regime, /api/pipeline/*, /api/inference/*, /api/economics/*, /api/budget/*,
/api/rag/*, /api/sitrep, /api/strategy/*, /api/trade-learnings/*
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse
from shared import (
    ALPACA_BASE,
    ALPACA_KEY,
    ALPACA_SECRET,
    SUPABASE_URL,
    _require_auth,
    _validate_pipeline_name,
    _validate_ticker,
    _validate_uuid,
    clamp_days,
    get_http,
    sb_headers,
)

router = APIRouter()

ALLOWED_BUDGET_KEYS = {"daily_claude_budget", "daily_perplexity_budget"}


# ============================================================================
# Account & Positions
# ============================================================================


@router.get("/api/account")
async def get_account(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    client = get_http()
    resp = await client.get(
        f"{ALPACA_BASE}/v2/account",
        headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
    )
    if resp.status_code == 200:
        data = resp.json()
        return {
            "equity": float(data.get("equity", 0)),
            "cash": float(data.get("cash", 0)),
            "buying_power": float(data.get("buying_power", 0)),
            "portfolio_value": float(data.get("portfolio_value", 0)),
            "account_number": data.get("account_number", ""),
            "status": data.get("status", ""),
            "paper": data.get("account_number", "").startswith("PA"),
        }
    return {"error": f"Alpaca {resp.status_code}"}


@router.get("/api/positions")
async def get_positions(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    client = get_http()
    resp = await client.get(
        f"{ALPACA_BASE}/v2/positions",
        headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
    )
    if resp.status_code == 200:
        return [
            {
                "symbol": p.get("symbol"),
                "qty": float(p.get("qty", 0)),
                "avg_entry": float(p.get("avg_entry_price", 0)),
                "current_price": float(p.get("current_price", 0)),
                "market_value": float(p.get("market_value", 0)),
                "unrealized_pl": float(p.get("unrealized_pl", 0)),
                "unrealized_plpc": float(p.get("unrealized_plpc", 0)) * 100,
                "side": p.get("side", ""),
            }
            for p in resp.json()
        ]
    return []


@router.get("/api/trades")
async def get_trades(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_decisions",
        headers=sb_headers(),
        params={
            "select": "id,ticker,action,entry_price,exit_price,pnl,outcome,signals_fired,hold_days,reasoning,what_worked,improvement,created_at",
            "order": "created_at.desc",
            "limit": "50",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/performance")
async def get_performance(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(f"{SUPABASE_URL}/rest/v1/account_performance", headers=sb_headers())
    if resp.status_code == 200:
        rows = resp.json()
        return rows[0] if rows else {}
    return {}


@router.get("/api/regime")
async def get_regime(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    regime_file = Path.home() / ".openclaw/workspace/memory/regime-current.json"
    if regime_file.exists():
        return json.loads(regime_file.read_text())
    return {"regime": "UNKNOWN", "action": "No regime data — run regime.py first"}


# ============================================================================
# Pipeline & Meta-Learning API Routes
# ============================================================================


@router.get("/api/pipeline/runs")
async def get_pipeline_runs(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
    pipeline: str = "",
):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    params: dict = {
        "select": "id,pipeline_name,step_name,status,started_at,completed_at,duration_ms,error_message,metadata",
        "step_name": "eq.root",
        "started_at": f"gte.{cutoff}",
        "order": "started_at.desc",
        "limit": "100",
    }
    if pipeline:
        pipeline = _validate_pipeline_name(pipeline)
        params["pipeline_name"] = f"eq.{pipeline}"
    client = get_http()
    resp = await client.get(f"{SUPABASE_URL}/rest/v1/pipeline_runs", headers=sb_headers(), params=params)
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/pipeline/health")
async def get_pipeline_health(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"score": 0, "total": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={"select": "status", "step_name": "neq.root", "started_at": f"gte.{cutoff}", "limit": "2000"},
    )
    if resp.status_code != 200:
        return {"score": 0, "total": 0}
    runs = resp.json()
    total = len(runs)
    if total == 0:
        return {"score": 100, "total": 0, "successes": 0, "failures": 0}
    successes = sum(1 for r in runs if r["status"] == "success")
    failures = sum(1 for r in runs if r["status"] == "failure")
    return {
        "score": round(successes / total * 100, 1) if total else 0,
        "total": total,
        "successes": successes,
        "failures": failures,
    }


@router.get("/api/pipeline/run/{run_id}")
async def get_pipeline_run_detail(
    run_id: str, request: Request, oc_session: str | None = Cookie(None)
):
    _require_auth(request, oc_session)
    run_id = _validate_uuid(run_id)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs", headers=sb_headers(), params={"id": f"eq.{run_id}"}
    )
    root = resp.json()[0] if resp.status_code == 200 and resp.json() else None
    if not root:
        return {}
    resp2 = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={
            "select": "id,step_name,status,started_at,completed_at,duration_ms,input_snapshot,output_snapshot,error_message,parent_run_id",
            "or": f"(id.eq.{run_id},parent_run_id.eq.{run_id})",
            "order": "started_at.asc",
        },
    )
    steps = resp2.json() if resp2.status_code == 200 else []
    return {"root": root, "steps": steps}


# ============================================================================
# Signals API Routes
# ============================================================================


@router.get("/api/signals/accuracy")
async def get_signal_accuracy(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/signal_accuracy_report",
        headers=sb_headers(),
        params={"order": "week_start.desc", "limit": "12"},
    )
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/signals/evaluations")
async def get_signal_evaluations(
    request: Request, oc_session: str | None = Cookie(None), days: int = 7
):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/signal_evaluations",
        headers=sb_headers(),
        params={
            "select": "id,ticker,scan_date,scan_type,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision,reasoning",
            "scan_date": f"gte.{cutoff}",
            "order": "created_at.desc",
            "limit": "100",
        },
    )
    return resp.json() if resp.status_code == 200 else []


# ============================================================================
# Inference Engine API Routes
# ============================================================================


@router.get("/api/inference/chain/{chain_id}")
async def get_inference_chain_detail(
    chain_id: str, request: Request, oc_session: str | None = Cookie(None)
):
    _require_auth(request, oc_session)
    chain_id = _validate_uuid(chain_id)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={"id": f"eq.{chain_id}"},
    )
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return {}


# ============================================================================
# Economics & Budget API Routes
# ============================================================================


@router.get("/api/economics/summary")
async def get_economics_summary(
    request: Request, oc_session: str | None = Cookie(None), days: int = 30
):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/cost_ledger",
        headers=sb_headers(),
        params={"select": "category,amount", "ledger_date": f"gte.{cutoff}", "limit": "1000"},
    )
    if resp.status_code != 200:
        return {}
    entries = resp.json()
    total_costs = 0.0
    total_pnl = 0.0
    by_category: dict = {}
    for e in entries:
        amt = float(e.get("amount", 0))
        cat = e.get("category", "other")
        by_category[cat] = by_category.get(cat, 0) + amt
        if cat == "trade_pnl":
            total_pnl += amt
        else:
            total_costs += amt
    net = total_pnl + total_costs
    roi = round(total_pnl / abs(total_costs) * 100, 1) if total_costs != 0 else 0
    return {
        "total_costs": round(abs(total_costs), 2),
        "total_pnl": round(total_pnl, 2),
        "net": round(net, 2),
        "roi_pct": roi,
        "by_category": {k: round(v, 4) for k, v in by_category.items()},
        "days": days,
    }


@router.get("/api/economics/breakdown")
async def get_economics_breakdown(
    request: Request, oc_session: str | None = Cookie(None), days: int = 30
):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/cost_ledger",
        headers=sb_headers(),
        params={
            "select": "category,subcategory,amount,ledger_date",
            "ledger_date": f"gte.{cutoff}",
            "order": "ledger_date.desc",
            "limit": "1000",
        },
    )
    if resp.status_code != 200:
        return []
    breakdown: dict = {}
    for e in resp.json():
        key = f"{e.get('category', 'other')}|{e.get('subcategory', '')}"
        if key not in breakdown:
            breakdown[key] = {
                "category": e["category"],
                "subcategory": e.get("subcategory", ""),
                "total": 0,
                "count": 0,
            }
        breakdown[key]["total"] += float(e.get("amount", 0))
        breakdown[key]["count"] += 1
    return sorted(breakdown.values(), key=lambda x: x["total"])


@router.get("/api/economics/history")
async def get_economics_history(
    request: Request, oc_session: str | None = Cookie(None), days: int = 90
):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/cost_ledger",
        headers=sb_headers(),
        params={
            "select": "ledger_date,category,amount",
            "ledger_date": f"gte.{cutoff}",
            "order": "ledger_date.asc",
            "limit": "1000",
        },
    )
    if resp.status_code != 200:
        return []
    by_date: dict = {}
    for e in resp.json():
        d = e["ledger_date"]
        if d not in by_date:
            by_date[d] = {"date": d, "costs": 0, "pnl": 0}
        amt = float(e.get("amount", 0))
        if e.get("category") == "trade_pnl":
            by_date[d]["pnl"] += amt
        else:
            by_date[d]["costs"] += amt
    result = sorted(by_date.values(), key=lambda x: x["date"])
    cum_costs = 0.0
    cum_pnl = 0.0
    for row in result:
        cum_costs += row["costs"]
        cum_pnl += row["pnl"]
        row["cum_costs"] = round(cum_costs, 2)
        row["cum_pnl"] = round(cum_pnl, 2)
        row["cum_net"] = round(cum_pnl + cum_costs, 2)
        row["costs"] = round(row["costs"], 4)
        row["pnl"] = round(row["pnl"], 4)
    return result


@router.get("/api/budget/config")
async def get_budget_config(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    today = datetime.now(timezone.utc).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/budget_config",
        headers=sb_headers(),
        params={"order": "config_key.asc", "limit": "50"},
    )
    configs = resp.json() if resp.status_code == 200 else []
    resp2 = await client.get(
        f"{SUPABASE_URL}/rest/v1/cost_ledger",
        headers=sb_headers(),
        params={"select": "category,amount", "ledger_date": f"eq.{today}", "limit": "500"},
    )
    today_costs = resp2.json() if resp2.status_code == 200 else []
    spend_by_cat: dict = {}
    for c in today_costs:
        cat = c.get("category", "")
        spend_by_cat[cat] = spend_by_cat.get(cat, 0) + abs(float(c.get("amount", 0)))
    for cfg in configs:
        key = cfg.get("config_key", "")
        if "claude" in key:
            cfg["today_spend"] = round(spend_by_cat.get("claude_api", 0), 4)
        elif "perplexity" in key:
            cfg["today_spend"] = round(spend_by_cat.get("perplexity_api", 0), 4)
        else:
            cfg["today_spend"] = 0
    return configs


@router.post("/api/budget/config")
async def update_budget_config(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        raise HTTPException(status_code=500, detail="No Supabase connection")
    body = await request.json()
    config_key = body.get("config_key")
    value = body.get("value")
    if not config_key or value is None:
        raise HTTPException(status_code=400, detail="Missing config_key or value")
    if config_key not in ALLOWED_BUDGET_KEYS:
        raise HTTPException(status_code=400, detail="Invalid config key")
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Value must be a number")
    if value < 0 or value > 100:
        raise HTTPException(status_code=400, detail="Value must be between 0 and 100")
    client = get_http()
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/budget_config",
        params={"config_key": f"eq.{config_key}"},
        headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=representation"},
        json={"value": value, "updated_at": datetime.now(timezone.utc).isoformat(), "updated_by": "dashboard_ui"},
    )
    if resp.status_code in (200, 204):
        return {"ok": True}
    raise HTTPException(status_code=resp.status_code, detail="Failed to update")


# ============================================================================
# RAG Status API Routes
# ============================================================================


@router.get("/api/rag/status")
async def get_rag_status(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    tables = [
        "signal_evaluations", "meta_reflections", "catalyst_events",
        "inference_chains", "pattern_templates", "trade_learnings",
    ]
    result = {}
    client = get_http()
    for table in tables:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**sb_headers(), "Prefer": "count=exact"},
            params={"select": "id", "limit": "0"},
        )
        total = int(resp.headers.get("content-range", "0/0").split("/")[-1]) if resp.status_code == 200 else 0
        resp2 = await client.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**sb_headers(), "Prefer": "count=exact"},
            params={"select": "id", "embedding": "not.is.null", "limit": "0"},
        )
        with_embedding = int(resp2.headers.get("content-range", "0/0").split("/")[-1]) if resp2.status_code == 200 else 0
        coverage = round(with_embedding / total * 100, 1) if total > 0 else 0
        result[table] = {"total": total, "with_embedding": with_embedding, "coverage_pct": coverage}
    return result


@router.get("/api/rag/coverage")
async def get_rag_coverage(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    return await get_rag_status(request, oc_session)


@router.get("/api/rag/activity")
async def get_rag_activity(
    request: Request, oc_session: str | None = Cookie(None), days: int = 7
):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={
            "select": "pipeline_name,step_name,output_snapshot,duration_ms,started_at",
            "step_name": "like.*rag*",
            "started_at": f"gte.{cutoff}",
            "order": "started_at.desc",
            "limit": "20",
        },
    )
    return resp.json() if resp.status_code == 200 else []


# ============================================================================
# Sit-Rep: Decision Intelligence Briefing
# ============================================================================


@router.get("/api/sitrep")
async def get_sitrep(
    request: Request, oc_session: str | None = Cookie(None), days: int = 30
):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = get_http()
    trades_resp, chains_resp, signals_resp, catalysts_resp = await asyncio.gather(
        client.get(
            f"{SUPABASE_URL}/rest/v1/trade_decisions", headers=sb_headers(),
            params={"select": "id,ticker,action,entry_price,exit_price,pnl,outcome,signals_fired,hold_days,reasoning,what_worked,improvement,created_at", "created_at": f"gte.{cutoff}", "order": "created_at.desc", "limit": "50"},
        ),
        client.get(
            f"{SUPABASE_URL}/rest/v1/inference_chains", headers=sb_headers(),
            params={"select": "id,ticker,chain_date,max_depth_reached,final_confidence,final_decision,stopping_reason,tumblers,catalyst_event_ids,reasoning_summary,actual_outcome,actual_pnl,created_at", "chain_date": f"gte.{cutoff[:10]}", "order": "created_at.desc", "limit": "100"},
        ),
        client.get(
            f"{SUPABASE_URL}/rest/v1/signal_evaluations", headers=sb_headers(),
            params={"select": "id,ticker,scan_date,scan_type,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision,reasoning,created_at", "scan_date": f"gte.{cutoff[:10]}", "order": "created_at.desc", "limit": "100"},
        ),
        client.get(
            f"{SUPABASE_URL}/rest/v1/catalyst_events", headers=sb_headers(),
            params={"select": "id,ticker,catalyst_type,headline,direction,magnitude,sentiment_score,event_time", "event_time": f"gte.{cutoff}", "order": "event_time.desc", "limit": "100"},
        ),
    )
    trades = trades_resp.json() if trades_resp.status_code == 200 else []
    chains = chains_resp.json() if chains_resp.status_code == 200 else []
    signals = signals_resp.json() if signals_resp.status_code == 200 else []
    catalysts = catalysts_resp.json() if catalysts_resp.status_code == 200 else []

    chains_by_ticker: dict = {}
    for c in chains:
        chains_by_ticker.setdefault(c.get("ticker", ""), []).append(c)
    signals_by_ticker: dict = {}
    for s in signals:
        signals_by_ticker.setdefault(s.get("ticker", ""), []).append(s)
    catalysts_by_ticker: dict = {}
    for cat in catalysts:
        t = cat.get("ticker", "")
        if t:
            catalysts_by_ticker.setdefault(t, []).append(cat)

    results = []
    for trade in trades:
        ticker = trade.get("ticker", "")
        results.append({
            "type": "trade",
            "trade": trade,
            "chains": chains_by_ticker.get(ticker, [])[:3],
            "signals": signals_by_ticker.get(ticker, [])[:3],
            "catalysts": catalysts_by_ticker.get(ticker, [])[:5],
        })
    for chain in chains:
        if chain.get("final_decision") in ("watch", "skip", "veto"):
            ticker = chain.get("ticker", "")
            results.append({
                "type": "analysis",
                "chain": chain,
                "signals": signals_by_ticker.get(ticker, [])[:2],
                "catalysts": catalysts_by_ticker.get(ticker, [])[:3],
            })
    return results[:60]


# ============================================================================
# Strategy Profiles
# ============================================================================


@router.get("/api/strategy/profiles")
async def get_strategy_profiles(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers=sb_headers(),
        params={
            "select": "id,profile_name,description,active,annual_target_pct,daily_target_pct,weekly_target_pct,min_signal_score,min_tumbler_depth,min_confidence,max_risk_per_trade_pct,max_concurrent_positions,max_portfolio_risk_pct,position_size_method,trade_style,max_hold_days,circuit_breakers_enabled,self_modify_enabled,self_modify_requires_approval,prefer_high_beta,created_at",
            "order": "created_at.asc",
            "limit": "50",
        },
    )
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/strategy/active")
async def get_active_strategy(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers=sb_headers(),
        params={"select": "id,profile_name,active,annual_target_pct,daily_target_pct", "active": "eq.true", "limit": "1"},
    )
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return {}


@router.post("/api/strategy/activate")
async def activate_strategy(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    body = await request.json()
    profile_id = _validate_uuid(body.get("id", ""))
    client = get_http()
    now = datetime.now(timezone.utc).isoformat()
    await client.patch(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers={**sb_headers(), "Content-Type": "application/json"},
        params={"active": "eq.true"},
        json={"active": False, "updated_at": now},
    )
    resp = await client.patch(
        f"{SUPABASE_URL}/rest/v1/strategy_profiles",
        headers={**sb_headers(), "Content-Type": "application/json", "Prefer": "return=representation"},
        params={"id": f"eq.{profile_id}"},
        json={"active": True, "updated_at": now},
    )
    if resp.status_code != 200 or not resp.json():
        raise HTTPException(status_code=500, detail="Failed to activate profile")
    return resp.json()[0]


# ============================================================================
# Trade Learnings API Routes
# ============================================================================


@router.get("/api/trade-learnings")
async def get_trade_learnings(
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 60,
    ticker: str = "",
    outcome: str = "",
):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    params: dict = {
        "select": "id,ticker,trade_date,entry_price,exit_price,pnl,pnl_pct,outcome,hold_days,"
                  "expected_direction,expected_confidence,actual_direction,actual_move_pct,"
                  "expectation_accuracy,catalyst_match,key_variance,what_worked,what_failed,"
                  "key_lesson,tumbler_depth,inference_chain_id,created_at",
        "trade_date": f"gte.{cutoff}",
        "order": "trade_date.desc",
        "limit": "50",
    }
    if ticker:
        params["ticker"] = f"eq.{_validate_ticker(ticker)}"
    if outcome:
        params["outcome"] = f"eq.{outcome}"
    client = get_http()
    resp = await client.get(f"{SUPABASE_URL}/rest/v1/trade_learnings", headers=sb_headers(), params=params)
    return resp.json() if resp.status_code == 200 else []


@router.get("/api/trade-learnings/stats")
async def get_trade_learnings_stats(
    request: Request, oc_session: str | None = Cookie(None), days: int = 60
):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_learnings",
        headers=sb_headers(),
        params={"select": "outcome,pnl_pct,expectation_accuracy,tumbler_depth,expected_confidence", "trade_date": f"gte.{cutoff}", "limit": "500"},
    )
    if resp.status_code != 200:
        return {}
    rows = resp.json()
    if not rows:
        return {"total": 0}
    outcomes: dict = {}
    accuracy_counts: dict = {}
    total_pnl = 0.0
    depth_by_outcome: dict = {}
    for r in rows:
        o = r.get("outcome", "SCRATCH")
        outcomes[o] = outcomes.get(o, 0) + 1
        a = r.get("expectation_accuracy", "missed")
        accuracy_counts[a] = accuracy_counts.get(a, 0) + 1
        total_pnl += float(r.get("pnl_pct", 0) or 0)
        depth = str(r.get("tumbler_depth", 0) or 0)
        if depth not in depth_by_outcome:
            depth_by_outcome[depth] = {"wins": 0, "losses": 0, "total": 0}
        depth_by_outcome[depth]["total"] += 1
        if o in ("STRONG_WIN", "WIN"):
            depth_by_outcome[depth]["wins"] += 1
        elif o in ("LOSS", "STRONG_LOSS"):
            depth_by_outcome[depth]["losses"] += 1
    total = len(rows)
    wins = outcomes.get("STRONG_WIN", 0) + outcomes.get("WIN", 0)
    losses = outcomes.get("LOSS", 0) + outcomes.get("STRONG_LOSS", 0)
    well_called = accuracy_counts.get("met", 0) + accuracy_counts.get("exceeded", 0)
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "scratches": outcomes.get("SCRATCH", 0),
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "avg_pnl_pct": round(total_pnl / total, 2) if total else 0,
        "expectation_accuracy_pct": round(well_called / total * 100, 1) if total else 0,
        "outcomes": outcomes,
        "accuracy_distribution": accuracy_counts,
        "depth_performance": depth_by_outcome,
    }


@router.get("/api/trade-learnings/{learning_id}")
async def get_trade_learning_detail(
    learning_id: str, request: Request, oc_session: str | None = Cookie(None)
):
    _require_auth(request, oc_session)
    learning_id = _validate_uuid(learning_id)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_learnings", headers=sb_headers(), params={"id": f"eq.{learning_id}"}
    )
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]
    return {}


# ============================================================================
# Logging & Observability API Routes
# ============================================================================

_KNOWN_DOMAINS = frozenset([
    "pipeline", "trades", "positions", "predictions",
    "meta", "catalysts", "economics", "sitrep",
])


def _empty_domain_summary() -> list:
    return [
        {"domain": domain, "success": 0, "failure": 0, "total": 0, "last_run": None}
        for domain in sorted(_KNOWN_DOMAINS)
    ]


@router.get("/api/logs/domains")
async def get_logs_domains(request: Request, oc_session: str | None = Cookie(None)):
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return _empty_domain_summary()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={"select": "step_name,status,started_at", "started_at": f"gte.{cutoff}", "limit": "2000"},
    )
    if resp.status_code != 200:
        return _empty_domain_summary()
    rows = resp.json()
    domain_data: dict[str, dict] = {
        domain: {"success": 0, "failure": 0, "last_run": None} for domain in _KNOWN_DOMAINS
    }
    for row in rows:
        step_name = row.get("step_name") or ""
        if ":" not in step_name:
            continue
        domain = step_name.split(":", 1)[0]
        if domain not in _KNOWN_DOMAINS:
            continue
        status = row.get("status", "")
        started_at = row.get("started_at")
        if status == "success":
            domain_data[domain]["success"] += 1
        elif status in ("failure", "timeout"):
            domain_data[domain]["failure"] += 1
        current_last = domain_data[domain]["last_run"]
        if started_at and (current_last is None or started_at > current_last):
            domain_data[domain]["last_run"] = started_at
    result = []
    for domain in sorted(_KNOWN_DOMAINS):
        d = domain_data[domain]
        total = d["success"] + d["failure"]
        result.append({"domain": domain, "success": d["success"], "failure": d["failure"], "total": total, "last_run": d["last_run"]})
    return result


@router.get("/api/logs/domain/{domain_name}")
async def get_logs_domain(
    domain_name: str,
    request: Request,
    oc_session: str | None = Cookie(None),
    days: int = 7,
):
    _require_auth(request, oc_session)
    if domain_name not in _KNOWN_DOMAINS:
        return JSONResponse(
            {"error": f"Unknown domain '{domain_name}'. Valid domains: {sorted(_KNOWN_DOMAINS)}"},
            status_code=400,
        )
    days = clamp_days(days, 30)
    if not SUPABASE_URL:
        return {"domain": domain_name, "functions": []}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={"select": "id,step_name,status,duration_ms,started_at,error_message,input_snapshot,output_snapshot", "step_name": f"like.{domain_name}:*", "started_at": f"gte.{cutoff}", "order": "started_at.desc", "limit": "500"},
    )
    if resp.status_code != 200:
        return {"domain": domain_name, "functions": []}
    rows = resp.json()
    funcs: dict[str, dict] = {}
    prefix = f"{domain_name}:"
    for row in rows:
        step_name = row.get("step_name") or ""
        if not step_name.startswith(prefix):
            continue
        fn_name = step_name[len(prefix):]
        if fn_name not in funcs:
            funcs[fn_name] = {"name": fn_name, "success_count": 0, "failure_count": 0, "_durations": [], "runs": []}
        status = row.get("status", "")
        if status == "success":
            funcs[fn_name]["success_count"] += 1
        elif status in ("failure", "timeout"):
            funcs[fn_name]["failure_count"] += 1
        dur = row.get("duration_ms")
        if dur is not None:
            try:
                funcs[fn_name]["_durations"].append(float(dur))
            except (ValueError, TypeError):
                pass
        if len(funcs[fn_name]["runs"]) < 20:
            funcs[fn_name]["runs"].append({
                "id": row.get("id"),
                "status": status,
                "duration_ms": dur,
                "started_at": row.get("started_at"),
                "error_message": row.get("error_message"),
                "input_snapshot": row.get("input_snapshot"),
                "output_snapshot": row.get("output_snapshot"),
            })
    functions_list = []
    for fn_name, fn_data in sorted(funcs.items()):
        durations = fn_data.pop("_durations")
        fn_data["avg_duration_ms"] = round(sum(durations) / len(durations)) if durations else None
        functions_list.append(fn_data)
    return {"domain": domain_name, "functions": functions_list}
