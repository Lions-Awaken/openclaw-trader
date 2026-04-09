#!/usr/bin/env python3
"""
Watches for simulator trigger rows in system_health and spawns test_system.py on ridley.

This daemon runs on ridley (the Jetson — which has Ollama, crontab, and local scripts).
The Fly.io dashboard writes a trigger row to system_health; this watcher picks it up and
spawns test_system.py with the matching run_id so results flow back via Supabase.

Design:
  - Polls system_health every POLL_INTERVAL seconds for trigger rows with status='skip'
  - A trigger row has check_name='_trigger' and run_type='simulator'
  - If a trigger has no real result rows yet, it hasn't been picked up — spawn the simulator
  - After picking up, mark the trigger row status='pass' to prevent double-spawn
  - test_system.py reads SIMULATOR_RUN_ID from the environment and writes results to Supabase

Usage:
  python scripts/simulator_watcher.py
  # Runs forever — managed by cron flock pattern to prevent duplicate instances
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import _client, sb_get, sb_headers  # noqa: E402

SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
POLL_INTERVAL: int = 15  # seconds
SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT: str = os.path.dirname(SCRIPT_DIR)


def check_for_triggers() -> tuple[str, int] | None:
    """Look for unclaimed simulator triggers and atomically claim one.

    Returns (run_id, concurrency) or None if no unclaimed trigger found.
    """
    rows = sb_get(
        "system_health",
        {
            "select": "run_id,value",
            "run_type": "eq.simulator",
            "check_name": "eq._trigger",
            "status": "eq.skip",  # unclaimed
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    if not rows:
        return None

    run_id: str = rows[0]["run_id"]

    # Extract concurrency from the trigger row's value field (e.g. "concurrency=4")
    trigger_value: str = rows[0].get("value", "")
    concurrency = 1
    if "concurrency=" in trigger_value:
        try:
            concurrency = int(trigger_value.split("concurrency=")[1].split()[0])
        except (ValueError, IndexError):
            pass

    if not SUPABASE_URL:
        print("[watcher] No SUPABASE_URL — cannot claim trigger")
        return None

    # Atomically claim it by updating status from skip to pass.
    # If another watcher already claimed it, this will update 0 rows.
    try:
        resp = _client.patch(
            f"{SUPABASE_URL}/rest/v1/system_health",
            headers={**sb_headers(), "Prefer": "return=representation"},
            params={
                "run_id": f"eq.{run_id}",
                "check_name": "eq._trigger",
                "status": "eq.skip",  # Only claim if still unclaimed
            },
            json={"status": "pass", "value": "claimed by watcher"},
        )
        if resp.status_code == 200 and resp.json():
            # We successfully claimed it
            print(f"[watcher] Claimed trigger for run_id={run_id} concurrency={concurrency}")
            return (run_id, concurrency)
    except Exception as e:
        print(f"[watcher] Failed to claim trigger for run_id={run_id}: {e}")
        return None

    # Another watcher got it first
    return None


def spawn_simulator(run_id: str, concurrency: int = 1) -> None:
    """Launch test_system.py with the given run_id as a background process."""
    print(f"[watcher] Spawning simulator for run_id={run_id} concurrency={concurrency}")
    log_path = "/tmp/openclaw_simulator.log"
    env = {**os.environ, "SIMULATOR_RUN_ID": run_id, "SIMULATOR_CONCURRENCY": str(concurrency)}
    try:
        with open(log_path, "a") as log_fh:  # noqa: WPS515
            subprocess.Popen(
                [sys.executable, os.path.join(SCRIPT_DIR, "test_system.py")],
                env=env,
                cwd=PROJECT_ROOT,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
        print(f"[watcher] Simulator spawned — logs at {log_path}")
    except Exception as e:
        print(f"[watcher] Failed to spawn simulator: {e}")


def main() -> None:
    print(f"[watcher] Simulator watcher started, polling every {POLL_INTERVAL}s")
    if not SUPABASE_URL:
        print("[watcher] WARNING: SUPABASE_URL not set — will poll but cannot act")

    while True:
        try:
            trigger = check_for_triggers()
            if trigger:
                run_id, concurrency = trigger
                spawn_simulator(run_id, concurrency)
                # Sleep longer after a spawn to prevent double-triggering during startup
                time.sleep(30)
            else:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("[watcher] Shutting down")
            break
        except Exception as e:
            print(f"[watcher] Unhandled error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
