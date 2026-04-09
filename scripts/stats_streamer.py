#!/usr/bin/env python3
"""
Stats streamer — pushes real-time hardware metrics from ridley to Supabase.

Runs as a persistent daemon on ridley. Reads CPU/GPU/mem/temp from sysfs
via collectors.py and INSERTs a fresh row into system_stats every 5 seconds.

The Fly.io SSE stream (/api/system/stream) polls system_stats at 2s intervals
using ORDER BY collected_at DESC LIMIT 1, so each new row immediately becomes
the live reading for all connected dashboards.

Run:  python3 scripts/stats_streamer.py
Stop: kill the process (SIGTERM handled gracefully)
Cron: @reboot (persistent daemon — add to ridley crontab)
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone

import httpx

# ── Path setup ─────────────────────────────────────────────────────────────────
# This script lives in scripts/. We need systems-console/ for collectors and config.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "systems-console"))

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
from config import SUPABASE_KEY, SUPABASE_URL, sb_headers  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────
INTERVAL: int = 5  # seconds between rows
CPU_CORE_COUNT: int = 6
MEM_TOTAL_MB: int = 7620  # Jetson Orin Nano Super — constant

# ── HTTP client (shared, persistent) ──────────────────────────────────────────
_client = httpx.Client(timeout=10.0)

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_running = True


def _handle_signal(sig: int, frame: object) -> None:
    global _running
    print(f"[streamer] Signal {sig} received — shutting down", flush=True)
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── Metric collection ─────────────────────────────────────────────────────────


def _safe_float(val: object, default: float = 0.0) -> float:
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def collect_metrics() -> dict:
    """
    Read all hardware sensors and return a dict whose keys match
    system_stats column names exactly (as confirmed from the live DB row).
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
        # /proc/meminfo values are in kB
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
        row["ollama_mem_mb"] = round(get_process_rss_mb("ollama"), 0)
    except Exception:
        row["ollama_mem_mb"] = 0

    try:
        row["openclaw_mem_mb"] = round(get_process_rss_mb("scanner"), 0)
    except Exception:
        row["openclaw_mem_mb"] = 0

    # ── GPU ────────────────────────────────────────────────────────────────────
    try:
        gpu_pct, _gpu_freq = get_gpu_load()
        row["gpu_load_pct"] = round(gpu_pct, 1)
    except Exception:
        row["gpu_load_pct"] = 0.0

    # ── Thermal zones ─────────────────────────────────────────────────────────
    # Zone 0 = cpu, Zone 1 = gpu, Zone 8 = tj (junction/hotspot)
    # server.py reads cpu_temp_c and gpu_temp_c; takes max as TJ gauge
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

    # NVMe mount is /mnt/nvme on ridley; skip gracefully if absent
    try:
        nvme_pct, nvme_used_gb, _nvme_total = get_disk_usage("/mnt/nvme")
        row["disk_nvme_pct"] = round(nvme_pct, 1)
        row["disk_nvme_used_gb"] = round(nvme_used_gb, 1)
    except Exception:
        row["disk_nvme_pct"] = 0.0
        row["disk_nvme_used_gb"] = 0.0

    # ── Process count ──────────────────────────────────────────────────────────
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "ax", "--no-headers"],
            capture_output=True, text=True, timeout=3
        )
        row["process_count"] = len(result.stdout.strip().splitlines()) if result.returncode == 0 else 0
    except Exception:
        row["process_count"] = 0

    # ── Power rails ────────────────────────────────────────────────────────────
    # Not stored in dedicated columns — server uses power_draw hardcoded to 0
    # for now. We include it in row anyway in case the schema adds it later.
    try:
        rails = get_power_rails()
        row["_power_vdd_in_mw"] = rails.get("vdd_in", 0.0)
        row["_power_cpu_gpu_cv_mw"] = rails.get("vdd_cpu_gpu_cv", 0.0)
    except Exception:
        pass  # Power rails are optional — INA3221 may not be populated

    # ── Ollama status (passive probe — no subprocess) ──────────────────────────
    # We check the ollama Unix socket via the REST API quickly.
    # The heartbeat.py already writes to stack_heartbeats; here we write
    # ollama_running/ollama_models/ollama_vram_mb to system_stats so _build_metrics
    # has a fallback when no fresh heartbeat is available.
    try:
        resp = _client.get("http://localhost:11434/api/tags", timeout=2.0)
        if resp.status_code == 200:
            models_data = resp.json().get("models", [])
            row["ollama_running"] = True
            # Store as JSON string to match existing format seen in live DB row:
            # "[{\"name\": \"qwen2.5:3b\", \"size_mb\": 2279, \"vram_mb\": 2279}]"
            import json
            simplified = [
                {
                    "name": m.get("name", ""),
                    "size_mb": m.get("size", 0) // (1024 * 1024),
                }
                for m in models_data
            ]
            row["ollama_models"] = json.dumps(simplified)
            # VRAM estimate: sum of model sizes (best available without nvidia-smi)
            row["ollama_vram_mb"] = sum(m.get("size_mb", 0) for m in simplified)
        else:
            row["ollama_running"] = False
            row["ollama_models"] = "[]"
            row["ollama_vram_mb"] = 0
    except Exception:
        row["ollama_running"] = False
        row["ollama_models"] = "[]"
        row["ollama_vram_mb"] = 0

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
        print("[streamer] SUPABASE_URL or SUPABASE_SERVICE_KEY not set — skipping write", flush=True)
        return False

    # collected_at is inserted by DB default (now()) — we don't include it
    payload = {k: v for k, v in metrics.items()}

    try:
        resp = _client.post(
            f"{SUPABASE_URL}/rest/v1/system_stats",
            headers={**sb_headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code in (200, 201):
            return True
        # Log non-success responses (e.g. constraint violations or schema mismatches)
        print(f"[streamer] Supabase returned {resp.status_code}: {resp.text[:200]}", flush=True)
        return False
    except httpx.TimeoutException:
        print("[streamer] Supabase write timed out", flush=True)
        return False
    except Exception as e:
        print(f"[streamer] Supabase write error: {e}", flush=True)
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────


def main() -> None:
    if not SUPABASE_URL:
        print("[streamer] FATAL: SUPABASE_URL is not set. Did you source ~/.openclaw/workspace/.env?", flush=True)
        sys.exit(1)

    print(
        f"[streamer] Started — writing system_stats every {INTERVAL}s | "
        f"pid={os.getpid()} | {datetime.now(timezone.utc).isoformat()}",
        flush=True,
    )

    # Warm up the CPU differential sampler (first call always returns 0%)
    try:
        get_cpu_utilization()
    except Exception:
        pass
    time.sleep(1)  # let /proc/stat accumulate one tick

    consecutive_failures = 0

    while _running:
        loop_start = time.monotonic()
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

            if ok:
                consecutive_failures = 0
                print(
                    f"[streamer] OK | CPU={cpu}% GPU={gpu}% MEM={mem}% "
                    f"TJ={tj:.1f}C disk={disk}%",
                    flush=True,
                )
            else:
                consecutive_failures += 1
                print(
                    f"[streamer] WRITE FAIL ({consecutive_failures}) | "
                    f"CPU={cpu}% GPU={gpu}% MEM={mem}%",
                    flush=True,
                )

            # Back off on repeated failures to avoid hammering Supabase
            if consecutive_failures >= 10:
                print("[streamer] 10 consecutive failures — sleeping 60s before retry", flush=True)
                time.sleep(60)
                consecutive_failures = 0
                continue

        except Exception as e:
            print(f"[streamer] Unhandled error in main loop: {e}", flush=True)
            consecutive_failures += 1

        # Sleep for the remainder of the interval (account for collection time)
        elapsed = time.monotonic() - loop_start
        sleep_time = max(0.0, INTERVAL - elapsed)
        time.sleep(sleep_time)

    print("[streamer] Exiting cleanly", flush=True)
    _client.close()


if __name__ == "__main__":
    main()
