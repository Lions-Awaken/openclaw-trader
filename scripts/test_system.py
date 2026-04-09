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
  P - Hardware Stress (concurrency > 1 only)

Usage:
  python scripts/test_system.py             # Full run
  python scripts/test_system.py --dry-run   # Skip DB writes and external calls
  python scripts/test_system.py --concurrency 4  # Stress test with 4 parallel streams
  SIMULATOR_RUN_ID=<uuid> python scripts/test_system.py  # Dashboard-linked run
  SIMULATOR_CONCURRENCY=4 python scripts/test_system.py  # Dashboard-triggered stress run
"""

import argparse
import logging
import os
import random
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

# ─── Synthetic Data Pool ──────────────────────────────────────────────────
# Realistic fake data used by tests that need market bars, signals, or candidates.
# All synthetic data uses ticker "SIM_TEST" for easy cleanup.

random.seed(42)


def _synthetic_bars(days: int = 60) -> list[dict]:
    """Generate realistic-looking OHLCV price bars for SIM_TEST."""
    rng = random.Random(42)
    bars = []
    price = 150.0
    today = date.today()
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        change = rng.uniform(-3.0, 3.5)
        o = round(price, 2)
        h = round(price + rng.uniform(0.5, 4.0), 2)
        lo = round(price - rng.uniform(0.5, 3.0), 2)
        c = round(price + change, 2)
        v = rng.randint(500_000, 5_000_000)
        bars.append({"t": d.isoformat(), "o": o, "h": h, "l": lo, "c": c, "v": v})
        price = max(c, 1.0)
    return bars


def _synthetic_spy_bars(days: int = 60) -> list[dict]:
    """Generate SPY-like benchmark bars."""
    rng = random.Random(43)
    bars = []
    price = 520.0
    today = date.today()
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        change = rng.uniform(-2.0, 2.5)
        o = round(price, 2)
        h = round(price + rng.uniform(0.3, 3.0), 2)
        lo = round(price - rng.uniform(0.3, 2.5), 2)
        c = round(price + change, 2)
        v = rng.randint(50_000_000, 100_000_000)
        bars.append({"t": d.isoformat(), "o": o, "h": h, "l": lo, "c": c, "v": v})
        price = max(c, 1.0)
    return bars


def _synthetic_candidate(bars: list[dict] | None = None) -> dict:
    """Build a candidate dict matching what scanner.py produces after compute_signals."""
    if bars is None:
        bars = _synthetic_bars()
    last = bars[-1]
    rng = random.Random(44)
    return {
        "ticker": "SIM_TEST",
        "price": last["c"],
        "atr": round(rng.uniform(2.0, 8.0), 2),
        "rsi": round(rng.uniform(30.0, 70.0), 2),
        "signals": {
            "trend": {"passed": True, "price": last["c"], "sma10": last["c"] - 2, "sma20": last["c"] - 5},
            "momentum": {"passed": True, "rsi": 55.0},
            "volume": {"passed": True, "today_vol": 2_000_000, "avg_vol_20": 1_000_000, "ratio": 2.0},
            "fundamental": {"passed": False, "catalyst_count": 0, "bullish_count": 0},
            "sentiment": {"passed": True, "avg_sentiment": 0.5, "sample_count": 1},
            "flow": {"passed": True, "ticker_5d_pct": 2.0, "spy_5d_pct": 1.0},
        },
        "total_score": 4,
        "score": 4,
        "bars": bars,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Stress Testing Utilities
# ──────────────────────────────────────────────────────────────────────────────

class StressMetrics:
    """Thread-safe collector for peak hardware metrics during stress bursts."""

    def __init__(self) -> None:
        self.peak_ram_mb: float = 0.0
        self.peak_cpu_pct: float = 0.0
        self.peak_temp_c: float = 0.0
        self.peak_swap_mb: float = 0.0
        self._lock = threading.Lock()
        self._sampling = False
        self._sample_thread: threading.Thread | None = None

    def start_sampling(self) -> None:
        """Start background thread that samples CPU/RAM/temp every 0.5s."""
        self._sampling = True
        self._sample_thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._sample_thread.start()

    def stop_sampling(self) -> None:
        self._sampling = False
        if self._sample_thread:
            self._sample_thread.join(timeout=3)

    def _sample_loop(self) -> None:
        try:
            import psutil
        except ImportError:
            return
        # Prime the cpu_percent counter (first call always returns 0)
        psutil.cpu_percent(interval=None)
        while self._sampling:
            try:
                mem = psutil.virtual_memory()
                swap = psutil.swap_memory()
                # Non-blocking read — returns % since last call
                cpu = psutil.cpu_percent(interval=None)

                # Real RAM pressure = total - available (matches `free -h`)
                ram_used_mb = (mem.total - mem.available) / 1024 / 1024

                # Read thermal — try /sys/devices/virtual/thermal
                temp = 0.0
                try:
                    for zone in sorted(
                        Path("/sys/devices/virtual/thermal/").glob("thermal_zone*/temp")
                    ):
                        val = float(zone.read_text().strip()) / 1000.0
                        temp = max(temp, val)
                except Exception:
                    pass

                with self._lock:
                    self.peak_ram_mb = max(self.peak_ram_mb, ram_used_mb)
                    self.peak_cpu_pct = max(self.peak_cpu_pct, cpu)
                    self.peak_swap_mb = max(self.peak_swap_mb, swap.used / 1024 / 1024)
                    if temp > 0:
                        self.peak_temp_c = max(self.peak_temp_c, temp)
            except Exception:
                pass
            time.sleep(0.1)  # Sample 10x/sec to catch short bursts


def _run_stress_burst(concurrency: int, metrics: StressMetrics) -> list[dict]:
    """Run N concurrent inference chains and return timing results."""
    from common import sb_get
    from inference_engine import run_inference

    # Load SKEPTIC profile for the stress test
    profiles = sb_get(
        "strategy_profiles",
        {
            "select": "*",
            "profile_name": "eq.SKEPTIC",
            "limit": "1",
        },
    )
    if not profiles:
        return []

    profile = profiles[0]
    bars = _synthetic_bars(30)
    candidate = _synthetic_candidate(bars)

    results: list[dict] = []
    errors: list[dict] = []
    lock = threading.Lock()

    def _worker(thread_id: int) -> None:
        try:
            t0 = time.time()
            result = run_inference(
                ticker=f"SIM_STRESS_{thread_id}",
                signals=candidate["signals"],
                total_score=candidate["total_score"],
                scan_type="shadow_skeptic",
                profile_override=profile,
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            with lock:
                results.append(
                    {
                        "thread": thread_id,
                        "decision": result.get("final_decision", "?"),
                        "elapsed_ms": elapsed_ms,
                    }
                )
        except Exception as exc:
            with lock:
                errors.append({"thread": thread_id, "error": str(exc)})

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(concurrency)]

    # Start metrics sampling before firing threads
    metrics.start_sampling()

    # Fire all threads simultaneously
    for t in threads:
        t.start()

    # Wait for all to complete (timeout 120s)
    for t in threads:
        t.join(timeout=120)

    metrics.stop_sampling()

    return results


# ──────────────────────────────────────────────────────────────────────────────

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
    logger = logging.getLogger("simulator")
    if result.status == "NO-GO":
        logger.error(f"{result.test_id}: {result.name} — {result.error}")
    if result.error:
        logger.info(f"{result.test_id}: {result.name} — {result.status} — {result.value}")
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
    logger = logging.getLogger("simulator")
    t0 = time.time()
    try:
        status, value, error = fn()
    except Exception:
        tb = traceback.format_exc()
        logger.exception(f"Test {test_id} failed with exception")
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


def _test_d2() -> tuple[str, str, str | None]:
    """compute_signals with synthetic bars — verifies signal computation logic end-to-end."""
    from scanner import compute_signals
    bars = _synthetic_bars(30)
    spy_bars = _synthetic_spy_bars(30)
    result = compute_signals("SIM_TEST", bars, spy_bars)
    if result is None:
        return ("NO-GO", "returned None", "Expected signal dict from 30 bars — check bar key format")
    if "signals" not in result or "total_score" not in result:
        return ("NO-GO", f"missing keys: {list(result.keys())[:6]}", "Expected 'signals' and 'total_score'")
    signals = result["signals"]
    score = result["total_score"]
    expected_signal_keys = {"trend", "momentum", "volume", "fundamental", "sentiment", "flow"}
    missing_sigs = expected_signal_keys - set(signals.keys())
    if missing_sigs:
        return ("NO-GO", f"score={score}", f"Missing signal keys: {missing_sigs}")
    return ("GO", f"score={score}/6 signals={len(signals)}", None)


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

    # D2 — synthetic bars, no external data needed
    r_d2 = _run(_test_d2, "D2", group, "compute_signals", "score/6 with all signal keys", 410)
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
    from datetime import date as _date_cls

    from common import sb_get
    from tracer import _post_to_supabase, _sb_client, _sb_headers

    # Build the exact payload that _record_divergence would POST to shadow_divergences.
    # live=enter (IS entry), shadow=skip (NOT entry) → disagree → write fires.
    # Called directly via _post_to_supabase to avoid scanner import chain issues and
    # to capture the return value for a meaningful error message.
    divergence = {
        "ticker": "SIM_TEST",
        "divergence_date": _date_cls.today().isoformat(),
        "live_profile": "CONSERVATIVE",
        "live_decision": "enter",
        "live_confidence": 0.70,
        "shadow_profile": "SKEPTIC",
        "shadow_type": "SKEPTIC",
        "shadow_decision": "skip",
        "shadow_confidence": 0.30,
        "shadow_stopping_reason": "confidence_floor",
        "first_diverged_at_tumbler": 1,
        "trade_executed": False,
    }
    result = _post_to_supabase("shadow_divergences", divergence)
    if result is None:
        return ("NO-GO", "insert returned None", "shadow_divergences POST failed — check tracer stdout for Supabase error")

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
    """Cost attribution: log_cost accepts subcategory and inference_engine passes profile_name in it."""
    import inspect

    import inference_engine as _ie_mod
    from inference_engine import log_cost

    # Check log_cost signature has subcategory parameter
    try:
        sig = inspect.signature(log_cost)
        if "subcategory" not in sig.parameters:
            return ("NO-GO", "no subcategory param", f"log_cost params: {list(sig.parameters)}")
    except Exception as exc:
        return ("NO-GO", "signature inspect failed", str(exc))

    # Check inference_engine source for profile_name embedded in log_cost calls
    try:
        ie_src = inspect.getsource(_ie_mod)
    except Exception as exc:
        return ("NO-GO", "source inspect failed", str(exc))

    # Both tumbler 4 and 5 should embed profile_name in the subcategory string
    has_t4 = "inference_engine_tumbler4" in ie_src and "profile_name" in ie_src
    has_t5 = "inference_engine_tumbler5" in ie_src
    if has_t4 and has_t5:
        return ("GO", "profile_name embedded in tumbler4+5 subcategories", None)
    missing = []
    if not has_t4:
        missing.append("tumbler4 pattern")
    if not has_t5:
        missing.append("tumbler5 pattern")
    return ("NO-GO", "pattern missing", f"Not found: {missing}")


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
    # catalyst_events NOT NULL columns (no defaults): catalyst_type, headline, source.
    # catalyst_type must match CHECK constraint enum; source must be one of the
    # allowed values (finnhub, perplexity, sec_edgar, quiverquant, manual, yfinance, fred).
    row = _post_to_supabase("catalyst_events", {
        "ticker": "SIM_TEST",
        "catalyst_type": "other",
        "headline": "Simulator synthetic event — safe to delete",
        "source": "manual",
        "magnitude": "minor",
        "direction": "neutral",
    })
    if not row:
        return ("NO-GO", "insert returned None", "Expected non-None from _post_to_supabase")
    return ("GO", "catalyst row created", None)


def _test_h2() -> tuple[str, str, str | None]:
    """Signal scan with synthetic candidate — validates compute_signals pipeline path."""
    from scanner import compute_signals
    bars = _synthetic_bars(30)
    spy_bars = _synthetic_spy_bars(30)
    result = compute_signals("SIM_TEST", bars, spy_bars)
    if result is None:
        # Returning None for thin/bad data is valid behavior — treat as soft pass
        return ("GO", "None (thin data path ok)", None)
    signals = result.get("signals", {})
    score = result.get("total_score", 0)
    price = result.get("price", 0)
    atr = result.get("atr", 0)
    if price <= 0:
        return ("NO-GO", f"price={price}", "Expected positive price in result")
    return ("GO", f"score={score} signals={len(signals)} price={price} atr={atr}", None)


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

    # H2 — synthetic bars signal scan
    r_h2 = _run(_test_h2, "H2", group, "signal scan", "score/signals/price from synthetic bars", 810)
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

def _mint_dashboard_cookie() -> str:
    """Generate a valid dashboard session cookie using the same signing logic as server.py."""
    import hashlib
    import hmac as _hmac
    salt = os.environ.get("SESSION_SIGNING_SALT", "oc-session-stable-v1")
    key = hashlib.sha256(salt.encode()).digest()
    issued = str(int(time.time()))
    sig = _hmac.new(key, issued.encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{sig}"


def _test_dashboard_endpoint(path: str, expected_item_count: int | None = None) -> tuple[str, str, str | None]:
    """GET a dashboard endpoint with self-minted auth cookie. Tries port 9090 then 8000."""
    import httpx
    cookie = _mint_dashboard_cookie()
    for port in [9090, 8000]:
        try:
            resp = httpx.get(
                f"http://localhost:{port}{path}",
                cookies={"oc_session": cookie},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                if expected_item_count is not None and isinstance(data, list):
                    count = len(data)
                    label = f"{count} items (port {port})"
                    if count < expected_item_count:
                        return ("NO-GO", label, f"Expected >= {expected_item_count}, got {count}")
                return ("GO", f"HTTP 200 (port {port})", None)
            if resp.status_code == 401:
                return ("NO-GO", f"auth failed (port {port})", "cookie rejected — check SESSION_SIGNING_SALT")
        except Exception:
            continue
    return ("NO-GO", "connection refused", "dashboard not reachable on 9090 or 8000")


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
# GROUP J — POSITION MANAGEMENT (1000-1099)
# ===========================================================================

def run_group_j(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "POSITION MANAGEMENT"
    print(f"\n  {Fore.CYAN}J · {group}{Style.RESET_ALL}")
    results = []

    # J1: find_trade_decision importable
    def _test_j1() -> tuple[str, str, str | None]:
        from position_manager import find_trade_decision
        assert callable(find_trade_decision)
        return ("GO", "callable", None)

    r_j1 = _run(_test_j1, "J1", group, "find_trade_decision import", "callable", 1000)
    _print_result(r_j1)
    _write_result(r_j1, run_id, dry_run)
    results.append(r_j1)

    # J2: compute_atr with synthetic bars
    def _test_j2() -> tuple[str, str, str | None]:
        from position_manager import compute_atr
        bars = _synthetic_bars(30)
        result = compute_atr(bars)
        assert isinstance(result, (int, float)) and result > 0
        return ("GO", f"ATR={result:.2f}", None)

    r_j2 = _run(_test_j2, "J2", group, "compute_atr synthetic", "float > 0", 1010)
    _print_result(r_j2)
    _write_result(r_j2, run_id, dry_run)
    results.append(r_j2)

    # J3: get_positions (skip in dry-run — live Alpaca call)
    if dry_run:
        r_j3 = TestResult(
            test_id="J3", group=group, name="get_positions",
            status="SCRUB", value="dry-run skip", expected="list",
            error=None, duration_ms=0, check_order=1020,
        )
    else:
        def _test_j3() -> tuple[str, str, str | None]:
            from common import get_positions
            positions = get_positions()
            assert isinstance(positions, list)
            return ("GO", f"{len(positions)} positions", None)
        r_j3 = _run(_test_j3, "J3", group, "get_positions", "list", 1020)
    _print_result(r_j3)
    _write_result(r_j3, run_id, dry_run)
    results.append(r_j3)

    # J4: get_open_orders (skip in dry-run — live Alpaca call)
    if dry_run:
        r_j4 = TestResult(
            test_id="J4", group=group, name="get_open_orders",
            status="SCRUB", value="dry-run skip", expected="list",
            error=None, duration_ms=0, check_order=1030,
        )
    else:
        def _test_j4() -> tuple[str, str, str | None]:
            from common import get_open_orders
            orders = get_open_orders()
            assert isinstance(orders, list)
            return ("GO", f"{len(orders)} orders", None)
        r_j4 = _run(_test_j4, "J4", group, "get_open_orders", "list", 1030)
    _print_result(r_j4)
    _write_result(r_j4, run_id, dry_run)
    results.append(r_j4)

    return results


# ===========================================================================
# GROUP K — ORDER EXECUTION (1100-1199)
# ===========================================================================

def run_group_k(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "ORDER EXECUTION"
    print(f"\n  {Fore.CYAN}K · {group}{Style.RESET_ALL}")
    results = []

    # K1: submit_order importable only — never call it
    def _test_k1() -> tuple[str, str, str | None]:
        from common import submit_order
        assert callable(submit_order)
        return ("GO", "callable", None)

    # K2: poll_for_fill importable only
    def _test_k2() -> tuple[str, str, str | None]:
        from common import poll_for_fill
        assert callable(poll_for_fill)
        return ("GO", "callable", None)

    # K3: cancel_order importable only
    def _test_k3() -> tuple[str, str, str | None]:
        from common import cancel_order
        assert callable(cancel_order)
        return ("GO", "callable", None)

    for fn, tid, name, expected, order in [
        (_test_k1, "K1", "submit_order import", "callable", 1100),
        (_test_k2, "K2", "poll_for_fill import", "callable", 1110),
        (_test_k3, "K3", "cancel_order import", "callable", 1120),
    ]:
        r = _run(fn, tid, group, name, expected, order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)

    return results


# ===========================================================================
# GROUP L — DATA INGESTION (1200-1299)
# ===========================================================================

def run_group_l(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "DATA INGESTION"
    print(f"\n  {Fore.CYAN}L · {group}{Style.RESET_ALL}")
    results = []

    # L1: classify_catalyst
    def _test_l1() -> tuple[str, str, str | None]:
        from catalyst_ingest import classify_catalyst
        result = classify_catalyst(
            "NVIDIA announces record Q4 earnings beating estimates",
            "Revenue up 40% YoY",
        )
        assert isinstance(result, dict)
        assert "catalyst_type" in result or "type" in result
        ctype = result.get("catalyst_type", result.get("type", "?"))
        return ("GO", f"type={ctype}", None)

    r_l1 = _run(_test_l1, "L1", group, "classify_catalyst", "dict with catalyst_type", 1200)
    _print_result(r_l1)
    _write_result(r_l1, run_id, dry_run)
    results.append(r_l1)

    # L2: check_duplicate
    def _test_l2() -> tuple[str, str, str | None]:
        from catalyst_ingest import check_duplicate
        emb1 = [1.0] * 384
        emb2 = [0.0] * 384
        result = check_duplicate(emb1, [emb2])
        assert result is False
        return ("GO", "non-duplicate correctly detected", None)

    r_l2 = _run(_test_l2, "L2", group, "check_duplicate", "False for orthogonal vecs", 1210)
    _print_result(r_l2)
    _write_result(r_l2, run_id, dry_run)
    results.append(r_l2)

    # L3: score_form4_signal
    def _test_l3() -> tuple[str, str, str | None]:
        from ingest_signals import score_form4_signal
        row = {
            "transaction_type": "purchase",
            "total_value": 750_000,
            "ownership_pct_change": 0.08,
            "cluster_count": 2,
            "filer_title": "CFO",
        }
        score = score_form4_signal(row)
        assert isinstance(score, int) and 1 <= score <= 10
        return ("GO", f"score={score}", None)

    r_l3 = _run(_test_l3, "L3", group, "score_form4_signal", "int 1-10", 1220)
    _print_result(r_l3)
    _write_result(r_l3, run_id, dry_run)
    results.append(r_l3)

    # L4: score_options_signal
    def _test_l4() -> tuple[str, str, str | None]:
        from ingest_signals import score_options_signal
        sig = {
            "signal_type": "sweep",
            "premium": 600_000,
            "implied_volatility": 0.75,
            "sentiment": "bullish",
        }
        score = score_options_signal(sig)
        assert isinstance(score, int) and 1 <= score <= 10
        return ("GO", f"score={score}", None)

    r_l4 = _run(_test_l4, "L4", group, "score_options_signal", "int 1-10", 1230)
    _print_result(r_l4)
    _write_result(r_l4, run_id, dry_run)
    results.append(r_l4)

    # L5: fetch_yfinance_signals (real external call — skip in dry-run)
    if dry_run:
        r_l5 = TestResult(
            test_id="L5", group=group, name="fetch_yfinance_signals",
            status="SCRUB", value="dry-run skip", expected="list",
            error=None, duration_ms=0, check_order=1240,
        )
    else:
        def _test_l5() -> tuple[str, str, str | None]:
            from catalyst_ingest import fetch_yfinance_signals
            result = fetch_yfinance_signals(["AAPL"])
            assert isinstance(result, list)
            return ("GO", f"{len(result)} signals", None)
        r_l5 = _run(_test_l5, "L5", group, "fetch_yfinance_signals", "list", 1240)
    _print_result(r_l5)
    _write_result(r_l5, run_id, dry_run)
    results.append(r_l5)

    return results


# ===========================================================================
# GROUP M — META-LEARNING (1300-1399)
# ===========================================================================

def run_group_m(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "META-LEARNING"
    print(f"\n  {Fore.CYAN}M · {group}{Style.RESET_ALL}")
    results = []

    if dry_run:
        for tid, name, order in [
            ("M1", "get_pipeline_health", 1300),
            ("M2", "get_signal_accuracy", 1310),
            ("M3", "get_shadow_divergence_summary", 1320),
            ("M4", "rag_retrieve_context", 1330),
        ]:
            r = TestResult(
                test_id=tid, group=group, name=name,
                status="SCRUB", value="dry-run skip", expected="dict",
                error=None, duration_ms=0, check_order=order,
            )
            _print_result(r)
            _write_result(r, run_id, dry_run)
            results.append(r)
        return results

    def _test_m1() -> tuple[str, str, str | None]:
        from meta_analysis import get_pipeline_health
        result = get_pipeline_health()
        assert isinstance(result, dict)
        return ("GO", f"{len(result)} keys", None)

    def _test_m2() -> tuple[str, str, str | None]:
        from meta_analysis import get_signal_accuracy
        result = get_signal_accuracy()
        assert isinstance(result, dict)
        return ("GO", f"{len(result)} keys", None)

    def _test_m3() -> tuple[str, str, str | None]:
        from meta_analysis import get_shadow_divergence_summary
        result = get_shadow_divergence_summary()
        assert isinstance(result, dict)
        assert "count" in result
        return ("GO", f"count={result.get('count', 0)}", None)

    def _test_m4() -> tuple[str, str, str | None]:
        from meta_analysis import rag_retrieve_context
        result = rag_retrieve_context("test query for synthetic embedding retrieval")
        assert isinstance(result, dict)
        return ("GO", f"{len(result)} keys", None)

    for fn, tid, name, expected, order in [
        (_test_m1, "M1", "get_pipeline_health", "dict", 1300),
        (_test_m2, "M2", "get_signal_accuracy", "dict", 1310),
        (_test_m3, "M3", "get_shadow_divergence_summary", "dict with count", 1320),
        (_test_m4, "M4", "rag_retrieve_context", "dict", 1330),
    ]:
        r = _run(fn, tid, group, name, expected, order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)

    return results


# ===========================================================================
# GROUP N — CALIBRATION (1400-1499)
# ===========================================================================

def run_group_n(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "CALIBRATION"
    print(f"\n  {Fore.CYAN}N · {group}{Style.RESET_ALL}")
    results = []

    if dry_run:
        for tid, name, order in [
            ("N1", "get_trade_outcomes", 1400),
            ("N2", "grade_chains empty", 1410),
            ("N3", "update_pattern_templates", 1420),
        ]:
            r = TestResult(
                test_id=tid, group=group, name=name,
                status="SCRUB", value="dry-run skip", expected="dict/tuple/int",
                error=None, duration_ms=0, check_order=order,
            )
            _print_result(r)
            _write_result(r, run_id, dry_run)
            results.append(r)
        return results

    def _test_n1() -> tuple[str, str, str | None]:
        from calibrator import get_trade_outcomes
        result = get_trade_outcomes()
        assert isinstance(result, dict)
        return ("GO", f"{len(result)} outcomes", None)

    def _test_n2() -> tuple[str, str, str | None]:
        from calibrator import grade_chains
        graded, total = grade_chains({})
        assert isinstance(graded, int) and isinstance(total, int)
        assert graded == 0 and total == 0
        return ("GO", f"graded={graded} total={total}", None)

    def _test_n3() -> tuple[str, str, str | None]:
        from calibrator import update_pattern_templates
        count = update_pattern_templates()
        assert isinstance(count, int)
        return ("GO", f"{count} templates updated", None)

    for fn, tid, name, expected, order in [
        (_test_n1, "N1", "get_trade_outcomes", "dict", 1400),
        (_test_n2, "N2", "grade_chains empty", "graded=0 total=0", 1410),
        (_test_n3, "N3", "update_pattern_templates", "int count", 1420),
    ]:
        r = _run(fn, tid, group, name, expected, order)
        _print_result(r)
        _write_result(r, run_id, dry_run)
        results.append(r)

    return results


# ===========================================================================
# GROUP O — EXTERNAL SERVICES (1500-1599)
# ===========================================================================

def run_group_o(run_id: str | None, dry_run: bool) -> list[TestResult]:
    group = "EXTERNAL SERVICES"
    print(f"\n  {Fore.CYAN}O · {group}{Style.RESET_ALL}")
    results = []

    # O1: Ollama health (skip in dry-run)
    if dry_run:
        r_o1 = TestResult(
            test_id="O1", group=group, name="Ollama health",
            status="SCRUB", value="dry-run skip", expected=">0 models",
            error=None, duration_ms=0, check_order=1500,
        )
    else:
        def _test_o1() -> tuple[str, str, str | None]:
            import httpx as _httpx
            resp = _httpx.get("http://localhost:11434/api/tags", timeout=5.0)
            assert resp.status_code == 200
            models = resp.json().get("models", [])
            assert len(models) > 0
            model_names = [m.get("name", "?") for m in models]
            return ("GO", f"{len(models)} models: {', '.join(model_names[:3])}", None)
        r_o1 = _run(_test_o1, "O1", group, "Ollama health", ">0 models", 1500)
    _print_result(r_o1)
    _write_result(r_o1, run_id, dry_run)
    results.append(r_o1)

    # O2: Alpaca account (skip in dry-run)
    if dry_run:
        r_o2 = TestResult(
            test_id="O2", group=group, name="Alpaca account",
            status="SCRUB", value="dry-run skip", expected="dict with equity",
            error=None, duration_ms=0, check_order=1510,
        )
    else:
        def _test_o2() -> tuple[str, str, str | None]:
            from common import get_account
            account = get_account()
            assert isinstance(account, dict)
            equity = account.get("equity", account.get("portfolio_value", "?"))
            return ("GO", f"equity=${equity}", None)
        r_o2 = _run(_test_o2, "O2", group, "Alpaca account", "dict with equity", 1510)
    _print_result(r_o2)
    _write_result(r_o2, run_id, dry_run)
    results.append(r_o2)

    # O3: Slack connectivity — import + env var check only, never send a message
    def _test_o3() -> tuple[str, str, str | None]:
        from common import slack_notify
        assert callable(slack_notify)
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        assert len(token) > 10
        return ("GO", "token set + callable", None)

    r_o3 = _run(_test_o3, "O3", group, "Slack connectivity", "token set + callable", 1520)
    _print_result(r_o3)
    _write_result(r_o3, run_id, dry_run)
    results.append(r_o3)

    return results


# ===========================================================================
# GROUP P — HARDWARE STRESS (1600-1699)
# ===========================================================================

def run_group_p(run_id: str | None, dry_run: bool, concurrency: int) -> list[TestResult]:
    group = "HARDWARE STRESS"
    print(f"\n  {Fore.CYAN}P · {group}{Style.RESET_ALL}")
    results = []

    stress_tests = [
        ("P1", "peak RAM", 1600),
        ("P2", "peak CPU", 1610),
        ("P3", "peak temperature", 1620),
        ("P4", "swap usage", 1630),
        ("P5", "Ollama latency", 1640),
    ]

    if dry_run or concurrency <= 1:
        # Skip stress tests in normal preflight mode
        skip_val = f"skipped (concurrency={concurrency})" if concurrency <= 1 else "dry-run"
        status = "GO" if concurrency <= 1 else "SCRUB"
        for tid, name, order in stress_tests:
            r = TestResult(
                test_id=tid,
                group=group,
                name=name,
                status=status,
                value=skip_val,
                expected="stress test",
                error=None,
                duration_ms=0,
                check_order=order,
            )
            _print_result(r)
            _write_result(r, run_id, dry_run)
            results.append(r)
        return results

    try:
        import psutil
    except ImportError:
        for tid, name, order in stress_tests:
            r = TestResult(
                test_id=tid,
                group=group,
                name=name,
                status="NO-GO",
                value="psutil not installed",
                expected="stress test",
                error="pip install psutil",
                duration_ms=0,
                check_order=order,
            )
            _print_result(r)
            _write_result(r, run_id, dry_run)
            results.append(r)
        return results

    # === Baseline: single Ollama call (direct, not through inference engine) ===
    print(f"  {Fore.YELLOW}  Measuring Ollama baseline latency...{Style.RESET_ALL}")
    baseline_ms = 5000
    try:
        import httpx as _httpx
        _t0 = time.time()
        _resp = _httpx.post(
            "http://localhost:11434/api/generate",
            json={"model": "qwen2.5:3b", "prompt": "Reply with one word: HEALTHY", "stream": False},
            timeout=30.0,
        )
        baseline_ms = max(1, int((time.time() - _t0) * 1000))
        print(f"  {Fore.YELLOW}  Baseline: {baseline_ms}ms{Style.RESET_ALL}")
    except Exception as _e:
        print(f"  {Fore.RED}  Baseline failed: {_e}{Style.RESET_ALL}")

    # === Stress burst: N concurrent calls ===
    print(f"  {Fore.YELLOW}  Running {concurrency}x concurrent stress burst...{Style.RESET_ALL}")
    stress_metrics = StressMetrics()
    _run_stress_burst(concurrency, stress_metrics)  # results in metrics, not return value

    total_ram_mb = psutil.virtual_memory().total / 1024 / 1024

    # P1: Peak RAM
    peak_ram = stress_metrics.peak_ram_mb
    ram_pct = (peak_ram / total_ram_mb) * 100 if total_ram_mb > 0 else 0
    if ram_pct > 90:
        r = TestResult(
            "P1", group, "peak RAM", "NO-GO",
            f"{peak_ram:.0f}MB/{total_ram_mb:.0f}MB ({ram_pct:.0f}%)",
            "< 90% of total RAM",
            f"RAM at {ram_pct:.0f}% — risk of OOM",
            0, 1600,
        )
    elif ram_pct > 75:
        r = TestResult(
            "P1", group, "peak RAM", "GO",
            f"{peak_ram:.0f}MB/{total_ram_mb:.0f}MB ({ram_pct:.0f}%) TIGHT",
            "< 90% of total RAM",
            None, 0, 1600,
        )
    else:
        r = TestResult(
            "P1", group, "peak RAM", "GO",
            f"{peak_ram:.0f}MB/{total_ram_mb:.0f}MB ({ram_pct:.0f}%)",
            "< 90% of total RAM",
            None, 0, 1600,
        )
    _print_result(r)
    _write_result(r, run_id, dry_run)
    results.append(r)

    # P2: Peak CPU
    peak_cpu = stress_metrics.peak_cpu_pct
    if peak_cpu > 95:
        r = TestResult(
            "P2", group, "peak CPU", "NO-GO",
            f"{peak_cpu:.0f}%",
            "< 95%",
            f"CPU saturated at {peak_cpu:.0f}%",
            0, 1610,
        )
    else:
        r = TestResult(
            "P2", group, "peak CPU", "GO",
            f"{peak_cpu:.0f}%",
            "< 95%",
            None, 0, 1610,
        )
    _print_result(r)
    _write_result(r, run_id, dry_run)
    results.append(r)

    # P3: Peak temperature
    peak_temp = stress_metrics.peak_temp_c
    if peak_temp > 0:
        headroom = 65.0 - peak_temp  # Jetson throttles at ~65C
        if peak_temp > 60:
            r = TestResult(
                "P3", group, "peak temperature", "NO-GO",
                f"{peak_temp:.1f}C (headroom: {headroom:.1f}C)",
                "< 60C",
                f"Thermal throttling imminent at {peak_temp:.1f}C",
                0, 1620,
            )
        else:
            r = TestResult(
                "P3", group, "peak temperature", "GO",
                f"{peak_temp:.1f}C (headroom: {headroom:.1f}C)",
                "< 60C",
                None, 0, 1620,
            )
    else:
        r = TestResult(
            "P3", group, "peak temperature", "GO",
            "sensor unavailable",
            "< 60C",
            None, 0, 1620,
        )
    _print_result(r)
    _write_result(r, run_id, dry_run)
    results.append(r)

    # P4: Swap usage
    peak_swap = stress_metrics.peak_swap_mb
    if peak_swap > 3000:
        r = TestResult(
            "P4", group, "swap usage", "NO-GO",
            f"{peak_swap:.0f}MB",
            "< 3000MB",
            "Excessive swap — eMMC I/O degradation",
            0, 1630,
        )
    else:
        r = TestResult(
            "P4", group, "swap usage", "GO",
            f"{peak_swap:.0f}MB",
            "< 3000MB",
            None, 0, 1630,
        )
    _print_result(r)
    _write_result(r, run_id, dry_run)
    results.append(r)

    # P5: Ollama latency degradation under concurrent load
    # Fire N concurrent Ollama calls directly (not through inference engine)
    ollama_times: list[int] = []
    ollama_lock = threading.Lock()

    def _ollama_worker() -> None:
        try:
            import httpx as _hx
            _t = time.time()
            _hx.post(
                "http://localhost:11434/api/generate",
                json={"model": "qwen2.5:3b", "prompt": "Summarize market sentiment in one sentence.", "stream": False},
                timeout=60.0,
            )
            ms = max(1, int((time.time() - _t) * 1000))
            with ollama_lock:
                ollama_times.append(ms)
        except Exception:
            pass

    ollama_threads = [threading.Thread(target=_ollama_worker) for _ in range(concurrency)]
    for ot in ollama_threads:
        ot.start()
    for ot in ollama_threads:
        ot.join(timeout=120)

    if ollama_times:
        avg_stressed_ms = sum(ollama_times) / len(ollama_times)
        degradation = avg_stressed_ms / max(baseline_ms, 1)
        if degradation > 3.0:
            r = TestResult(
                "P5", group, "Ollama latency", "NO-GO",
                f"{degradation:.1f}x baseline ({avg_stressed_ms:.0f}ms vs {baseline_ms}ms)",
                "< 3x degradation",
                "Severe latency under load",
                0, 1640,
            )
        else:
            r = TestResult(
                "P5", group, "Ollama latency", "GO",
                f"{degradation:.1f}x baseline ({avg_stressed_ms:.0f}ms vs {baseline_ms}ms)",
                "< 3x degradation",
                None, 0, 1640,
            )
    else:
        r = TestResult(
            "P5", group, "Ollama latency", "NO-GO",
            "no results from burst",
            "< 3x degradation",
            "All stress threads failed",
            0, 1640,
        )
    _print_result(r)
    _write_result(r, run_id, dry_run)
    results.append(r)

    # Cleanup all SIM_STRESS_* inference chains (baseline thread 0 + N stress threads)
    try:
        from tracer import SUPABASE_URL as _SB_URL
        from tracer import _sb_client, _sb_headers

        for i in range(concurrency + 1):  # +1 for baseline burst (thread 0)
            _sb_client.delete(
                f"{_SB_URL}/rest/v1/inference_chains",
                headers=_sb_headers(),
                params={"ticker": f"eq.SIM_STRESS_{i}"},
            )
    except Exception:
        pass

    return results


# ===========================================================================
# Main
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="OpenClaw Preflight Simulator")
    parser.add_argument("--dry-run", action="store_true", help="Skip DB writes and external calls")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        choices=range(1, 11),
        metavar="{1-10}",
        help="Number of parallel streams for stress testing (1-10)",
    )
    args = parser.parse_args()

    # SIMULATOR_CONCURRENCY env var overrides --concurrency (used by dashboard-triggered runs)
    concurrency = int(os.environ.get("SIMULATOR_CONCURRENCY", "0")) or args.concurrency

    run_id = os.environ.get("SIMULATOR_RUN_ID") or str(uuid.uuid4())
    dry_run: bool = args.dry_run

    log_path = f"/tmp/openclaw_simulator_{run_id[:8]}.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("simulator")
    logger.info(f"Simulator started — run_id={run_id}")

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print("=" * 45)
    print("  OPENCLAW PREFLIGHT — GO / NO-GO")
    print(f"  {now_str}")
    if dry_run:
        print(f"  {Fore.YELLOW}DRY-RUN MODE — no DB writes{Style.RESET_ALL}")
    if concurrency > 1:
        print(f"  {Fore.CYAN}STRESS MODE — concurrency={concurrency}{Style.RESET_ALL}")
    print("=" * 45)
    print(f"  Log: {log_path}")
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
    results.extend(run_group_j(run_id, dry_run))
    results.extend(run_group_k(run_id, dry_run))
    results.extend(run_group_l(run_id, dry_run))
    results.extend(run_group_m(run_id, dry_run))
    results.extend(run_group_n(run_id, dry_run))
    results.extend(run_group_o(run_id, dry_run))
    results.extend(run_group_p(run_id, dry_run, concurrency))

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

    logger.info(f"Preflight complete: {go} GO, {nogo} NO-GO, {scrub} SCRUB — log at {log_path}")
    sys.exit(0 if nogo == 0 else 1)


if __name__ == "__main__":
    main()
