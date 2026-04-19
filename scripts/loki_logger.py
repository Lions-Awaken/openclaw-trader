#!/usr/bin/env python3
"""
Loki Logger — ships Python logs to Grafana Cloud Loki.

Payload conforms to the fleet logging standard
(`~/projects/claudefleet/protocols/logging-standard.md`):

    top-level required: timestamp, level, project, message
    top-level recommended: service, function, event, duration_ms, success
    metadata: {ticker, pipeline, step, scan_type, decision, confidence,
               candidates, trades_placed, spy_bars, error, ...}

Labels (unchanged): app=openclaw-trader, script=<script>, host=<hostname>.

A `log_config` Supabase row for `project_id='openclaw-trader'` is polled
(startup + every 60s) and drives dynamic min_level + sample_rate. The
poller fails open to INFO / 1.0 if the row or table is unreachable.

Env vars:
    LOKI_URL       — Loki push endpoint (e.g. https://logs-prod-xxx.grafana.net)
    LOKI_USER      — Grafana Cloud user ID
    LOKI_API_KEY   — Grafana Cloud API key
    SUPABASE_URL   — (optional) for log_config polling
    SUPABASE_SERVICE_KEY — (optional) for log_config polling

Usage:
    from loki_logger import get_logger
    logger = get_logger("scanner")
    logger.info("Scan started", extra={"event": "scan_started",
                                       "metadata": {"tickers": 39}})
"""

from __future__ import annotations

import json
import logging
import os
import platform
import random
import sys
import time
from datetime import datetime, timezone

import httpx

# ── Config ────────────────────────────────────────────────────────────────────
LOKI_URL = os.environ.get("LOKI_URL", "")
LOKI_USER = os.environ.get("LOKI_USER", "")
LOKI_API_KEY = os.environ.get("LOKI_API_KEY", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

PROJECT_ID = "openclaw-trader"       # matches log_config.project_id
PROJECT_SHORT = "openclaw"           # matches standard's `project` field
_HOSTNAME = platform.node()

# Fields the fleet standard promotes to top level
_TOP_LEVEL_OPTIONAL = ("service", "function", "event", "duration_ms",
                       "success", "account_id", "stream_id", "task_id")

# Domain fields historically attached to records — nested under metadata
_DOMAIN_FIELDS = ("ticker", "pipeline", "step", "scan_type", "decision",
                  "confidence", "error", "spy_bars", "candidates",
                  "trades_placed")

_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}

_LEVEL_FROM_NAME = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


# ── log_config poller ─────────────────────────────────────────────────────────
class LogConfig:
    """Dynamic log config fetched from Supabase. Fails open to INFO / 1.0."""

    POLL_INTERVAL = 60.0

    def __init__(self) -> None:
        self.min_level: int = logging.INFO
        self.sample_rate: float = 1.0
        self._last_poll: float = 0.0
        self._client = httpx.Client(timeout=3.0)
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_poll < self.POLL_INTERVAL:
            return
        self._last_poll = now

        if not SUPABASE_URL or not SUPABASE_KEY:
            return  # fail open, keep defaults

        try:
            resp = self._client.get(
                f"{SUPABASE_URL}/rest/v1/log_config",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                },
                params={
                    "project_id": f"eq.{PROJECT_ID}",
                    "select": "min_level,sample_rate",
                    "limit": "1",
                },
            )
            if resp.status_code != 200:
                return  # fail open (e.g. 404 while Phase 0 hasn't created the table)
            rows = resp.json()
            if not rows:
                return
            row = rows[0]
            lvl = str(row.get("min_level", "INFO")).upper()
            self.min_level = _LEVEL_FROM_NAME.get(lvl, logging.INFO)
            rate = row.get("sample_rate", 1.0)
            try:
                self.sample_rate = max(0.0, min(1.0, float(rate)))
            except (TypeError, ValueError):
                self.sample_rate = 1.0
        except Exception:
            # Network blip — keep last-known-good values
            pass

    def should_emit(self, level: int) -> bool:
        """Decide whether a record at `level` should ship.

        min_level is the floor. Above floor, sample_rate applies uniformly
        (WARN/ERROR still gate behind min_level, but also sample if rate < 1).
        """
        if level < self.min_level:
            return False
        if self.sample_rate >= 1.0:
            return True
        if self.sample_rate <= 0.0:
            return False
        return random.random() < self.sample_rate


_log_config = LogConfig()


# ── Handler ───────────────────────────────────────────────────────────────────
class LokiHandler(logging.Handler):
    """Logging handler that pushes entries to Grafana Cloud Loki.

    Payload conforms to protocols/logging-standard.md.
    """

    def __init__(
        self,
        script_name: str,
        batch_size: int = 10,
        flush_interval: float = 5.0,
    ) -> None:
        super().__init__()
        self.script_name = script_name
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._buffer: list[tuple[str, str]] = []  # (timestamp_ns, json_line)
        self._last_flush = time.time()
        self._client = httpx.Client(timeout=5.0)
        self._push_url = f"{LOKI_URL}/loki/api/v1/push"
        self._auth = (LOKI_USER, LOKI_API_KEY) if LOKI_USER and LOKI_API_KEY else None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_config.refresh()  # cheap; respects POLL_INTERVAL

            if not _log_config.should_emit(record.levelno):
                return

            payload = self._build_payload(record)

            ts_ns = str(int(record.created * 1_000_000_000))
            line = json.dumps(payload, separators=(",", ":"), default=str)

            self._buffer.append((ts_ns, line))

            if (len(self._buffer) >= self.batch_size
                    or (time.time() - self._last_flush) > self.flush_interval):
                self.flush()
        except Exception:
            pass  # Never let logging crash the app

    def _build_payload(self, record: logging.LogRecord) -> dict:
        """Construct the standard-schema JSON payload for one log record."""
        ts_iso = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"

        payload: dict = {
            "timestamp": ts_iso,
            "level": _LEVEL_NAMES.get(record.levelno, record.levelname),
            "project": PROJECT_SHORT,
            "message": record.getMessage(),
        }

        # function — auto from the log record if the caller didn't override
        func = getattr(record, "function", None) or record.funcName
        if func and func != "<module>":
            payload["function"] = func

        # service — default to script_name
        payload["service"] = getattr(record, "service", None) or self.script_name

        # Top-level recommended fields (set only if the caller attached them)
        for key in _TOP_LEVEL_OPTIONAL:
            if key in payload:
                continue
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val

        # Metadata: explicit `metadata` extra wins; otherwise gather domain fields
        metadata: dict = {}
        explicit_meta = getattr(record, "metadata", None)
        if isinstance(explicit_meta, dict):
            metadata.update(explicit_meta)
        for key in _DOMAIN_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                metadata[key] = val
        if metadata:
            payload["metadata"] = metadata

        # Exception info → message tail (standard forbids full tracebacks in prod,
        # but preserves the class/message for triage)
        if record.exc_info:
            exc_type, exc_val, _ = record.exc_info
            payload["message"] = f"{payload['message']} | {exc_type.__name__}: {exc_val}"

        return payload

    def flush(self) -> None:
        if not self._buffer or not LOKI_URL:
            self._buffer.clear()
            return

        entries = self._buffer[:]
        self._buffer.clear()
        self._last_flush = time.time()

        loki_payload = {
            "streams": [{
                "stream": {
                    "app": "openclaw-trader",
                    "script": self.script_name,
                    "host": _HOSTNAME,
                },
                "values": entries,
            }]
        }

        try:
            self._client.post(
                self._push_url,
                json=loki_payload,
                auth=self._auth,
                headers={"Content-Type": "application/json"},
            )
        except Exception:
            pass  # Drop on failure — logs are best-effort

    def close(self) -> None:
        self.flush()
        self._client.close()
        super().close()


# ── Print capture ─────────────────────────────────────────────────────────────
class PrintCapture:
    """Captures print() output and routes it through the logger."""

    def __init__(self, logger: logging.Logger, original_stdout):
        self.logger = logger
        self.original = original_stdout

    def write(self, text: str) -> None:
        self.original.write(text)
        text = text.strip()
        if text:
            self.logger.info(text)

    def flush(self) -> None:
        self.original.flush()


# ── Public API ────────────────────────────────────────────────────────────────
def get_logger(script_name: str, capture_print: bool = True) -> logging.Logger:
    """Set up and return a logger that ships to Loki + console.

    Args:
        script_name: Identifies this script in Loki (e.g. "scanner",
                     "position_manager"). Also becomes the `script` label
                     and the default `service` field.
        capture_print: If True, captures print() statements automatically.
    """
    logger = logging.getLogger(f"openclaw.{script_name}")

    if logger.handlers:
        return logger  # Already set up

    logger.setLevel(logging.DEBUG)  # level gating happens in the handler

    # Console handler (preserves existing print behavior for humans)
    console = logging.StreamHandler(sys.__stdout__)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    # Loki handler (ships to Grafana Cloud)
    if LOKI_URL:
        loki = LokiHandler(script_name, batch_size=5, flush_interval=3.0)
        loki.setLevel(logging.DEBUG)  # gating is dynamic via log_config
        loki.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(loki)

    # Capture print() statements automatically
    if capture_print and LOKI_URL:
        sys.stdout = PrintCapture(logger, sys.__stdout__)

    return logger
