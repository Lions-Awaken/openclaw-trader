"""
Health & Simulator API — /api/health/* and /api/simulator/* and /api/preflight/*

System health check results, run triggers, flight-status manifest,
preflight simulator, and stack/latency checks.
"""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Request
from shared import (
    ALPACA_BASE,
    ALPACA_KEY,
    ALPACA_SECRET,
    ANTHROPIC_API_KEY,
    FINNHUB_KEY,
    RIDLEY_URL,
    SENTRY_AUTH_TOKEN,
    SENTRY_ORG,
    SENTRY_PROJECT,
    SUPABASE_URL,
    _require_auth,
    _validate_uuid,
    get_http,
    sb_headers,
)

router = APIRouter()

# ============================================================================
# Flight manifest for the preflight status endpoint
# ============================================================================

FLIGHT_MANIFEST = [
    {
        "name": "health_check",
        "pipeline_name": "health_check",
        "schedule": "5:00 AM weekdays",
        "criticality": "high",
        "freshness_hours": 26,
        "writes_pipeline_runs": False,
    },
    {
        "name": "catalyst_ingest",
        "pipeline_name": "catalyst_ingest",
        "schedule": "5:30/9:15/12:50 weekdays",
        "criticality": "high",
        "freshness_hours": 26,
        "writes_pipeline_runs": True,
    },
    {
        "name": "ingest_form4",
        "pipeline_name": "ingest",
        "schedule": "6:00 AM weekdays",
        "criticality": "medium",
        "freshness_hours": 26,
        "writes_pipeline_runs": True,
    },
    {
        "name": "scanner",
        "pipeline_name": "scanner",
        "schedule": "6:35/9:30 weekdays",
        "criticality": "high",
        "freshness_hours": 26,
        "writes_pipeline_runs": True,
    },
    {
        "name": "ingest_options_flow",
        "pipeline_name": "ingest",
        "schedule": "7:00 AM weekdays",
        "criticality": "medium",
        "freshness_hours": 26,
        "writes_pipeline_runs": True,
    },
    {
        "name": "position_manager",
        "pipeline_name": "position_manager",
        "schedule": "Every 30m market hours",
        "criticality": "high",
        "freshness_hours": 2,
        "writes_pipeline_runs": True,
    },
    {
        "name": "meta_daily",
        "pipeline_name": "meta_daily",
        "schedule": "1:30 PM weekdays",
        "criticality": "high",
        "freshness_hours": 26,
        "writes_pipeline_runs": True,
    },
    {
        "name": "meta_weekly",
        "pipeline_name": "meta_weekly",
        "schedule": "4:00 PM Sundays",
        "criticality": "medium",
        "freshness_hours": 170,
        "writes_pipeline_runs": True,
    },
    {
        "name": "calibrator",
        "pipeline_name": "calibrator",
        "schedule": "4:30 PM Sundays",
        "criticality": "medium",
        "freshness_hours": 170,
        "writes_pipeline_runs": True,
    },
    {
        "name": "heartbeat",
        "pipeline_name": "heartbeat",
        "schedule": "Every 5 min",
        "criticality": "low",
        "freshness_hours": 1,
        "writes_pipeline_runs": True,
    },
]


# ============================================================================
# Health check routes
# ============================================================================


@router.get("/api/health/latest")
async def get_health_latest(request: Request, oc_session: str = Cookie(None)):
    """Most recent health check run results, grouped by check_group."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"run_id": None, "checks": []}

    client = get_http()

    # Get the most recent run_id
    latest_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_health",
        headers=sb_headers(),
        params={
            "select": "run_id,run_type,created_at",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    if latest_resp.status_code != 200 or not latest_resp.json():
        return {"run_id": None, "checks": []}

    latest = latest_resp.json()[0]
    run_id = latest["run_id"]
    run_type = latest["run_type"]
    run_created_at = latest["created_at"]

    # Fetch all rows for that run_id
    rows_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_health",
        headers=sb_headers(),
        params={
            "run_id": f"eq.{run_id}",
            "order": "check_order.asc",
            "limit": "200",
        },
    )
    if rows_resp.status_code != 200:
        return {"run_id": run_id, "checks": []}

    rows = rows_resp.json()

    total_pass = sum(1 for r in rows if r.get("status") == "pass")
    total_fail = sum(1 for r in rows if r.get("status") == "fail")
    total_warn = sum(1 for r in rows if r.get("status") == "warn")
    total_skip = sum(1 for r in rows if r.get("status") == "skip")
    total_duration_ms = sum(int(r.get("duration_ms") or 0) for r in rows)

    return {
        "run_id": run_id,
        "run_type": run_type,
        "created_at": run_created_at,
        "total_pass": total_pass,
        "total_fail": total_fail,
        "total_warn": total_warn,
        "total_skip": total_skip,
        "duration_ms": total_duration_ms,
        "checks": rows,
    }


@router.post("/api/health/run")
async def trigger_health_run(request: Request, oc_session: str = Cookie(None)):
    """Trigger system_check.py --mode health as a subprocess with a new run_id."""
    _require_auth(request, oc_session)
    run_id = str(uuid.uuid4())
    scripts_dir = Path(__file__).parent.parent.parent / "scripts" / "system_check.py"
    subprocess_env = {**os.environ, "HEALTH_RUN_ID": run_id}
    subprocess.Popen(
        [sys.executable, str(scripts_dir), "--mode", "health", "--notify-always"],
        env=subprocess_env,
        cwd=str(Path(__file__).parent.parent.parent),
    )
    return {"status": "triggered", "run_id": run_id}


@router.get("/api/health/history")
async def get_health_history(request: Request, oc_session: str = Cookie(None)):
    """Last 7 health check runs as summary rows for the history strip."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []

    client = get_http()

    rows_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_health",
        headers=sb_headers(),
        params={
            "select": "run_id,run_type,status,created_at",
            "order": "created_at.desc",
            "limit": "500",
        },
    )
    if rows_resp.status_code != 200:
        return []

    rows = rows_resp.json()

    # Aggregate by run_id — preserve insertion order (desc by created_at)
    seen: dict[str, dict] = {}
    for r in rows:
        rid = r["run_id"]
        if rid not in seen:
            seen[rid] = {
                "run_id": rid,
                "run_type": r.get("run_type"),
                "created_at": r.get("created_at"),
                "pass": 0,
                "fail": 0,
                "warn": 0,
                "skip": 0,
                "worst": "pass",
            }
        entry = seen[rid]
        status = r.get("status", "skip")
        entry[status] = entry.get(status, 0) + 1
        if status == "fail":
            entry["worst"] = "fail"
        elif status == "warn" and entry["worst"] != "fail":
            entry["worst"] = "warn"

    runs = list(seen.values())[:7]
    return runs


@router.get("/api/health/latency")
async def get_latency(request: Request, oc_session: str | None = Cookie(None)):
    """Measure round-trip latency to Alpaca (NYSE data feed)."""
    _require_auth(request, oc_session)
    result = {"nyse_ms": None, "timestamp": datetime.now(timezone.utc).isoformat()}

    try:
        client = get_http()
        start = time.monotonic()
        r = await client.get(
            "https://data.alpaca.markets/v2/stocks/SPY/quotes/latest",
            headers={
                "APCA-API-KEY-ID": ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
        )
        elapsed = (time.monotonic() - start) * 1000
        if r.status_code == 200:
            result["nyse_ms"] = round(elapsed)
    except Exception:
        pass

    return result


@router.get("/api/health/stack")
async def get_stack_health(request: Request, oc_session: str | None = Cookie(None)):
    """Real health checks against every service in the tech stack."""
    _require_auth(request, oc_session)

    client = get_http()

    async def _check_supabase() -> bool:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/budget_config",
                headers=sb_headers(),
                params={"select": "id", "limit": "1"},
            )
            return r.status_code == 200
        except Exception:
            return False

    async def _check_alpaca() -> bool:
        try:
            r = await client.get(
                f"{ALPACA_BASE}/v2/account",
                headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            )
            return r.status_code == 200
        except Exception:
            return False

    async def _check_ollama() -> bool:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/system_stats",
                headers=sb_headers(),
                params={
                    "select": "ollama_running,collected_at",
                    "order": "collected_at.desc",
                    "limit": "1",
                },
            )
            if r.status_code == 200 and r.json():
                row = r.json()[0]
                collected_at = row.get("collected_at", "")
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
                return bool(row.get("ollama_running", False)) and collected_at > cutoff
            return False
        except Exception:
            return False

    async def _check_finnhub() -> bool:
        try:
            if FINNHUB_KEY:
                r = await client.get(
                    "https://finnhub.io/api/v1/quote",
                    params={"symbol": "AAPL", "token": FINNHUB_KEY},
                )
                return r.status_code == 200 and r.json().get("c", 0) > 0
            return False
        except Exception:
            return False

    async def _check_sentry() -> bool:
        try:
            if SENTRY_AUTH_TOKEN:
                r = await client.get(
                    f"https://sentry.io/api/0/projects/{SENTRY_ORG}/{SENTRY_PROJECT}/",
                    headers={"Authorization": f"Bearer {SENTRY_AUTH_TOKEN}"},
                )
                return r.status_code == 200
            return False
        except Exception:
            return False

    async def _check_pgvector() -> bool:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/signal_evaluations",
                headers={**sb_headers(), "Prefer": "count=exact"},
                params={"select": "id", "embedding": "not.is.null", "limit": "0"},
            )
            return r.status_code == 200
        except Exception:
            return False

    async def _check_tumbler() -> bool:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/system_stats",
                headers=sb_headers(),
                params={
                    "select": "ollama_running,collected_at",
                    "order": "collected_at.desc",
                    "limit": "1",
                },
            )
            if r.status_code == 200 and r.json():
                row = r.json()[0]
                collected_at = row.get("collected_at", "")
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
                return bool(row.get("ollama_running", False)) and collected_at > cutoff
            return False
        except Exception:
            return False

    async def _check_claude() -> bool:
        try:
            if ANTHROPIC_API_KEY:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={"model": "claude-sonnet-4-6", "max_tokens": 1, "messages": []},
                )
                return r.status_code in (200, 400, 429, 529)
            return False
        except Exception:
            return False

    (
        supabase_ok,
        alpaca_ok,
        ollama_ok,
        finnhub_ok,
        sentry_ok,
        pgvector_ok,
        tumbler_ok,
        claude_ok,
    ) = await asyncio.gather(
        _check_supabase(),
        _check_alpaca(),
        _check_ollama(),
        _check_finnhub(),
        _check_sentry(),
        _check_pgvector(),
        _check_tumbler(),
        _check_claude(),
    )

    return {
        "supabase": supabase_ok,
        "alpaca": alpaca_ok,
        "ollama": ollama_ok,
        "finnhub": finnhub_ok,
        "sentry": sentry_ok,
        "pgvector": pgvector_ok,
        "tumbler": tumbler_ok,
        "claude": claude_ok,
    }


@router.get("/api/health/flight-status")
async def get_flight_status(request: Request, oc_session: str = Cookie(None)):
    """Manifest vs reality for today's scheduled functions.

    Consolidated from N+1 per-entry queries into 2 total queries:
    one against pipeline_runs (all pipeline_names at once, latest per name
    resolved in Python), one against system_health (for health_check entry).
    """
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []

    client = get_http()
    now = datetime.now(timezone.utc)

    max_freshness_h = max(e["freshness_hours"] for e in FLIGHT_MANIFEST)
    cutoff = (now - timedelta(hours=max_freshness_h + 12)).isoformat()

    pipeline_names = list(
        {e["pipeline_name"] for e in FLIGHT_MANIFEST if e["writes_pipeline_runs"]}
    )

    latest_per_pipeline: dict[str, str] = {}
    try:
        or_filter = ",".join(f"pipeline_name.eq.{pn}" for pn in pipeline_names)
        pr_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/pipeline_runs",
            headers=sb_headers(),
            params={
                "select": "pipeline_name,started_at",
                "or": f"({or_filter})",
                "step_name": "eq.root",
                "started_at": f"gte.{cutoff}",
                "order": "started_at.desc",
                "limit": "500",
            },
        )
        if pr_resp.status_code == 200:
            for row in pr_resp.json():
                pn = row.get("pipeline_name", "")
                if pn and pn not in latest_per_pipeline:
                    latest_per_pipeline[pn] = row["started_at"]
    except Exception:
        pass

    health_check_last_run: str | None = None
    try:
        sh_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/system_health",
            headers=sb_headers(),
            params={
                "select": "created_at",
                "run_type": "eq.scheduled",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if sh_resp.status_code == 200 and sh_resp.json():
            health_check_last_run = sh_resp.json()[0].get("created_at")
    except Exception:
        pass

    def _compute_entry(entry: dict) -> dict:
        name = entry["name"]
        pipeline_name = entry["pipeline_name"]
        freshness_hours = entry["freshness_hours"]
        writes_pipeline_runs = entry["writes_pipeline_runs"]

        if writes_pipeline_runs:
            last_run_at = latest_per_pipeline.get(pipeline_name)
        else:
            last_run_at = health_check_last_run

        if last_run_at:
            try:
                last_dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                age_h = (now - last_dt).total_seconds() / 3600
                freshness_ok = age_h <= freshness_hours
                status = "ran" if freshness_ok else "stale"
            except (ValueError, AttributeError):
                status = "missing"
                freshness_ok = False
        else:
            status = "missing"
            freshness_ok = False

        return {
            "name": name,
            "schedule": entry["schedule"],
            "pipeline_name": pipeline_name,
            "criticality": entry["criticality"],
            "last_run_at": last_run_at,
            "status": status,
            "freshness_ok": freshness_ok,
            "freshness_hours": freshness_hours,
        }

    return [_compute_entry(e) for e in FLIGHT_MANIFEST]


# ============================================================================
# Simulator / Preflight routes
# ============================================================================


@router.post("/api/simulator/run")
async def trigger_simulator(request: Request, oc_session: str = Cookie(None)):
    """Proxy a preflight simulator trigger to ridley's local dashboard."""
    _require_auth(request, oc_session)

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    concurrency = min(max(int(body.get("concurrency", 1)), 1), 10)

    run_id = str(uuid.uuid4())
    client = get_http()
    try:
        resp = await client.post(
            f"{RIDLEY_URL}/api/preflight/trigger",
            headers={"Content-Type": "application/json"},
            json={"run_id": run_id, "concurrency": concurrency},
            timeout=10.0,
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=503, detail=f"Ridley unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Ridley returned {resp.status_code}")
    return {"status": "triggered", "run_id": run_id, "concurrency": concurrency}


@router.post("/api/preflight/trigger")
async def trigger_preflight_local(request: Request):
    """Spawn system_check.py --mode preflight locally on ridley. Called by Fly.io proxy.

    This endpoint is intentionally unauthenticated — it is only reachable from
    within the private Fly.io / ridley network, never from the public internet.
    """
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    run_id = str(body.get("run_id", str(uuid.uuid4())))
    concurrency = min(max(int(body.get("concurrency", 1)), 1), 10)

    scripts_dir = Path(__file__).parent.parent.parent / "scripts" / "system_check.py"
    env = {**os.environ, "SIMULATOR_RUN_ID": run_id, "SIMULATOR_CONCURRENCY": str(concurrency)}
    subprocess.Popen(
        [sys.executable, str(scripts_dir), "--mode", "preflight", "--concurrency", str(concurrency)],
        env=env,
        cwd=str(Path(__file__).parent.parent.parent),
        stdout=open("/tmp/openclaw_simulator.log", "a"),  # noqa: SIM115
        stderr=subprocess.STDOUT,
    )
    return {"status": "triggered", "run_id": run_id, "concurrency": concurrency}


@router.get("/api/simulator/status")
async def get_simulator_status(
    request: Request,
    oc_session: str = Cookie(None),
    run_id: str = "",
):
    """Return all test results written so far for a given simulator run_id."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"run_id": None, "checks": [], "summary": {}}

    client = get_http()

    if not run_id:
        latest_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/system_health",
            headers=sb_headers(),
            params={
                "select": "run_id,created_at",
                "run_type": "eq.simulator",
                "order": "created_at.desc",
                "limit": "1",
            },
        )
        if latest_resp.status_code != 200 or not latest_resp.json():
            return {"run_id": None, "checks": [], "summary": {}}
        run_id = latest_resp.json()[0]["run_id"]

    run_id = _validate_uuid(run_id)

    rows_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_health",
        headers=sb_headers(),
        params={
            "run_id": f"eq.{run_id}",
            "run_type": "eq.simulator",
            "check_name": "neq._trigger",
            "order": "check_order.asc",
            "limit": "100",
        },
    )
    if rows_resp.status_code != 200:
        return {"run_id": run_id, "checks": [], "summary": {}}

    checks = rows_resp.json()
    total = len(checks)
    go_count = sum(1 for c in checks if c.get("status") == "pass")
    nogo_count = sum(1 for c in checks if c.get("status") == "fail")
    scrub_count = sum(1 for c in checks if c.get("status") == "skip")
    complete = total >= 72

    return {
        "run_id": run_id,
        "checks": checks,
        "summary": {
            "total": total,
            "go": go_count,
            "nogo": nogo_count,
            "scrub": scrub_count,
            "complete": complete,
        },
    }
