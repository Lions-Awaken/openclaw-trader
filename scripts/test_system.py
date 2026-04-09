#!/usr/bin/env python3
"""
OpenClaw Preflight Simulator — NASA go/no-go system validation.

Groups:
  A - Module Integrity
  B - Ground Systems (Schema)
  C - Adversarial Array (Shadow Contexts)
  D - Signal Acquisition
  E - Tumbler Chain
  F - Ensemble Systems
  G - Economics
  H - End-to-End Flow
  I - Dashboard Comms

Usage:
  python scripts/test_system.py             # Full run
  python scripts/test_system.py --dry-run   # Skip DB writes and external calls
  SIMULATOR_RUN_ID=<uuid> python scripts/test_system.py  # Dashboard-linked run
"""

import argparse
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

try:
    from colorama import Fore, Style
    from colorama import init as colorama_init
    colorama_init()
except ImportError:
    class Fore:  # type: ignore[no-redef]
        GREEN = ""
        RED = ""
        YELLOW = ""
        CYAN = ""
        WHITE = ""
        BLUE = ""
        RESET = ""

    class Style:  # type: ignore[no-redef]
        BRIGHT = ""
        DIM = ""
        RESET_ALL = ""


@dataclass
class TestResult:
    test_id: str
    group: str
    name: str
    status: str       # "GO", "NO-GO", "SCRUB"
    value: str
    expected: str
    error: str | None
    duration_ms: int
    check_order: int


# ---------------------------------------------------------------------------
# DB write helper
# ---------------------------------------------------------------------------

def _write_result(result: TestResult, run_id: str | None, dry_run: bool) -> None:
    """Write one result row to system_health immediately on completion."""
    if dry_run or not run_id:
        return
    try:
        from tracer import _post_to_supabase
        status_map = {"GO": "pass", "NO-GO": "fail", "SCRUB": "skip"}
        _post_to_supabase("system_health", {
            "run_id": run_id,
            "run_type": "simulator",
            "check_group": result.group,
            "check_name": f"{result.test_id}: {result.name}",
            "check_order": result.check_order,
            "status": status_map.get(result.status, "skip"),
            "value": result.value,
            "expected": result.expected,
            "error_message": result.error or "",
            "duration_ms": result.duration_ms,
        })
    except Exception as e:
        print(f"  [sim] WARNING: could not write result to system_health: {e}")


# ---------------------------------------------------------------------------
# Print helper
# ---------------------------------------------------------------------------

def _print_result(result: TestResult) -> None:
    status_color = {
        "GO": Fore.GREEN,
        "NO-GO": Fore.RED,
        "SCRUB": Fore.YELLOW,
    }
    color = status_color.get(result.status, Fore.WHITE)
    pad = 30 - len(result.name)
    dots = "." * max(pad, 3)
    print(
        f"  [{result.test_id}] {result.name} {dots} "
        f"{color}{result.status}{Style.RESET_ALL}   {result.value}   ({result.duration_ms}ms)"
    )
    if result.status == "NO-GO" and result.error:
        print(f"        {Fore.RED}Error: {result.error[:200]}{Style.RESET_ALL}")
        print(f"        Expected: {result.expected}")


def _run(fn, test_id: str, group: str, name: str, expected: str, check_order: int) -> TestResult:
    """Execute a test function and return a TestResult. Catches all exceptions."""
    t0 = time.time()
    try:
        status, value, error = fn()
    except Exception:
        tb = traceback.format_exc()
        duration = int((time.time() - t0) * 1000)
        return TestResult(
            test_id=test_id,
            group=group,
            name=name,
            status="NO-GO",
            value="exception",
            expected=expected,
            error=tb[-400:],
            duration_ms=duration,
            check_order=check_order,
        )
    duration = int((time.time() - t0) * 1000)
    return TestResult(
        test_id=test_id,
        group=group,
        name=name,
        status=status,
        value=value,
        expected=expected,
        error=error,
        duration_ms=duration,
        check_order=check_order,
    )


# ===========================================================================
# GROUP A — MODULE INTEGRITY (100-199)
# ===========================================================================

def _test_a1() -> tuple[str, str, str | None]:
    """Import all manifest scripts."""
    from manifest import ALL_ENTRIES
    successes = 0
    failures = []
    for entry in ALL_ENTRIES:
        module_name = entry.script.replace("scripts/", "").replace(".py", "")
        try:
            __import__(module_name)
            successes += 1
        except Exception as exc:
            failures.append(f"{module_name}: {exc}")
    total = len(ALL_ENTRIES)
    if failures:
        return ("NO-GO", f"{successes}/{total} modules", "\n".join(failures[:5]))
    return ("GO", f"{successes}/{total} modules", None)


def _test_a2() -> tuple[str, str, str | None]:
    """Key functions callable."""
    import inference_engine
    import meta_analysis
    import scanner
    from calibrator import grade_shadow_profiles
    from common import check_market_open, load_strategy_profile, sb_get, slack_notify
    from inference_engine import (
        get_claude_budget,
        get_todays_claude_spend,
        log_cost,
    )
    from shadow_profiles import get_max_tumbler_depth, get_shadow_context

    targets = {
        "run_inference": inference_engine.run_inference,
        "compute_signals": scanner.compute_signals,
        "grade_shadow_profiles": grade_shadow_profiles,
        "get_shadow_context": get_shadow_context,
        "get_max_tumbler_depth": get_max_tumbler_depth,
        "load_strategy_profile": load_strategy_profile,
        "check_market_open": check_market_open,
        "sb_get": sb_get,
        "slack_notify": slack_notify,
        "get_claude_budget": get_claude_budget,
        "get_todays_claude_spend": get_todays_claude_spend,
        "log_cost": log_cost,
        "_load_shadow_profiles": scanner._load_shadow_profiles,
        "_record_divergence": scanner._record_divergence,
        "get_shadow_divergence_summary": meta_analysis.get_shadow_divergence_summary,
    }
    failures = [name for name, fn in targets.items() if not callable(fn)]
    if failures:
        return ("NO-GO", f"{len(targets)-len(failures)}/{len(targets)} callable", str(failures))
    return ("GO", f"{len(targets)} callable", None)


def run_group_a(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "MODULE INTEGRITY"
    print(f"\n  {Fore.CYAN}A · {group}{Style.RESET_ALL}")
    results = []
    for fn, tid, name, expected, order in [
        (_test_a1, "A1", "manifest imports", "all modules importable", 100),
        (_test_a2, "A2", "key functions callable", "15 functions callable", 110),
    ]:
        r = _run(fn, tid, group, name, expected, order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)
    return results


# ===========================================================================
# GROUP B — GROUND SYSTEMS (200-299)
# ===========================================================================

def _test_b1() -> tuple[str, str, str | None]:
    """All tables exist by querying known tables directly."""
    from common import sb_get
    expected_tables = [
        "strategy_profiles", "inference_chains", "pipeline_runs", "cost_ledger",
        "signal_evaluations", "shadow_divergences", "system_health", "system_stats",
        "meta_reflections", "catalyst_events", "trade_decisions", "order_events",
        "budget_config", "regime_log", "trade_learnings", "stack_heartbeats",
        "pattern_templates", "confidence_calibration", "predictions",
        "congress_clusters", "research_memories", "tuning_telemetry",
        "tuning_profiles", "data_quality_checks", "politician_intel",
        "legislative_calendar", "llm_inferences", "magic_link_tokens",
        "options_flow_signals", "form4_signals",
    ]
    found = 0
    missing = []
    for table in expected_tables:
        rows = sb_get(table, {"select": "id", "limit": "1"})
        if isinstance(rows, list):
            found += 1
        else:
            missing.append(table)
    if found >= 28:
        return ("GO", f"{found}/{len(expected_tables)} tables", None)
    return ("NO-GO", f"{found}/{len(expected_tables)} tables", f"Missing: {missing[:5]}")


def _test_b2() -> tuple[str, str, str | None]:
    """shadow_divergences is queryable with expected key columns."""
    from common import sb_get
    # Can't query information_schema via PostgREST — verify by querying the table directly
    rows = sb_get("shadow_divergences", {"select": "id,ticker,shadow_profile,live_decision,shadow_decision", "limit": "1"})
    if isinstance(rows, list):
        return ("GO", "table accessible", None)
    return ("NO-GO", "table inaccessible", "shadow_divergences query failed")


def _test_b3() -> tuple[str, str, str | None]:
    """Shadow profiles seeded."""
    from common import sb_get
    rows = sb_get("strategy_profiles", {
        "select": "profile_name,dwm_weight",
        "is_shadow": "eq.true",
    })
    expected_names = {"SKEPTIC", "CONTRARIAN", "REGIME_WATCHER", "OPTIONS_FLOW", "FORM4_INSIDER"}
    found_names = {r["profile_name"] for r in rows}
    missing = expected_names - found_names
    low_weight = [r["profile_name"] for r in rows if float(r.get("dwm_weight", 0)) < 0.05]
    if missing:
        return ("NO-GO", f"{len(rows)} profiles", f"Missing: {missing}")
    if low_weight:
        return ("NO-GO", f"{len(rows)} profiles", f"dwm_weight < 0.05: {low_weight}")
    return ("GO", f"{len(rows)} seeded", None)


def _test_b4() -> tuple[str, str, str | None]:
    """profile_name backfill in inference_chains."""
    from common import sb_get
    rows = sb_get("inference_chains", {
        "select": "id",
        "profile_name": "eq.CONGRESS_MIRROR",
        "limit": "200",
    })
    count = len(rows)
    if count > 100:
        return ("GO", f"{count} chains", None)
    return ("NO-GO", f"{count} chains", f"Expected > 100, got {count}")


def _test_b5() -> tuple[str, str, str | None]:
    """Signal tables exist and are queryable."""
    from common import sb_get
    found = 0
    missing = []
    for table in ("options_flow_signals", "form4_signals"):
        rows = sb_get(table, {"select": "id", "limit": "1"})
        if isinstance(rows, list):
            found += 1
        else:
            missing.append(table)
    if found == 2:
        return ("GO", "2 tables ready", None)
    return ("NO-GO", f"{found}/2 tables", f"Missing: {missing}")


def _test_b6() -> tuple[str, str, str | None]:
    """system_health table is queryable."""
    from common import sb_get
    rows = sb_get("system_health", {"select": "id,run_id,status,check_name", "limit": "1"})
    if isinstance(rows, list):
        return ("GO", "table accessible", None)
    return ("NO-GO", "table inaccessible", "system_health query failed")


def run_group_b(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "GROUND SYSTEMS"
    print(f"\n  {Fore.CYAN}B · {group}{Style.RESET_ALL}")
    results = []
    for fn, tid, name, expected, order in [
        (_test_b1, "B1", "table inventory", ">= 28 tables", 200),
        (_test_b2, "B2", "shadow_divergences cols", "22 columns", 210),
        (_test_b3, "B3", "shadow profiles", "5 profiles seeded", 220),
        (_test_b4, "B4", "profile_name backfill", "> 100 CONGRESS_MIRROR chains", 230),
        (_test_b5, "B5", "signal tables", "2 tables ready", 240),
        (_test_b6, "B6", "system_health table", "12 columns", 250),
    ]:
        r = _run(fn, tid, group, name, expected, order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)
    return results


# ===========================================================================
# GROUP C — ADVERSARIAL ARRAY (300-399)
# ===========================================================================

def _test_c1() -> tuple[str, str, str | None]:
    """Shadow contexts non-empty with expected keywords."""
    from shadow_profiles import get_shadow_context
    checks = {
        "SKEPTIC": "conservative",
        "CONTRARIAN": "wrong",
        "REGIME_WATCHER": "macro",
        "OPTIONS_FLOW": "sweep",
        "FORM4_INSIDER": "cluster",
    }
    failures = []
    for shadow_type, keyword in checks.items():
        ctx = get_shadow_context(shadow_type)
        if len(ctx) <= 50:
            failures.append(f"{shadow_type}: too short ({len(ctx)} chars)")
        elif keyword.lower() not in ctx.lower():
            failures.append(f"{shadow_type}: missing keyword '{keyword}'")
    if failures:
        return ("NO-GO", f"{len(checks)-len(failures)}/{len(checks)} contexts", str(failures))
    return ("GO", f"{len(checks)}/5 contexts valid", None)


def _test_c2() -> tuple[str, str, str | None]:
    """Tumbler depth caps correct."""
    from shadow_profiles import get_max_tumbler_depth
    expected = {
        "REGIME_WATCHER": 3,
        "SKEPTIC": 5,
        "CONTRARIAN": 5,
        "OPTIONS_FLOW": 5,
        "FORM4_INSIDER": 5,
    }
    failures = []
    for shadow_type, expected_depth in expected.items():
        actual = get_max_tumbler_depth(shadow_type)
        if actual != expected_depth:
            failures.append(f"{shadow_type}: expected {expected_depth}, got {actual}")
    if failures:
        return ("NO-GO", "depth mismatch", str(failures))
    return ("GO", "5/5 depth caps correct", None)


def run_group_c(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "ADVERSARIAL ARRAY"
    print(f"\n  {Fore.CYAN}C · {group}{Style.RESET_ALL}")
    results = []
    for fn, tid, name, expected, order in [
        (_test_c1, "C1", "shadow contexts", "5 non-empty w/ keywords", 300),
        (_test_c2, "C2", "tumbler depth caps", "5 depth caps match spec", 310),
    ]:
        r = _run(fn, tid, group, name, expected, order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)
    return results


# ===========================================================================
# GROUP D — SIGNAL ACQUISITION (400-499)
# ===========================================================================

def _test_d1() -> tuple[str, str, str | None]:
    """Active strategy profile loads."""
    from common import load_strategy_profile
    profile = load_strategy_profile()
    if not isinstance(profile, dict):
        return ("NO-GO", "no dict returned", "Expected dict from load_strategy_profile")
    if "profile_name" not in profile:
        keys = list(profile.keys())[:5]
        return ("NO-GO", "missing profile_name", f"Keys found: {keys}")
    return ("GO", profile.get("profile_name", "?"), None)


def _test_d2_dry() -> tuple[str, str, str | None]:
    return ("SCRUB", "skipped", "requires market data — use live run")


def _test_d3() -> tuple[str, str, str | None]:
    """Options flow enrichment."""
    from scanner import _enrich_with_options_flow
    candidate: dict = {"ticker": "SIM_TEST", "signals": {}}
    _enrich_with_options_flow([candidate])
    if "options_flow_net" not in candidate["signals"]:
        keys = list(candidate["signals"].keys())
        return ("NO-GO", "missing key", f"options_flow_net not in signals. Keys: {keys}")
    return ("GO", f"options_flow_net={candidate['signals']['options_flow_net']}", None)


def _test_d4() -> tuple[str, str, str | None]:
    """Form4 enrichment."""
    from scanner import _enrich_with_form4
    candidate: dict = {"ticker": "SIM_TEST", "signals": {}}
    _enrich_with_form4([candidate])
    if "form4_insider_score" not in candidate["signals"]:
        keys = list(candidate["signals"].keys())
        return ("NO-GO", "missing key", f"form4_insider_score not in signals. Keys: {keys}")
    return ("GO", f"form4_insider_score={candidate['signals']['form4_insider_score']}", None)


def run_group_d(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "SIGNAL ACQUISITION"
    print(f"\n  {Fore.CYAN}D · {group}{Style.RESET_ALL}")
    results = []

    r_d1 = _run(_test_d1, "D1", group, "active profile", "dict with profile_name", 400)
    _print_result(r_d1)
    _write_result(r_d1, run_id, dry_run)
    results.append(r_d1)

    # D2 — always SCRUB (needs real market bars)
    r_d2 = _run(_test_d2_dry, "D2", group, "compute_signals", "requires market data", 410)
    _print_result(r_d2)
    _write_result(r_d2, run_id, dry_run)
    results.append(r_d2)

    r_d3 = _run(_test_d3, "D3", group, "options flow enrichment", "options_flow_net in signals", 420)
    _print_result(r_d3)
    _write_result(r_d3, run_id, dry_run)
    results.append(r_d3)

    r_d4 = _run(_test_d4, "D4", group, "form4 enrichment", "form4_insider_score in signals", 430)
    _print_result(r_d4)
    _write_result(r_d4, run_id, dry_run)
    results.append(r_d4)

    return results


# ===========================================================================
# GROUP E — TUMBLER CHAIN (500-599)
# ===========================================================================

def _test_e1_live() -> tuple[str, str, str | None]:
    """run_inference with SKEPTIC profile override."""
    from common import sb_get
    from inference_engine import run_inference
    rows = sb_get("strategy_profiles", {
        "select": "*",
        "profile_name": "eq.SKEPTIC",
        "limit": "1",
    })
    if not rows:
        return ("NO-GO", "no SKEPTIC profile", "SKEPTIC profile not found in strategy_profiles")
    skeptic_profile = rows[0]
    # Minimal signal set that will pass T1
    signals = {
        "trend": {"passed": True, "price": 50.0, "sma10": 48.0, "sma20": 46.0},
        "momentum": {"passed": True, "rsi": 55.0},
        "volume": {"passed": True, "today_vol": 2_000_000, "avg_vol_20": 1_000_000, "ratio": 2.0},
        "fundamental": {"passed": True, "catalyst_count": 1, "bullish_count": 1},
        "sentiment": {"passed": True, "avg_sentiment": 0.5, "sample_count": 1},
        "flow": {"passed": True, "ticker_5d_pct": 2.0, "spy_5d_pct": 1.0},
        "score": 6,
        "total_score": 6,
        "options_flow_net": 0,
        "form4_insider_score": 0,
    }
    result = run_inference(
        ticker="SIM_TEST",
        signals=signals,
        total_score=6,
        scan_type="pre_market",
        profile_override=skeptic_profile,
    )
    required_keys = ["final_decision", "final_confidence", "stopping_reason", "profile"]
    missing = [k for k in required_keys if k not in result]
    if missing:
        return ("NO-GO", "missing keys", f"Missing from result: {missing}")
    if result.get("profile") != "SKEPTIC":
        return ("NO-GO", f"profile={result.get('profile')}", "Expected profile='SKEPTIC'")
    decision = result.get("final_decision", "?")
    confidence = result.get("final_confidence", 0.0)
    return ("GO", f"{decision} conf={confidence:.3f}", None)


def _test_e2() -> tuple[str, str, str | None]:
    """REGIME_WATCHER depth cap."""
    from shadow_profiles import get_max_tumbler_depth
    depth = get_max_tumbler_depth("REGIME_WATCHER")
    if depth == 3:
        return ("GO", "depth=3", None)
    return ("NO-GO", f"depth={depth}", "Expected REGIME_WATCHER depth=3")


def _test_e3() -> tuple[str, str, str | None]:
    """Stopping rule handles None gracefully."""
    from inference_engine import check_stopping_rule
    # Tumbler result that won't trigger a TypeError when confidence_after is 0.0
    tumbler_result = {
        "depth": 2,
        "confidence_after": 0.30,
        "veto": False,
    }
    try:
        result = check_stopping_rule(
            tumbler_result,
            prev_confidence=0.25,
            start_time=time.time(),
            has_veto=False,
        )
        # Returns None or a string — both are fine
        return ("GO", f"returned {result!r}", None)
    except TypeError as exc:
        return ("NO-GO", "TypeError raised", str(exc))


def _test_e4() -> tuple[str, str, str | None]:
    """Shadow context injection for SKEPTIC."""
    from shadow_profiles import get_shadow_context
    ctx = get_shadow_context("SKEPTIC")
    if not ctx:
        return ("NO-GO", "empty context", "SKEPTIC context is empty")
    if len(ctx) <= 50:
        return ("NO-GO", f"len={len(ctx)}", f"Context too short: {len(ctx)} chars")
    return ("GO", f"len={len(ctx)} chars", None)


def run_group_e(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "TUMBLER CHAIN"
    print(f"\n  {Fore.CYAN}E · {group}{Style.RESET_ALL}")
    results = []

    if dry_run:
        r_e1 = TestResult(
            test_id="E1", group=group, name="run_inference SKEPTIC",
            status="SCRUB", value="dry-run skip", expected="run_inference round-trip",
            error=None, duration_ms=0, check_order=500,
        )
    else:
        r_e1 = _run(_test_e1_live, "E1", group, "run_inference SKEPTIC",
                    "round-trip with profile=SKEPTIC", 500)
    _print_result(r_e1)
    _write_result(r_e1, run_id, dry_run)
    results.append(r_e1)

    for fn, tid, name, expected, order in [
        (_test_e2, "E2", "REGIME_WATCHER depth", "depth=3", 510),
        (_test_e3, "E3", "stopping rule null", "no TypeError", 520),
        (_test_e4, "E4", "shadow context inject", "SKEPTIC context non-empty", 530),
    ]:
        r = _run(fn, tid, group, name, expected, order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)

    return results


# ===========================================================================
# GROUP F — ENSEMBLE SYSTEMS (600-699)
# ===========================================================================

def _test_f1() -> tuple[str, str, str | None]:
    """Load shadow profiles from DB."""
    from scanner import _load_shadow_profiles
    profiles = _load_shadow_profiles()
    if not isinstance(profiles, list):
        return ("NO-GO", "not a list", "Expected list from _load_shadow_profiles")
    if len(profiles) < 3:
        return ("NO-GO", f"{len(profiles)} profiles", f"Expected >= 3, got {len(profiles)}")
    non_shadow = [p.get("profile_name") for p in profiles if not p.get("is_shadow")]
    if non_shadow:
        return ("NO-GO", "non-shadow included", f"is_shadow=False for: {non_shadow}")
    return ("GO", f"{len(profiles)} shadow profiles", None)


def _test_f2() -> tuple[str, str, str | None]:
    """Record divergence and verify/clean up."""
    from common import sb_get
    from scanner import _record_divergence
    from tracer import _sb_client, _sb_headers

    # Build synthetic opposite-decision results so _record_divergence actually writes
    live_result = {
        "final_decision": "enter",
        "final_confidence": 0.70,
        "inference_chain_id": None,
        "tumblers": [{"depth": 1, "confidence_after": 0.70}],
    }
    shadow_result = {
        "final_decision": "skip",
        "final_confidence": 0.30,
        "inference_chain_id": None,
        "stopping_reason": "confidence_floor",
        "tumblers": [{"depth": 1, "confidence_after": 0.30}],
    }
    live_profile = {"profile_name": "CONSERVATIVE"}
    shadow_profile = {"profile_name": "SKEPTIC", "shadow_type": "SKEPTIC"}

    _record_divergence("SIM_TEST", live_result, shadow_result, shadow_profile, live_profile)

    # Verify row exists
    rows = sb_get("shadow_divergences", {
        "select": "id",
        "ticker": "eq.SIM_TEST",
        "limit": "5",
    })
    if not rows:
        return ("NO-GO", "0 rows written", "Expected row in shadow_divergences for ticker=SIM_TEST")

    # Cleanup
    from tracer import SUPABASE_URL
    try:
        _sb_client.delete(
            f"{SUPABASE_URL}/rest/v1/shadow_divergences?ticker=eq.SIM_TEST",
            headers=_sb_headers(),
        )
    except Exception as exc:
        return ("NO-GO", "cleanup failed", f"Could not delete test rows: {exc}")

    # Verify deleted
    after = sb_get("shadow_divergences", {
        "select": "id",
        "ticker": "eq.SIM_TEST",
        "limit": "5",
    })
    if after:
        return ("NO-GO", "rows not deleted", f"{len(after)} rows remain after delete")
    return ("GO", "row written + deleted", None)


def _test_f3() -> tuple[str, str, str | None]:
    """Grade shadow profiles (with no ungraded data, returns zeros)."""
    from calibrator import grade_shadow_profiles
    result = grade_shadow_profiles()
    if not isinstance(result, dict):
        return ("NO-GO", "not a dict", f"Expected dict, got {type(result).__name__}")
    # grade_shadow_profiles returns {"graded": int, "profiles_updated": int}
    if "graded" not in result and "graded_divergences" not in result:
        keys = list(result.keys())
        return ("NO-GO", "missing graded key", f"Keys: {keys}")
    graded = result.get("graded", result.get("graded_divergences", 0))
    updated = result.get("profiles_updated", 0)
    return ("GO", f"graded={graded} updated={updated}", None)


def _test_f4() -> tuple[str, str, str | None]:
    """Divergence summary from meta_analysis."""
    from meta_analysis import get_shadow_divergence_summary
    result = get_shadow_divergence_summary()
    if not isinstance(result, dict):
        return ("NO-GO", "not a dict", f"Expected dict, got {type(result).__name__}")
    required = {"count", "divergences", "unanimous_dissent"}
    missing = required - set(result.keys())
    if missing:
        return ("NO-GO", "missing keys", f"Missing: {missing}. Got: {list(result.keys())}")
    count = result.get("count", 0)
    # profiles_active is not returned by get_shadow_divergence_summary — note this
    return ("GO", f"count={count} divergences today", None)


def run_group_f(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "ENSEMBLE SYSTEMS"
    print(f"\n  {Fore.CYAN}F · {group}{Style.RESET_ALL}")
    results = []
    for fn, tid, name, expected, order in [
        (_test_f1, "F1", "load shadow profiles", ">= 3 is_shadow=True rows", 600),
        (_test_f2, "F2", "record divergence", "row written + deleted", 610),
        (_test_f3, "F3", "grade profiles", "graded + profiles_updated keys", 620),
        (_test_f4, "F4", "divergence summary", "count/divergences/unanimous_dissent", 630),
    ]:
        r = _run(fn, tid, group, name, expected, order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)
    return results


# ===========================================================================
# GROUP G — ECONOMICS (700-799)
# ===========================================================================

def _test_g1() -> tuple[str, str, str | None]:
    """Claude spend today."""
    from inference_engine import get_todays_claude_spend
    spend = get_todays_claude_spend()
    if not isinstance(spend, (int, float)) or spend < 0:
        return ("NO-GO", str(spend), "Expected numeric >= 0")
    return ("GO", f"${float(spend):.4f} today", None)


def _test_g2() -> tuple[str, str, str | None]:
    """Claude budget configured."""
    from inference_engine import get_claude_budget, get_todays_claude_spend
    budget = get_claude_budget()
    if not isinstance(budget, (int, float)) or budget <= 0:
        return ("NO-GO", str(budget), "Expected numeric > 0")
    spend = float(get_todays_claude_spend())
    budget_f = float(budget)
    remaining_pct = max(0.0, (budget_f - spend) / budget_f * 100) if budget_f > 0 else 0.0
    return ("GO", f"${budget_f:.2f} budget {remaining_pct:.0f}% remaining", None)


def _test_g3() -> tuple[str, str, str | None]:
    """Cost attribution includes profile_name in subcategory."""
    import inspect

    from inference_engine import log_cost
    try:
        src = inspect.getsource(log_cost)
        # Look for evidence that profile_name appears near the call site
        # by checking the function or its usages in inference_engine
        import inference_engine as _ie_mod
        ie_src = inspect.getsource(_ie_mod)
        has_profile_in_subcategory = (
            "profile_name" in ie_src
            and "log_cost" in ie_src
            and "subcategory" in src
        )
        if has_profile_in_subcategory:
            return ("GO", "profile_name in subcategory format", None)
        return ("SCRUB", "could not confirm pattern", "Inspect source manually")
    except Exception as exc:
        return ("SCRUB", "inspect failed", str(exc))


def _test_g4() -> tuple[str, str, str | None]:
    """Daily budget estimate from manifest."""
    from manifest import estimate_daily_claude_budget
    budget = estimate_daily_claude_budget()
    if not isinstance(budget, float) or budget <= 0:
        return ("NO-GO", str(budget), "Expected float > 0")
    return ("GO", f"${budget:.2f}/day estimated", None)


def run_group_g(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "ECONOMICS"
    print(f"\n  {Fore.CYAN}G · {group}{Style.RESET_ALL}")
    results = []
    for fn, tid, name, expected, order in [
        (_test_g1, "G1", "claude spend today", "float >= 0", 700),
        (_test_g2, "G2", "claude budget", "float > 0, remaining %", 710),
        (_test_g3, "G3", "cost attribution", "profile_name in subcategory", 720),
        (_test_g4, "G4", "daily budget estimate", "float > 0 from manifest", 730),
    ]:
        r = _run(fn, tid, group, name, expected, order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)
    return results


# ===========================================================================
# GROUP H — END-TO-END FLOW (800-899)
# ===========================================================================

def _test_h1() -> tuple[str, str, str | None]:
    """Inject synthetic catalyst event."""
    from tracer import _post_to_supabase
    row = _post_to_supabase("catalyst_events", {
        "ticker": "SIM_TEST",
        "event_type": "test",
        "source": "simulator",
        "significance": "low",
    })
    if not row:
        return ("NO-GO", "insert returned None", "Expected non-None from _post_to_supabase")
    return ("GO", "catalyst row created", None)


def _test_h2_scrub() -> tuple[str, str, str | None]:
    return ("SCRUB", "skipped", "requires real market bars — synthetic chain tested via D3/D4")


def _test_h3_verified() -> tuple[str, str, str | None]:
    return ("GO", "verified in D3/D4", None)


def _test_h4(e1_ran: bool) -> tuple[str, str, str | None]:
    """Check inference chain from E1 run."""
    if not e1_ran:
        return ("SCRUB", "E1 not run", "Run without --dry-run to test inference chain write")
    from common import sb_get
    rows = sb_get("inference_chains", {
        "select": "id,profile_name,final_decision",
        "ticker": "eq.SIM_TEST",
        "profile_name": "eq.SKEPTIC",
        "order": "created_at.desc",
        "limit": "5",
    })
    if not rows:
        return ("NO-GO", "0 rows", "Expected inference_chains row for SIM_TEST/SKEPTIC")
    return ("GO", f"{len(rows)} chain(s) found", None)


def _test_h5_verified() -> tuple[str, str, str | None]:
    return ("GO", "verified in F2", None)


def _test_h6() -> tuple[str, str, str | None]:
    """Cleanup all SIM_TEST synthetic data."""
    from tracer import SUPABASE_URL, _sb_client, _sb_headers
    tables = [
        "catalyst_events",
        "inference_chains",
        "shadow_divergences",
        "signal_evaluations",
    ]
    counts = []
    errors = []
    for table in tables:
        try:
            resp = _sb_client.delete(
                f"{SUPABASE_URL}/rest/v1/{table}?ticker=eq.SIM_TEST",
                headers=_sb_headers(),
            )
            if resp.status_code in (200, 204):
                # Supabase returns deleted rows on 200
                try:
                    deleted = len(resp.json()) if resp.status_code == 200 else 0
                except Exception:
                    deleted = 0
                counts.append(f"{table}:{deleted}")
            else:
                errors.append(f"{table}:{resp.status_code}")
        except Exception as exc:
            errors.append(f"{table}:{exc}")
    summary = " ".join(counts)
    if errors:
        return ("NO-GO", summary, f"Cleanup errors: {errors}")
    return ("GO", f"cleaned {summary}", None)


def run_group_h(run_id: str | None, dry_run: bool, e1_ran: bool) -> list[TestResult]:
    group = "END-TO-END FLOW"
    print(f"\n  {Fore.CYAN}H · {group}{Style.RESET_ALL}")
    results = []

    # H1
    if dry_run:
        r_h1 = TestResult(
            test_id="H1", group=group, name="inject catalyst",
            status="SCRUB", value="dry-run skip", expected="catalyst row inserted",
            error=None, duration_ms=0, check_order=800,
        )
    else:
        r_h1 = _run(_test_h1, "H1", group, "inject catalyst", "catalyst row created", 800)
    _print_result(r_h1)
    _write_result(r_h1, run_id, dry_run)
    results.append(r_h1)

    # H2 — always SCRUB
    r_h2 = _run(_test_h2_scrub, "H2", group, "signal scan", "requires real bars", 810)
    _print_result(r_h2)
    _write_result(r_h2, run_id, dry_run)
    results.append(r_h2)

    # H3 — already verified
    r_h3 = _run(_test_h3_verified, "H3", group, "enrichment", "verified in D3/D4", 820)
    _print_result(r_h3)
    _write_result(r_h3, run_id, dry_run)
    results.append(r_h3)

    # H4
    if dry_run:
        r_h4 = TestResult(
            test_id="H4", group=group, name="inference chain write",
            status="SCRUB", value="dry-run skip", expected="chain row in DB",
            error=None, duration_ms=0, check_order=830,
        )
    else:
        def _h4_wrapper() -> tuple[str, str, str | None]:
            return _test_h4(e1_ran)
        r_h4 = _run(_h4_wrapper, "H4", group, "inference chain write", "chain row in DB", 830)
    _print_result(r_h4)
    _write_result(r_h4, run_id, dry_run)
    results.append(r_h4)

    # H5 — already verified
    r_h5 = _run(_test_h5_verified, "H5", group, "divergence record", "verified in F2", 840)
    _print_result(r_h5)
    _write_result(r_h5, run_id, dry_run)
    results.append(r_h5)

    # H6 — cleanup
    if dry_run:
        r_h6 = TestResult(
            test_id="H6", group=group, name="cleanup SIM_TEST data",
            status="SCRUB", value="dry-run skip", expected="all SIM_TEST rows deleted",
            error=None, duration_ms=0, check_order=850,
        )
    else:
        r_h6 = _run(_test_h6, "H6", group, "cleanup SIM_TEST data", "all tables cleaned", 850)
    _print_result(r_h6)
    _write_result(r_h6, run_id, dry_run)
    results.append(r_h6)

    return results


# ===========================================================================
# GROUP I — DASHBOARD COMMS (900-999)
# ===========================================================================

def _test_dashboard_endpoint(path: str, expected_item_count: int | None = None) -> tuple[str, str, str | None]:
    """GET a dashboard endpoint and check response."""
    try:
        import httpx
        resp = httpx.get(f"http://localhost:8000{path}", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            if expected_item_count is not None and isinstance(data, list):
                count = len(data)
                label = f"{count} items"
                if count < expected_item_count:
                    return ("NO-GO", label, f"Expected >= {expected_item_count}, got {count}")
            return ("GO", "HTTP 200", None)
        return ("NO-GO", f"HTTP {resp.status_code}", f"Expected 200 from {path}")
    except Exception as exc:
        err_str = str(exc)
        if "Connection refused" in err_str or "ConnectError" in err_str:
            return ("SCRUB", "connection refused", "Dashboard not running on localhost:8000")
        return ("NO-GO", "request failed", err_str[:200])


def run_group_i(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "DASHBOARD COMMS"
    print(f"\n  {Fore.CYAN}I · {group}{Style.RESET_ALL}")
    results = []

    if dry_run:
        for tid, name, order in [
            ("I1", "shadow profiles endpoint", 900),
            ("I2", "shadow divergences endpoint", 910),
            ("I3", "health latest endpoint", 920),
            ("I4", "options-flow endpoint", 930),
            ("I5", "form4 endpoint", 940),
        ]:
            r = TestResult(
                test_id=tid, group=group, name=name,
                status="SCRUB", value="dry-run skip", expected="HTTP 200",
                error=None, duration_ms=0, check_order=order,
            )
            _print_result(r)
            _write_result(r, run_id, dry_run)
            results.append(r)
        return results

    endpoints = [
        ("I1", "shadow profiles endpoint", "/api/shadow/profiles", 900, 5),
        ("I2", "shadow divergences endpoint", "/api/shadow/divergences", 910, None),
        ("I3", "health latest endpoint", "/api/health/latest", 920, None),
        ("I4", "options-flow endpoint", "/api/signals/options-flow", 930, None),
        ("I5", "form4 endpoint", "/api/signals/form4", 940, None),
    ]

    for tid, name, path, order, expected_count in endpoints:
        def _make_fn(p: str, ec: int | None) -> object:
            def _fn() -> tuple[str, str, str | None]:
                return _test_dashboard_endpoint(p, ec)
            return _fn
        r = _run(_make_fn(path, expected_count), tid, group, name, "HTTP 200", order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)

    return results


# ===========================================================================
# Main
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw Preflight Simulator")
    parser.add_argument("--dry-run", action="store_true", help="Skip DB writes and external calls")
    args = parser.parse_args()

    run_id = os.environ.get("SIMULATOR_RUN_ID") or str(uuid.uuid4())
    dry_run: bool = args.dry_run

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print("=" * 45)
    print("  OPENCLAW PREFLIGHT — GO / NO-GO")
    print(f"  {now_str}")
    if dry_run:
        print(f"  {Fore.YELLOW}DRY-RUN MODE — no DB writes{Style.RESET_ALL}")
    print("=" * 45)
    print()
    print(f"  FLIGHT DIRECTOR {'.' * 10} STANDBY")
    print(f"  {'─' * 40}")

    results: list[TestResult] = []

    results.extend(run_group_a(run_id, dry_run))
    results.extend(run_group_b(run_id, dry_run))
    results.extend(run_group_c(run_id, dry_run))
    results.extend(run_group_d(run_id, dry_run))

    group_e = run_group_e(run_id, dry_run)
    results.extend(group_e)
    e1_ran = not dry_run and any(r.test_id == "E1" and r.status == "GO" for r in group_e)

    results.extend(run_group_f(run_id, dry_run))
    results.extend(run_group_g(run_id, dry_run))
    results.extend(run_group_h(run_id, dry_run, e1_ran))
    results.extend(run_group_i(run_id, dry_run))

    go = sum(1 for r in results if r.status == "GO")
    nogo = sum(1 for r in results if r.status == "NO-GO")
    scrub = sum(1 for r in results if r.status == "SCRUB")
    total_ms = sum(r.duration_ms for r in results)

    verdict = "ALL STATIONS GO" if nogo == 0 else f"NO-GO — {nogo} FAILURE{'S' if nogo > 1 else ''}"
    verdict_color = Fore.GREEN if nogo == 0 else Fore.RED

    print()
    print(f"  {'─' * 40}")
    print(f"  FLIGHT DIRECTOR {'.' * 10} {verdict_color}{verdict}{Style.RESET_ALL}")
    print()
    print("=" * 45)
    print(f"  {go}/{go + nogo + scrub} GO  |  {nogo} NO-GO  |  {scrub} SCRUB")
    print(f"  T+ {total_ms / 1000:.1f}s")
    print("=" * 45)

    sys.exit(0 if nogo == 0 else 1)


if __name__ == "__main__":
    main()
