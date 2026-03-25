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
    """Write heartbeat to Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[heartbeat] No Supabase credentials, skipping {service}")
        return

    now = datetime.now(timezone.utc).isoformat()
    try:
        client = _client
        # Upsert via PATCH
        resp = client.patch(
            f"{SUPABASE_URL}/rest/v1/stack_heartbeats?service=eq.{service}",
            headers={**_sb_headers(), "Prefer": "return=representation"},
            json={"last_seen": now, "metadata": metadata},
        )
        if resp.status_code in (200, 204):
            status = "UP" if metadata.get("alive") else "DOWN"
            print(f"[heartbeat] {service}: {status}")
        else:
            print(f"[heartbeat] {service}: failed to write ({resp.status_code})")
    except Exception as e:
        print(f"[heartbeat] {service}: error — {e}")


def run():
    ollama_status = check_ollama()
    update_heartbeat("ollama", ollama_status)

    tumbler_status = check_tumbler()
    update_heartbeat("tumbler", tumbler_status)


if __name__ == "__main__":
    run()
