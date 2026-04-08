#!/usr/bin/env python3
"""
health_check.py — Comprehensive system health check for OpenClaw Trader.

Runs 49 checks across 13 groups. Writes results to system_health table.
Posts Slack summary on failures (or always with --notify-always).

Usage:
    python scripts/health_check.py                  # full check, Slack on failures
    python scripts/health_check.py --notify-always  # full check, always post Slack
    python scripts/health_check.py --group signals  # single group only
    python scripts/health_check.py --dry-run        # print only, no DB write, no Slack

Set HEALTH_RUN_ID env var to use a specific run_id (for dashboard-triggered runs).
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import httpx

# ── path setup — must happen before project imports ──────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

import colorama  # noqa: E402
from colorama import Fore, Style  # noqa: E402

colorama.init(autoreset=True)

from common import (  # noqa: E402
    ALPACA_PAPER,
    ANTHROPIC_API_KEY,
    OLLAMA_URL,
    SLACK_BOT_TOKEN,
    SUPABASE_URL,
    _client,
    alpaca_headers,
    check_market_open,
    load_strategy_profile,
    sb_get,
    sb_headers,
    slack_notify,
)
from tracer import _post_to_supabase  # noqa: E402

# ── Check result container ────────────────────────────────────────────────────


class CheckResult(NamedTuple):
    check_order: int
    check_name: str
    check_group: str
    status: str          # pass / fail / warn / skip
    value: str
    expected: str
    error_message: str
    duration_ms: int


# ── Terminal output helpers ───────────────────────────────────────────────────

STATUS_COLORS = {
    "pass": Fore.GREEN,
    "fail": Fore.RED,
    "warn": Fore.YELLOW,
    "skip": Fore.WHITE + Style.DIM,
}

STATUS_ICONS = {
    "pass": "✅ PASS",
    "fail": "❌ FAIL",
    "warn": "⚠️  WARN",
    "skip": "── SKIP",
}


def _print_group_header(name: str) -> None:
    label = name.upper()
    print(f"\n  {Fore.CYAN}{label}{Style.RESET_ALL}")
    print(f"  {'─' * 50}")


def _print_check(result: CheckResult) -> None:
    icon = STATUS_ICONS.get(result.status, result.status)
    color = STATUS_COLORS.get(result.status, "")
    label = f"[{result.check_order}] {result.check_name}"
    dots_needed = max(0, 46 - len(label))
    dots = "." * dots_needed
    duration = f"({result.duration_ms}ms)" if result.duration_ms > 0 else ""
    detail = result.value if result.value else ""
    line = f"  {label} {dots} {color}{icon}{Style.RESET_ALL}  {detail}  {Style.DIM}{duration}{Style.RESET_ALL}"
    print(line)
    if result.status == "fail" and result.error_message:
        print(f"      {Fore.RED}{result.error_message}{Style.RESET_ALL}")


# ── DB write helper ───────────────────────────────────────────────────────────

def _write_result(result: CheckResult, run_id: str, run_type: str, dry_run: bool) -> None:
    if dry_run:
        return
    _post_to_supabase("system_health", {
        "run_id": run_id,
        "run_type": run_type,
        "check_group": result.check_group,
        "check_name": result.check_name,
        "check_order": result.check_order,
        "status": result.status,
        "value": result.value or None,
        "expected": result.expected or None,
        "error_message": result.error_message or None,
        "duration_ms": result.duration_ms,
    })


# ── Individual check helpers ──────────────────────────────────────────────────

def _run_check(
    order: int,
    name: str,
    group: str,
    fn,
) -> CheckResult:
    """Execute a single check function, catching all exceptions."""
    t0 = time.time()
    try:
        status, value, expected, error = fn()
    except Exception as exc:
        elapsed = int((time.time() - t0) * 1000)
        return CheckResult(order, name, group, "fail", "", "", str(exc), elapsed)
    elapsed = int((time.time() - t0) * 1000)
    return CheckResult(order, name, group, status, value, expected, error, elapsed)


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 1 — infrastructure (101–107)
# ═══════════════════════════════════════════════════════════════════════════════

def check_101_supabase_reachable():
    resp = _client.get(
        f"{SUPABASE_URL}/rest/v1/",
        headers=sb_headers(),
        timeout=10.0,
    )
    if resp.status_code == 200:
        return "pass", f"HTTP {resp.status_code}", "200", ""
    return "fail", f"HTTP {resp.status_code}", "200", f"Unexpected status {resp.status_code}"


def check_102_ollama_alive():
    resp = _client.get(f"{OLLAMA_URL}/api/tags", timeout=10.0)
    if resp.status_code != 200:
        return "fail", f"HTTP {resp.status_code}", "200", "Ollama not responding"
    models = resp.json().get("models", [])
    if not models:
        return "fail", "0 models", ">0 models", "Ollama has no models loaded"
    names = ", ".join(m.get("name", "?") for m in models[:3])
    return "pass", f"models: {names}", ">0 models", ""


def check_103_ollama_model_loaded():
    resp = _client.get(f"{OLLAMA_URL}/api/tags", timeout=10.0)
    if resp.status_code != 200:
        return "fail", "no response", "qwen model present", "Ollama not responding"
    models = resp.json().get("models", [])
    names = [m.get("name", "") for m in models]
    qwen_models = [n for n in names if "qwen" in n.lower()]
    if not qwen_models:
        return "fail", f"models: {names[:5]}", "qwen in model list", "qwen model not found in Ollama"
    return "pass", qwen_models[0], "qwen model present", ""


def check_104_alpaca_api():
    resp = _client.get(
        f"{ALPACA_PAPER}/v2/clock",
        headers=alpaca_headers(),
        timeout=10.0,
    )
    if resp.status_code == 200:
        is_open = resp.json().get("is_open", False)
        return "pass", f"is_open={is_open}", "200 OK", ""
    return "fail", f"HTTP {resp.status_code}", "200 OK", f"Alpaca clock returned {resp.status_code}"


def check_105_env_vars():
    required = {
        "SUPABASE_URL": os.environ.get("SUPABASE_URL", ""),
        "SUPABASE_SERVICE_KEY": os.environ.get("SUPABASE_SERVICE_KEY", ""),
        "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
        "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
        "CLAUDE_API_KEY": ANTHROPIC_API_KEY,  # mapped name in task spec
        "SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        return "fail", f"missing: {', '.join(missing)}", "6 vars set", f"Missing env vars: {', '.join(missing)}"
    return "pass", "6/6 set", "6 vars set", ""


def check_106_disk_space():
    usage = shutil.disk_usage("/home")
    free_gb = usage.free / (1024 ** 3)
    if free_gb < 2.0:
        return "fail", f"{free_gb:.1f}GB free", ">2GB free", f"Only {free_gb:.1f}GB free on /home"
    return "pass", f"{free_gb:.1f}GB free", ">2GB free", ""


def check_107_memory():
    try:
        import psutil
        mem = psutil.virtual_memory()
        avail_gb = mem.available / (1024 ** 3)
        if avail_gb < 1.0:
            return "warn", f"{avail_gb:.2f}GB avail", ">1GB avail", f"Low RAM: only {avail_gb:.2f}GB available"
        return "pass", f"{avail_gb:.2f}GB avail", ">1GB avail", ""
    except ImportError:
        pass
    # Fallback: /proc/meminfo
    with open("/proc/meminfo") as f:
        lines = f.readlines()
    info = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            info[parts[0].rstrip(":")] = int(parts[1])
    avail_kb = info.get("MemAvailable", 0)
    avail_gb = avail_kb / (1024 ** 2)
    if avail_gb < 1.0:
        return "warn", f"{avail_gb:.2f}GB avail", ">1GB avail", f"Low RAM: only {avail_gb:.2f}GB available"
    return "pass", f"{avail_gb:.2f}GB avail", ">1GB avail", ""


INFRASTRUCTURE_CHECKS = [
    (101, "Supabase reachable",   check_101_supabase_reachable),
    (102, "Ollama alive",         check_102_ollama_alive),
    (103, "Ollama model loaded",  check_103_ollama_model_loaded),
    (104, "Alpaca API",           check_104_alpaca_api),
    (105, "Env vars present",     check_105_env_vars),
    (106, "Disk space",           check_106_disk_space),
    (107, "Memory",               check_107_memory),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 2 — database (201–207)
# ═══════════════════════════════════════════════════════════════════════════════

EXPECTED_TABLES = [
    "strategy_profiles", "inference_chains", "pipeline_runs", "cost_ledger",
    "signal_evaluations", "shadow_divergences", "system_health", "system_stats",
    "meta_reflections", "catalyst_events", "trade_decisions", "order_events",
    "budget_config", "regime_log", "trade_learnings", "stack_heartbeats",
    "pattern_templates", "confidence_calibration", "predictions",
    "congress_clusters", "research_memories", "tuning_telemetry", "tuning_profiles",
    "data_quality_checks", "politician_intel", "legislative_calendar",
    "llm_inferences", "magic_link_tokens", "options_flow_signals", "form4_signals",
]


def check_201_tables_exist():
    # Query information_schema via REST introspection endpoint
    # Supabase exposes table names via its OpenAPI spec; use REST HEAD requests
    missing = []
    for table in EXPECTED_TABLES:
        try:
            resp = _client.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers={**sb_headers(), "Prefer": "count=exact"},
                params={"limit": "0"},
                timeout=10.0,
            )
            # 200 = table exists, 404 = not found, 401 = auth issue
            if resp.status_code not in (200, 206):
                missing.append(table)
        except Exception:
            missing.append(table)
    if missing:
        return "fail", f"missing: {', '.join(missing[:5])}", f"{len(EXPECTED_TABLES)} tables", f"Missing tables: {', '.join(missing)}"
    return "pass", f"{len(EXPECTED_TABLES)}/{len(EXPECTED_TABLES)} tables", f"{len(EXPECTED_TABLES)} tables", ""


def check_202_active_profile_congress_mirror():
    rows = sb_get("strategy_profiles", {"select": "profile_name,active", "active": "eq.true", "limit": "1"})
    if not rows:
        return "fail", "no active profile", "CONGRESS_MIRROR", "No active strategy profile found"
    name = rows[0].get("profile_name", "")
    if name != "CONGRESS_MIRROR":
        return "warn", f"active={name}", "CONGRESS_MIRROR", f"Active profile is {name!r}, expected CONGRESS_MIRROR"
    return "pass", name, "CONGRESS_MIRROR", ""


def check_203_shadow_profiles_seeded():
    rows = sb_get("strategy_profiles", {"select": "profile_name", "is_shadow": "eq.true"})
    count = len(rows)
    if count < 3:
        return "fail", f"{count} shadow profiles", ">=3", f"Only {count} shadow profiles seeded"
    names = ", ".join(r.get("profile_name", "?") for r in rows)
    return "pass", f"{count} profiles: {names}", ">=3", ""


def check_204_shadow_dwm_weights_valid():
    rows = sb_get("strategy_profiles", {"select": "profile_name,dwm_weight", "is_shadow": "eq.true"})
    invalid = [r["profile_name"] for r in rows if float(r.get("dwm_weight", 0)) < 0.05]
    if invalid:
        return "fail", f"invalid weights: {invalid}", "all dwm_weight>=0.05", f"Profiles with dwm_weight<0.05: {invalid}"
    return "pass", f"all {len(rows)} weights valid", "all dwm_weight>=0.05", ""


def check_205_inference_chains_backfilled():
    rows = sb_get("inference_chains", {
        "select": "id",
        "profile_name": "eq.CONGRESS_MIRROR",
        "limit": "200",
    })
    count = len(rows)
    if count < 100:
        return "warn", f"{count} rows", ">100 rows", f"Only {count} inference_chains with profile_name=CONGRESS_MIRROR"
    return "pass", f"{count} rows", ">100 rows", ""


def check_206_budget_config_present():
    rows = sb_get("budget_config", {
        "select": "config_key,value",
        "config_key": "eq.daily_claude_budget",
    })
    if not rows:
        return "fail", "key missing", "daily_claude_budget present", "daily_claude_budget not found in budget_config"
    val = float(rows[0].get("value", 0))
    if val <= 0:
        return "fail", f"value={val}", "value>0", f"daily_claude_budget value is {val}"
    return "pass", f"${val:.2f}/day", "daily_claude_budget present", ""


def check_207_recent_pipeline_runs():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    rows = sb_get("pipeline_runs", {
        "select": "id,pipeline_name,started_at",
        "started_at": f"gte.{cutoff}",
        "step_name": "eq.root",
        "order": "started_at.desc",
        "limit": "5",
    })
    if not rows:
        return "warn", "0 runs in 48h", ">=1 pipeline run in 48h", "No pipeline runs in the last 48 hours"
    latest = rows[0].get("pipeline_name", "?")
    return "pass", f"{len(rows)} runs, latest: {latest}", ">=1 pipeline run in 48h", ""


DATABASE_CHECKS = [
    (201, "All tables exist",                  check_201_tables_exist),
    (202, "Active profile is CONGRESS_MIRROR", check_202_active_profile_congress_mirror),
    (203, "3+ shadow profiles seeded",         check_203_shadow_profiles_seeded),
    (204, "Shadow DWM weights valid",          check_204_shadow_dwm_weights_valid),
    (205, "inference_chains backfilled",       check_205_inference_chains_backfilled),
    (206, "Budget config present",             check_206_budget_config_present),
    (207, "Recent pipeline runs",              check_207_recent_pipeline_runs),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 3 — crons (301–304)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_crontab() -> str:
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        return result.stdout
    except Exception:
        return ""


def check_301_crontab_entries():
    cron = _get_crontab()
    required = ["scanner", "calibrator", "meta_analysis"]
    missing = [k for k in required if k not in cron]
    if not cron:
        return "warn", "crontab empty or inaccessible", "scanner+calibrator+meta_analysis", "Could not read crontab"
    if missing:
        return "fail", f"missing: {', '.join(missing)}", "scanner+calibrator+meta_analysis", f"Crontab missing entries: {missing}"
    return "pass", "scanner+calibrator+meta_analysis found", "scanner+calibrator+meta_analysis", ""


def check_302_script_files_exist():
    script_dir = os.path.dirname(__file__)
    required_scripts = [
        "scanner.py", "inference_engine.py", "calibrator.py", "meta_analysis.py",
        "common.py", "tracer.py", "shadow_profiles.py", "health_check.py",
    ]
    missing = [s for s in required_scripts if not os.path.isfile(os.path.join(script_dir, s))]
    if missing:
        return "fail", f"missing: {', '.join(missing)}", "8 scripts present", f"Missing scripts: {missing}"
    return "pass", f"all {len(required_scripts)} scripts present", "8 scripts present", ""


def check_303_slack_watcher_cron():
    cron = _get_crontab()
    if not cron:
        return "warn", "crontab inaccessible", "slack_watcher in crontab", "Could not read crontab"
    if "slack_watcher" not in cron:
        return "warn", "not found", "slack_watcher in crontab", "slack_watcher not in crontab"
    return "pass", "found in crontab", "slack_watcher in crontab", ""


def check_304_slack_notify_sh():
    hook_path = os.path.expanduser("~/.claude/hooks/slack_notify.sh")
    if not os.path.isfile(hook_path):
        return "fail", "file missing", "~/.claude/hooks/slack_notify.sh exists+executable", f"Not found: {hook_path}"
    if not os.access(hook_path, os.X_OK):
        return "fail", "not executable", "~/.claude/hooks/slack_notify.sh exists+executable", f"Not executable: {hook_path}"
    return "pass", "exists and executable", "~/.claude/hooks/slack_notify.sh exists+executable", ""


CRON_CHECKS = [
    (301, "Crontab entries present", check_301_crontab_entries),
    (302, "Script files exist",      check_302_script_files_exist),
    (303, "Slack watcher in cron",   check_303_slack_watcher_cron),
    (304, "slack_notify.sh exists",  check_304_slack_notify_sh),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 4 — signals (401–405)
# ═══════════════════════════════════════════════════════════════════════════════

def check_401_load_strategy_profile():
    profile = load_strategy_profile()
    if not isinstance(profile, dict):
        return "fail", str(type(profile)), "dict with profile_name", "load_strategy_profile() did not return dict"
    name = profile.get("profile_name", "")
    if not name:
        return "warn", "no profile_name", "dict with profile_name", "profile_name key is empty"
    return "pass", name, "dict with profile_name", ""


def check_402_check_market_open():
    result = check_market_open()
    if not isinstance(result, tuple) or len(result) != 2:
        return "fail", str(result), "(bool, str) tuple", "check_market_open() did not return a 2-tuple"
    is_open, reason = result
    return "pass", f"is_open={is_open}, reason={reason}", "(bool, str) tuple", ""


def check_403_compute_signals_importable():
    try:
        from scanner import compute_signals
        if callable(compute_signals):
            return "pass", "compute_signals callable", "callable", ""
        return "fail", "not callable", "callable", "compute_signals is not callable"
    except ImportError as e:
        return "fail", "ImportError", "callable", str(e)


def check_404_catalyst_events_fresh():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    rows = sb_get("catalyst_events", {
        "select": "id",
        "created_at": f"gte.{cutoff}",
        "limit": "10",
    })
    if not rows:
        return "warn", "0 events in 48h", ">=1 catalyst_event in 48h", "No catalyst events in last 48h"
    return "pass", f"{len(rows)} events in 48h", ">=1 catalyst_event in 48h", ""


def check_405_politician_intel_seeded():
    rows = sb_get("politician_intel", {"select": "id", "limit": "20"})
    count = len(rows)
    if count < 10:
        return "fail", f"{count} rows", ">=10 rows", f"Only {count} rows in politician_intel"
    return "pass", f"{count}+ rows", ">=10 rows", ""


SIGNAL_CHECKS = [
    (401, "load_strategy_profile() valid",   check_401_load_strategy_profile),
    (402, "check_market_open() responds",    check_402_check_market_open),
    (403, "compute_signals importable",      check_403_compute_signals_importable),
    (404, "Catalyst events fresh",           check_404_catalyst_events_fresh),
    (405, "Politician intel seeded",         check_405_politician_intel_seeded),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 5 — tumblers (501–506)
# ═══════════════════════════════════════════════════════════════════════════════

def check_501_t1_gate_logic():
    # Skip — requires live inference call with real price data
    return "skip", "requires live inference call", "n/a", ""


def check_502_ollama_test_prompt():
    resp = _client.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": "qwen2.5:3b",
            "prompt": "Reply with exactly one word: HEALTHY",
            "stream": False,
            "options": {"num_predict": 10, "temperature": 0.0},
            "keep_alive": "0",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        return "fail", f"HTTP {resp.status_code}", "HEALTHY in response", f"Ollama generate returned {resp.status_code}"
    text = resp.json().get("response", "")
    if "HEALTHY" in text.upper():
        return "pass", f'response: "{text.strip()[:40]}"', "HEALTHY in response", ""
    return "warn", f'response: "{text.strip()[:40]}"', "HEALTHY in response", "Response does not contain HEALTHY"


def check_503_run_inference_importable():
    try:
        from inference_engine import run_inference
        if callable(run_inference):
            return "pass", "run_inference callable", "callable", ""
        return "fail", "not callable", "callable", "run_inference is not callable"
    except ImportError as e:
        return "fail", "ImportError", "callable", str(e)


def check_504_check_stopping_rule_importable():
    try:
        from inference_engine import check_stopping_rule
        if callable(check_stopping_rule):
            return "pass", "check_stopping_rule callable", "callable", ""
        return "fail", "not callable", "callable", "check_stopping_rule is not callable"
    except ImportError as e:
        return "fail", "ImportError", "callable", str(e)


def check_505_regime_watcher_depth_cap():
    from shadow_profiles import get_max_tumbler_depth
    depth = get_max_tumbler_depth("REGIME_WATCHER")
    if depth != 3:
        return "fail", f"depth={depth}", "3", f"REGIME_WATCHER max depth is {depth}, expected 3"
    return "pass", "depth=3", "3", ""


def check_506_shadow_context_returns_strings():
    from shadow_profiles import get_shadow_context
    shadow_types = ["SKEPTIC", "CONTRARIAN", "REGIME_WATCHER", "OPTIONS_FLOW", "FORM4_INSIDER"]
    empty = []
    short = []
    for stype in shadow_types:
        ctx = get_shadow_context(stype)
        if not ctx:
            empty.append(stype)
        elif len(ctx) < 50:
            short.append(stype)
    if empty:
        return "fail", f"empty: {empty}", "5 non-empty contexts", f"Shadow types with empty context: {empty}"
    if short:
        return "warn", f"short (<50 chars): {short}", "5 non-empty contexts", f"Suspiciously short context for: {short}"
    return "pass", "all 5 contexts non-empty", "5 non-empty contexts", ""


TUMBLER_CHECKS = [
    (501, "T1 gate logic",                  check_501_t1_gate_logic),
    (502, "Ollama test prompt",             check_502_ollama_test_prompt),
    (503, "run_inference importable",       check_503_run_inference_importable),
    (504, "check_stopping_rule importable", check_504_check_stopping_rule_importable),
    (505, "REGIME_WATCHER depth cap=3",    check_505_regime_watcher_depth_cap),
    (506, "Shadow context returns strings", check_506_shadow_context_returns_strings),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 6 — ensemble (601–606)
# ═══════════════════════════════════════════════════════════════════════════════

def check_601_shadow_context_lengths():
    from shadow_profiles import get_shadow_context
    shadow_types = ["SKEPTIC", "CONTRARIAN", "REGIME_WATCHER", "OPTIONS_FLOW", "FORM4_INSIDER"]
    short = []
    for stype in shadow_types:
        ctx = get_shadow_context(stype)
        if len(ctx) < 50:
            short.append(f"{stype}({len(ctx)})")
    if short:
        return "fail", f"short: {short}", "all len>50", f"Context too short for: {short}"
    return "pass", "all 5 contexts len>50", "all len>50", ""


def check_602_max_tumbler_depth_values():
    from shadow_profiles import get_max_tumbler_depth
    expected = {"REGIME_WATCHER": 3, "SKEPTIC": 5, "CONTRARIAN": 5, "OPTIONS_FLOW": 5, "FORM4_INSIDER": 5}
    wrong = []
    for stype, exp in expected.items():
        actual = get_max_tumbler_depth(stype)
        if actual != exp:
            wrong.append(f"{stype}={actual}(exp {exp})")
    if wrong:
        return "fail", f"wrong: {wrong}", "REGIME_WATCHER=3, others=5", f"Depth mismatch: {wrong}"
    return "pass", "REGIME_WATCHER=3, others=5", "REGIME_WATCHER=3, others=5", ""


def check_603_load_shadow_profiles():
    try:
        from scanner import _load_shadow_profiles
        profiles = _load_shadow_profiles()
        if not isinstance(profiles, list):
            return "fail", str(type(profiles)), "list of >=3 dicts", "_load_shadow_profiles() did not return list"
        if len(profiles) < 3:
            return "fail", f"{len(profiles)} profiles", "list of >=3 dicts", f"Only {len(profiles)} shadow profiles loaded"
        return "pass", f"{len(profiles)} shadow profiles", "list of >=3 dicts", ""
    except ImportError as e:
        return "fail", "ImportError", "list of >=3 dicts", str(e)


def check_604_record_divergence_callable():
    try:
        from scanner import _record_divergence
        if callable(_record_divergence):
            return "pass", "_record_divergence callable", "callable", ""
        return "fail", "not callable", "callable", "_record_divergence is not callable"
    except ImportError as e:
        return "fail", "ImportError", "callable", str(e)


def check_605_shadow_divergence_summary_structure():
    try:
        from meta_analysis import get_shadow_divergence_summary
        result = get_shadow_divergence_summary()
        if not isinstance(result, dict):
            return "fail", str(type(result)), "dict with count/divergences/unanimous_dissent", "get_shadow_divergence_summary() did not return dict"
        expected_keys = {"count", "divergences", "unanimous_dissent"}
        missing_keys = expected_keys - set(result.keys())
        if missing_keys:
            return "fail", f"missing keys: {missing_keys}", "count/divergences/unanimous_dissent", f"Missing dict keys: {missing_keys}"
        return "pass", f"count={result['count']}", "count/divergences/unanimous_dissent", ""
    except ImportError as e:
        return "fail", "ImportError", "dict with expected keys", str(e)


def check_606_grade_shadow_profiles_callable():
    try:
        from calibrator import grade_shadow_profiles
        if callable(grade_shadow_profiles):
            return "pass", "grade_shadow_profiles callable", "callable", ""
        return "fail", "not callable", "callable", "grade_shadow_profiles is not callable"
    except ImportError as e:
        return "fail", "ImportError", "callable", str(e)


ENSEMBLE_CHECKS = [
    (601, "Shadow context lengths valid",          check_601_shadow_context_lengths),
    (602, "Max tumbler depth values correct",      check_602_max_tumbler_depth_values),
    (603, "_load_shadow_profiles returns >=3",     check_603_load_shadow_profiles),
    (604, "_record_divergence callable",           check_604_record_divergence_callable),
    (605, "Shadow divergence summary structure",   check_605_shadow_divergence_summary_structure),
    (606, "grade_shadow_profiles callable",        check_606_grade_shadow_profiles_callable),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 7 — logging (701–705)
# ═══════════════════════════════════════════════════════════════════════════════

def check_701_tracer_writes():
    import tracer as _tracer_mod
    if callable(_tracer_mod.PipelineTracer):
        return "pass", "PipelineTracer class exists and is callable", "callable", ""
    return "fail", "not callable", "callable", "PipelineTracer is not callable"


def check_702_log_cost_importable():
    try:
        from inference_engine import log_cost
        if callable(log_cost):
            return "pass", "log_cost callable", "callable", ""
        return "fail", "not callable", "callable", "log_cost is not callable"
    except ImportError as e:
        return "fail", "ImportError", "callable", str(e)


def check_703_todays_claude_spend():
    import inference_engine as ie
    from inference_engine import get_todays_claude_spend
    original_today = ie.TODAY
    ie.TODAY = datetime.now(timezone.utc).date().isoformat()
    try:
        spend = get_todays_claude_spend()
    finally:
        ie.TODAY = original_today
    if not isinstance(spend, (int, float)):
        return "fail", str(type(spend)), "numeric >=0", "get_todays_claude_spend() did not return a number"
    if spend < 0:
        return "fail", f"${spend:.4f}", ">=0", f"Negative claude spend: {spend}"
    return "pass", f"${float(spend):.4f} today", "numeric >=0", ""


def check_704_claude_budget():
    from inference_engine import get_claude_budget
    budget = get_claude_budget()
    if not isinstance(budget, (int, float)):
        return "fail", str(type(budget)), "numeric >0", "get_claude_budget() did not return a number"
    if budget <= 0:
        return "fail", f"${budget:.2f}", ">0", f"Budget is {budget}"
    import inference_engine as ie
    original_today = ie.TODAY
    ie.TODAY = datetime.now(timezone.utc).date().isoformat()
    try:
        spend = ie.get_todays_claude_spend()
    finally:
        ie.TODAY = original_today
    remaining = max(0.0, budget - spend)
    pct = int(remaining / budget * 100) if budget > 0 else 0
    return "pass", f"${budget:.2f}/day, {pct}% remaining", "float >0", ""


def check_705_cost_ledger_has_rows():
    rows = sb_get("cost_ledger", {"select": "id", "limit": "1"})
    if not rows:
        return "warn", "0 rows", ">0 rows", "cost_ledger is empty — no costs recorded yet"
    # Get total count via limit param trick
    all_rows = sb_get("cost_ledger", {"select": "id", "limit": "1000"})
    return "pass", f"{len(all_rows)}+ rows", ">0 rows", ""


LOGGING_CHECKS = [
    (701, "PipelineTracer importable",     check_701_tracer_writes),
    (702, "log_cost importable",           check_702_log_cost_importable),
    (703, "get_todays_claude_spend",       check_703_todays_claude_spend),
    (704, "get_claude_budget",             check_704_claude_budget),
    (705, "cost_ledger has rows",          check_705_cost_ledger_has_rows),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 8 — dashboard (801–804)
# ═══════════════════════════════════════════════════════════════════════════════

def _is_dashboard_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "server.py"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
        result2 = subprocess.run(
            ["pgrep", "-f", "uvicorn"],
            capture_output=True, text=True, timeout=5,
        )
        return result2.returncode == 0 and bool(result2.stdout.strip())
    except Exception:
        return False


def check_801_dashboard_process():
    if _is_dashboard_running():
        return "pass", "server.py or uvicorn running", "dashboard process found", ""
    return "warn", "no process found", "dashboard process found", "Dashboard process not detected (pgrep -f server.py / uvicorn)"


def _check_dashboard_endpoint(path: str, label: str):
    if not _is_dashboard_running():
        return "skip", "dashboard not running", f"{path} returns data", ""
    try:
        resp = _client.get(f"http://localhost:8000{path}", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return "pass", "HTTP 200, data present", f"{path} returns data", ""
            return "warn", "HTTP 200, empty body", f"{path} returns data", f"{label} returned empty response"
        return "fail", f"HTTP {resp.status_code}", f"{path} returns data", f"{label} returned {resp.status_code}"
    except Exception as e:
        return "fail", "connection error", f"{path} returns data", str(e)


def check_802_shadow_profiles_endpoint():
    return _check_dashboard_endpoint("/api/shadow/profiles", "GET /api/shadow/profiles")


def check_803_shadow_divergences_endpoint():
    return _check_dashboard_endpoint("/api/shadow/divergences", "GET /api/shadow/divergences")


def check_804_shadow_unanimous_endpoint():
    return _check_dashboard_endpoint("/api/shadow/unanimous", "GET /api/shadow/unanimous")


DASHBOARD_CHECKS = [
    (801, "Dashboard process running",           check_801_dashboard_process),
    (802, "/api/shadow/profiles returns data",   check_802_shadow_profiles_endpoint),
    (803, "/api/shadow/divergences returns data", check_803_shadow_divergences_endpoint),
    (804, "/api/shadow/unanimous returns data",  check_804_shadow_unanimous_endpoint),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 9 — claude_api (901–903)
# ═══════════════════════════════════════════════════════════════════════════════

def check_901_claude_api_canary(dry_run: bool = False):
    if dry_run:
        return "skip", "dry-run mode", "HTTP 200 + HEALTHY", ""
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return "skip", "ANTHROPIC_API_KEY not set", "HTTP 200 + HEALTHY", ""
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "Reply with only the word HEALTHY"}],
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return "fail", f"HTTP {resp.status_code}", "200 + HEALTHY", f"Claude API returned {resp.status_code}: {resp.text[:120]}"
    body = resp.json()
    text = ""
    for block in body.get("content", []):
        text += block.get("text", "")
    if "HEALTHY" not in text.upper():
        return "warn", f"response: {text.strip()[:60]!r}", "HEALTHY in response", "Claude did not reply with HEALTHY"
    return "pass", f"response: {text.strip()[:40]!r}", "HTTP 200 + HEALTHY", ""


def check_902_budget_preflight():
    from inference_engine import get_claude_budget, get_todays_claude_spend
    from manifest import estimate_daily_claude_budget
    budget = get_claude_budget()
    spent = get_todays_claude_spend()
    remaining = budget - spent
    needed = estimate_daily_claude_budget()
    detail = f"budget=${budget:.2f}, spent=${spent:.4f}, remaining=${remaining:.4f}, needed=${needed:.4f}"
    if remaining < needed:
        return "fail", detail, "remaining>=needed", f"Insufficient Claude budget: remaining ${remaining:.4f} < needed ${needed:.4f}"
    if remaining < 2 * needed:
        return "warn", detail, "remaining>=2*needed (comfortable)", f"Claude budget tight: ${remaining:.4f} remaining vs ${needed:.4f} needed/day"
    return "pass", detail, "remaining>=needed", ""


def check_903_claude_api_key_valid():
    key = ANTHROPIC_API_KEY
    if len(key) > 20:
        return "pass", f"key length={len(key)}", "length>20", ""
    if not key:
        return "fail", "not set", "length>20", "ANTHROPIC_API_KEY is not set"
    return "fail", f"key length={len(key)}", "length>20", f"ANTHROPIC_API_KEY suspiciously short ({len(key)} chars)"


# check_901 takes a dry_run param — wrap it for the registry tuple pattern
def _check_901_wrapper():
    # dry_run not available at call time from registry; read from argv as best-effort
    dry_run = "--dry-run" in sys.argv
    return check_901_claude_api_canary(dry_run=dry_run)


CLAUDE_API_CHECKS = [
    (901, "Claude API canary",        _check_901_wrapper),
    (902, "Budget pre-flight",        check_902_budget_preflight),
    (903, "Claude API key valid",     check_903_claude_api_key_valid),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 10 — crontab_drift (1001–1002)
# ═══════════════════════════════════════════════════════════════════════════════

def check_1001_crontab_vs_manifest():
    from manifest import MANIFEST
    cron = _get_crontab()
    if not cron:
        return "warn", "crontab empty or inaccessible", "all scheduled scripts in crontab", "Could not read crontab"
    # Filter entries that have real cron schedules (not manual/on_trade_close)
    scheduled = [
        e for e in MANIFEST
        if e.schedule not in ("manual", "on_trade_close")
    ]
    # Extract the bare script filename for matching
    missing = []
    for entry in scheduled:
        script_name = os.path.basename(entry.script)
        if script_name not in cron:
            missing.append(script_name)
    # Deduplicate (some scripts appear multiple times in MANIFEST)
    missing = sorted(set(missing))
    if missing:
        return "warn", f"missing from crontab: {', '.join(missing)}", "all scheduled scripts in crontab", f"Scripts not found in crontab: {missing}"
    return "pass", f"all {len(scheduled)} schedule entries matched", "all scheduled scripts in crontab", ""


def check_1002_script_files_on_disk():
    from manifest import ALL_ENTRIES
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    missing = []
    for entry in ALL_ENTRIES:
        full_path = os.path.join(project_root, entry.script)
        if not os.path.exists(full_path):
            missing.append(entry.script)
    if missing:
        return "warn", f"missing: {', '.join(missing[:5])}", "all manifest scripts on disk", f"Scripts not found on disk: {missing}"
    return "pass", f"all {len(ALL_ENTRIES)} manifest scripts present on disk", "all manifest scripts on disk", ""


CRONTAB_DRIFT_CHECKS = [
    (1001, "Crontab vs manifest",       check_1001_crontab_vs_manifest),
    (1002, "Script files on disk",      check_1002_script_files_on_disk),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 11 — output_quality (1101–1103)
# ═══════════════════════════════════════════════════════════════════════════════

def check_1101_yesterday_output_validation():
    from manifest import MANIFEST, validate_output
    failures = []
    checked = 0
    for entry in MANIFEST:
        if entry.output_validator is None:
            continue
        if not entry.writes_to_pipeline_runs:
            continue
        rows = sb_get("pipeline_runs", {
            "select": "output_snapshot",
            "pipeline_name": f"eq.{entry.pipeline_name}",
            "step_name": "eq.root",
            "order": "created_at.desc",
            "limit": "1",
        })
        if not rows:
            continue
        checked += 1
        snap = rows[0].get("output_snapshot") or {}
        if not validate_output(entry, snap):
            failures.append(entry.pipeline_name)
    if failures:
        return "warn", f"failed validation: {', '.join(failures)}", "all validators pass", f"Output validators failed for: {failures}"
    if checked == 0:
        return "skip", "no recent runs with output_snapshot", "validators pass", ""
    return "pass", f"{checked} validators passed", "all validators pass", ""


def check_1102_meta_reflection_quality():
    rows = sb_get("meta_reflections", {
        "select": "signal_assessment",
        "order": "created_at.desc",
        "limit": "1",
    })
    if not rows:
        return "warn", "no rows found", "signal_assessment non-empty and len>50", "meta_reflections table is empty"
    assessment = rows[0].get("signal_assessment", "") or ""
    if not assessment:
        return "warn", "signal_assessment is null/empty", "non-empty and len>50", "signal_assessment is missing"
    if "Unable to assess" in assessment:
        return "warn", f"value: {assessment[:80]!r}", "no 'Unable to assess'", "signal_assessment contains error-like text"
    if len(assessment) <= 50:
        return "warn", f"len={len(assessment)}", "len>50", f"signal_assessment is suspiciously short: {assessment[:60]!r}"
    return "pass", f"len={len(assessment)}, starts: {assessment[:60]!r}", "non-empty and len>50", ""


def check_1103_catalyst_source_diversity():
    rows = sb_get("pipeline_runs", {
        "select": "output_snapshot",
        "pipeline_name": "eq.catalyst_ingest",
        "step_name": "eq.root",
        "order": "created_at.desc",
        "limit": "1",
    })
    if not rows:
        return "skip", "no catalyst_ingest root run found", ">=3 sources active", ""
    snap = rows[0].get("output_snapshot") or {}
    # Look for per-source counts: keys like fetch_finnhub, fetch_sec_edgar, etc.
    source_keys = [k for k in snap if k.startswith("fetch_") or k in ("finnhub", "sec_edgar", "quiverquant", "yfinance", "fred")]
    # Also try total_inserted breakdown if it's a nested dict
    active_sources = 0
    if source_keys:
        active_sources = sum(1 for k in source_keys if int(snap.get(k) or 0) > 0)
    else:
        # Fallback: check total_inserted as proxy
        total = int(snap.get("total_inserted", 0))
        if total > 5:
            return "pass", f"total_inserted={total} (source detail not in snapshot)", ">=3 sources active", ""
        return "warn", f"total_inserted={total}, no per-source breakdown in snapshot", ">=3 sources active", "Cannot verify source diversity — no per-source counts in output_snapshot"
    if active_sources < 3:
        return "warn", f"{active_sources} sources with >0 events", ">=3 sources active", f"Only {active_sources} catalyst sources produced events"
    return "pass", f"{active_sources} sources active", ">=3 sources active", ""


OUTPUT_QUALITY_CHECKS = [
    (1101, "Yesterday's output validation",   check_1101_yesterday_output_validation),
    (1102, "Meta reflection quality",         check_1102_meta_reflection_quality),
    (1103, "Catalyst source diversity",       check_1103_catalyst_source_diversity),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 12 — data_freshness (1201–1204)
# ═══════════════════════════════════════════════════════════════════════════════

def _is_weekday() -> bool:
    return datetime.now(timezone.utc).weekday() < 5  # 0=Mon … 4=Fri


def check_1201_catalyst_events_fresh():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    rows = sb_get("catalyst_events", {
        "select": "id",
        "created_at": f"gte.{cutoff}",
        "limit": "200",
    })
    count = len(rows)
    if count == 0:
        return "fail", "0 events in 48h", ">=1 catalyst_event in 48h", "No catalyst events in the last 48 hours"
    return "pass", f"{count} events in 48h", ">=1 catalyst_event in 48h", ""


def check_1202_inference_chains_fresh():
    if not _is_weekday():
        return "skip", "weekend — no scanner runs expected", "weekday only", ""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    rows = sb_get("inference_chains", {
        "select": "id",
        "created_at": f"gte.{cutoff}",
        "limit": "10",
    })
    count = len(rows)
    if count == 0:
        return "fail", "0 inference_chains in 48h", ">=1 in 48h (weekday)", "No inference chains in last 48h"
    return "pass", f"{count} inference_chains in 48h", ">=1 in 48h (weekday)", ""


def check_1203_pipeline_runs_fresh_manifest():
    from manifest import MANIFEST
    stale = []
    checked = 0
    for entry in MANIFEST:
        if entry.criticality != "high":
            continue
        if not entry.writes_to_pipeline_runs:
            continue
        if entry.freshness_hours is None:
            continue
        checked += 1
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=entry.freshness_hours)).isoformat()
        rows = sb_get("pipeline_runs", {
            "select": "id",
            "pipeline_name": f"eq.{entry.pipeline_name}",
            "step_name": "eq.root",
            "created_at": f"gte.{cutoff}",
            "limit": "1",
        })
        if not rows:
            stale.append(f"{entry.name}(>{entry.freshness_hours}h)")
    if stale:
        return "warn", f"stale: {', '.join(stale)}", "all high-criticality entries fresh", f"Stale manifest entries: {stale}"
    if checked == 0:
        return "skip", "no high-criticality entries with freshness_hours", "entries checked", ""
    return "pass", f"{checked} high-criticality entries all fresh", "all high-criticality entries fresh", ""


def check_1204_shadow_divergences_fresh():
    if not _is_weekday():
        return "skip", "weekend — no scanner runs expected", "weekday only", ""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    rows = sb_get("shadow_divergences", {
        "select": "id",
        "created_at": f"gte.{cutoff}",
        "limit": "10",
    })
    count = len(rows)
    if count == 0:
        return "fail", "0 shadow_divergences in 48h", ">=1 in 48h (weekday)", "No shadow divergences flowing — ensemble may be broken"
    return "pass", f"{count} shadow_divergences in 48h", ">=1 in 48h (weekday)", ""


DATA_FRESHNESS_CHECKS = [
    (1201, "Catalyst events fresh",                  check_1201_catalyst_events_fresh),
    (1202, "Inference chains fresh",                 check_1202_inference_chains_fresh),
    (1203, "Pipeline runs fresh (manifest-driven)",  check_1203_pipeline_runs_fresh_manifest),
    (1204, "Shadow divergences flowing",             check_1204_shadow_divergences_fresh),
]


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 13 — historical_regression (1301–1303)
# ═══════════════════════════════════════════════════════════════════════════════

def check_1301_catalyst_volume_regression():
    rows = sb_get("pipeline_runs", {
        "select": "output_snapshot",
        "pipeline_name": "eq.catalyst_ingest",
        "step_name": "eq.root",
        "order": "created_at.desc",
        "limit": "20",
    })
    if len(rows) < 3:
        return "skip", f"only {len(rows)} catalyst_ingest runs in DB", ">=3 historical runs", ""
    counts = []
    for row in rows:
        snap = row.get("output_snapshot") or {}
        val = snap.get("total_inserted")
        if val is not None:
            counts.append(int(val))
    if len(counts) < 3:
        return "skip", "total_inserted not in output_snapshot", ">=3 runs with total_inserted", ""
    historical = counts[1:]   # exclude most recent
    avg = sum(historical) / len(historical)
    latest = counts[0]
    threshold = avg * 0.5
    detail = f"avg={avg:.0f}, latest={latest}"
    if avg == 0:
        return "skip", "avg=0 (all historical runs inserted 0)", "non-zero baseline", ""
    if latest < threshold:
        return "warn", detail, f"latest>={threshold:.0f} (50% of avg)", f"Catalyst volume dropped: latest={latest} < 50% of avg={avg:.0f}"
    return "pass", detail, f"latest>={threshold:.0f} (50% of avg)", ""


def check_1302_scanner_candidate_regression():
    rows = sb_get("pipeline_runs", {
        "select": "output_snapshot",
        "pipeline_name": "eq.scanner",
        "step_name": "eq.root",
        "order": "created_at.desc",
        "limit": "20",
    })
    if len(rows) < 3:
        return "skip", f"only {len(rows)} scanner runs in DB", ">=3 historical runs", ""
    counts = []
    for row in rows:
        snap = row.get("output_snapshot") or {}
        val = snap.get("candidates")
        if val is not None:
            counts.append(int(val))
    if len(counts) < 3:
        return "skip", "candidates not in output_snapshot", ">=3 runs with candidates", ""
    historical = counts[1:]
    avg = sum(historical) / len(historical)
    latest = counts[0]
    threshold = avg * 0.5
    detail = f"avg={avg:.1f}, latest={latest}"
    if avg == 0:
        return "skip", "avg=0 (all historical scans found 0 candidates)", "non-zero baseline", ""
    if latest < threshold:
        return "warn", detail, f"latest>={threshold:.1f} (50% of avg)", f"Scanner candidate count dropped: latest={latest} < 50% of avg={avg:.1f}"
    return "pass", detail, f"latest>={threshold:.1f} (50% of avg)", ""


def check_1303_shadow_divergence_rate():
    if not _is_weekday():
        return "skip", "weekend — no scanner runs expected", "weekday only", ""
    cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    div_rows = sb_get("shadow_divergences", {
        "select": "id",
        "created_at": f"gte.{cutoff_7d}",
        "limit": "1000",
    })
    divergences = len(div_rows)
    # Count scanner shadow_inference steps as a proxy for total shadow runs
    scan_rows = sb_get("pipeline_runs", {
        "select": "id,output_snapshot",
        "pipeline_name": "eq.scanner",
        "step_name": "eq.shadow_inference",
        "created_at": f"gte.{cutoff_7d}",
        "limit": "200",
    })
    runs = len(scan_rows)
    if runs == 0:
        return "skip", "0 scanner shadow_inference runs in last 7 days", "runs>0", ""
    # Estimate candidates per run from output_snapshots
    candidate_counts = []
    for row in scan_rows:
        snap = row.get("output_snapshot") or {}
        val = snap.get("candidates")
        if val is not None:
            candidate_counts.append(int(val))
    avg_candidates = (sum(candidate_counts) / len(candidate_counts)) if candidate_counts else 1.0
    if avg_candidates < 1:
        avg_candidates = 1.0
    denominator = runs * avg_candidates
    rate = divergences / denominator if denominator > 0 else 0.0
    detail = f"rate={rate:.1%}, divergences={divergences}, runs={runs}, avg_candidates={avg_candidates:.1f}"
    if rate < 0.05:
        return "warn", detail, "5%–80%", f"Shadow divergence rate {rate:.1%} below 5% — ensemble may be too agreeable"
    if rate > 0.80:
        return "warn", detail, "5%–80%", f"Shadow divergence rate {rate:.1%} above 80% — ensemble may be over-diverging"
    return "pass", detail, "5%–80%", ""


HISTORICAL_REGRESSION_CHECKS = [
    (1301, "Catalyst volume regression",     check_1301_catalyst_volume_regression),
    (1302, "Scanner candidate regression",   check_1302_scanner_candidate_regression),
    (1303, "Shadow divergence rate",         check_1303_shadow_divergence_rate),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Check group registry
# ═══════════════════════════════════════════════════════════════════════════════

ALL_GROUPS: dict[str, list] = {
    "infrastructure":         INFRASTRUCTURE_CHECKS,
    "database":               DATABASE_CHECKS,
    "crons":                  CRON_CHECKS,
    "signals":                SIGNAL_CHECKS,
    "tumblers":               TUMBLER_CHECKS,
    "ensemble":               ENSEMBLE_CHECKS,
    "logging":                LOGGING_CHECKS,
    "dashboard":              DASHBOARD_CHECKS,
    "claude_api":             CLAUDE_API_CHECKS,
    "crontab_drift":          CRONTAB_DRIFT_CHECKS,
    "output_quality":         OUTPUT_QUALITY_CHECKS,
    "data_freshness":         DATA_FRESHNESS_CHECKS,
    "historical_regression":  HISTORICAL_REGRESSION_CHECKS,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Main runner
# ═══════════════════════════════════════════════════════════════════════════════

def _build_slack_message(results: list[CheckResult], run_id: str) -> str:
    failures = [r for r in results if r.status == "fail"]
    warns = [r for r in results if r.status == "warn"]
    total = len(results)
    passed = len([r for r in results if r.status == "pass"])
    skipped = len([r for r in results if r.status == "skip"])

    if not failures and not warns:
        icon = "✅"
        headline = f"{icon} OPENCLAW HEALTH CHECK — ALL PASS ({passed}/{total})"
    elif failures:
        icon = "🔴"
        headline = f"{icon} OPENCLAW HEALTH CHECK — {len(failures)} FAILURE{'S' if len(failures) > 1 else ''}"
    else:
        icon = "🟡"
        headline = f"{icon} OPENCLAW HEALTH CHECK — {len(warns)} WARNING{'S' if len(warns) > 1 else ''}"

    lines = [
        headline,
        f"pass={passed}  fail={len(failures)}  warn={len(warns)}  skip={skipped}  total={total}",
        f"run_id: {run_id}",
    ]

    for r in failures:
        lines.append(f"\n[{r.check_order}] {r.check_name}")
        if r.error_message:
            lines.append(f"  Error: {r.error_message}")
        if r.value:
            lines.append(f"  Got: {r.value}  Expected: {r.expected}")

    for r in warns:
        lines.append(f"\n[{r.check_order}] {r.check_name} (WARN)")
        if r.error_message:
            lines.append(f"  {r.error_message}")

    return "\n".join(lines)


def run_checks(
    groups: list[str],
    run_id: str,
    run_type: str,
    dry_run: bool,
    notify_always: bool,
) -> list[CheckResult]:
    now_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"\n  {Fore.CYAN}{'━' * 51}{Style.RESET_ALL}")
    print(f"  {Fore.WHITE}OPENCLAW SYSTEM HEALTH — {now_str}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{'━' * 51}{Style.RESET_ALL}")
    if dry_run:
        print(f"  {Fore.YELLOW}[DRY RUN — no DB writes, no Slack]{Style.RESET_ALL}")

    all_results: list[CheckResult] = []

    for group_name in groups:
        checks = ALL_GROUPS.get(group_name, [])
        if not checks:
            continue
        _print_group_header(group_name)
        for order, name, fn in checks:
            result = _run_check(order, name, group_name, fn)
            _print_check(result)
            _write_result(result, run_id, run_type, dry_run)
            all_results.append(result)

    # Summary
    failures = [r for r in all_results if r.status == "fail"]
    warns = [r for r in all_results if r.status == "warn"]
    passed = [r for r in all_results if r.status == "pass"]
    skipped = [r for r in all_results if r.status == "skip"]

    print(f"\n  {Fore.CYAN}{'━' * 51}{Style.RESET_ALL}")
    summary_parts = [
        f"{Fore.GREEN}PASS {len(passed)}{Style.RESET_ALL}",
        f"{Fore.RED}FAIL {len(failures)}{Style.RESET_ALL}",
        f"{Fore.YELLOW}WARN {len(warns)}{Style.RESET_ALL}",
        f"{Style.DIM}SKIP {len(skipped)}{Style.RESET_ALL}",
        f"TOTAL {len(all_results)}",
    ]
    print(f"  {'  '.join(summary_parts)}")
    print(f"  run_id: {Style.DIM}{run_id}{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}{'━' * 51}{Style.RESET_ALL}\n")

    # Slack
    if not dry_run:
        should_notify = notify_always or bool(failures) or bool(warns)
        if should_notify:
            msg = _build_slack_message(all_results, run_id)
            slack_notify(msg)

    return all_results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OpenClaw system health check — 49 checks across 13 groups.",
    )
    parser.add_argument(
        "--notify-always",
        action="store_true",
        help="Always post Slack summary, not just on failures.",
    )
    parser.add_argument(
        "--group",
        choices=list(ALL_GROUPS.keys()),
        default=None,
        help="Run only one check group.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results only — no DB writes, no Slack.",
    )
    args = parser.parse_args()

    run_id = os.environ.get("HEALTH_RUN_ID") or str(uuid.uuid4())
    run_type = "manual" if os.environ.get("HEALTH_RUN_ID") else "scheduled"

    groups = [args.group] if args.group else list(ALL_GROUPS.keys())

    results = run_checks(
        groups=groups,
        run_id=run_id,
        run_type=run_type,
        dry_run=args.dry_run,
        notify_always=args.notify_always,
    )

    failures = [r for r in results if r.status == "fail"]
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
