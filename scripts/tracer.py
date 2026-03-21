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

import json
import os
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TUNING_PROFILE_PATH = Path.home() / ".openclaw/workspace/active_tuning_profile.json"
BUFFER_PATH = Path.home() / ".openclaw/workspace/tracer_buffer.jsonl"


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
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers=_sb_headers(),
                json=data,
            )
            if resp.status_code in (200, 201):
                rows = resp.json()
                return rows[0] if rows else data
            else:
                _buffer_locally(table, data)
                return None
    except Exception:
        _buffer_locally(table, data)
        return None


def _patch_supabase(table: str, row_id: str, data: dict) -> bool:
    """PATCH an existing row. Returns True on success."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.patch(
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
    """Read the active tuning profile ID from local file or query Supabase."""
    # Try local cache first (set by tuning profile activation script)
    if TUNING_PROFILE_PATH.exists():
        try:
            data = json.loads(TUNING_PROFILE_PATH.read_text())
            return data.get("id")
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback: query Supabase for the active profile
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"{SUPABASE_URL}/rest/v1/tuning_profiles",
                headers=_sb_headers(),
                params={"status": "eq.active", "limit": "1", "select": "id"},
            )
            if resp.status_code == 200:
                rows = resp.json()
                if rows:
                    return rows[0]["id"]
    except Exception:
        pass

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
        """Build and store the telemetry record."""
        if not self.tuning_profile_id:
            return None

        end_snapshot = _collect_system_snapshot()
        wall_clock = int((time.time() - self.start_time) * 1000)

        # Final peak sample
        self.sample()

        telemetry = {
            "pipeline_run_id": pipeline_run_id,
            "tuning_profile_id": self.tuning_profile_id,
            "pipeline_name": self.pipeline_name,
            "step_count": self.step_count,
            "wall_clock_ms": wall_clock,
            "ram_start_mb": self.start_snapshot.get("rss_mb"),
            "ram_peak_mb": self._rss_peak,
            "ram_end_mb": end_snapshot.get("rss_mb"),
            "cpu_temp_start_c": self.start_snapshot.get("cpu_temp_c"),
            "cpu_temp_max_c": max(
                self.start_snapshot.get("cpu_temp_c", 0),
                end_snapshot.get("cpu_temp_c", 0),
            ) or None,
            "gpu_temp_start_c": self.start_snapshot.get("gpu_temp_c"),
            "gpu_temp_max_c": max(
                self.start_snapshot.get("gpu_temp_c", 0),
                end_snapshot.get("gpu_temp_c", 0),
            ) or None,
            "ollama_inference_count": self.ollama_calls,
            "ollama_tokens_generated": self.ollama_tokens,
            "ollama_avg_tokens_per_sec": (
                round(sum(self.ollama_tps_samples) / len(self.ollama_tps_samples), 1)
                if self.ollama_tps_samples else None
            ),
            "ollama_min_tokens_per_sec": (
                round(min(self.ollama_tps_samples), 1)
                if self.ollama_tps_samples else None
            ),
            "ollama_max_tokens_per_sec": (
                round(max(self.ollama_tps_samples), 1)
                if self.ollama_tps_samples else None
            ),
            "embedding_count": self.embedding_count,
            "embedding_avg_ms": (
                round(sum(self.embedding_ms_samples) / len(self.embedding_ms_samples))
                if self.embedding_ms_samples else None
            ),
            "embedding_max_ms": (
                max(self.embedding_ms_samples) if self.embedding_ms_samples else None
            ),
            "claude_call_count": self.claude_calls,
            "claude_total_tokens": self.claude_tokens,
            "claude_avg_latency_ms": (
                round(sum(self.claude_latency_samples) / len(self.claude_latency_samples))
                if self.claude_latency_samples else None
            ),
            "metadata": {
                "load_avg_start": self.start_snapshot.get("load_avg_1m"),
                "load_avg_end": end_snapshot.get("load_avg_1m"),
            },
        }

        # Remove None values to keep the insert clean
        telemetry = {k: v for k, v in telemetry.items() if v is not None}

        return _post_to_supabase("tuning_telemetry", telemetry)


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

        _post_to_supabase("pipeline_runs", root_data)
        self._current_parent_id = self.root_id

    @contextmanager
    def step(
        self,
        step_name: str,
        input_snapshot: dict | None = None,
    ):
        """Context manager for a pipeline step. Automatically records timing and errors."""
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
    tracer = PipelineTracer("self_test", metadata={"test": True})

    with tracer.step("step_1", input_snapshot={"msg": "hello"}) as result:
        time.sleep(0.1)
        result.set({"status": "ok"})

    with tracer.step("step_2") as result:
        result.set({"computed": 42})

    tracer.complete({"test": "passed"})
    print("[tracer] Self-test complete. Check pipeline_runs table.")

    # Flush any buffered writes
    flushed = flush_buffer()
    if flushed:
        print(f"[tracer] Flushed {flushed} buffered writes.")
