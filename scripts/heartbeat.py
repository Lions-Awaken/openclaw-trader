#!/usr/bin/env python3
"""
Heartbeat — reports service liveness to Supabase every 5 minutes.

Runs on ridley (Jetson). Checks Ollama and Tumbler engine availability,
writes timestamps to stack_heartbeats table so the dashboard can read them.

Cron: */5 * * * * /path/to/heartbeat.py
"""

import os
import sys
from datetime import datetime, timezone

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from common import slack_notify
from tracer import _sb_headers

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Reusable HTTP client
_client = httpx.Client(timeout=15.0)


def check_ollama() -> dict:
    """Check if Ollama is running and responsive."""
    try:
        client = _client
        resp = client.get(f"{OLLAMA_URL}/api/tags")
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            return {
                "alive": True,
                "models": [m.get("name", "") for m in models],
                "model_count": len(models),
            }
    except Exception:
        pass
    return {"alive": False}


def check_tumbler() -> dict:
    """Check if the tumbler engine dependencies are available."""
    result = {"alive": True, "checks": {}}

    # Check Ollama (needed for embeddings + qwen)
    try:
        client = _client
        resp = client.get(f"{OLLAMA_URL}/api/tags")
        result["checks"]["ollama"] = resp.status_code == 200
    except Exception:
        result["checks"]["ollama"] = False
        result["alive"] = False

    # Check Supabase (needed for RAG queries)
    try:
        client = _client
        resp = client.get(
            f"{SUPABASE_URL}/rest/v1/budget_config",
            headers=_sb_headers(),
            params={"select": "id", "limit": "1"},
        )
        result["checks"]["supabase"] = resp.status_code == 200
    except Exception:
        result["checks"]["supabase"] = False
        result["alive"] = False

    return result


def update_heartbeat(service: str, metadata: dict):
    """Write heartbeat to Supabase via upsert (POST + merge-duplicates)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[heartbeat] No Supabase credentials, skipping {service}")
        return

    now = datetime.now(timezone.utc).isoformat()
    try:
        resp = _client.post(
            f"{SUPABASE_URL}/rest/v1/stack_heartbeats",
            headers={
                **_sb_headers(),
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            json={"service": service, "last_seen": now, "metadata": metadata},
        )
        status = "UP" if metadata.get("alive") else "DOWN"
        print(f"[heartbeat] {service}: {status} ({resp.status_code})")
    except Exception as e:
        print(f"[heartbeat] {service}: error — {e}")


def run():
    ollama_status = check_ollama()
    update_heartbeat("ollama", ollama_status)

    tumbler_status = check_tumbler()
    update_heartbeat("tumbler", tumbler_status)

    # Alert only when a service is DOWN (not on every healthy check)
    down_services = []
    if not ollama_status.get("alive"):
        down_services.append("ollama")
    if not tumbler_status.get("alive"):
        failed_checks = [k for k, v in tumbler_status.get("checks", {}).items() if not v]
        down_services.append(f"tumbler ({', '.join(failed_checks)})" if failed_checks else "tumbler")
    if down_services:
        slack_notify(f"*Heartbeat ALERT* — service(s) DOWN: {', '.join(f'`{s}`' for s in down_services)}")


if __name__ == "__main__":
    run()
