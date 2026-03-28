#!/usr/bin/env python3
"""
Loki Logger — ships Python logs to Grafana Cloud Loki.

Lightweight handler that pushes log entries to Loki's HTTP API.
No agent required on the host. Uses httpx (already a dependency).

Usage:
    from loki_logger import get_logger
    logger = get_logger("scanner")
    logger.info("Scan started", extra={"tickers": 39, "profile": "UNLEASHED"})

Env vars:
    LOKI_URL      — Loki push endpoint (e.g. https://logs-prod-xxx.grafana.net)
    LOKI_USER     — Grafana Cloud user ID
    LOKI_API_KEY  — Grafana Cloud API key
"""

import json
import logging
import os
import platform
import sys
import time

import httpx

LOKI_URL = os.environ.get("LOKI_URL", "")
LOKI_USER = os.environ.get("LOKI_USER", "")
LOKI_API_KEY = os.environ.get("LOKI_API_KEY", "")

_HOSTNAME = platform.node()


class LokiHandler(logging.Handler):
    """Logging handler that pushes entries to Grafana Cloud Loki."""

    def __init__(self, script_name: str, batch_size: int = 10, flush_interval: float = 5.0):
        super().__init__()
        self.script_name = script_name
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._buffer: list[tuple[str, str]] = []  # (timestamp_ns, line)
        self._last_flush = time.time()
        self._client = httpx.Client(timeout=5.0)
        self._push_url = f"{LOKI_URL}/loki/api/v1/push"
        self._auth = (LOKI_USER, LOKI_API_KEY) if LOKI_USER and LOKI_API_KEY else None

    def emit(self, record: logging.LogRecord):
        try:
            ts_ns = str(int(record.created * 1_000_000_000))
            msg = self.format(record)

            # Attach structured metadata if present
            extra = {}
            for key in ("ticker", "pipeline", "step", "scan_type", "decision",
                        "confidence", "error", "duration_ms", "spy_bars",
                        "candidates", "trades_placed"):
                val = getattr(record, key, None)
                if val is not None:
                    extra[key] = val

            if extra:
                line = json.dumps({"msg": msg, **extra})
            else:
                line = msg

            self._buffer.append((ts_ns, line))

            if len(self._buffer) >= self.batch_size or (time.time() - self._last_flush) > self.flush_interval:
                self.flush()
        except Exception:
            pass  # Never let logging crash the app

    def flush(self):
        if not self._buffer or not LOKI_URL:
            self._buffer.clear()
            return

        entries = self._buffer[:]
        self._buffer.clear()
        self._last_flush = time.time()

        payload = {
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
                json=payload,
                auth=self._auth,
                headers={"Content-Type": "application/json"},
            )
        except Exception:
            pass  # Drop on failure — logs are best-effort

    def close(self):
        self.flush()
        self._client.close()
        super().close()


class PrintCapture:
    """Captures print() output and routes it through the logger."""

    def __init__(self, logger: logging.Logger, original_stdout):
        self.logger = logger
        self.original = original_stdout

    def write(self, text: str):
        self.original.write(text)
        text = text.strip()
        if text:
            self.logger.info(text)

    def flush(self):
        self.original.flush()


def get_logger(script_name: str, capture_print: bool = True) -> logging.Logger:
    """Set up and return a logger that ships to Loki + console.

    Args:
        script_name: Identifies this script in Loki (e.g. "scanner", "position_manager")
        capture_print: If True, also captures print() statements automatically
    """
    logger = logging.getLogger(f"openclaw.{script_name}")

    if logger.handlers:
        return logger  # Already set up

    logger.setLevel(logging.DEBUG)

    # Console handler (preserves existing print behavior)
    console = logging.StreamHandler(sys.__stdout__)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    # Loki handler (ships to Grafana Cloud)
    if LOKI_URL:
        loki = LokiHandler(script_name, batch_size=5, flush_interval=3.0)
        loki.setLevel(logging.INFO)
        loki.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(loki)

    # Capture print() statements automatically
    if capture_print and LOKI_URL:
        sys.stdout = PrintCapture(logger, sys.__stdout__)

    return logger
