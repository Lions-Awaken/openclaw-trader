#!/usr/bin/env python3
"""
Shadow Performance Rollup — weekly aggregation of shadow positions per agent.

Runs Sunday 9:00 AM PDT. For each shadow profile (is_shadow=true), queries
shadow_positions closed during the just-ended week, computes performance
metrics, compares against live trade P&L for the same period, and UPSERTs
a row into shadow_performance.

Schedule: 0 9 * * 0 (Sunday 9:00 AM PDT)
pipeline_name: shadow_performance_rollup
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from common import SUPABASE_URL, _client, sb_get, sb_headers, slack_notify
from tracer import PipelineTracer, set_active_tracer, traced

# ---------------------------------------------------------------------------
# Week window helpers
# ---------------------------------------------------------------------------

def _week_window(ref: date) -> tuple[date, date]:
    """Return (week_start, week_end) for the ISO week containing ref.

    week_start = Monday of the week containing ref.
    week_end   = the following Monday (exclusive upper bound).

    When running on a Sunday we intentionally use the just-ended week
    (Mon–Sat) rather than the week that starts today.
    """
    if ref.weekday() == 6:
        # Sunday → use the week that just ended (Mon–Sat)
        week_start = ref - timedelta(days=6)
    else:
        week_start = ref - timedelta(days=ref.weekday())

    week_end = week_start + timedelta(days=7)
    return week_start, week_end


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _fetch_shadow_profiles() -> list[dict]:
    """Return all shadow profiles (is_shadow=true) from strategy_profiles."""
    return sb_get("strategy_profiles", {
        "select": "profile_name,dwm_weight",
        "is_shadow": "eq.true",
    })


@traced("shadow_rollup")
def _fetch_closed_positions(profile_name: str, week_start: date, week_end: date) -> list[dict]:
    """Return shadow_positions closed within [week_start, week_end)."""
    return sb_get("shadow_positions", {
        "select": "id,final_pnl,was_divergent,shadow_was_right,status",
        "shadow_profile": f"eq.{profile_name}",
        "status": "in.(closed,stopped,expired)",
        "exit_date": f"gte.{week_start.isoformat()}",
        "exit_date": f"lt.{week_end.isoformat()}",  # noqa: F601 — Supabase REST uses repeated params
    })


def _fetch_closed_positions_raw(
    profile_name: str, week_start: date, week_end: date
) -> list[dict]:
    """Fetch closed shadow_positions using explicit query params (handles dup key)."""
    if not SUPABASE_URL:
        return []
    try:
        resp = _client.get(
            f"{SUPABASE_URL}/rest/v1/shadow_positions",
            headers=sb_headers(),
            params=[
                ("select", "id,final_pnl,was_divergent,shadow_was_right,status"),
                ("shadow_profile", f"eq.{profile_name}"),
                ("status", "in.(closed,stopped,expired)"),
                ("exit_date", f"gte.{week_start.isoformat()}"),
                ("exit_date", f"lt.{week_end.isoformat()}"),
            ],
        )
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        print(f"[shadow_rollup] _fetch_closed_positions_raw error: {e}")
        return []


def _fetch_opened_count(profile_name: str, week_start: date, week_end: date) -> int:
    """Count shadow_positions opened this week (any status)."""
    if not SUPABASE_URL:
        return 0
    try:
        resp = _client.get(
            f"{SUPABASE_URL}/rest/v1/shadow_positions",
            headers={**sb_headers(), "Prefer": "count=exact"},
            params=[
                ("select", "id"),
                ("shadow_profile", f"eq.{profile_name}"),
                ("entry_date", f"gte.{week_start.isoformat()}"),
                ("entry_date", f"lt.{week_end.isoformat()}"),
            ],
        )
        if resp.status_code == 200:
            content_range = resp.headers.get("Content-Range", "")
            # Format: "0-N/total"
            if "/" in content_range:
                return int(content_range.split("/")[-1])
    except Exception as e:
        print(f"[shadow_rollup] _fetch_opened_count error: {e}")
    return 0


def _fetch_live_pnl(week_start: date, week_end: date) -> float:
    """Sum pnl from trade_decisions for the same calendar period."""
    if not SUPABASE_URL:
        return 0.0
    try:
        resp = _client.get(
            f"{SUPABASE_URL}/rest/v1/trade_decisions",
            headers=sb_headers(),
            params=[
                ("select", "pnl"),
                ("created_at", f"gte.{week_start.isoformat()}T00:00:00Z"),
                ("created_at", f"lt.{week_end.isoformat()}T00:00:00Z"),
                ("pnl", "not.is.null"),
            ],
        )
        if resp.status_code == 200:
            rows = resp.json()
            return sum(float(r.get("pnl", 0) or 0) for r in rows)
    except Exception as e:
        print(f"[shadow_rollup] _fetch_live_pnl error: {e}")
    return 0.0


def _fetch_dwm_weight(profile_name: str) -> float | None:
    """Get current dwm_weight for a profile from strategy_profiles."""
    rows = sb_get("strategy_profiles", {
        "select": "dwm_weight",
        "profile_name": f"eq.{profile_name}",
        "limit": "1",
    })
    if rows:
        return rows[0].get("dwm_weight")
    return None


# ---------------------------------------------------------------------------
# Metrics calculation
# ---------------------------------------------------------------------------

def _compute_metrics(
    closed: list[dict],
    trades_opened: int,
    live_pnl: float,
    dwm_weight_start: float | None,
    dwm_weight_end: float | None,
    profile_name: str,
    week_start: date,
) -> dict:
    """Aggregate closed positions into a shadow_performance row."""
    trades_closed = len(closed)

    pnl_values = [float(r.get("final_pnl", 0) or 0) for r in closed]
    trades_won = sum(1 for v in pnl_values if v > 0)
    trades_lost = sum(1 for v in pnl_values if v <= 0)
    total_pnl = round(sum(pnl_values), 4)
    win_rate_pct = round(trades_won / trades_closed * 100, 2) if trades_closed else None
    avg_pnl_per_trade = round(total_pnl / trades_closed, 4) if trades_closed else None
    best_trade_pnl = round(max(pnl_values), 4) if pnl_values else None
    worst_trade_pnl = round(min(pnl_values), 4) if pnl_values else None

    # Divergent subset
    divergent = [r for r in closed if r.get("was_divergent")]
    divergent_trades = len(divergent)
    div_wins = sum(1 for r in divergent if float(r.get("final_pnl", 0) or 0) > 0)
    divergent_win_rate = round(div_wins / divergent_trades * 100, 2) if divergent_trades else None

    # Live comparison
    live_pnl_rounded = round(live_pnl, 4)
    vs_live_delta = round(total_pnl - live_pnl_rounded, 4) if live_pnl_rounded != 0 else None

    return {
        "shadow_profile": profile_name,
        "week_start": week_start.isoformat(),
        "trades_opened": trades_opened,
        "trades_closed": trades_closed,
        "trades_won": trades_won,
        "trades_lost": trades_lost,
        "win_rate_pct": win_rate_pct,
        "total_pnl": total_pnl,
        "avg_pnl_per_trade": avg_pnl_per_trade,
        "best_trade_pnl": best_trade_pnl,
        "worst_trade_pnl": worst_trade_pnl,
        "divergent_trades": divergent_trades,
        "divergent_win_rate": divergent_win_rate,
        "live_pnl_same_period": live_pnl_rounded,
        "vs_live_delta": vs_live_delta,
        "dwm_weight_start": dwm_weight_start,
        "dwm_weight_end": dwm_weight_end,
    }


# ---------------------------------------------------------------------------
# UPSERT
# ---------------------------------------------------------------------------

def _upsert_performance(row: dict) -> bool:
    """UPSERT one shadow_performance row using Prefer: resolution=merge-duplicates."""
    if not SUPABASE_URL:
        return False
    try:
        resp = _client.post(
            f"{SUPABASE_URL}/rest/v1/shadow_performance",
            headers={
                **sb_headers(),
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
            json=row,
        )
        if resp.status_code in (200, 201):
            return True
        print(
            f"[shadow_rollup] UPSERT failed for {row['shadow_profile']}: "
            f"{resp.status_code} {resp.text[:300]}"
        )
    except Exception as e:
        print(f"[shadow_rollup] UPSERT error: {e}")
    return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run() -> None:
    today = date.today()
    week_start, week_end = _week_window(today)

    tracer = PipelineTracer(
        "shadow_performance_rollup",
        metadata={
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "run_date": today.isoformat(),
        },
    )
    set_active_tracer(tracer)

    try:
        # Step 1: Load shadow profiles
        with tracer.step("shadow_rollup:fetch_profiles") as result:
            profiles = _fetch_shadow_profiles()
            result.set({"profiles_found": len(profiles)})

        if not profiles:
            print("[shadow_rollup] No shadow profiles found — nothing to aggregate.")
            tracer.complete({"profiles": 0, "rows_upserted": 0})
            return

        # Step 2: Fetch live P&L once (shared across all profiles)
        with tracer.step("shadow_rollup:fetch_live_pnl") as result:
            live_pnl = _fetch_live_pnl(week_start, week_end)
            result.set({"live_pnl": live_pnl})

        # Step 3: Per-profile aggregation + UPSERT
        summary_lines: list[str] = []
        upserted = 0

        for profile in profiles:
            profile_name = profile["profile_name"]
            dwm_weight_start = profile.get("dwm_weight")

            with tracer.step(f"shadow_rollup:aggregate_{profile_name}") as result:
                closed = _fetch_closed_positions_raw(profile_name, week_start, week_end)
                trades_opened = _fetch_opened_count(profile_name, week_start, week_end)
                dwm_weight_end = _fetch_dwm_weight(profile_name)

                row = _compute_metrics(
                    closed=closed,
                    trades_opened=trades_opened,
                    live_pnl=live_pnl,
                    dwm_weight_start=dwm_weight_start,
                    dwm_weight_end=dwm_weight_end,
                    profile_name=profile_name,
                    week_start=week_start,
                )

                ok = _upsert_performance(row)
                if ok:
                    upserted += 1

                result.set({
                    "trades_closed": row["trades_closed"],
                    "trades_opened": trades_opened,
                    "total_pnl": row["total_pnl"],
                    "win_rate_pct": row["win_rate_pct"],
                    "upserted": ok,
                })

                pnl_str = f"${row['total_pnl']:+.2f}"
                wr_str = f"{row['win_rate_pct']:.1f}%" if row["win_rate_pct"] is not None else "n/a"
                summary_lines.append(
                    f"  • `{profile_name}` — "
                    f"{row['trades_closed']} closed / "
                    f"{row['trades_won']}W {row['trades_lost']}L "
                    f"({wr_str}) · P&L {pnl_str}"
                )

        # Step 4: Slack notification
        with tracer.step("shadow_rollup:notify") as result:
            week_label = f"{week_start.strftime('%b %d')}–{(week_end - timedelta(days=1)).strftime('%b %d')}"
            body = "\n".join(summary_lines) if summary_lines else "  (no data)"
            msg = (
                f"*Shadow Performance Rollup* — week of {week_label}\n"
                f"Live P&L same period: `${live_pnl:+.2f}`\n"
                f"{body}"
            )
            sent = slack_notify(msg)
            result.set({"sent": sent})

        tracer.complete({
            "week_start": week_start.isoformat(),
            "profiles_processed": len(profiles),
            "rows_upserted": upserted,
            "live_pnl": live_pnl,
        })
        print(
            f"[shadow_rollup] Complete. "
            f"Week: {week_start} → {week_end}. "
            f"Profiles: {len(profiles)}. "
            f"Rows upserted: {upserted}."
        )

    except Exception as e:
        import traceback
        tracer.fail(str(e), traceback.format_exc())
        print(f"[shadow_rollup] FATAL: {e}")
        slack_notify(f"*Shadow Performance Rollup FATAL*: {e}")
        raise


if __name__ == "__main__":
    run()
