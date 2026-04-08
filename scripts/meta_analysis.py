#!/usr/bin/env python3
"""Meta-analysis — daily and weekly reflections with RAG + chain analysis.

Usage:
    python scripts/meta_analysis.py daily    # replaces meta_daily.py  (4:30 PM ET)
    python scripts/meta_analysis.py weekly   # replaces meta_weekly.py (Sunday 7 PM ET)

Daily (4:30 PM ET):
    1. Query today's pipeline_runs -> health summary
    2. Query today's signal_evaluations -> per-signal accuracy
    3. RAG retrieve similar past days (reflections + signal patterns + catalysts)
    4. Inference chain depth analysis
    5. Catalyst correlation
    6. Adjustment impact tracking (trades_since_applied, pnl_since_applied)
    7. Shadow profile divergence summary
    8. Claude Sonnet generates meta-reflection with all context
    9. Propose strategy adjustments (auto-approve within +/-5% weight bounds)
    10. Embed, store, and log cost

Weekly (Sunday 7 PM ET):
    1. Aggregate all daily reflections from the week
    2. Full signal accuracy report
    3. Trend analysis across last 4 weekly reflections
    4. Pattern template discovery (cluster similar chains -> Claude names them)
    5. Calibration report integration
    6. Cross-layer analysis (catalyst-driven vs non-catalyst entries)
    7. Deeper Claude Sonnet analysis
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    call_claude,
    generate_embedding,
    sb_get,
    sb_rpc,
    slack_notify,
)
from tracer import (
    PipelineTracer,
    _patch_supabase,
    _post_to_supabase,
    set_active_tracer,
    traced,
)

# Module-level date globals — reassigned at the start of each runner
TODAY_STR = ""   # ISO string — used by daily mode
TODAY = date.today()  # date object — used by weekly mode
WEEK_START = ""  # ISO string — used by weekly mode


# ===========================================================================
# Shared helpers
# ===========================================================================

def get_active_profile() -> dict:
    """Load the active strategy profile."""
    rows = sb_get("strategy_profiles", {
        "select": "profile_name,self_modify_enabled,self_modify_requires_approval,self_modify_max_delta_pct",
        "active": "eq.true",
        "limit": "1",
    })
    return rows[0] if rows else {}


def rag_retrieve_context(embed_text: str) -> dict:
    """RAG retrieve similar past reflections, signal patterns, and catalysts."""
    embedding = generate_embedding(embed_text)
    if not embedding:
        return {"reflections": [], "signals": [], "catalysts": []}

    reflections = sb_rpc("match_meta_reflections", {
        "query_embedding": embedding,
        "match_threshold": 0.5,
        "match_count": 3,
    })

    signals = sb_rpc("match_signal_evaluations", {
        "query_embedding": embedding,
        "match_threshold": 0.5,
        "match_count": 3,
    })

    catalysts = sb_rpc("match_catalyst_events", {
        "query_embedding": embedding,
        "match_threshold": 0.4,
        "match_count": 5,
    })

    return {
        "reflections": reflections,
        "signals": signals,
        "catalysts": catalysts,
    }


def get_catalyst_correlation(trades: list[dict], catalysts: list[dict]) -> dict:
    """Correlate trades with catalyst events."""
    trade_tickers = {t["ticker"] for t in trades}
    catalyst_tickers = {c["ticker"] for c in catalysts if c.get("ticker")}

    overlap = trade_tickers & catalyst_tickers
    return {
        "trades_with_catalysts": len(overlap),
        "total_trades": len(trade_tickers),
        "total_catalysts": len(catalysts),
        "catalyst_driven_pct": round(len(overlap) / len(trade_tickers) * 100, 1) if trade_tickers else 0,
        "overlapping_tickers": sorted(overlap),
    }


def update_adjustment_impact() -> list[dict]:
    """Update trades_since_applied and pnl_since_applied for applied adjustments."""
    applied = sb_get("strategy_adjustments", {
        "select": "id,parameter_name,applied_at",
        "status": "eq.applied",
    })

    updated_adjustments = []
    for adj in applied:
        adj_id = adj["id"]
        applied_at = adj.get("applied_at", adj.get("created_at", ""))
        if not applied_at:
            continue

        trades_since = sb_get("trade_decisions", {
            "select": "pnl",
            "created_at": f"gte.{applied_at}",
        })

        trades_count = len(trades_since)
        pnl_sum = sum(float(t.get("pnl", 0) or 0) for t in trades_since)

        _patch_supabase("strategy_adjustments", adj_id, {
            "trades_since_applied": trades_count,
            "pnl_since_applied": round(pnl_sum, 4),
        })

        updated_adjustments.append({
            "parameter": adj["parameter_name"],
            "trades_since": trades_count,
            "pnl_since": round(pnl_sum, 2),
        })

    return updated_adjustments


@traced("meta")
def auto_approve_adjustments(adjustments: list[dict]) -> list[dict]:
    """Auto-approve adjustments based on active strategy profile settings."""
    profile = get_active_profile()
    self_modify = profile.get("self_modify_enabled", False)
    needs_approval = profile.get("self_modify_requires_approval", True)
    max_delta = float(profile.get("self_modify_max_delta_pct", 5.0))

    approved = []
    for adj in adjustments:
        try:
            prev = float(str(adj.get("current_value", "0")).replace("%", ""))
            new = float(str(adj.get("suggested_value", "0")).replace("%", ""))
            delta = abs(new - prev)

            if self_modify and not needs_approval:
                # UNLEASHED mode: auto-approve within max_delta, no bounds check
                if delta <= max_delta:
                    adj["status"] = "applied"
                else:
                    adj["status"] = "approved"
            elif delta <= 5.0 and 5.0 <= new <= 40.0:
                # CONSERVATIVE mode: tight bounds
                adj["status"] = "approved"
            else:
                adj["status"] = "proposed"
            approved.append(adj)
        except (ValueError, TypeError):
            adj["status"] = "proposed"
            approved.append(adj)
    return approved


# ===========================================================================
# Daily-specific data gathering
# ===========================================================================

@traced("meta")
def get_pipeline_health() -> dict:
    """Today's pipeline run stats."""
    runs = sb_get("pipeline_runs", {
        "select": "pipeline_name,step_name,status,duration_ms,error_message",
        "started_at": f"gte.{TODAY_STR}T00:00:00Z",
        "step_name": "neq.root",
    })

    total = len(runs)
    if total == 0:
        return {"total": 0, "success_rate": 0, "failures": []}

    successes = sum(1 for r in runs if r["status"] == "success")
    failures = [
        {"pipeline": r["pipeline_name"], "step": r["step_name"], "error": r.get("error_message", "")}
        for r in runs if r["status"] == "failure"
    ]
    avg_duration = sum(r.get("duration_ms") or 0 for r in runs) / total

    return {
        "total": total,
        "successes": successes,
        "success_rate": round(successes / total * 100, 1) if total else 0,
        "avg_duration_ms": round(avg_duration),
        "failures": failures[:10],
    }


@traced("meta")
def get_signal_accuracy() -> dict:
    """Today's signal evaluation stats."""
    evals = sb_get("signal_evaluations", {
        "select": "ticker,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision",
        "scan_date": f"eq.{TODAY_STR}",
    })

    if not evals:
        return {"total": 0, "signals": {}}

    signal_names = ["trend", "momentum", "volume", "fundamental", "sentiment", "flow"]
    signal_stats = {}

    for sig_name in signal_names:
        fired = sum(1 for e in evals if e.get(sig_name, {}).get("passed", False))
        signal_stats[sig_name] = {
            "fired": fired,
            "total": len(evals),
            "fire_rate": round(fired / len(evals) * 100, 1) if evals else 0,
        }

    decisions: dict[str, int] = {}
    for e in evals:
        d = e["decision"]
        decisions[d] = decisions.get(d, 0) + 1

    return {
        "total": len(evals),
        "signals": signal_stats,
        "decisions": decisions,
        "avg_score": round(sum(e["total_score"] for e in evals) / len(evals), 1),
    }


def get_data_quality_issues() -> list[dict]:
    """Today's failed data quality checks."""
    return sb_get("data_quality_checks", {
        "select": "check_name,target,expected_value,actual_value,severity",
        "checked_at": f"gte.{TODAY_STR}T00:00:00Z",
        "passed": "eq.false",
    })


@traced("meta")
def get_todays_trades() -> list[dict]:
    """Trades made today."""
    return sb_get("trade_decisions", {
        "select": "ticker,action,pnl,outcome,signals_fired,reasoning",
        "created_at": f"gte.{TODAY_STR}T00:00:00Z",
    })


def get_order_events() -> list[dict]:
    """Today's order events."""
    return sb_get("order_events", {
        "select": "ticker,event_type,side,qty_ordered,qty_filled,avg_fill_price",
        "created_at": f"gte.{TODAY_STR}T00:00:00Z",
    })


def get_inference_chain_analysis() -> dict:
    """Today's inference chain depth and decision analysis."""
    chains = sb_get("inference_chains", {
        "select": "ticker,max_depth_reached,final_confidence,final_decision,stopping_reason,tumblers",
        "chain_date": f"eq.{TODAY_STR}",
    })

    if not chains:
        return {"total": 0}

    depth_dist: dict[int, int] = {}
    decision_dist: dict[str, int] = {}
    stop_dist: dict[str, int] = {}
    avg_confidence = 0.0

    for c in chains:
        depth = c.get("max_depth_reached", 0)
        depth_dist[depth] = depth_dist.get(depth, 0) + 1

        decision = c.get("final_decision", "skip")
        decision_dist[decision] = decision_dist.get(decision, 0) + 1

        stop = c.get("stopping_reason", "unknown")
        stop_dist[stop] = stop_dist.get(stop, 0) + 1

        avg_confidence += float(c.get("final_confidence", 0))

    return {
        "total": len(chains),
        "depth_distribution": depth_dist,
        "decision_distribution": decision_dist,
        "stopping_reasons": stop_dist,
        "avg_confidence": round(avg_confidence / len(chains), 3),
    }


@traced("meta")
def get_todays_catalysts() -> list[dict]:
    """Catalyst events from today."""
    return sb_get("catalyst_events", {
        "select": "ticker,catalyst_type,headline,direction,magnitude,sentiment_score",
        "event_time": f"gte.{TODAY_STR}T00:00:00Z",
        "order": "event_time.desc",
        "limit": "20",
    })


@traced("meta")
def get_shadow_divergence_summary() -> dict:
    """Get today's shadow divergences for meta-analysis context.

    Also importable by health_check.py and test_system.py:
        from meta_analysis import get_shadow_divergence_summary
    """
    today = date.today().isoformat()
    divs = sb_get("shadow_divergences", {
        "select": "ticker,live_decision,live_confidence,shadow_profile,shadow_type,"
                  "shadow_decision,shadow_confidence,shadow_stopping_reason,"
                  "first_diverged_at_tumbler,shadow_was_right,save_value",
        "divergence_date": f"eq.{today}",
    })

    if not divs:
        return {"count": 0, "divergences": [], "unanimous_dissent": []}

    live_entries = {d["ticker"] for d in divs if d["live_decision"] in ("enter", "strong_enter")}
    all_shadow_profiles = {d["shadow_profile"] for d in divs}
    unanimous_dissent = []

    for ticker in live_entries:
        ticker_divs = [d for d in divs if d["ticker"] == ticker]
        dissenting_profiles = {
            d["shadow_profile"] for d in ticker_divs
            if d["shadow_decision"] not in ("enter", "strong_enter")
        }
        if dissenting_profiles == all_shadow_profiles and len(dissenting_profiles) >= 2:
            unanimous_dissent.append({
                "ticker": ticker,
                "live_confidence": max(d["live_confidence"] for d in ticker_divs if d.get("live_confidence")),
                "shadow_reasons": [
                    f"{d['shadow_profile']}: {d['shadow_decision']} ({d.get('shadow_stopping_reason', '?')})"
                    for d in ticker_divs
                ],
            })

    return {
        "count": len(divs),
        "divergences": divs[:20],
        "unanimous_dissent": unanimous_dissent,
    }


# ===========================================================================
# Daily reflection generation
# ===========================================================================

@traced("meta")
def generate_daily_reflection(context: dict) -> tuple[dict, float]:
    """Use Claude Sonnet to generate daily meta-reflection. Returns (reflection, cost)."""

    rag_context = context.get("rag_context", {})
    rag_section = ""
    if rag_context.get("reflections"):
        rag_section += "\n### Similar Past Reflections (RAG)\n"
        for ref in rag_context["reflections"][:3]:
            rag_section += f"- {ref.get('reflection_date', '?')}: {ref.get('patterns_observed', '')[:120]}\n"

    if rag_context.get("catalysts"):
        rag_section += "\n### Related Recent Catalysts (RAG)\n"
        for cat in rag_context["catalysts"][:5]:
            rag_section += f"- {cat.get('headline', '')[:100]} ({cat.get('catalyst_type', '?')}, {cat.get('direction', '?')})\n"

    chain_section = ""
    chain_analysis = context.get("chain_analysis", {})
    if chain_analysis.get("total", 0) > 0:
        chain_section = f"""
### Inference Chain Analysis
Total chains: {chain_analysis['total']}
Avg confidence: {chain_analysis.get('avg_confidence', 0)}
Depth distribution: {json.dumps(chain_analysis.get('depth_distribution', {}))}
Decision distribution: {json.dumps(chain_analysis.get('decision_distribution', {}))}
Stopping reasons: {json.dumps(chain_analysis.get('stopping_reasons', {}))}
"""

    catalyst_section = ""
    cat_corr = context.get("catalyst_correlation", {})
    if cat_corr.get("total_catalysts", 0) > 0:
        catalyst_section = f"""
### Catalyst Correlation
{cat_corr.get('trades_with_catalysts', 0)}/{cat_corr.get('total_trades', 0)} trades had catalyst support ({cat_corr.get('catalyst_driven_pct', 0)}%)
Total catalysts today: {cat_corr.get('total_catalysts', 0)}
"""

    adj_section = ""
    adj_impact = context.get("adjustment_impact", [])
    if adj_impact:
        adj_section = "\n### Strategy Adjustment Impact\n"
        for adj in adj_impact:
            adj_section += f"- {adj['parameter']}: {adj['trades_since']} trades, ${adj['pnl_since']} P&L since applied\n"

    shadow_section = ""
    shadow_divs = context.get("shadow_divergences", {})
    if shadow_divs.get("count", 0) > 0:
        unanimous = shadow_divs.get("unanimous_dissent", [])
        shadow_section = f"""
### Shadow Profile Divergences (adversarial ensemble)
{json.dumps(shadow_divs, indent=2, default=str)[:800]}
"""
        if unanimous:
            shadow_section += (
                "\n**UNANIMOUS DISSENT DETECTED** — all shadow profiles disagreed with the live profile "
                "on the following tickers. This is a HIGH PRIORITY signal — analyze what the shadows saw "
                "that the live profile missed.\n"
            )

    prompt = f"""You are a trading system meta-analyst. Analyze today's trading operations and generate insights.

## Today's Data ({TODAY_STR})

### Pipeline Health
{json.dumps(context['pipeline_health'], indent=2)}

### Signal Evaluations
{json.dumps(context['signal_accuracy'], indent=2)}

### Data Quality Issues
{json.dumps(context['dq_issues'], indent=2)}

### Trades Made Today
{json.dumps(context['trades'], indent=2)}

### Order Events
{json.dumps(context['order_events'], indent=2)}

### Today's Catalysts
{json.dumps(context.get('catalysts', []), indent=2)}
{rag_section}{chain_section}{catalyst_section}{adj_section}{shadow_section}
## Instructions
Respond with a JSON object containing these fields:
- patterns_observed: Key patterns you see in today's operations, including cross-layer patterns between signals, catalysts, and inference chains (3-4 sentences)
- signal_assessment: How each signal performed, which ones were predictive vs noise, and how inference depth correlated with outcomes (2-3 sentences)
- operational_issues: Any pipeline failures, data quality problems, or timing issues (1-2 sentences, "None" if clean)
- counterfactuals: What would have happened with different signal weights or thresholds, referencing similar past days from RAG context (2-3 sentences)
- catalyst_insights: How catalysts correlated with trade outcomes today, any missed catalysts (1-2 sentences)
- adjustments: Array of objects with {{parameter_name, current_value, suggested_value, reason}} for any signal weight or threshold changes. Only suggest if data supports it. Empty array if no changes needed.

Respond ONLY with valid JSON, no markdown formatting."""

    cost = 0.0
    try:
        data = call_claude(
            model="claude-sonnet-4-6-20250514",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        if data is not None:
            content = data["content"][0]["text"]
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]

            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000

            return json.loads(content), cost
    except Exception as e:
        print(f"[meta_daily] Claude reflection failed: {e}")

    return {
        "patterns_observed": "Reflection generation failed — no Claude response",
        "signal_assessment": "Unable to assess",
        "operational_issues": "Meta-analysis pipeline itself failed",
        "counterfactuals": "N/A",
        "catalyst_insights": "N/A",
        "adjustments": [],
    }, cost


# ===========================================================================
# Weekly-specific data gathering
# ===========================================================================

@traced("meta")
def get_weekly_daily_reflections() -> list[dict]:
    """All daily reflections from this week."""
    return sb_get("meta_reflections", {
        "select": "reflection_date,patterns_observed,signal_assessment,operational_issues,counterfactuals,adjustments,pipeline_summary,signal_accuracy",
        "reflection_type": "eq.daily",
        "reflection_date": f"gte.{WEEK_START}",
        "order": "reflection_date.asc",
    })


def get_signal_accuracy_report() -> list[dict]:
    """Signal accuracy view for recent weeks."""
    return sb_get("signal_accuracy_report", {
        "order": "week_start.desc",
        "limit": "8",
    })


def get_previous_weekly_reflections() -> list[dict]:
    """Last 4 weekly reflections for trend analysis."""
    return sb_get("meta_reflections", {
        "select": "reflection_date,patterns_observed,signal_assessment,adjustments",
        "reflection_type": "eq.weekly",
        "order": "reflection_date.desc",
        "limit": "4",
    })


@traced("meta")
def get_week_trades() -> list[dict]:
    """All trades from this week."""
    return sb_get("trade_decisions", {
        "select": "ticker,action,pnl,outcome,signals_fired,hold_days,created_at",
        "created_at": f"gte.{WEEK_START}T00:00:00Z",
        "order": "created_at.asc",
    })


def get_strategy_adjustments() -> list[dict]:
    """All strategy adjustments and their impact."""
    return sb_get("strategy_adjustments", {
        "select": "parameter_name,previous_value,new_value,reason,status,trades_since_applied,pnl_since_applied,created_at",
        "order": "created_at.desc",
        "limit": "20",
    })


def get_pipeline_health_weekly() -> dict:
    """Aggregate pipeline health for the week."""
    runs = sb_get("pipeline_runs", {
        "select": "pipeline_name,status,duration_ms",
        "started_at": f"gte.{WEEK_START}T00:00:00Z",
        "step_name": "neq.root",
    })

    total = len(runs)
    if total == 0:
        return {"total": 0, "success_rate": 0}

    successes = sum(1 for r in runs if r["status"] == "success")
    by_pipeline: dict[str, dict[str, int]] = {}
    for r in runs:
        name = r["pipeline_name"]
        if name not in by_pipeline:
            by_pipeline[name] = {"total": 0, "success": 0}
        by_pipeline[name]["total"] += 1
        if r["status"] == "success":
            by_pipeline[name]["success"] += 1

    return {
        "total": total,
        "successes": successes,
        "success_rate": round(successes / total * 100, 1),
        "by_pipeline": by_pipeline,
    }


def get_week_inference_chains() -> list[dict]:
    """All inference chains from this week."""
    return sb_get("inference_chains", {
        "select": "ticker,chain_date,max_depth_reached,final_confidence,final_decision,stopping_reason,tumblers,actual_outcome,actual_pnl,catalyst_event_ids,pattern_template_ids",
        "chain_date": f"gte.{WEEK_START}",
        "order": "chain_date.asc",
    })


@traced("meta")
def get_week_catalysts() -> list[dict]:
    """All catalyst events from this week."""
    return sb_get("catalyst_events", {
        "select": "ticker,catalyst_type,headline,direction,magnitude,sentiment_score,actual_impact_pct",
        "event_time": f"gte.{WEEK_START}T00:00:00Z",
        "order": "event_time.desc",
        "limit": "50",
    })


def get_latest_calibration() -> dict | None:
    """Latest calibration report."""
    rows = sb_get("confidence_calibration", {
        "select": "calibration_week,buckets,brier_score,calibration_error,overconfidence_bias,active_factors,depth_factors",
        "order": "calibration_week.desc",
        "limit": "1",
    })
    return rows[0] if rows else None


def get_existing_patterns() -> list[dict]:
    """All active pattern templates."""
    return sb_get("pattern_templates", {
        "select": "id,pattern_name,pattern_description,pattern_category,times_matched,success_rate,status",
        "status": "eq.active",
        "order": "times_matched.desc",
    })


def get_tuning_performance() -> list[dict]:
    """Tuning profile performance comparison view."""
    return sb_get("tuning_profile_performance", {
        "order": "version.desc",
    })


@traced("meta")
def cross_layer_analysis(chains: list[dict], trades: list[dict], catalysts: list[dict]) -> dict:
    """Analyze cross-layer patterns: catalyst-driven vs non-catalyst entries."""
    catalyst_tickers = {c["ticker"] for c in catalysts if c.get("ticker")}

    catalyst_driven_trades = [t for t in trades if t["ticker"] in catalyst_tickers]
    non_catalyst_trades = [t for t in trades if t["ticker"] not in catalyst_tickers]

    def trade_stats(trade_list: list[dict]) -> dict:
        if not trade_list:
            return {"count": 0, "avg_pnl": 0, "win_rate": 0}
        total = len(trade_list)
        pnls = [float(t.get("pnl", 0) or 0) for t in trade_list]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "count": total,
            "avg_pnl": round(sum(pnls) / total, 2) if total else 0,
            "win_rate": round(wins / total * 100, 1) if total else 0,
        }

    depth_outcomes: dict[int, dict[str, int]] = {}
    for c in chains:
        depth = c.get("max_depth_reached", 0)
        outcome = c.get("actual_outcome", "")
        if depth not in depth_outcomes:
            depth_outcomes[depth] = {"total": 0, "wins": 0}
        depth_outcomes[depth]["total"] += 1
        if outcome in ("STRONG_WIN", "WIN"):
            depth_outcomes[depth]["wins"] += 1

    return {
        "catalyst_driven": trade_stats(catalyst_driven_trades),
        "non_catalyst": trade_stats(non_catalyst_trades),
        "depth_win_rates": {
            str(d): round(v["wins"] / v["total"] * 100, 1) if v["total"] > 0 else 0
            for d, v in sorted(depth_outcomes.items())
        },
        "total_chains": len(chains),
        "chains_with_catalysts": sum(1 for c in chains if c.get("catalyst_event_ids")),
        "chains_with_patterns": sum(1 for c in chains if c.get("pattern_template_ids")),
    }


# ===========================================================================
# Weekly-specific: pattern discovery
# ===========================================================================

@traced("meta")
def discover_patterns(chains: list[dict], existing_patterns: list[dict]) -> list[dict]:
    """Discover new pattern templates by clustering similar chains.

    Uses Claude to name and describe discovered patterns.
    """
    if len(chains) < 5:
        return []

    clusters: dict[str, list] = {}
    for c in chains:
        key = f"{c.get('final_decision', 'skip')}_{c.get('stopping_reason', 'unknown')}"
        if key not in clusters:
            clusters[key] = []
        clusters[key].append(c)

    significant_clusters = {k: v for k, v in clusters.items() if len(v) >= 3}
    if not significant_clusters:
        return []

    existing_names = {p["pattern_name"] for p in existing_patterns}

    cluster_desc = ""
    for key, cluster_chains in list(significant_clusters.items())[:5]:
        cluster_desc += f"\nCluster '{key}' ({len(cluster_chains)} chains):\n"
        for c in cluster_chains[:3]:
            cluster_desc += (
                f"  - {c.get('ticker', '?')}: depth={c.get('max_depth_reached', 0)}, "
                f"conf={c.get('final_confidence', 0):.2f}, outcome={c.get('actual_outcome', '?')}\n"
            )

    prompt = f"""You are a pattern recognition system for a trading agent. Identify reusable patterns from these inference chain clusters.

Existing patterns (avoid duplicates): {', '.join(existing_names) if existing_names else 'None yet'}

Chain clusters from this week:
{cluster_desc}

For each NEW pattern you identify (max 3), respond with a JSON array:
[{{"pattern_name": "snake_case_name", "pattern_description": "1-2 sentences", "pattern_category": "one of: catalyst_response, signal_combination, regime_transition, seasonal, sector_sympathy, mean_reversion, momentum_continuation", "trigger_conditions": {{"key": "value"}}}}]

If no new patterns are worth creating, respond with an empty array: []
Respond ONLY with valid JSON."""

    try:
        data = call_claude(
            model="claude-sonnet-4-6-20250514",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
        )
        if data is not None:
            content = data["content"][0]["text"]
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]

            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000
            if cost > 0:
                _post_to_supabase("cost_ledger", {
                    "ledger_date": TODAY.isoformat(),
                    "category": "claude_api",
                    "subcategory": "meta_weekly_pattern_discovery",
                    "amount": round(-cost, 6),
                    "description": "Weekly pattern template discovery",
                    "metadata": {
                        "model": "claude-sonnet-4-6",
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                })

            patterns = json.loads(content)
            new_patterns = [p for p in patterns if p.get("pattern_name") not in existing_names]
            return new_patterns[:3]
    except Exception as e:
        print(f"[meta_weekly] Pattern discovery failed: {e}")

    return []


# ===========================================================================
# Weekly reflection generation
# ===========================================================================

@traced("meta")
def generate_weekly_reflection(context: dict) -> tuple[dict, float]:
    """Use Claude Sonnet for deeper weekly analysis. Returns (reflection, cost)."""

    cal_section = ""
    calibration = context.get("calibration")
    if calibration:
        cal_section = f"""
### Confidence Calibration
Brier score: {calibration.get('brier_score', 'N/A')} (0 = perfect)
Calibration error: {calibration.get('calibration_error', 'N/A')}
Overconfidence bias: {calibration.get('overconfidence_bias', 'N/A')} (positive = overconfident)
Buckets: {json.dumps(calibration.get('buckets', []), indent=2)}
"""

    cross_section = ""
    cross = context.get("cross_layer", {})
    if cross:
        cross_section = f"""
### Cross-Layer Analysis
Catalyst-driven trades: {json.dumps(cross.get('catalyst_driven', {}), indent=2)}
Non-catalyst trades: {json.dumps(cross.get('non_catalyst', {}), indent=2)}
Depth win rates: {json.dumps(cross.get('depth_win_rates', {}), indent=2)}
Chains with catalysts: {cross.get('chains_with_catalysts', 0)}/{cross.get('total_chains', 0)}
Chains with pattern matches: {cross.get('chains_with_patterns', 0)}/{cross.get('total_chains', 0)}
"""

    chain_section = ""
    chains = context.get("chains", [])
    if chains:
        depth_dist: dict[int, int] = {}
        decision_dist: dict[str, int] = {}
        for c in chains:
            d = c.get("max_depth_reached", 0)
            depth_dist[d] = depth_dist.get(d, 0) + 1
            dec = c.get("final_decision", "skip")
            decision_dist[dec] = decision_dist.get(dec, 0) + 1

        chain_section = f"""
### Inference Chain Summary (Week)
Total chains: {len(chains)}
Depth distribution: {json.dumps(depth_dist)}
Decision distribution: {json.dumps(decision_dist)}
Avg confidence: {sum(float(c.get('final_confidence', 0)) for c in chains) / len(chains):.3f}
"""

    tuning_section = ""
    tuning_perf = context.get("tuning_performance", [])
    if tuning_perf:
        tuning_section = "\n### Hardware Tuning Performance\n"
        for tp in tuning_perf[:5]:
            tuning_section += (
                f"- v{tp.get('version', '?')} '{tp.get('profile_name', '?')}' ({tp.get('power_mode', '?')}, {tp.get('status', '?')}): "
                f"{tp.get('total_runs', 0)} runs, avg {tp.get('avg_wall_clock_ms', 0)}ms, "
                f"peak RAM {tp.get('avg_ram_peak_mb', 0)}MB, "
                f"tok/s {tp.get('avg_tokens_per_sec', '--')}, "
                f"embed {tp.get('avg_embedding_ms', '--')}ms, "
                f"throttles {tp.get('total_throttle_events', 0)}, "
                f"chain win rate {tp.get('chain_win_rate_pct', '--')}%\n"
            )

    prompt = f"""You are a trading system meta-analyst doing a weekly review. Analyze the full week's operations, identify multi-day patterns, and propose strategic changes.

## Week of {WEEK_START} to {TODAY.isoformat()}

### Daily Reflections This Week
{json.dumps(context['daily_reflections'], indent=2, default=str)}

### Signal Accuracy Report (Recent Weeks)
{json.dumps(context['signal_accuracy'], indent=2, default=str)}

### Previous Weekly Reflections (Last 4 weeks, for trend comparison)
{json.dumps(context['prev_weekly'], indent=2, default=str)}

### This Week's Trades
{json.dumps(context['trades'], indent=2, default=str)}

### Strategy Adjustments History
{json.dumps(context['adjustments'], indent=2, default=str)}

### Pipeline Health (Week Aggregate)
{json.dumps(context['pipeline_health'], indent=2, default=str)}

### This Week's Catalysts
{json.dumps(context.get('catalysts', [])[:20], indent=2, default=str)}
{cal_section}{cross_section}{chain_section}{tuning_section}
## Instructions
Respond with a JSON object:
- patterns_observed: Multi-day patterns, recurring themes across this week, cross-layer patterns between signals/catalysts/chains (3-4 sentences)
- signal_assessment: Which signals were most/least predictive this week, trend vs last 4 weeks, how inference depth correlated with outcomes (3-4 sentences)
- operational_issues: System reliability issues, timing problems, data quality trends, thermal throttling or resource constraints (2-3 sentences, "None" if clean)
- counterfactuals: "If we had changed X, we would have Y" analysis based on the week's data (2-3 sentences)
- catalyst_insights: How catalysts correlated with trade outcomes, which catalyst types were most predictive (2-3 sentences)
- calibration_notes: Analysis of stated vs actual confidence, where the system is over/under-confident (2-3 sentences)
- tuning_notes: Hardware tuning observations — did inference speed affect stopping_reason, did thermal throttles coincide with poor decisions, any recommended tuning profile changes (2-3 sentences, "N/A" if no tuning data)
- adjustments: Array of {{parameter_name, current_value, suggested_value, reason}}. Be more willing to suggest changes here than in daily reviews — weekly data gives more confidence. Include hardware tuning suggestions if data supports them. Empty array if no changes warranted.
- strategy_evolution_notes: Higher-level observations about how the strategy should evolve over time, including infrastructure optimizations (2-3 sentences)

Respond ONLY with valid JSON, no markdown formatting."""

    cost = 0.0
    try:
        data = call_claude(
            model="claude-sonnet-4-6-20250514",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        if data is not None:
            content = data["content"][0]["text"]
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]

            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000

            return json.loads(content), cost
    except Exception as e:
        print(f"[meta_weekly] Claude reflection failed: {e}")

    return {
        "patterns_observed": "Weekly reflection generation failed",
        "signal_assessment": "Unable to assess",
        "operational_issues": "Meta-analysis pipeline itself failed",
        "counterfactuals": "N/A",
        "catalyst_insights": "N/A",
        "calibration_notes": "N/A",
        "tuning_notes": "N/A",
        "adjustments": [],
        "strategy_evolution_notes": "N/A",
    }, cost


# ===========================================================================
# Main runners
# ===========================================================================

def run_daily() -> None:
    """Daily meta-analysis — mirrors original meta_daily.py run()."""
    global TODAY_STR
    TODAY_STR = date.today().isoformat()

    tracer = PipelineTracer("meta_daily", metadata={"date": TODAY_STR})
    set_active_tracer(tracer)

    stored = None

    try:
        with tracer.step("gather_pipeline_health") as result:
            pipeline_health = get_pipeline_health()
            result.set(pipeline_health)

        with tracer.step("gather_signal_accuracy") as result:
            signal_accuracy = get_signal_accuracy()
            result.set(signal_accuracy)

        with tracer.step("gather_data_quality") as result:
            dq_issues = get_data_quality_issues()
            result.set({"issues": len(dq_issues)})

        with tracer.step("gather_trades") as result:
            trades = get_todays_trades()
            order_events = get_order_events()
            result.set({"trades": len(trades), "orders": len(order_events)})

        with tracer.step("gather_chain_analysis") as result:
            chain_analysis = get_inference_chain_analysis()
            result.set(chain_analysis)

        with tracer.step("gather_catalysts") as result:
            catalysts = get_todays_catalysts()
            result.set({"count": len(catalysts)})

        with tracer.step("gather_catalyst_correlation") as result:
            catalyst_correlation = get_catalyst_correlation(trades, catalysts)
            result.set(catalyst_correlation)

        with tracer.step("update_adjustment_impact") as result:
            adjustment_impact = update_adjustment_impact()
            result.set({"updated": len(adjustment_impact)})

        with tracer.step("gather_shadow_divergences") as result:
            shadow_data = get_shadow_divergence_summary()
            result.set({
                "divergences": shadow_data["count"],
                "unanimous": len(shadow_data.get("unanimous_dissent", [])),
            })

        with tracer.step("rag_retrieve") as result:
            embed_text = (
                f"Date: {TODAY_STR}. "
                f"Pipeline health: {pipeline_health.get('success_rate', 0)}%. "
                f"Signal accuracy: {json.dumps(signal_accuracy.get('decisions', {}))}. "
                f"Chain analysis: avg confidence {chain_analysis.get('avg_confidence', 0)}, "
                f"depth dist {json.dumps(chain_analysis.get('depth_distribution', {}))}."
            )
            rag_context = rag_retrieve_context(embed_text)
            result.set({
                "reflections": len(rag_context.get("reflections", [])),
                "signals": len(rag_context.get("signals", [])),
                "catalysts": len(rag_context.get("catalysts", [])),
            })

        context = {
            "pipeline_health": pipeline_health,
            "signal_accuracy": signal_accuracy,
            "dq_issues": dq_issues,
            "trades": trades,
            "order_events": order_events,
            "chain_analysis": chain_analysis,
            "catalysts": catalysts,
            "catalyst_correlation": catalyst_correlation,
            "adjustment_impact": adjustment_impact,
            "rag_context": rag_context,
            "shadow_divergences": shadow_data,
        }

        with tracer.step("generate_reflection") as result:
            reflection, claude_cost = generate_daily_reflection(context)
            result.set({
                "has_adjustments": len(reflection.get("adjustments", [])),
                "claude_cost": claude_cost,
            })

        if claude_cost > 0:
            _post_to_supabase("cost_ledger", {
                "ledger_date": TODAY_STR,
                "category": "claude_api",
                "subcategory": "meta_daily",
                "amount": round(-claude_cost, 6),
                "description": f"Meta daily reflection for {TODAY_STR}",
                "metadata": {"model": "claude-sonnet-4-6"},
                "pipeline_run_id": tracer.root_id,
            })

        with tracer.step("generate_embedding") as result:
            embed_text = (
                f"Date: {TODAY_STR}. "
                f"Patterns: {reflection.get('patterns_observed', '')}. "
                f"Signals: {reflection.get('signal_assessment', '')}. "
                f"Issues: {reflection.get('operational_issues', '')}. "
                f"Catalysts: {reflection.get('catalyst_insights', '')}."
            )
            embedding = generate_embedding(embed_text)
            result.set({"has_embedding": embedding is not None})

        with tracer.step("store_reflection") as result:
            reflection_data = {
                "reflection_date": TODAY_STR,
                "reflection_type": "daily",
                "pipeline_summary": pipeline_health,
                "signal_accuracy": signal_accuracy,
                "patterns_observed": reflection.get("patterns_observed", ""),
                "signal_assessment": reflection.get("signal_assessment", ""),
                "operational_issues": reflection.get("operational_issues", ""),
                "counterfactuals": reflection.get("counterfactuals", ""),
                "adjustments": reflection.get("adjustments", []),
                "pipeline_run_id": tracer.root_id,
            }
            if embedding:
                reflection_data["embedding"] = embedding

            stored = _post_to_supabase("meta_reflections", reflection_data)
            result.set({"stored": stored is not None})

        adjustments = reflection.get("adjustments", [])
        if adjustments:
            with tracer.step("process_adjustments", input_snapshot={"count": len(adjustments)}) as result:
                approved = auto_approve_adjustments(adjustments)
                for adj in approved:
                    _post_to_supabase("strategy_adjustments", {
                        "parameter_name": adj.get("parameter_name", "unknown"),
                        "previous_value": str(adj.get("current_value", "")),
                        "new_value": str(adj.get("suggested_value", "")),
                        "reason": adj.get("reason", ""),
                        "status": adj.get("status", "proposed"),
                        "meta_reflection_id": stored.get("id") if stored else None,
                    })
                result.set({
                    "total": len(approved),
                    "auto_approved": sum(1 for a in approved if a["status"] == "approved"),
                })

        tracer.complete({"reflection_generated": True, "adjustments": len(adjustments)})
        print(f"[meta_daily] Complete. Patterns: {reflection.get('patterns_observed', 'N/A')[:100]}")
        adj_summary = f"`{len(adjustments)}` adjustments proposed" if adjustments else "no adjustments proposed"
        slack_notify(
            f"*Meta Daily ({TODAY_STR})* — pipeline health `{pipeline_health.get('success_rate', 0)}%` · {adj_summary}\n"
            f"{reflection.get('patterns_observed', 'N/A')[:120]}"
        )

    except Exception as e:
        tracer.fail(str(e))
        print(f"[meta_daily] Failed: {e}")
        slack_notify(f"*Meta Daily FATAL*: {e}")
        raise


def run_weekly() -> None:
    """Weekly meta-analysis — mirrors original meta_weekly.py run()."""
    global TODAY, WEEK_START
    TODAY = date.today()
    WEEK_START = (TODAY - timedelta(days=TODAY.weekday())).isoformat()

    tracer = PipelineTracer("meta_weekly", metadata={"week_start": WEEK_START})
    set_active_tracer(tracer)

    stored = None
    new_patterns: list[dict] = []

    try:
        with tracer.step("gather_daily_reflections") as result:
            daily_reflections = get_weekly_daily_reflections()
            result.set({"count": len(daily_reflections)})

        with tracer.step("gather_signal_accuracy") as result:
            signal_accuracy = get_signal_accuracy_report()
            result.set({"weeks": len(signal_accuracy)})

        with tracer.step("gather_previous_weekly") as result:
            prev_weekly = get_previous_weekly_reflections()
            result.set({"count": len(prev_weekly)})

        with tracer.step("gather_trades") as result:
            trades = get_week_trades()
            result.set({"count": len(trades)})

        with tracer.step("gather_adjustments") as result:
            adjustments = get_strategy_adjustments()
            result.set({"count": len(adjustments)})

        with tracer.step("gather_pipeline_health") as result:
            pipeline_health = get_pipeline_health_weekly()
            result.set(pipeline_health)

        with tracer.step("gather_inference_chains") as result:
            chains = get_week_inference_chains()
            result.set({"count": len(chains)})

        with tracer.step("gather_catalysts") as result:
            catalysts = get_week_catalysts()
            result.set({"count": len(catalysts)})

        with tracer.step("gather_calibration") as result:
            calibration = get_latest_calibration()
            result.set({"has_calibration": calibration is not None})

        with tracer.step("cross_layer_analysis") as result:
            cross_layer = cross_layer_analysis(chains, trades, catalysts)
            result.set(cross_layer)

        with tracer.step("gather_tuning_performance") as result:
            tuning_perf = get_tuning_performance()
            result.set({"profiles": len(tuning_perf)})

        with tracer.step("discover_patterns") as result:
            existing_patterns = get_existing_patterns()
            new_patterns = discover_patterns(chains, existing_patterns)
            result.set({"discovered": len(new_patterns)})

        if new_patterns:
            with tracer.step("store_patterns") as result:
                stored_count = 0
                for pattern in new_patterns:
                    embed_text = f"{pattern.get('pattern_name', '')}: {pattern.get('pattern_description', '')}"
                    embedding = generate_embedding(embed_text)

                    pattern_data: dict = {
                        "pattern_name": pattern["pattern_name"],
                        "pattern_description": pattern.get("pattern_description", ""),
                        "pattern_category": pattern.get("pattern_category", "signal_combination"),
                        "trigger_conditions": pattern.get("trigger_conditions", {}),
                    }
                    if embedding:
                        pattern_data["embedding"] = embedding

                    pat_stored = _post_to_supabase("pattern_templates", pattern_data)
                    if pat_stored:
                        stored_count += 1
                result.set({"stored": stored_count})

        context = {
            "daily_reflections": daily_reflections,
            "signal_accuracy": signal_accuracy,
            "prev_weekly": prev_weekly,
            "trades": trades,
            "adjustments": adjustments,
            "pipeline_health": pipeline_health,
            "chains": chains,
            "catalysts": catalysts,
            "calibration": calibration,
            "cross_layer": cross_layer,
            "tuning_performance": tuning_perf,
        }

        with tracer.step("generate_reflection") as result:
            reflection, claude_cost = generate_weekly_reflection(context)
            result.set({
                "has_adjustments": len(reflection.get("adjustments", [])),
                "claude_cost": claude_cost,
            })

        if claude_cost > 0:
            _post_to_supabase("cost_ledger", {
                "ledger_date": TODAY.isoformat(),
                "category": "claude_api",
                "subcategory": "meta_weekly",
                "amount": round(-claude_cost, 6),
                "description": f"Meta weekly reflection for week of {WEEK_START}",
                "metadata": {"model": "claude-sonnet-4-6"},
                "pipeline_run_id": tracer.root_id,
            })

        with tracer.step("generate_embedding") as result:
            embed_text = (
                f"Weekly reflection {WEEK_START}. "
                f"Patterns: {reflection.get('patterns_observed', '')}. "
                f"Signals: {reflection.get('signal_assessment', '')}. "
                f"Catalysts: {reflection.get('catalyst_insights', '')}. "
                f"Calibration: {reflection.get('calibration_notes', '')}. "
                f"Evolution: {reflection.get('strategy_evolution_notes', '')}."
            )
            embedding = generate_embedding(embed_text)
            result.set({"has_embedding": embedding is not None})

        with tracer.step("store_reflection") as result:
            reflection_data = {
                "reflection_date": TODAY.isoformat(),
                "reflection_type": "weekly",
                "pipeline_summary": pipeline_health,
                "signal_accuracy": {
                    "weekly_report": signal_accuracy,
                    "evolution_notes": reflection.get("strategy_evolution_notes", ""),
                    "calibration_notes": reflection.get("calibration_notes", ""),
                    "catalyst_insights": reflection.get("catalyst_insights", ""),
                },
                "patterns_observed": reflection.get("patterns_observed", ""),
                "signal_assessment": reflection.get("signal_assessment", ""),
                "operational_issues": reflection.get("operational_issues", ""),
                "counterfactuals": reflection.get("counterfactuals", ""),
                "adjustments": reflection.get("adjustments", []),
                "pipeline_run_id": tracer.root_id,
            }
            if embedding:
                reflection_data["embedding"] = embedding

            stored = _post_to_supabase("meta_reflections", reflection_data)
            result.set({"stored": stored is not None})

        adj_list = reflection.get("adjustments", [])
        if adj_list:
            with tracer.step("process_adjustments") as result:
                for adj in adj_list:
                    _post_to_supabase("strategy_adjustments", {
                        "parameter_name": adj.get("parameter_name", "unknown"),
                        "previous_value": str(adj.get("current_value", "")),
                        "new_value": str(adj.get("suggested_value", "")),
                        "reason": adj.get("reason", ""),
                        "status": "proposed",
                        "meta_reflection_id": stored.get("id") if stored else None,
                    })
                result.set({"proposed": len(adj_list)})

        tracer.complete({
            "weekly_reflection_generated": True,
            "patterns_discovered": len(new_patterns),
        })
        print(f"[meta_weekly] Complete. Patterns: {reflection.get('patterns_observed', 'N/A')[:100]}")
        pattern_note = f"`{len(new_patterns)}` new patterns discovered" if new_patterns else "no new patterns"
        trade_count = len(trades)
        wins = sum(1 for t in trades if float(t.get("pnl", 0) or 0) > 0)
        win_rate = round(wins / trade_count * 100) if trade_count else 0
        slack_notify(
            f"*Meta Weekly (w/o {WEEK_START})* — `{trade_count}` trades, `{win_rate}%` win rate · {pattern_note}\n"
            f"{reflection.get('patterns_observed', 'N/A')[:120]}"
        )

    except Exception as e:
        tracer.fail(str(e))
        print(f"[meta_weekly] Failed: {e}")
        slack_notify(f"*Meta Weekly FATAL*: {e}")
        raise


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Meta-analysis — daily and weekly reflections with RAG + chain analysis.",
    )
    parser.add_argument(
        "frequency",
        choices=["daily", "weekly"],
        help="'daily' runs at 4:30 PM ET, 'weekly' runs Sunday 7 PM ET",
    )
    args = parser.parse_args()

    if args.frequency == "daily":
        from loki_logger import get_logger
        _logger = get_logger("meta_daily")
        run_daily()
    else:
        from loki_logger import get_logger
        _logger = get_logger("meta_weekly")
        run_weekly()
