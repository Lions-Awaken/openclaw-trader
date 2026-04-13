#!/usr/bin/env python3
"""
Daily Operations Report — sent to Telegram + Slack after market close.

Gathers pipeline health, trade activity, system status, error logs,
and cost data into a single comprehensive report. Designed to be
readable by both humans and AI agents for immediate diagnosis.

Data strategy:
  - system_health table  → primary source for health check pass/fail/warn summary
  - pipeline_runs table  → single broad query, split into scanner/catalyst/errors
  - trade_decisions      → today's trades (not in health_check)
  - cost_ledger          → today's costs (not in health_check)
  - shadow_divergences   → today's shadow ensemble divergences (not in health_check)
  - meta_reflections     → today's LLM meta reflection (not in health_check)

Run: python scripts/daily_report.py
Cron: 2:00 PM PDT weekdays (after meta_daily completes)
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, datetime, timezone

import httpx  # noqa: E402 — used only for Telegram (not in common.py)
from common import SUPABASE_URL, sb_get, slack_notify  # noqa: F401
from inference_engine import get_claude_budget, get_todays_claude_spend
from manifest import MANIFEST, estimate_daily_claude_budget, validate_output
from tracer import PipelineTracer, traced

TODAY = date.today().isoformat()
NOW = datetime.now(timezone.utc)

# Telegram-only HTTP client — Telegram is not in common.py
_tg_client = httpx.Client(timeout=15.0)


# ==========================================================================
# Data gathering helpers
# ==========================================================================


def gather_pipeline_runs_today() -> list[dict]:
    """Single query: all pipeline_runs root steps from today (last 200 rows).

    Callers split this result by pipeline_name rather than issuing separate
    queries per pipeline. Reduces Supabase round trips significantly.
    """
    rows = sb_get(
        "pipeline_runs",
        {
            "select": (
                "pipeline_name,step_name,status,"
                "error_message,output_snapshot,created_at"
            ),
            "step_name": "eq.root",
            "order": "created_at.desc",
            "limit": "200",
        },
    )
    # Return rows from today and recent rows for pipeline status (last 3 days
    # so we can detect pipelines that simply haven't run today yet)
    return rows


def gather_pipeline_status(all_runs: list[dict]) -> dict:
    """Check every manifest entry — did it run today? Did it succeed?

    Uses pre-fetched pipeline_runs rows (no additional DB queries).
    The health_check entry is checked against system_health separately.
    """
    # Separate health_check verification — uses system_health not pipeline_runs
    hc_rows = sb_get(
        "system_health",
        {
            "select": "run_id,status,created_at",
            "run_type": "eq.scheduled",
            "order": "created_at.desc",
            "limit": "1",
        },
    )

    results: dict = {}
    for entry in MANIFEST:
        if not entry.writes_to_pipeline_runs:
            if entry.pipeline_name == "health_check":
                if hc_rows:
                    results[entry.name] = {
                        "status": "success",
                        "last_run": hc_rows[0].get("created_at"),
                        "errors": [],
                    }
                else:
                    results[entry.name] = {
                        "status": "missing",
                        "last_run": None,
                        "errors": ["No health check run found"],
                    }
            continue

        # Filter pre-fetched rows for this pipeline
        rows = [r for r in all_runs if r.get("pipeline_name") == entry.pipeline_name]

        if not rows:
            results[entry.name] = {
                "status": "missing",
                "last_run": None,
                "errors": [f"No {entry.pipeline_name} runs found"],
            }
            continue

        latest = rows[0]
        raw_status = latest.get("status", "unknown")
        errors: list[str] = []
        output: dict = latest.get("output_snapshot") or {}

        if entry.output_validator and output:
            if not validate_output(entry, output):
                errors.append(f"Output validation failed: {str(output)[:120]}")

        if latest.get("error_message"):
            errors.append(latest["error_message"])

        if raw_status == "success" and not errors:
            final_status = "success"
        elif raw_status == "success":
            final_status = "degraded"
        else:
            final_status = "failed"

        results[entry.name] = {
            "status": final_status,
            "last_run": latest.get("created_at"),
            "output": output,
            "errors": errors,
            "runs_today": len([r for r in rows if r.get("created_at", "").startswith(TODAY)]),
        }

    return results


def gather_trade_activity() -> dict:
    """Today's trading activity — not covered by health_check."""
    trades = sb_get(
        "trade_decisions",
        {
            "select": "ticker,direction,entry_price,exit_price,pnl,created_at",
            "order": "created_at.desc",
            "limit": "20",
        },
    )
    today_trades = [t for t in trades if t.get("created_at", "").startswith(TODAY)]

    orders = sb_get(
        "order_events",
        {
            "select": "ticker,side,qty,filled_avg_price,status,created_at",
            "order": "created_at.desc",
            "limit": "20",
        },
    )
    today_orders = [o for o in orders if o.get("created_at", "").startswith(TODAY)]

    return {
        "trades_today": len(today_trades),
        "orders_today": len(today_orders),
        "trades": today_trades[:5],
        "total_pnl": sum(
            float(t.get("pnl", 0) or 0) for t in today_trades
        ),
    }


def gather_scanner_results(all_runs: list[dict]) -> dict:
    """Today's scanner inference results — extracted from pre-fetched pipeline_runs."""
    today_runs = [
        r for r in all_runs
        if r.get("pipeline_name") == "scanner"
        and r.get("created_at", "").startswith(TODAY)
    ]

    total_candidates = 0
    total_actionable = 0
    decisions: dict = {}
    for run in today_runs:
        snap: dict = run.get("output_snapshot") or {}
        total_candidates += snap.get("candidates", 0)
        total_actionable += snap.get("actionable", 0)
        for ticker, decision in snap.get("decisions", {}).items():
            decisions[ticker] = decision

    return {
        "runs": len(today_runs),
        "total_candidates": total_candidates,
        "total_actionable": total_actionable,
        "decisions": decisions,
    }


def gather_shadow_activity() -> dict:
    """Shadow ensemble divergences today — not covered by health_check."""
    divs = sb_get(
        "shadow_divergences",
        {
            "select": "ticker,shadow_profile,live_decision,shadow_decision,created_at",
            "order": "created_at.desc",
            "limit": "50",
        },
    )
    today_divs = [d for d in divs if d.get("created_at", "").startswith(TODAY)]

    by_profile: dict = {}
    for d in today_divs:
        p = d.get("shadow_profile", "?")
        by_profile[p] = by_profile.get(p, 0) + 1

    return {
        "total_divergences": len(today_divs),
        "by_profile": by_profile,
    }


def gather_cost_data() -> dict:
    """Today's API costs — not covered by health_check."""
    rows = sb_get(
        "cost_ledger",
        {
            "select": "category,subcategory,amount",
            "ledger_date": f"eq.{TODAY}",
        },
    )
    total = sum(abs(float(r.get("amount", 0))) for r in rows)
    by_category: dict = {}
    for r in rows:
        cat = r.get("category", "unknown")
        by_category[cat] = by_category.get(cat, 0) + abs(float(r.get("amount", 0)))

    budget = get_claude_budget()
    spent = get_todays_claude_spend()
    daily_estimate = estimate_daily_claude_budget()
    remaining_pct = ((budget - spent) / budget * 100) if budget > 0 else 0.0

    return {
        "total_spend": total,
        "by_category": by_category,
        "claude_budget": budget,
        "claude_spent": spent,
        "claude_remaining_pct": remaining_pct,
        "daily_estimate": daily_estimate,
    }


def gather_catalyst_data(all_runs: list[dict]) -> dict:
    """Catalyst ingest summary — extracted from pre-fetched pipeline_runs."""
    today_runs = [
        r for r in all_runs
        if r.get("pipeline_name") == "catalyst_ingest"
        and r.get("created_at", "").startswith(TODAY)
    ]

    total_inserted = 0
    total_dupes = 0
    for run in today_runs:
        snap: dict = run.get("output_snapshot") or {}
        total_inserted += snap.get("total_inserted", 0)
        total_dupes += snap.get("total_duplicates", 0)

    return {
        "runs": len(today_runs),
        "total_inserted": total_inserted,
        "total_duplicates": total_dupes,
    }


def gather_health_check() -> dict:
    """Most recent health check results — primary data source from system_health.

    Groups check results by check_group and counts pass/fail/warn per group.
    Uses the latest run_id to ensure we only summarise one complete run.
    """
    rows = sb_get(
        "system_health",
        {
            "select": "run_id,check_name,check_group,status,value,error_message",
            "run_type": "eq.scheduled",
            "order": "created_at.desc",
            "limit": "100",
        },
    )
    if not rows:
        return {
            "total": 0,
            "pass": 0,
            "fail": 0,
            "warn": 0,
            "failures": [],
            "by_group": {},
        }

    # Restrict to the single most-recent run_id so we don't mix runs
    latest_run_id = rows[0].get("run_id")
    run_rows = [r for r in rows if r.get("run_id") == latest_run_id]

    total = len(run_rows)
    passes = sum(1 for r in run_rows if r.get("status") == "pass")
    fails = sum(1 for r in run_rows if r.get("status") == "fail")
    warns = sum(1 for r in run_rows if r.get("status") == "warn")

    failures = [
        {
            "check": r.get("check_name"),
            "value": r.get("value"),
            "error": r.get("error_message"),
        }
        for r in run_rows
        if r.get("status") in ("fail", "warn")
    ]

    # Group summary: {group_name: {pass: N, fail: N, warn: N}}
    by_group: dict = {}
    for r in run_rows:
        grp = r.get("check_group", "unknown")
        if grp not in by_group:
            by_group[grp] = {"pass": 0, "fail": 0, "warn": 0}
        s = r.get("status", "")
        if s in by_group[grp]:
            by_group[grp][s] += 1

    return {
        "total": total,
        "pass": passes,
        "fail": fails,
        "warn": warns,
        "failures": failures[:10],
        "by_group": by_group,
    }


def gather_errors(all_runs: list[dict]) -> list[dict]:
    """Pipeline errors from today — extracted from pre-fetched pipeline_runs."""
    return [
        r for r in all_runs
        if r.get("status") == "failure"
        and r.get("created_at", "").startswith(TODAY)
    ]


def gather_meta_reflection() -> dict:
    """Today's meta-analysis reflection."""
    rows = sb_get(
        "meta_reflections",
        {
            "select": "reflection_date,signal_assessment,operational_issues,adjustments",
            "reflection_date": f"eq.{TODAY}",
            "limit": "1",
        },
    )
    if not rows:
        return {"exists": False}
    r = rows[0]
    return {
        "exists": True,
        "signal_assessment": (r.get("signal_assessment") or "")[:300],
        "operational_issues": (r.get("operational_issues") or "")[:300],
        "adjustments": r.get("adjustments") or [],
    }


# ==========================================================================
# Report formatter
# ==========================================================================


def format_report(data: dict) -> str:
    """Format the full report as plain text (compatible with Telegram + Slack)."""
    lines: list[str] = []
    sep = "=" * 50

    lines.append(sep)
    lines.append(f"  OPENCLAW DAILY OPS REPORT -- {TODAY}")
    lines.append(sep)
    lines.append("")

    # ── PIPELINE STATUS ─────────────────────────────────────────────────
    pipelines: dict = data["pipelines"]
    working = [k for k, v in pipelines.items() if v["status"] == "success"]
    degraded = [k for k, v in pipelines.items() if v["status"] == "degraded"]
    failed = [k for k, v in pipelines.items() if v["status"] == "failed"]
    missing = [k for k, v in pipelines.items() if v["status"] == "missing"]

    lines.append("PIPELINE STATUS")
    lines.append(f"  Working:  {len(working)}/{len(pipelines)}")
    if working:
        lines.append(f"    {', '.join(working)}")
    if degraded:
        lines.append(f"  Degraded: {', '.join(degraded)}")
        for name in degraded:
            for err in pipelines[name].get("errors", []):
                lines.append(f"    ! {name}: {err[:140]}")
    if failed:
        lines.append(f"  FAILED:   {', '.join(failed)}")
        for name in failed:
            for err in pipelines[name].get("errors", []):
                lines.append(f"    ! {name}: {err[:140]}")
    if missing:
        lines.append(f"  Missing:  {', '.join(missing)}")
    lines.append("")

    # ── HEALTH CHECK ─────────────────────────────────────────────────────
    hc: dict = data["health_check"]
    lines.append(
        f"HEALTH CHECK: {hc['pass']} pass / {hc['fail']} fail / {hc['warn']} warn"
    )
    for f in hc.get("failures", [])[:5]:
        err_str = (f.get("error") or "")[:90]
        lines.append(f"  ! {f['check']}: {f['value']} -- {err_str}")
    lines.append("")

    # ── SCANNER ──────────────────────────────────────────────────────────
    scanner: dict = data["scanner"]
    lines.append(
        f"SCANNER: {scanner['runs']} runs, "
        f"{scanner['total_candidates']} candidates, "
        f"{scanner['total_actionable']} actionable"
    )
    if scanner.get("decisions"):
        top = list(scanner["decisions"].items())[:8]
        lines.append(f"  Decisions: {', '.join(f'{t}={d}' for t, d in top)}")
    lines.append("")

    # ── TRADES ───────────────────────────────────────────────────────────
    trades: dict = data["trades"]
    pnl_sign = "+" if trades["total_pnl"] >= 0 else ""
    lines.append(
        f"TRADES: {trades['trades_today']} trades, "
        f"{trades['orders_today']} orders, "
        f"P&L: {pnl_sign}${trades['total_pnl']:.2f}"
    )
    for t in trades.get("trades", [])[:3]:
        t_pnl = float(t.get("pnl", 0) or 0)
        sign = "+" if t_pnl >= 0 else ""
        lines.append(f"  {t.get('ticker')}: {sign}${t_pnl:.2f}")
    lines.append("")

    # ── SHADOW ENSEMBLE ──────────────────────────────────────────────────
    shadow: dict = data["shadow"]
    lines.append(f"SHADOW ENSEMBLE: {shadow['total_divergences']} divergences")
    if shadow.get("by_profile"):
        lines.append(
            f"  By profile: "
            f"{', '.join(f'{p}={c}' for p, c in shadow['by_profile'].items())}"
        )
    lines.append("")

    # ── CATALYSTS ────────────────────────────────────────────────────────
    cats: dict = data["catalysts"]
    lines.append(
        f"CATALYSTS: {cats['runs']} ingest runs, "
        f"{cats['total_inserted']} inserted, "
        f"{cats['total_duplicates']} dupes"
    )
    lines.append("")

    # ── COSTS ────────────────────────────────────────────────────────────
    costs: dict = data["costs"]
    lines.append(f"COSTS: ${costs['total_spend']:.4f} total")
    lines.append(
        f"  Claude: ${costs['claude_spent']:.4f} / "
        f"${costs['claude_budget']:.2f} budget "
        f"({costs['claude_remaining_pct']:.0f}% remaining)"
    )
    if costs.get("by_category"):
        cat_str = ", ".join(
            f"{k}=${v:.4f}" for k, v in costs["by_category"].items()
        )
        lines.append(f"  Breakdown: {cat_str}")
    lines.append("")

    # ── META REFLECTION ──────────────────────────────────────────────────
    meta: dict = data["meta"]
    if meta.get("exists"):
        lines.append("META REFLECTION:")
        if meta.get("signal_assessment"):
            lines.append(f"  Signal: {meta['signal_assessment'][:200]}")
        if meta.get("operational_issues"):
            lines.append(f"  Ops: {meta['operational_issues'][:200]}")
    else:
        lines.append("META REFLECTION: none generated today")
    lines.append("")

    # ── ERRORS ───────────────────────────────────────────────────────────
    errors: list[dict] = data["errors"]
    if errors:
        lines.append(f"ERRORS ({len(errors)}):")
        for e in errors[:5]:
            msg = (e.get("error_message") or "")[:140]
            lines.append(
                f"  [{e.get('pipeline_name')}.{e.get('step_name')}] {msg}"
            )
    else:
        lines.append("ERRORS: none")
    lines.append("")

    # ── ACTION ITEMS ─────────────────────────────────────────────────────
    lines.append("ACTION ITEMS:")
    has_actions = False
    if failed:
        lines.append(f"  [CRITICAL] Fix failed pipelines: {', '.join(failed)}")
        has_actions = True
    if degraded:
        lines.append(
            f"  [WARNING]  Investigate degraded: {', '.join(degraded)}"
        )
        has_actions = True
    if missing:
        lines.append(
            f"  [CHECK]    Pipelines didn't run: {', '.join(missing)}"
        )
        has_actions = True
    if hc["fail"] > 0:
        lines.append(
            f"  [CHECK]    {hc['fail']} health check failure(s) need review"
        )
        has_actions = True
    if costs["claude_remaining_pct"] < 30:
        lines.append(
            f"  [BUDGET]   Claude at {costs['claude_remaining_pct']:.0f}% "
            "-- may need increase"
        )
        has_actions = True
    if not has_actions:
        lines.append("  None -- all systems nominal")

    lines.append("")
    lines.append(sep)

    return "\n".join(lines)


# ==========================================================================
# Delivery
# ==========================================================================


def send_telegram(text: str) -> bool:
    """Send report to Telegram. Splits at 4000 chars to stay under the 4096 limit."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(
            "[report] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
            " -- skipping Telegram"
        )
        return False

    # Split into <=4000-char chunks on line boundaries
    chunks: list[str] = []
    if len(text) <= 4000:
        chunks = [text]
    else:
        current = ""
        for line in text.split("\n"):
            candidate = current + line + "\n"
            if len(candidate) > 4000:
                chunks.append(current)
                current = line + "\n"
            else:
                current = candidate
        if current:
            chunks.append(current)

    try:
        for chunk in chunks:
            resp = _tg_client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "",
                    "disable_web_page_preview": True,
                },
            )
            if resp.status_code != 200:
                print(
                    f"[report] Telegram send failed: "
                    f"{resp.status_code} {resp.text[:200]}"
                )
                return False
            time.sleep(0.5)  # Telegram rate-limit courtesy

        print(f"[report] Telegram: sent ({len(chunks)} message(s))")
        return True
    except Exception as exc:
        print(f"[report] Telegram error: {exc}")
        return False


def send_slack(text: str) -> bool:
    """Send report to Slack via common.slack_notify."""
    try:
        return slack_notify(text)
    except Exception as exc:
        print(f"[report] Slack error: {exc}")
        return False


# ==========================================================================
# Main entry point
# ==========================================================================


@traced("report")
def run() -> dict:
    """Generate and send daily ops report."""
    print(f"[report] Generating daily ops report for {TODAY}...")

    # Single broad pipeline_runs query — all helpers read from this list
    all_runs = gather_pipeline_runs_today()

    data = {
        "pipelines": gather_pipeline_status(all_runs),
        "trades": gather_trade_activity(),
        "scanner": gather_scanner_results(all_runs),
        "shadow": gather_shadow_activity(),
        "costs": gather_cost_data(),
        "catalysts": gather_catalyst_data(all_runs),
        "health_check": gather_health_check(),
        "errors": gather_errors(all_runs),
        "meta": gather_meta_reflection(),
    }

    report = format_report(data)
    print(report)

    tg_ok = send_telegram(report)
    sl_ok = send_slack(report)

    print(
        f"[report] Delivery: "
        f"Telegram={'OK' if tg_ok else 'FAIL'}, "
        f"Slack={'OK' if sl_ok else 'FAIL'}"
    )

    pipeline_map: dict = data["pipelines"]
    pipelines_working = len(
        [v for v in pipeline_map.values() if v["status"] == "success"]
    )
    pipelines_total = len(pipeline_map)

    return {
        "date": TODAY,
        "telegram": tg_ok,
        "slack": sl_ok,
        "pipelines_working": pipelines_working,
        "pipelines_total": pipelines_total,
        "errors": len(data["errors"]),
        "trades": data["trades"]["trades_today"],
    }


if __name__ == "__main__":
    tracer = PipelineTracer("daily_report", metadata={"date": TODAY})
    try:
        result = run()
        tracer.complete(result)
    except Exception as exc:
        import traceback

        tracer.fail(str(exc), traceback.format_exc())
        print(f"[report] FATAL: {exc}")
        traceback.print_exc()
