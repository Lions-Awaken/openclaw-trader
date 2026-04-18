#!/usr/bin/env python3
"""
Ollama Watchdog — health check + auto-restart for the Jetson Orin Nano.

The Jetson's unified memory architecture fragments over time, causing
CUDA buffer allocation failures ("cudaMalloc failed: out of memory").
This watchdog runs before every critical Ollama-dependent cron window,
tests both generation and embedding, and restarts Ollama with a cache
drop if either fails.

Schedule (PDT, weekdays):
  5:25   — before catalyst_ingest (5:30)
  6:30   — before scanner (6:35)
  8:55   — before catalyst_ingest (9:00) + scanner (9:30)
  12:45  — before catalyst_ingest (12:50)

pipeline_name: ollama_watchdog
"""

import os
import subprocess
import sys
import time

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "C0ANK2A0M7G")

_client = httpx.Client(timeout=30.0)


def _slack_notify(msg: str) -> None:
    """Best-effort Slack notification."""
    if not SLACK_BOT_TOKEN:
        return
    try:
        _client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": SLACK_CHANNEL, "text": msg},
        )
    except Exception:
        pass


def _test_generate() -> tuple[bool, str]:
    """Test qwen2.5:3b generation. Returns (ok, detail)."""
    try:
        resp = _client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": "qwen2.5:3b",
                "prompt": "Say OK",
                "stream": False,
                "options": {"num_predict": 5, "temperature": 0.0},
                "keep_alive": "0",
            },
            timeout=60.0,
        )
        if resp.status_code == 200:
            text = resp.json().get("response", "")
            return True, f"generate OK: {text.strip()[:30]}"
        return False, f"generate HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, f"generate error: {e}"


def _test_embed() -> tuple[bool, str]:
    """Test nomic-embed-text embedding. Returns (ok, detail)."""
    try:
        resp = _client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": "test"},
            timeout=30.0,
        )
        if resp.status_code == 200:
            emb = resp.json().get("embedding", [])
            return True, f"embed OK: {len(emb)} dims"
        return False, f"embed HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, f"embed error: {e}"


def _test_alive() -> tuple[bool, str]:
    """Test Ollama is responding at all."""
    try:
        resp = _client.get(f"{OLLAMA_URL}/api/tags", timeout=10.0)
        if resp.status_code == 200:
            models = [m["name"] for m in resp.json().get("models", [])]
            return True, f"alive: {models}"
        return False, f"alive HTTP {resp.status_code}"
    except Exception as e:
        return False, f"alive error: {e}"


def _restart_ollama() -> tuple[bool, str]:
    """Compact memory, drop caches, restart Ollama. Requires sudo."""
    try:
        # Compact memory first (defragment without evicting)
        subprocess.run(
            ["sudo", "sh", "-c",
             "echo 1 > /proc/sys/vm/compact_memory"],
            check=False,  # may not exist on all kernels
            capture_output=True,
            timeout=10,
        )
        # Then drop caches as backup (handles "genuinely too full")
        subprocess.run(
            ["sudo", "sh", "-c",
             "sync; echo 3 > /proc/sys/vm/drop_caches"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        # Restart Ollama
        subprocess.run(
            ["sudo", "systemctl", "restart", "ollama"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        # Wait for Ollama to come up
        time.sleep(5)
        return True, "restart OK"
    except subprocess.CalledProcessError as e:
        return False, f"restart failed: {e.stderr.decode()[:200]}"
    except Exception as e:
        return False, f"restart error: {e}"


def run() -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[ollama_watchdog] {ts} — starting health check")

    # Step 1: Is Ollama alive?
    alive_ok, alive_detail = _test_alive()
    print(f"  alive: {alive_detail}")
    if not alive_ok:
        print("  Ollama not responding — restarting")
        rok, rdetail = _restart_ollama()
        print(f"  {rdetail}")
        if rok:
            _slack_notify(
                f"[ollama_watchdog] Ollama was down, "
                f"restarted successfully at {ts}"
            )
        else:
            _slack_notify(
                f"[ollama_watchdog] CRITICAL: Ollama restart "
                f"failed at {ts}: {rdetail}"
            )
        return

    # Step 2: Test generate (catches CUDA OOM)
    gen_ok, gen_detail = _test_generate()
    print(f"  generate: {gen_detail}")

    # Step 3: Test embed
    emb_ok, emb_detail = _test_embed()
    print(f"  embed: {emb_detail}")

    if gen_ok and emb_ok:
        print("  [OK] All checks passed")
        return

    # Something failed — restart
    failures = []
    if not gen_ok:
        failures.append(f"generate: {gen_detail}")
    if not emb_ok:
        failures.append(f"embed: {emb_detail}")

    fail_summary = "; ".join(failures)
    print(f"  FAILED: {fail_summary} — restarting Ollama")

    rok, rdetail = _restart_ollama()
    print(f"  {rdetail}")

    if not rok:
        _slack_notify(
            f"[ollama_watchdog] CRITICAL: Ollama unhealthy and restart failed at {ts}\n"
            f"Failures: {fail_summary}\nRestart: {rdetail}"
        )
        sys.exit(1)

    # Verify after restart
    time.sleep(3)
    gen2_ok, gen2_detail = _test_generate()
    emb2_ok, emb2_detail = _test_embed()
    print(f"  post-restart generate: {gen2_detail}")
    print(f"  post-restart embed: {emb2_detail}")

    if gen2_ok and emb2_ok:
        _slack_notify(
            f"[ollama_watchdog] Ollama was unhealthy, restarted OK at {ts}\n"
            f"Before: {fail_summary}\nAfter: all checks pass"
        )
        print("  [RECOVERED] Restart successful")
    else:
        still_broken = []
        if not gen2_ok:
            still_broken.append(gen2_detail)
        if not emb2_ok:
            still_broken.append(emb2_detail)
        _slack_notify(
            f"[ollama_watchdog] CRITICAL: Ollama still "
            f"unhealthy after restart at {ts}\n"
            f"Failing: {'; '.join(still_broken)}"
        )
        print("  [CRITICAL] Still failing after restart")
        sys.exit(1)


if __name__ == "__main__":
    run()
