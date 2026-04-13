#!/usr/bin/env python3
"""
System Monitor — unified hardware metrics + service health daemon for ridley.

Merges the responsibilities of stats_streamer.py (hardware metrics every 5s)
and heartbeat.py (Ollama/Supabase liveness every 5 min) into a single
persistent daemon.

Every 5 seconds:
    - Collects CPU/GPU/RAM/temp/disk metrics
    - Includes cached service status (ollama_running, ollama_models, ollama_vram_mb)
    - INSERTs a new row into system_stats

Every 60 seconds (every 12th iteration):
    - Checks Ollama liveness (GET http://localhost:11434/api/tags)
    - Checks Supabase accessibility
    - Updates the cached service status
    - Sends Slack alert if a service transitions to DOWN

The Fly.io SSE stream (/api/system/stream) polls system_stats at 2s intervals
using ORDER BY collected_at DESC LIMIT 1, so each new row immediately becomes
the live reading for all connected dashboards.

Run:  python3 scripts/system_monitor.py
Stop: kill the process (SIGTERM/SIGINT handled gracefully)
Cron: @reboot (persistent daemon — add to ridley crontab)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx

# ── Path setup ─────────────────────────────────────────────────────────────────
# This script lives in scripts/. We need systems-console/ for collectors and config.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "systems-console"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))

from collectors import (  # noqa: E402
    get_cpu_freq_mhz,
    get_cpu_utilization,
    get_disk_usage,
    get_gpu_load,
    get_mem_stats,
    get_power_rails,
    get_process_rss_mb,
    get_thermal_zones,
    get_uptime_seconds,
)
from common import OLLAMA_URL, slack_notify  # noqa: E402
from config import SUPABASE_KEY, SUPABASE_URL, sb_headers  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────
INTERVAL: int = 5          # seconds between rows
SERVICE_CHECK_EVERY: int = 12  # iterations between service checks (60s)
CPU_CORE_COUNT: int = 6
MEM_TOTAL_MB: int = 7620   # Jetson Orin Nano Super — constant

# ── HTTP client (shared, persistent) ──────────────────────────────────────────
_client = httpx.Client(timeout=10.0)

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_running = True


def _handle_signal(sig: int, frame: object) -> None:
    global _running
    print(f"[monitor] Signal {sig} received — shutting down", flush=True)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── Service status cache ──────────────────────────────────────────────────────
# Initialized to "unknown" state; updated every SERVICE_CHECK_EVERY iterations.
_service_cache: dict = {
    "ollama_running": False,
    "ollama_models": "[]",
    "ollama_vram_mb": 0,
    "supabase_ok": False,
}

# Track previous down state to avoid spamming Slack on every iteration
_prev_ollama_down: bool = True
_prev_supabase_down: bool = True


# ── Service health checks ─────────────────────────────────────────────────────


def check_ollama() -> dict:
    """Check if Ollama is running and responsive. Returns status dict."""
    try:
        resp = _client.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        if resp.status_code == 200:
            models_data = resp.json().get("models", [])
            simplified = [
                {
                    "name": m.get("name", ""),
                    "size_mb": m.get("size", 0) // (1024 * 1024),
                }
                for m in models_data
            ]
            vram_mb = sum(m.get("size_mb", 0) for m in simplified)
            return {
                "alive": True,
                "models_json": json.dumps(simplified),
                "vram_mb": vram_mb,
            }
    except Exception:
        pass
    return {"alive": False, "models_json": "[]", "vram_mb": 0}


def check_supabase() -> bool:
    """Check if Supabase REST API is accessible."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    try:
        resp = _client.get(
            f"{SUPABASE_URL}/rest/v1/budget_config",
            headers=sb_headers(),
            params={"select": "id", "limit": "1"},
            timeout=5.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


def run_service_checks() -> None:
    """Run all service checks and update the cache. Send Slack alerts on DOWN."""
    global _service_cache, _prev_ollama_down, _prev_supabase_down

    ollama = check_ollama()
    supabase_ok = check_supabase()

    # Update cache
    _service_cache["ollama_running"] = ollama["alive"]
    _service_cache["ollama_models"] = ollama["models_json"]
    _service_cache["ollama_vram_mb"] = ollama["vram_mb"]
    _service_cache["supabase_ok"] = supabase_ok

    ollama_down = not ollama["alive"]
    supabase_down = not supabase_ok

    # Alert on transition to DOWN (not on every check)
    alerts = []
    if ollama_down and not _prev_ollama_down:
        alerts.append("`ollama`")
    if supabase_down and not _prev_supabase_down:
        alerts.append("`supabase`")

    if alerts:
        slack_notify(
            f"*System Monitor ALERT* — service(s) DOWN on ridley: {', '.join(alerts)}"
        )

    _prev_ollama_down = ollama_down
    _prev_supabase_down = supabase_down

    status_str = (
        f"ollama={'UP' if ollama['alive'] else 'DOWN'} "
        f"supabase={'UP' if supabase_ok else 'DOWN'}"
    )
    print(f"[monitor] Service check | {status_str}", flush=True)


# ── Metric collection ─────────────────────────────────────────────────────────


def _safe_float(val: object, default: float = 0.0) -> float:
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def collect_metrics() -> dict:
    """
    Read all hardware sensors and return a dict whose keys match
    system_stats column names exactly. Service status is injected from
    the cached values updated every SERVICE_CHECK_EVERY iterations.
    """
    row: dict = {}

    # ── CPU ──────────────────────────────────────────────────────────────────
    try:
        cpu_total, _cores = get_cpu_utilization()
        row["cpu_percent"] = round(cpu_total, 1)
    except Exception:
        row["cpu_percent"] = 0.0
    row["cpu_cores"] = CPU_CORE_COUNT

    try:
        row["cpu_freq_mhz"] = get_cpu_freq_mhz()
    except Exception:
        row["cpu_freq_mhz"] = 0

    # ── Load averages ─────────────────────────────────────────────────────────
    try:
        la1, la5, _la15 = os.getloadavg()
        row["load_avg_1m"] = round(la1, 2)
        row["load_avg_5m"] = round(la5, 2)
    except Exception:
        row["load_avg_1m"] = 0.0
        row["load_avg_5m"] = 0.0

    # ── Memory ────────────────────────────────────────────────────────────────
    try:
        mem = get_mem_stats()
        total_kb = mem.get("MemTotal", MEM_TOTAL_MB * 1024)
        avail_kb = mem.get("MemAvailable", 0)
        used_kb = max(0, total_kb - avail_kb)
        total_mb = total_kb // 1024
        used_mb = used_kb // 1024
        avail_mb = avail_kb // 1024
        row["mem_total_mb"] = total_mb
        row["mem_used_mb"] = used_mb
        row["mem_available_mb"] = avail_mb
        row["mem_percent"] = round((used_mb / total_mb) * 100, 1) if total_mb > 0 else 0.0
    except Exception:
        row["mem_total_mb"] = MEM_TOTAL_MB
        row["mem_used_mb"] = 0
        row["mem_available_mb"] = MEM_TOTAL_MB
        row["mem_percent"] = 0.0

    # ── Per-process memory (best-effort; never blocks row insert on failure) ──
    try:
        row["ollama_mem_mb"] = int(get_process_rss_mb("ollama"))
    except Exception:
        row["ollama_mem_mb"] = 0

    try:
        row["openclaw_mem_mb"] = int(get_process_rss_mb("scanner"))
    except Exception:
        row["openclaw_mem_mb"] = 0

    # ── GPU ────────────────────────────────────────────────────────────────────
    try:
        gpu_pct, _gpu_freq = get_gpu_load()
        row["gpu_load_pct"] = round(gpu_pct, 1)
    except Exception:
        row["gpu_load_pct"] = 0.0

    # ── Thermal zones ─────────────────────────────────────────────────────────
    try:
        zones = get_thermal_zones()
        row["cpu_temp_c"] = _safe_float(zones.get("cpu"), 0.0)
        row["gpu_temp_c"] = _safe_float(zones.get("gpu"), 0.0)
    except Exception:
        row["cpu_temp_c"] = 0.0
        row["gpu_temp_c"] = 0.0

    # ── Disk ───────────────────────────────────────────────────────────────────
    try:
        disk_pct, _used_gb, _total_gb = get_disk_usage("/")
        row["disk_root_pct"] = round(disk_pct, 1)
    except Exception:
        row["disk_root_pct"] = 0.0

    try:
        nvme_pct, nvme_used_gb, _nvme_total = get_disk_usage("/mnt/nvme")
        row["disk_nvme_pct"] = round(nvme_pct, 1)
        row["disk_nvme_used_gb"] = round(nvme_used_gb, 1)
    except Exception:
        row["disk_nvme_pct"] = 0.0
        row["disk_nvme_used_gb"] = 0.0

    # ── Process count ──────────────────────────────────────────────────────────
    try:
        result = subprocess.run(
            ["ps", "ax", "--no-headers"],
            capture_output=True, text=True, timeout=3
        )
        row["process_count"] = len(result.stdout.strip().splitlines()) if result.returncode == 0 else 0
    except Exception:
        row["process_count"] = 0

    # ── Power rails (optional — INA3221 may not be populated) ─────────────────
    try:
        rails = get_power_rails()
        row["_power_vdd_in_mw"] = rails.get("vdd_in", 0.0)
        row["_power_cpu_gpu_cv_mw"] = rails.get("vdd_cpu_gpu_cv", 0.0)
    except Exception:
        pass

    # ── Service status (injected from cache) ───────────────────────────────────
    row["ollama_running"] = _service_cache["ollama_running"]
    row["ollama_models"] = _service_cache["ollama_models"]
    row["ollama_vram_mb"] = _service_cache["ollama_vram_mb"]

    # ── Static / slow-changing fields ─────────────────────────────────────────
    row["power_mode"] = "MAXN_SUPER"
    try:
        row["uptime_seconds"] = get_uptime_seconds()
    except Exception:
        row["uptime_seconds"] = 0

    # Remove internal scratch keys (prefixed with _)
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ── Supabase write ────────────────────────────────────────────────────────────


def insert_metrics(metrics: dict) -> bool:
    """
    INSERT a new row into system_stats.

    system_stats uses a serial PK (id integer) + collected_at defaulting to now().
    We don't upsert — each call produces a new timestamped row so the history
    endpoint (/api/system/metrics/{name}/history) can return sparkline data.
    The SSE endpoint always reads ORDER BY collected_at DESC LIMIT 1.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(
            "[monitor] SUPABASE_URL or SUPABASE_SERVICE_KEY not set — skipping write",
            flush=True,
        )
        return False

    payload = dict(metrics)

    try:
        resp = _client.post(
            f"{SUPABASE_URL}/rest/v1/system_stats",
            headers={**sb_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code in (200, 201):
            return True
        print(
            f"[monitor] Supabase returned {resp.status_code}: {resp.text[:200]}",
            flush=True,
        )
        return False
    except httpx.TimeoutException:
        print("[monitor] Supabase write timed out", flush=True)
        return False
    except Exception as e:
        print(f"[monitor] Supabase write error: {e}", flush=True)
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────


def main() -> None:
    if not SUPABASE_URL:
        print(
            "[monitor] FATAL: SUPABASE_URL is not set. "
            "Did you source ~/.openclaw/workspace/.env?",
            flush=True,
        )
        sys.exit(1)

    print(
        f"[monitor] Started — hardware metrics every {INTERVAL}s, "
        f"service checks every {INTERVAL * SERVICE_CHECK_EVERY}s | "
        f"pid={os.getpid()} | {datetime.now(timezone.utc).isoformat()}",
        flush=True,
    )

    # Warm up the CPU differential sampler (first call always returns 0%)
    try:
        get_cpu_utilization()
    except Exception:
        pass
    time.sleep(1)  # let /proc/stat accumulate one tick

    # Run initial service check immediately so first rows have real service status
    run_service_checks()

    consecutive_failures = 0
    iteration = 0

    while _running:
        loop_start = time.monotonic()
        iteration += 1

        # Run service checks every SERVICE_CHECK_EVERY iterations (after the first)
        if iteration > 1 and iteration % SERVICE_CHECK_EVERY == 0:
            try:
                run_service_checks()
            except Exception as e:
                print(f"[monitor] Service check error: {e}", flush=True)

        try:
            metrics = collect_metrics()
            ok = insert_metrics(metrics)

            cpu = metrics.get("cpu_percent", 0)
            gpu = metrics.get("gpu_load_pct", 0)
            mem = metrics.get("mem_percent", 0)
            cpu_t = metrics.get("cpu_temp_c", 0)
            gpu_t = metrics.get("gpu_temp_c", 0)
            tj = max(cpu_t, gpu_t)
            disk = metrics.get("disk_root_pct", 0)
            ollama = "UP" if metrics.get("ollama_running") else "DOWN"

            if ok:
                consecutive_failures = 0
                print(
                    f"[monitor] OK | CPU={cpu}% GPU={gpu}% MEM={mem}% "
                    f"TJ={tj:.1f}C disk={disk}% ollama={ollama}",
                    flush=True,
                )
            else:
                consecutive_failures += 1
                print(
                    f"[monitor] WRITE FAIL ({consecutive_failures}) | "
                    f"CPU={cpu}% GPU={gpu}% MEM={mem}%",
                    flush=True,
                )

            # Back off on repeated failures to avoid hammering Supabase
            if consecutive_failures >= 10:
                print(
                    "[monitor] 10 consecutive failures — sleeping 60s before retry",
                    flush=True,
                )
                time.sleep(60)
                consecutive_failures = 0
                continue

        except Exception as e:
            print(f"[monitor] Unhandled error in main loop: {e}", flush=True)
            consecutive_failures += 1

        # Sleep for the remainder of the interval (account for collection time)
        elapsed = time.monotonic() - loop_start
        sleep_time = max(0.0, INTERVAL - elapsed)
        time.sleep(sleep_time)

    print("[monitor] Exiting cleanly", flush=True)
    _client.close()


if __name__ == "__main__":
    main()
