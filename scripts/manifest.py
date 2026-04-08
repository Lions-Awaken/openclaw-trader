"""
OpenClaw Function Manifest — canonical registry of every scheduled/triggered function.

This file is the SINGLE SOURCE OF TRUTH for what the system should be doing.
Health checks diff this manifest against pipeline_runs to detect silent failures.

RULE: When you add a new script, cron entry, or pipeline function, you MUST add
an entry here. See CLAUDE.md § Function Manifest for the convention.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ManifestEntry:
    """One executable function in the OpenClaw system."""

    name: str  # Human-readable name
    script: str  # Relative path from project root
    pipeline_name: str  # Value in pipeline_runs.pipeline_name
    schedule: str  # Cron expression or trigger description
    schedule_desc: str  # Human-readable schedule
    expected_steps: list[str] = field(default_factory=list)  # Key step_names to verify
    criticality: str = "high"  # high | medium | low
    writes_to_pipeline_runs: bool = True  # False for health_check (writes to system_health)
    dependencies: list[str] = field(default_factory=list)  # Other manifest entry names
    output_validator: Callable[[dict], bool] | None = None
    # Takes a pipeline_runs.output_snapshot dict, returns True if output looks healthy.
    # None means no validation (always passes).
    freshness_hours: int | None = None
    # Max hours since last pipeline_run before considered stale.
    # None means no freshness check.
    estimated_claude_cost: float = 0.0
    # Expected Claude API cost per run in USD.


# ─── Output Validators ────────────────────────────────────────────────────────


def _valid_catalyst(snap: dict) -> bool:
    """Catalyst ingest should produce events."""
    return snap.get("total_inserted", 0) > 5


def _valid_scanner(snap: dict) -> bool:
    """Scanner should find candidates."""
    return snap.get("candidates", 0) > 0


def _valid_meta(snap: dict) -> bool:
    """Meta reflection should not be empty/error."""
    text = str(snap)
    return "Unable to assess" not in text and len(text) > 20


def _valid_heartbeat(snap: dict) -> bool:
    """Heartbeat should complete."""
    return True  # presence of a run is sufficient


def _valid_calibrator(snap: dict) -> bool:
    """Calibrator should report grading results."""
    return True  # presence of a run is sufficient


def _valid_ingest(snap: dict) -> bool:
    """Ingest scripts — any completion is valid (tables may be empty initially)."""
    return True


# ─── Scheduled Functions (cron on ridley, all times PDT) ─────────────────────

MANIFEST: list[ManifestEntry] = [
    ManifestEntry(
        name="health_check",
        script="scripts/health_check.py",
        pipeline_name="health_check",
        schedule="0 5 * * 1-5",
        schedule_desc="5:00 AM PDT weekdays",
        expected_steps=[],
        criticality="high",
        writes_to_pipeline_runs=False,  # writes to system_health table
        output_validator=None,
        freshness_hours=26,
        estimated_claude_cost=0.0,
    ),
    ManifestEntry(
        name="catalyst_ingest_morning",
        script="scripts/catalyst_ingest.py",
        pipeline_name="catalyst_ingest",
        schedule="30 5 * * 1-5",
        schedule_desc="5:30 AM PDT weekdays",
        expected_steps=[
            "catalysts:fetch_finnhub",
            "catalysts:fetch_sec_edgar",
            "catalysts:fetch_quiverquant",
            "catalysts:fetch_yfinance",
            "catalysts:fetch_fred",
        ],
        criticality="high",
        output_validator=_valid_catalyst,
        freshness_hours=26,
        estimated_claude_cost=0.0,
    ),
    ManifestEntry(
        name="catalyst_ingest_midday",
        script="scripts/catalyst_ingest.py",
        pipeline_name="catalyst_ingest",
        schedule="0 9 * * 1-5",
        schedule_desc="9:00 AM PDT weekdays",
        expected_steps=[
            "catalysts:fetch_finnhub",
            "catalysts:fetch_sec_edgar",
            "catalysts:fetch_quiverquant",
            "catalysts:fetch_yfinance",
            "catalysts:fetch_fred",
        ],
        criticality="high",
        output_validator=_valid_catalyst,
        freshness_hours=26,
        estimated_claude_cost=0.0,
    ),
    ManifestEntry(
        name="catalyst_ingest_afternoon",
        script="scripts/catalyst_ingest.py",
        pipeline_name="catalyst_ingest",
        schedule="50 12 * * 1-5",
        schedule_desc="12:50 PM PDT weekdays",
        expected_steps=[
            "catalysts:fetch_finnhub",
            "catalysts:fetch_sec_edgar",
            "catalysts:fetch_quiverquant",
            "catalysts:fetch_yfinance",
            "catalysts:fetch_fred",
        ],
        criticality="high",
        output_validator=_valid_catalyst,
        freshness_hours=26,
        estimated_claude_cost=0.0,
    ),
    ManifestEntry(
        name="ingest_signals_form4",
        script="scripts/ingest_signals.py",
        pipeline_name="ingest",
        schedule="0 6 * * 1-5",
        schedule_desc="6:00 AM PDT weekdays",
        expected_steps=["root"],
        criticality="medium",
        dependencies=["catalyst_ingest_morning"],
        output_validator=_valid_ingest,
        freshness_hours=26,
        estimated_claude_cost=0.0,
    ),
    ManifestEntry(
        name="scanner_morning",
        script="scripts/scanner.py",
        pipeline_name="scanner",
        schedule="35 6 * * 1-5",
        schedule_desc="6:35 AM PDT weekdays",
        expected_steps=[
            "pipeline:signal_scan",
            "pipeline:signal_enrichment",
            "pipeline:inference",
            "pipeline:shadow_inference",
            "pipeline:execution",
        ],
        criticality="high",
        dependencies=["catalyst_ingest_morning"],
        output_validator=_valid_scanner,
        freshness_hours=26,
        estimated_claude_cost=0.03,
    ),
    ManifestEntry(
        name="ingest_signals_options",
        script="scripts/ingest_signals.py",
        pipeline_name="ingest",
        schedule="0 7 * * 1-5",
        schedule_desc="7:00 AM PDT weekdays",
        expected_steps=["root"],
        criticality="medium",
        output_validator=_valid_ingest,
        freshness_hours=26,
        estimated_claude_cost=0.0,
    ),
    ManifestEntry(
        name="scanner_midday",
        script="scripts/scanner.py",
        pipeline_name="scanner",
        schedule="30 9 * * 1-5",
        schedule_desc="9:30 AM PDT weekdays",
        expected_steps=[
            "pipeline:signal_scan",
            "pipeline:signal_enrichment",
            "pipeline:inference",
            "pipeline:shadow_inference",
            "pipeline:execution",
        ],
        criticality="high",
        dependencies=["catalyst_ingest_midday"],
        output_validator=_valid_scanner,
        freshness_hours=26,
        estimated_claude_cost=0.03,
    ),
    ManifestEntry(
        name="position_manager",
        script="scripts/position_manager.py",
        pipeline_name="position_manager",
        schedule="0,30 6-11 * * 1-5",
        schedule_desc="Every 30m 6:00-11:30 AM + 12:00 + 12:45 PDT weekdays",
        expected_steps=["root"],
        criticality="high",
        output_validator=None,
        freshness_hours=2,
        estimated_claude_cost=0.0,
    ),
    ManifestEntry(
        name="meta_daily",
        script="scripts/meta_analysis.py",
        pipeline_name="meta_daily",
        schedule="30 13 * * 1-5",
        schedule_desc="1:30 PM PDT weekdays",
        expected_steps=[
            "meta:gather_pipeline_health",
            "meta:gather_signal_accuracy",
            "meta:gather_trades",
            "meta:gather_chain_analysis",
            "meta:gather_catalysts",
            "meta:gather_shadow_divergences",
            "meta:rag_retrieve",
            "meta:generate_reflection",
            "meta:store_reflection",
        ],
        criticality="high",
        dependencies=["scanner_midday"],
        output_validator=_valid_meta,
        freshness_hours=26,
        estimated_claude_cost=0.02,
    ),
    ManifestEntry(
        name="meta_weekly",
        script="scripts/meta_analysis.py",
        pipeline_name="meta_weekly",
        schedule="0 16 * * 0",
        schedule_desc="4:00 PM PDT Sundays",
        expected_steps=["root"],
        criticality="medium",
        output_validator=_valid_meta,
        freshness_hours=170,
        estimated_claude_cost=0.02,
    ),
    ManifestEntry(
        name="calibrator",
        script="scripts/calibrator.py",
        pipeline_name="calibrator",
        schedule="30 16 * * 0",
        schedule_desc="4:30 PM PDT Sundays",
        expected_steps=["meta:grade_chains", "meta:update_pattern_templates", "meta:grade_shadows"],
        criticality="medium",
        dependencies=["meta_weekly"],
        output_validator=_valid_calibrator,
        freshness_hours=170,
        estimated_claude_cost=0.0,
    ),
    ManifestEntry(
        name="heartbeat",
        script="scripts/heartbeat.py",
        pipeline_name="heartbeat",
        schedule="*/5 * * * *",
        schedule_desc="Every 5 minutes",
        expected_steps=["sitrep:check_ollama", "sitrep:check_tumbler", "sitrep:update_heartbeat"],
        criticality="low",
        output_validator=_valid_heartbeat,
        freshness_hours=1,
        estimated_claude_cost=0.0,
    ),
]

# ─── Event-Triggered Functions (not cron, fired by other scripts) ────────────

EVENT_TRIGGERED: list[ManifestEntry] = [
    ManifestEntry(
        name="post_trade_analysis",
        script="scripts/post_trade_analysis.py",
        pipeline_name="post_trade_analysis",
        schedule="on_trade_close",
        schedule_desc="Triggered when a trade closes",
        expected_steps=["root"],
        criticality="medium",
    ),
    ManifestEntry(
        name="legislative_calendar",
        script="scripts/legislative_calendar.py",
        pipeline_name="legislative_calendar",
        schedule="manual",
        schedule_desc="Manual / ad-hoc",
        expected_steps=["root"],
        criticality="low",
    ),
    ManifestEntry(
        name="test_system",
        script="scripts/test_system.py",
        pipeline_name="simulator",
        schedule="manual",
        schedule_desc="On-demand preflight simulator",
        expected_steps=[],
        criticality="low",
        writes_to_pipeline_runs=False,  # writes to system_health
    ),
    ManifestEntry(
        name="simulator_watcher",
        script="scripts/simulator_watcher.py",
        pipeline_name="simulator_watcher",
        schedule="persistent",
        schedule_desc="Persistent daemon on ridley — polls for simulator triggers every 15s",
        expected_steps=[],
        criticality="low",
        writes_to_pipeline_runs=False,  # does not write pipeline_runs, manages system_health
    ),
]

ALL_ENTRIES: list[ManifestEntry] = MANIFEST + EVENT_TRIGGERED


def get_weekday_entries() -> list[ManifestEntry]:
    """Return entries expected to run on a weekday (M-F)."""
    return [e for e in MANIFEST if "1-5" in e.schedule or "*" in e.schedule.split()[4]]


def get_sunday_entries() -> list[ManifestEntry]:
    """Return entries expected to run on Sunday."""
    return [e for e in MANIFEST if e.schedule.split()[4] in ("0", "*")]


def get_entry(name: str) -> ManifestEntry | None:
    """Look up a manifest entry by name."""
    for e in ALL_ENTRIES:
        if e.name == name:
            return e
    return None


def validate_output(entry: ManifestEntry, snapshot: dict) -> bool:
    """Run an entry's output validator against a pipeline_runs output_snapshot."""
    if entry.output_validator is None:
        return True
    try:
        return entry.output_validator(snapshot)
    except Exception:
        return False


def estimate_daily_claude_budget() -> float:
    """Sum expected Claude API costs for a full weekday of scheduled runs."""
    total = 0.0
    for entry in get_weekday_entries():
        total += entry.estimated_claude_cost
    return total
