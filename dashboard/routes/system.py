"""
System Metrics API — /api/system/* and /api/llm/stats

Real-time system telemetry from the Jetson Orin Nano (ridley),
SSE streaming endpoint, metric history, and LLM inference statistics.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import StreamingResponse
from shared import (
    ALPACA_BASE,
    ALPACA_KEY,
    ALPACA_SECRET,
    SUPABASE_URL,
    _require_auth,
    get_http,
    sb_headers,
)

router = APIRouter()

# ============================================================================
# SSE tier intervals (seconds)
# ============================================================================

_FAST_INTERVAL = 2
_MED_INTERVAL = 5
_SLOW_INTERVAL = 30


# ============================================================================
# Metric helper functions
# ============================================================================


def _status(value: float, normal_lt: float, warning_lt: float) -> str:
    """Compute normal/warning/critical status. normal < normal_lt, warning < warning_lt."""
    if value < normal_lt:
        return "normal"
    if value < warning_lt:
        return "warning"
    return "critical"


def _status_gt(value: float, normal_gt: float, warning_gt: float) -> str:
    """Inverted thresholds — higher is better (e.g. pipeline_health)."""
    if value > normal_gt:
        return "normal"
    if value > warning_gt:
        return "warning"
    return "critical"


def _build_metrics(
    row: dict,
    pipeline_runs: list[dict],
    cron_rows: list[dict],
    stack_services: dict[str, bool],
    inference_rows: list[dict],
    network_ms: float,
    ollama_heartbeat: dict | None,
) -> dict:
    """Assemble the full metrics dict from raw DB data."""
    cpu_pct = float(row.get("cpu_percent", 0) or 0)
    mem_pct = float(row.get("mem_percent", 0) or 0)
    gpu_pct = float(row.get("gpu_load_pct", 0) or 0)
    cpu_temp = float(row.get("cpu_temp_c", 0) or 0)
    gpu_temp = float(row.get("gpu_temp_c", 0) or 0)
    tj = max(cpu_temp, gpu_temp)
    cores = int(row.get("cpu_cores", 6) or 6)

    # pipeline_health
    total_runs = len(pipeline_runs)
    success_runs = sum(1 for r in pipeline_runs if r.get("status") == "success")
    pipeline_pct = round(success_runs / total_runs * 100, 1) if total_runs else 0.0

    # inference_latency from pipeline_runs
    durations = [
        float(r.get("duration_ms") or 0)
        for r in inference_rows
        if r.get("duration_ms") is not None
    ]
    durations.sort()
    n = len(durations)
    lat_value = durations[n // 2] if n else 0.0
    lat_p95 = durations[int(n * 0.95)] if n > 1 else lat_value

    # cron_health — latest root run per pipeline
    _cron_seen: dict[str, dict] = {}
    for cr in cron_rows:
        name = cr.get("pipeline_name", "")
        if name not in _cron_seen:
            _cron_seen[name] = cr

    def _cron_entry(pipeline_key: str, max_age_h: float) -> dict:
        row_c = _cron_seen.get(pipeline_key)
        if not row_c:
            return {"last_run": None, "status": "unknown", "stale": True}
        last_run_str = row_c.get("started_at") or ""
        try:
            last_run_dt = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - last_run_dt).total_seconds() / 3600
            stale = age_h > max_age_h
        except (ValueError, AttributeError):
            stale = True
        return {
            "last_run": last_run_str or None,
            "status": row_c.get("status", "unknown"),
            "stale": stale,
        }

    cron_pipelines = {
        "scanner": _cron_entry("scanner", 13),
        "catalyst_ingest": _cron_entry("catalyst_ingest", 9),
        "position_manager": _cron_entry("position_manager", 1),
        "meta_daily": _cron_entry("meta_daily", 25),
        "meta_weekly": _cron_entry("meta_weekly", 170),
        "calibrator": _cron_entry("calibrator", 170),
    }
    fresh_count = sum(1 for v in cron_pipelines.values() if not v["stale"])

    # ollama_status — read directly from system_stats
    running = row.get("ollama_running", False)
    ollama_val = "loaded" if running else "down"
    raw_models = row.get("ollama_models") or []
    models_loaded = raw_models if isinstance(raw_models, list) else []
    vram_mb = int(row.get("ollama_vram_mb", 0) or 0)

    # stack_health
    svc_count = sum(1 for v in stack_services.values() if v)

    # disk_root
    disk_root_pct = float(row.get("disk_root_pct", 0) or 0)
    total_gb = 60.0  # Jetson Orin Nano eMMC
    used_gb = round(disk_root_pct * total_gb / 100, 1)

    return {
        "cpu_usage": {
            "value": cpu_pct,
            "status": _status(cpu_pct, 70, 90),
            "per_core": [cpu_pct] * cores,
            "freq_mhz": float(row.get("cpu_freq_mhz", 0) or 0),
        },
        "mem_usage": {
            "value": mem_pct,
            "status": _status(mem_pct, 75, 90),
            "total_mb": float(row.get("mem_total_mb", 0) or 0),
            "used_mb": float(row.get("mem_used_mb", 0) or 0),
            "available_mb": float(row.get("mem_available_mb", 0) or 0),
            "breakdown": {
                "ollama_mb": float(row.get("ollama_mem_mb", 0) or 0),
                "gateway_mb": 0.0,
                "openclaw_mb": float(row.get("openclaw_mem_mb", 0) or 0),
            },
        },
        "gpu_load": {
            "value": gpu_pct,
            "status": _status(gpu_pct, 70, 90),
            "freq_mhz": 1020,
        },
        "tj_temp": {
            "value": tj,
            "status": _status(tj, 70, 85),
            "zones": {
                "cpu": cpu_temp,
                "gpu": gpu_temp,
                "cv0": None,
                "cv1": None,
                "cv2": None,
                "soc0": None,
                "soc1": None,
                "soc2": None,
                "tj": tj,
            },
        },
        "inference_latency": {
            "value": lat_value,
            "status": _status(lat_value, 5000, 15000),
            "p50": lat_value,
            "p95": lat_p95,
            "sample_count": n,
        },
        "ollama_tokens_per_sec": {
            "value": 0.0,
            "status": "normal",
            "min": 0.0,
            "max": 0.0,
        },
        "pipeline_health": {
            "value": pipeline_pct,
            "status": _status_gt(pipeline_pct, 95, 80),
            "total": total_runs,
            "successes": success_runs,
            "failures": total_runs - success_runs,
        },
        "cron_health": {
            "value": fresh_count,
            "status": "normal" if fresh_count >= 5 else "warning" if fresh_count >= 3 else "critical",
            "pipelines": cron_pipelines,
        },
        "swap_usage": {
            "value": 0.0,
            "status": "normal",
            "total_mb": 0,
        },
        "disk_root_usage": {
            "value": disk_root_pct,
            "status": _status(disk_root_pct, 75, 90),
            "used_gb": used_gb,
            "total_gb": total_gb,
        },
        "power_draw": {
            "value": 0.0,
            "status": "normal",
            "rails": {"vdd_in": 0, "vdd_cpu_gpu_cv": 0, "vdd_soc": 0},
        },
        "ollama_status": {
            "value": ollama_val,
            "status": "normal" if ollama_val != "down" else "critical",
            "models_loaded": models_loaded,
            "vram_mb": vram_mb,
        },
        "stack_health": {
            "value": svc_count,
            "status": "normal"
            if svc_count >= 7
            else "warning"
            if svc_count >= 5
            else "critical",
            "services": {
                k: stack_services.get(k, False)
                for k in [
                    "supabase",
                    "alpaca",
                    "ollama",
                    "finnhub",
                    "sentry",
                    "pgvector",
                    "tumbler",
                    "claude",
                ]
            },
        },
        "network_latency": {
            "value": network_ms,
            "status": _status(network_ms, 100, 250) if network_ms > 0 else "normal",
        },
    }


async def _fetch_system_data(client) -> dict:
    """Fetch all raw data needed to build the metrics dict. Returns dict of raw results."""
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    async def _get_stats() -> dict:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/system_stats",
                headers=sb_headers(),
                params={"order": "collected_at.desc", "limit": "1"},
            )
            rows = r.json() if r.status_code == 200 else []
            return rows[0] if rows else {}
        except Exception:
            return {}

    async def _get_pipeline_runs() -> list[dict]:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/pipeline_runs",
                headers=sb_headers(),
                params={
                    "select": "status",
                    "step_name": "eq.root",
                    "started_at": f"gte.{cutoff_24h}",
                    "limit": "500",
                },
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _get_cron_rows() -> list[dict]:
        """Get latest root run per pipeline. Use 7-day window + high limit to catch weekly jobs."""
        try:
            cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/pipeline_runs",
                headers=sb_headers(),
                params={
                    "select": "pipeline_name,status,started_at",
                    "step_name": "eq.root",
                    "started_at": f"gte.{cutoff_7d}",
                    "order": "started_at.desc",
                    "limit": "500",
                },
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _get_inference_rows() -> list[dict]:
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/pipeline_runs",
                headers=sb_headers(),
                params={
                    "select": "duration_ms",
                    "or": "step_name.like.*call_claude*,step_name.like.*call_ollama*",
                    "started_at": f"gte.{cutoff_24h}",
                    "limit": "200",
                },
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _get_network_ms() -> float:
        try:
            t0 = asyncio.get_event_loop().time()
            r = await client.get(
                f"{ALPACA_BASE}/v2/clock",
                headers={
                    "APCA-API-KEY-ID": ALPACA_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET,
                },
                timeout=5.0,
            )
            elapsed = (asyncio.get_event_loop().time() - t0) * 1000
            return round(elapsed, 1) if r.status_code in (200, 403) else 9999.0
        except Exception:
            return 9999.0

    async def _get_stack_live() -> dict[str, bool]:
        """Live-ping all 8 services for stack health."""
        results: dict[str, bool] = {}
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/budget_config",
                headers=sb_headers(),
                params={"select": "id", "limit": "1"},
            )
            results["supabase"] = r.status_code == 200
        except Exception:
            results["supabase"] = False
        try:
            r = await client.get(
                f"{ALPACA_BASE}/v2/account",
                headers={
                    "APCA-API-KEY-ID": ALPACA_KEY,
                    "APCA-API-SECRET-KEY": ALPACA_SECRET,
                },
            )
            results["alpaca"] = r.status_code == 200
        except Exception:
            results["alpaca"] = False
        from shared import FINNHUB_KEY

        results["finnhub"] = bool(FINNHUB_KEY)
        try:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/rpc/match_meta_reflections",
                headers=sb_headers(),
                json={"query_embedding": [0.0] * 768, "match_threshold": 0.0, "match_count": 1},
            )
            results["pgvector"] = r.status_code in (200, 406)
        except Exception:
            results["pgvector"] = False
        from shared import ANTHROPIC_API_KEY

        results["claude"] = bool(ANTHROPIC_API_KEY and len(ANTHROPIC_API_KEY) > 10)
        from shared import SENTRY_AUTH_TOKEN

        results["sentry"] = bool(SENTRY_AUTH_TOKEN or __import__("os").environ.get("SENTRY_DSN", ""))
        return results

    (stats_row, pipeline_runs, cron_rows, inference_rows, network_ms) = await asyncio.gather(
        _get_stats(),
        _get_pipeline_runs(),
        _get_cron_rows(),
        _get_inference_rows(),
        _get_network_ms(),
    )

    stack_services = await _get_stack_live()
    ollama_running = bool((stats_row or {}).get("ollama_running", False))
    stack_services["ollama"] = ollama_running
    stack_services["tumbler"] = ollama_running and stack_services.get("supabase", False)

    return {
        "stats_row": stats_row,
        "pipeline_runs": pipeline_runs,
        "cron_rows": cron_rows,
        "inference_rows": inference_rows,
        "stack_services": stack_services,
        "ollama_heartbeat": None,
        "network_ms": float(network_ms),
        "collected_at": (stats_row or {}).get("collected_at"),
    }


# ============================================================================
# Route handlers
# ============================================================================


@router.get("/api/system/current")
async def get_system_current(request: Request, oc_session: str | None = Cookie(None)):
    """Latest system stats from Jetson."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_stats",
        headers=sb_headers(),
        params={"order": "collected_at.desc", "limit": "1"},
    )
    if resp.status_code == 200:
        rows = resp.json()
        return rows[0] if rows else {}
    return {}


@router.get("/api/system/history")
async def get_system_history(
    request: Request,
    oc_session: str | None = Cookie(None),
    minutes: int = 30,
):
    """System stats history for charts."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_stats",
        headers=sb_headers(),
        params={
            "select": "cpu_percent,mem_percent,gpu_load_pct,gpu_temp_c,cpu_temp_c,collected_at",
            "collected_at": f"gte.{cutoff}",
            "order": "collected_at.asc",
        },
    )
    if resp.status_code == 200:
        return resp.json()
    return []


@router.get("/api/system/info")
async def system_info(request: Request, oc_session: str | None = Cookie(None)):
    """Static hardware info for the systems console header."""
    _require_auth(request, oc_session)
    return {
        "hostname": "ridley",
        "hardware": {
            "device": "Jetson Orin Nano Super",
            "cpu": "6x ARM Cortex-A78AE",
            "gpu": "Orin (Ampere)",
            "ram_mb": 7620,
            "power_mode": "MAXN_SUPER",
        },
    }


@router.get("/api/system/metrics")
async def system_metrics(request: Request, oc_session: str | None = Cookie(None)):
    """Full snapshot of all system metrics for initial load and reconnection."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"timestamp": None, "metrics": {}}

    client = get_http()
    raw = await _fetch_system_data(client)
    metrics = _build_metrics(
        row=raw["stats_row"],
        pipeline_runs=raw["pipeline_runs"],
        cron_rows=raw["cron_rows"],
        stack_services=raw["stack_services"],
        inference_rows=raw["inference_rows"],
        network_ms=raw["network_ms"],
        ollama_heartbeat=raw["ollama_heartbeat"],
    )
    return {"timestamp": raw["collected_at"], "metrics": metrics}


@router.get("/api/system/metrics/{metric_name}/history")
async def system_metric_history(
    metric_name: str,
    request: Request,
    oc_session: str | None = Cookie(None),
    window: int = 300,
) -> dict:
    """Historical datapoints for a single metric (sparklines). Window in seconds."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"datapoints": []}

    col_map = {
        "cpu_usage": "cpu_percent",
        "mem_usage": "mem_percent",
        "gpu_load": "gpu_load_pct",
        "tj_temp": "gpu_temp_c",
    }
    col = col_map.get(metric_name)
    if not col:
        return {"datapoints": []}

    window = min(max(60, window), 3600)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/system_stats",
        headers=sb_headers(),
        params={
            "select": f"{col},collected_at",
            "collected_at": f"gte.{cutoff}",
            "order": "collected_at.asc",
            "limit": "150",
        },
    )
    rows = resp.json() if resp.status_code == 200 else []
    return {
        "datapoints": [
            {"value": float(r.get(col, 0) or 0), "ts": r.get("collected_at")} for r in rows
        ]
    }


@router.get("/api/system/stream")
async def system_stream(request: Request, oc_session: str | None = Cookie(None)):
    """Server-Sent Events stream — real-time metric updates at three tiers."""
    _require_auth(request, oc_session)

    client = get_http()

    async def generate():
        last_fast = 0.0
        last_med = 0.0
        last_slow = 0.0
        prev_status: dict[str, str] = {}

        while True:
            if await request.is_disconnected():
                break

            now = asyncio.get_event_loop().time()
            send_fast = now - last_fast >= _FAST_INTERVAL
            send_med = now - last_med >= _MED_INTERVAL
            send_slow = now - last_slow >= _SLOW_INTERVAL

            if not (send_fast or send_med or send_slow):
                await asyncio.sleep(0.5)
                continue

            try:
                raw = await _fetch_system_data(client)
            except Exception:
                await asyncio.sleep(2)
                continue

            all_metrics = _build_metrics(
                row=raw["stats_row"],
                pipeline_runs=raw["pipeline_runs"],
                cron_rows=raw["cron_rows"],
                stack_services=raw["stack_services"],
                inference_rows=raw["inference_rows"],
                network_ms=raw["network_ms"],
                ollama_heartbeat=raw["ollama_heartbeat"],
            )

            updates: dict = {}
            alerts: list[dict] = []

            def _maybe_add(key: str) -> None:
                m = all_metrics.get(key)
                if m is None:
                    return
                updates[key] = m
                status = m.get("status", "normal")
                if prev_status.get(key) not in (None, status):
                    alerts.append(
                        {
                            "metric": key,
                            "value": m.get("value"),
                            "status": status,
                            "message": f"{key} transitioned to {status}",
                        }
                    )
                prev_status[key] = status

            if send_fast:
                last_fast = now
                for k in ("cpu_usage", "mem_usage", "gpu_load", "tj_temp"):
                    _maybe_add(k)

            if send_med:
                last_med = now
                for k in ("ollama_status", "swap_usage", "power_draw"):
                    _maybe_add(k)

            if send_slow:
                last_slow = now
                for k in (
                    "inference_latency",
                    "ollama_tokens_per_sec",
                    "pipeline_health",
                    "cron_health",
                    "stack_health",
                    "network_latency",
                    "disk_root_usage",
                ):
                    _maybe_add(k)

            if updates:
                ts = raw.get("collected_at") or datetime.now(timezone.utc).isoformat()
                payload = json.dumps({"timestamp": ts, "updates": updates})
                yield f"event: metrics\ndata: {payload}\n\n"

            for alert in alerts:
                yield f"event: alert\ndata: {json.dumps(alert)}\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/llm/stats")
async def get_llm_stats(request: Request, oc_session: str | None = Cookie(None)):
    """LLM inference statistics derived from pipeline_runs."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return {"models": [], "recent": []}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/pipeline_runs",
        headers=sb_headers(),
        params={
            "select": "id,step_name,status,duration_ms,started_at,input_snapshot,output_snapshot",
            "or": "step_name.like.*call_ollama*,step_name.like.*call_claude*",
            "started_at": f"gte.{cutoff}",
            "order": "started_at.desc",
            "limit": "200",
        },
    )
    rows: list[dict] = resp.json() if resp.status_code == 200 else []

    model_stats: dict[str, dict] = {}
    for row in rows:
        step = row.get("step_name", "")
        if "call_claude" in step:
            model = "claude"
        elif "call_ollama" in step:
            model = "qwen2.5:3b"
        else:
            model = step
        entry = model_stats.setdefault(
            model,
            {"model": model, "total_calls": 0, "total_duration_ms": 0, "avg_duration_ms": 0},
        )
        entry["total_calls"] += 1
        entry["total_duration_ms"] += int(row.get("duration_ms") or 0)

    for entry in model_stats.values():
        calls = entry["total_calls"]
        entry["avg_duration_ms"] = round(entry["total_duration_ms"] / calls) if calls else 0

    recent = rows[:20]
    return {"models": list(model_stats.values()), "recent": recent}
