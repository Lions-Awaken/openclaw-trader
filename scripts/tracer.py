#!/usr/bin/env python3
"""
PipelineTracer — lightweight execution tracing for OpenClaw trading scripts.

Usage:
    from tracer import PipelineTracer

    tracer = PipelineTracer("pre_market_scan")

    with tracer.step("regime_check", input_snapshot={"spy_price": 520.0}):
        regime = check_regime()

    with tracer.step("signal_eval", input_snapshot={"tickers": ["NVDA", "AAPL"]}):
        results = evaluate_signals(tickers)

Context manager automatically records start/end times, status, errors.
Failed Supabase writes buffer to local JSONL file for retry.
Automatically captures active tuning profile and hardware telemetry.
"""

import atexit
import functools
import json
import os
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Module-level client for all Supabase writes (reused, not per-call)
_sb_client = httpx.Client(timeout=10.0)
atexit.register(_sb_client.close)
TUNING_PROFILE_PATH = Path.home() / ".openclaw/workspace/active_tuning_profile.json"
BUFFER_PATH = Path.home() / ".openclaw/workspace/tracer_buffer.jsonl"

# Map pipeline_name → domain prefix for step_names that aren't inside an @traced function
_PIPELINE_TO_DOMAIN: dict[str, str] = {
    "scanner": "pipeline",
    "catalyst_ingest": "catalysts",
    "meta_daily": "meta",
    "meta_weekly": "meta",
    "calibrator": "meta",
    "heartbeat": "sitrep",
    "position_manager": "positions",
    "post_trade_analysis": "economics",
    "legislative_calendar": "catalysts",
    "health_check": "pipeline",
    "ingest": "catalysts",
    "simulator": "pipeline",
}

# Thread-local active tracer + category for @traced() decorator support
_active_tracer = threading.local()


def set_active_tracer(tracer):
    """Set the active tracer for the current thread. Called by PipelineTracer.__init__()."""
    _active_tracer.instance = tracer


def get_active_tracer():
    """Get the active tracer for the current thread, or None."""
    return getattr(_active_tracer, 'instance', None)


def clear_active_tracer():
    """Clear the active tracer. Called by PipelineTracer.complete()/fail()."""
    _active_tracer.instance = None


def traced(domain: str):
    """Decorator to trace function execution into pipeline_runs.

    When an active PipelineTracer exists (set via set_active_tracer),
    creates a child step named '{domain}:{function_name}'.
    When no tracer is active, the function runs untraced (zero overhead).

    Also sets a thread-local category so that any bare tracer.step() calls
    made from within this function receive the same domain prefix.

    Usage:
        @traced("catalysts")
        def fetch_finnhub_news(ticker, lookback_hours=24):
            ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tracer = get_active_tracer()
            if tracer is None:
                return fn(*args, **kwargs)
            step_name = f"{domain}:{fn.__name__}"
            # Safe snapshot: capture first string arg (usually ticker/table name)
            input_snap = {}
            if args and isinstance(args[0], str):
                input_snap["arg0"] = args[0]
            prev_category = getattr(_active_tracer, 'category', None)
            _active_tracer.category = domain
            try:
                with tracer.step(step_name, input_snapshot=input_snap) as result:
                    ret = fn(*args, **kwargs)
                    if isinstance(ret, dict):
                        result.set(ret)
                    return ret
            finally:
                _active_tracer.category = prev_category
        return wrapper
    return decorator


def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _post_to_supabase(table: str, data: dict) -> dict | None:
    """Fire-and-forget POST to Supabase. Buffer locally on failure."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        _buffer_locally(table, data)
        return None

    try:
        resp = _sb_client.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=_sb_headers(),
            json=data,
        )
        if resp.status_code in (200, 201):
            rows = resp.json()
            return rows[0] if rows else data
        else:
            print(f"[tracer] Supabase write FAILED: {table} → {resp.status_code} {resp.text[:300]}")
            _buffer_locally(table, data)
            return None
    except Exception as e:
        print(f"[tracer] Supabase write ERROR: {table} → {e}")
        _buffer_locally(table, data)
        return None


def _patch_supabase(table: str, row_id: str, data: dict) -> bool:
    """PATCH an existing row. Returns True on success."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False

    try:
        resp = _sb_client.patch(
            f"{SUPABASE_URL}/rest/v1/{table}?id=eq.{row_id}",
            headers=_sb_headers(),
            json=data,
        )
        return resp.status_code in (200, 204)
    except Exception:
        return False


def _buffer_locally(table: str, data: dict):
    """Append failed write to local JSONL buffer for later retry."""
    BUFFER_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "table": table,
        "data": data,
        "buffered_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(BUFFER_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def flush_buffer():
    """Retry all buffered writes. Called manually or by a cron job."""
    if not BUFFER_PATH.exists():
        return 0

    lines = BUFFER_PATH.read_text().strip().split("\n")
    if not lines or lines == [""]:
        return 0

    remaining = []
    flushed = 0

    for line in lines:
        try:
            entry = json.loads(line)
            result = _post_to_supabase(entry["table"], entry["data"])
            if result is not None:
                flushed += 1
            else:
                remaining.append(line)
        except (json.JSONDecodeError, KeyError):
            continue  # Drop malformed entries

    if remaining:
        BUFFER_PATH.write_text("\n".join(remaining) + "\n")
    else:
        BUFFER_PATH.unlink(missing_ok=True)

    return flushed


def _get_active_tuning_profile_id() -> str | None:
    """Tuning system removed (SLIM-01). Always returns None."""
    return None


def _collect_system_snapshot() -> dict:
    """Collect a point-in-time hardware snapshot. Best-effort, non-blocking."""
    snapshot = {}
    try:
        import resource
        # RSS in MB (Linux)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        snapshot["rss_mb"] = round(usage.ru_maxrss / 1024, 1)  # Linux reports in KB
    except Exception:
        pass

    try:
        # CPU percent via /proc/stat (fast, no subprocess)
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
            snapshot["load_avg_1m"] = load1
    except Exception:
        pass

    try:
        # GPU temp from Jetson thermal zones
        for tz_path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
            type_path = tz_path.parent / "type"
            if type_path.exists():
                tz_type = type_path.read_text().strip()
                if "gpu" in tz_type.lower():
                    snapshot["gpu_temp_c"] = round(int(tz_path.read_text().strip()) / 1000, 1)
                elif "cpu" in tz_type.lower() and "cpu_temp_c" not in snapshot:
                    snapshot["cpu_temp_c"] = round(int(tz_path.read_text().strip()) / 1000, 1)
    except Exception:
        pass

    return snapshot


class TelemetryCollector:
    """Collects hardware telemetry over the lifetime of a pipeline run."""

    def __init__(self, pipeline_name: str, tuning_profile_id: str | None):
        self.pipeline_name = pipeline_name
        self.tuning_profile_id = tuning_profile_id
        self.start_time = time.time()
        self.start_snapshot = _collect_system_snapshot()
        self.step_count = 0

        # LLM call tracking (populated by scripts via accumulate_*)
        self.ollama_calls = 0
        self.ollama_tokens = 0
        self.ollama_tps_samples: list[float] = []
        self.embedding_count = 0
        self.embedding_ms_samples: list[int] = []
        self.claude_calls = 0
        self.claude_tokens = 0
        self.claude_latency_samples: list[int] = []

        # Peak tracking
        self._rss_peak = self.start_snapshot.get("rss_mb", 0)

    def sample(self):
        """Take a mid-run sample to track peaks."""
        snap = _collect_system_snapshot()
        rss = snap.get("rss_mb", 0)
        if rss > self._rss_peak:
            self._rss_peak = rss

    def accumulate_ollama(self, tokens: int = 0, tokens_per_sec: float = 0):
        self.ollama_calls += 1
        self.ollama_tokens += tokens
        if tokens_per_sec > 0:
            self.ollama_tps_samples.append(tokens_per_sec)

    def accumulate_embedding(self, duration_ms: int = 0):
        self.embedding_count += 1
        if duration_ms > 0:
            self.embedding_ms_samples.append(duration_ms)

    def accumulate_claude(self, tokens: int = 0, latency_ms: int = 0):
        self.claude_calls += 1
        self.claude_tokens += tokens
        if latency_ms > 0:
            self.claude_latency_samples.append(latency_ms)

    def finalize(self, pipeline_run_id: str) -> dict | None:
        """No-op — tuning_telemetry table removed (SLIM-01)."""
        return None


class PipelineTracer:
    """Traces pipeline execution as a tree of steps in Supabase.
    Automatically captures active tuning profile and hardware telemetry."""

    def __init__(self, pipeline_name: str, metadata: dict | None = None):
        self.pipeline_name = pipeline_name
        self.root_id = str(uuid.uuid4())
        self.metadata = metadata or {}
        self._current_parent_id: str | None = None

        # Resolve active tuning profile
        self._tuning_profile_id = _get_active_tuning_profile_id()

        # Start telemetry collection
        self.telemetry = TelemetryCollector(pipeline_name, self._tuning_profile_id)

        # Create root pipeline_run entry
        root_data = {
            "id": self.root_id,
            "pipeline_name": pipeline_name,
            "step_name": "root",
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "metadata": self.metadata,
        }
        if self._tuning_profile_id:
            root_data["tuning_profile_id"] = self._tuning_profile_id

        root_stored = None
        for attempt in range(3):
            root_stored = _post_to_supabase("pipeline_runs", root_data)
            if root_stored:
                break
            print(f"[tracer] Root row write failed (attempt {attempt + 1}/3), retrying...")
            time.sleep(2)
        if not root_stored:
            print("[tracer] WARNING: root pipeline_run not persisted — all child writes will fail FK constraints")
        self._current_parent_id = self.root_id
        set_active_tracer(self)

    @contextmanager
    def step(
        self,
        step_name: str,
        input_snapshot: dict | None = None,
    ):
        """Context manager for a pipeline step. Automatically records timing and errors.

        If step_name doesn't already contain a colon and isn't "root", a domain
        prefix is prepended automatically:
          1. If a @traced category is active on this thread, use that.
          2. Otherwise look up the pipeline_name in _PIPELINE_TO_DOMAIN.
          3. Final fallback: use the pipeline_name itself as the prefix.
        """
        if ":" not in step_name and step_name != "root":
            category = getattr(_active_tracer, 'category', None)
            if not category:
                category = _PIPELINE_TO_DOMAIN.get(self.pipeline_name, self.pipeline_name)
            step_name = f"{category}:{step_name}"

        step_id = str(uuid.uuid4())
        start_time = datetime.now(timezone.utc)

        step_data = {
            "id": step_id,
            "pipeline_name": self.pipeline_name,
            "step_name": step_name,
            "parent_run_id": self._current_parent_id,
            "status": "running",
            "started_at": start_time.isoformat(),
            "input_snapshot": input_snapshot or {},
        }
        if self._tuning_profile_id:
            step_data["tuning_profile_id"] = self._tuning_profile_id
        _post_to_supabase("pipeline_runs", step_data)

        # Allow nested steps
        prev_parent = self._current_parent_id
        self._current_parent_id = step_id
        self.telemetry.step_count += 1

        result = StepResult()
        try:
            yield result
            # Step completed successfully
            end_time = datetime.now(timezone.utc)
            _patch_supabase("pipeline_runs", step_id, {
                "status": "success",
                "completed_at": end_time.isoformat(),
                "output_snapshot": result.output or {},
            })
            # Mid-run telemetry sample
            self.telemetry.sample()
        except Exception as e:
            end_time = datetime.now(timezone.utc)
            _patch_supabase("pipeline_runs", step_id, {
                "status": "failure",
                "completed_at": end_time.isoformat(),
                "error_message": str(e),
                "error_traceback": traceback.format_exc(),
            })
            raise
        finally:
            self._current_parent_id = prev_parent

    def complete(self, output_snapshot: dict | None = None):
        """Mark the root pipeline run as complete and finalize telemetry."""
        _patch_supabase("pipeline_runs", self.root_id, {
            "status": "success",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "output_snapshot": output_snapshot or {},
        })
        # Store hardware telemetry for this run
        self.telemetry.finalize(self.root_id)
        clear_active_tracer()

    def fail(self, error: str, tb: str = ""):
        """Mark the root pipeline run as failed and finalize telemetry."""
        _patch_supabase("pipeline_runs", self.root_id, {
            "status": "failure",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error_message": error,
            "error_traceback": tb,
        })
        # Still store telemetry on failure — valuable for diagnosing issues
        self.telemetry.finalize(self.root_id)
        clear_active_tracer()

    def log_order_event(
        self,
        order_id: str,
        ticker: str,
        event_type: str,
        side: str,
        qty_ordered: float | None = None,
        qty_filled: float | None = None,
        price: float | None = None,
        avg_fill_price: float | None = None,
        raw_event: dict | None = None,
    ):
        """Log an order lifecycle event."""
        _post_to_supabase("order_events", {
            "order_id": order_id,
            "ticker": ticker,
            "event_type": event_type,
            "side": side,
            "qty_ordered": qty_ordered,
            "qty_filled": qty_filled,
            "price": price,
            "avg_fill_price": avg_fill_price,
            "raw_event": raw_event or {},
            "pipeline_run_id": self._current_parent_id or self.root_id,
        })

    def log_data_quality(
        self,
        check_name: str,
        target: str,
        passed: bool,
        expected_value: str = "",
        actual_value: str = "",
        severity: str = "warning",
    ):
        """Log a data quality check result."""
        _post_to_supabase("data_quality_checks", {
            "check_name": check_name,
            "target": target,
            "passed": passed,
            "expected_value": expected_value,
            "actual_value": actual_value,
            "severity": severity,
            "pipeline_run_id": self._current_parent_id or self.root_id,
        })

    def log_signal_evaluation(
        self,
        ticker: str,
        signals: dict,
        total_score: int,
        decision: str,
        reasoning: str = "",
        scan_type: str = "pre_market",
        embedding: list[float] | None = None,
    ):
        """Log a full signal evaluation for a ticker."""
        data = {
            "ticker": ticker,
            "scan_type": scan_type,
            "trend": signals.get("trend", {}),
            "momentum": signals.get("momentum", {}),
            "volume": signals.get("volume", {}),
            "fundamental": signals.get("fundamental", {}),
            "sentiment": signals.get("sentiment", {}),
            "flow": signals.get("flow", {}),
            "total_score": total_score,
            "decision": decision,
            "reasoning": reasoning,
            "pipeline_run_id": self._current_parent_id or self.root_id,
        }
        if embedding:
            data["embedding"] = embedding
        _post_to_supabase("signal_evaluations", data)


class StepResult:
    """Mutable container for step output, set within the context manager."""

    def __init__(self):
        self.output: dict | None = None

    def set(self, output: dict):
        self.output = output


if __name__ == "__main__":
    # Self-test: create a traced pipeline run
    print("[tracer] Running self-test...")

    # Test 1: decorator is a no-op without active tracer
    @traced("test")
    def helper_no_context():
        return {"result": "ok"}

    out = helper_no_context()
    assert out == {"result": "ok"}, "Decorator should be a no-op without active tracer"
    print("[tracer] ✓ Decorator no-op without active tracer")

    # Test 2: decorator traces within pipeline context
    @traced("test")
    def helper_with_context(ticker):
        return {"ticker": ticker, "score": 42}

    tracer = PipelineTracer("self_test", metadata={"test": True})

    with tracer.step("step_1", input_snapshot={"msg": "hello"}) as result:
        time.sleep(0.1)
        result.set({"status": "ok"})

    # This should create a "test:helper_with_context" step
    out = helper_with_context("NVDA")
    assert out == {"ticker": "NVDA", "score": 42}, "Decorator should return function result"
    print("[tracer] ✓ Decorator traced within pipeline context")

    tracer.complete({"test": "passed"})

    # After complete, active tracer should be cleared
    assert get_active_tracer() is None, "Active tracer should be cleared after complete()"
    print("[tracer] ✓ Active tracer cleared after complete()")

    print("[tracer] Self-test complete. Check pipeline_runs table.")

    # Flush any buffered writes
    flushed = flush_buffer()
    if flushed:
        print(f"[tracer] Flushed {flushed} buffered writes.")
