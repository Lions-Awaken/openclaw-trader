#!/usr/bin/env python3
"""
system_check.py — Unified system health check and preflight simulator.

Two modes:
  --mode health      Daily lightweight checks (cron 5AM weekdays)
  --mode preflight   Full NASA go/no-go preflight (on-demand from dashboard)

Usage:
  python scripts/system_check.py --mode health
  python scripts/system_check.py --mode health --dry-run --group signals
  python scripts/system_check.py --mode preflight
  python scripts/system_check.py --mode preflight --dry-run
  python scripts/system_check.py --mode preflight --concurrency 4
"""

import argparse
import os
import sys

# Ensure scripts/ is on the path for project imports
sys.path.insert(0, os.path.dirname(__file__))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OpenClaw system check — health or preflight mode.",
    )
    parser.add_argument(
        "--mode",
        choices=["health", "preflight"],
        required=True,
        help="health = daily lightweight checks, preflight = full NASA go/no-go",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results only — no DB writes, no Slack, no external calls.",
    )
    parser.add_argument(
        "--group",
        default=None,
        help="(health mode only) Run a single check group.",
    )
    parser.add_argument(
        "--notify-always",
        action="store_true",
        help="(health mode only) Always post Slack summary.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="(preflight mode only) Parallel streams for stress testing (1-10).",
    )
    args = parser.parse_args()

    if args.mode == "health":
        from checks.health import run_health

        return run_health(
            dry_run=args.dry_run,
            group=args.group,
            notify_always=args.notify_always,
        )
    else:
        from checks.preflight import run_preflight

        return run_preflight(
            dry_run=args.dry_run,
            concurrency=args.concurrency,
        )


if __name__ == "__main__":
    sys.exit(main())
