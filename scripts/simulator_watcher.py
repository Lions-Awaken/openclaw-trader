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


def check_for_triggers() -> str | None:
    """Return a run_id that has a trigger row but no real results yet, or None."""
    rows = sb_get(
        "system_health",
        {
            "select": "run_id,created_at",
            "run_type": "eq.simulator",
            "check_name": "eq._trigger",
            "status": "eq.skip",
            "order": "created_at.desc",
            "limit": "5",
        },
    )

    for row in rows:
        run_id: str = row["run_id"]
        # Check if this run_id already has real results (rows beyond the trigger)
        results = sb_get(
            "system_health",
            {
                "select": "id",
                "run_id": f"eq.{run_id}",
                "check_name": "neq._trigger",
                "limit": "1",
            },
        )
        if not results:
            # No real result rows yet — this trigger is unclaimed
            return run_id
    return None


def mark_trigger_started(run_id: str) -> None:
    """Update the trigger row to 'pass' so subsequent watcher cycles skip it."""
    if not SUPABASE_URL:
        print("[watcher] No SUPABASE_URL — cannot mark trigger started")
        return
    try:
        _client.patch(
            f"{SUPABASE_URL}/rest/v1/system_health",
            headers={**sb_headers(), "Prefer": "return=minimal"},
            params={"run_id": f"eq.{run_id}", "check_name": "eq._trigger"},
            json={"status": "pass", "value": "picked up by ridley"},
        )
        print(f"[watcher] Marked trigger started for run_id={run_id}")
    except Exception as e:
        print(f"[watcher] Failed to mark trigger started for run_id={run_id}: {e}")


def spawn_simulator(run_id: str) -> None:
    """Launch test_system.py with the given run_id as a background process."""
    print(f"[watcher] Spawning simulator for run_id={run_id}")
    log_path = "/tmp/openclaw_simulator.log"
    env = {**os.environ, "SIMULATOR_RUN_ID": run_id}
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
            run_id = check_for_triggers()
            if run_id:
                mark_trigger_started(run_id)
                spawn_simulator(run_id)
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
