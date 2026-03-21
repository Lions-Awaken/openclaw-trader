#!/usr/bin/env python3
"""
Meta-Weekly Analysis — runs Sunday 7 PM ET.

1. Aggregate all daily reflections from the week
2. Full signal accuracy report
3. Trend analysis across last 4 weekly reflections
4. Pattern template discovery (cluster similar chains -> Claude names them)
5. Calibration report integration
6. Cross-layer analysis (catalyst-driven vs non-catalyst entries)
7. Deeper Claude Sonnet analysis
"""

import json
import os
import sys
from datetime import date, timedelta

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from tracer import PipelineTracer, _post_to_supabase, _sb_headers

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

TODAY = date.today()
WEEK_START = (TODAY - timedelta(days=TODAY.weekday())).isoformat()


def sb_get(path: str, params: dict | None = None) -> list:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers=_sb_headers(),
            params=params or {},
        )
        if resp.status_code == 200:
            return resp.json()
    return []


def sb_rpc(fn_name: str, params: dict) -> list:
    """Call a Supabase RPC function."""
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{fn_name}",
            headers=_sb_headers(),
            json=params,
        )
        if resp.status_code == 200:
            return resp.json()
    return []


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
    by_pipeline = {}
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


def cross_layer_analysis(chains: list[dict], trades: list[dict], catalysts: list[dict]) -> dict:
    """Analyze cross-layer patterns: catalyst-driven vs non-catalyst entries."""
    # Trades with catalyst support
    catalyst_tickers = {c["ticker"] for c in catalysts if c.get("ticker")}

    catalyst_driven_trades = [t for t in trades if t["ticker"] in catalyst_tickers]
    non_catalyst_trades = [t for t in trades if t["ticker"] not in catalyst_tickers]

    def trade_stats(trade_list):
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

    # Chain depth analysis
    depth_outcomes = {}
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


def discover_patterns(chains: list[dict], existing_patterns: list[dict]) -> list[dict]:
    """Discover new pattern templates by clustering similar chains.

    Uses Claude to name and describe discovered patterns.
    """
    if len(chains) < 5:
        return []

    # Group chains by decision + stopping_reason
    clusters: dict[str, list] = {}
    for c in chains:
        key = f"{c.get('final_decision', 'skip')}_{c.get('stopping_reason', 'unknown')}"
        if key not in clusters:
            clusters[key] = []
        clusters[key].append(c)

    # Only consider clusters with 3+ chains
    significant_clusters = {k: v for k, v in clusters.items() if len(v) >= 3}
    if not significant_clusters:
        return []

    existing_names = {p["pattern_name"] for p in existing_patterns}

    # Ask Claude to identify and name patterns
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
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6-20250514",
                    "max_tokens": 512,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data["content"][0]["text"]
                if content.startswith("```"):
                    content = content.split("\n", 1)[1].rsplit("```", 1)[0]

                # Log cost
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
                        "metadata": {"model": "claude-sonnet-4-6", "input_tokens": input_tokens, "output_tokens": output_tokens},
                    })

                patterns = json.loads(content)
                # Filter out duplicates
                new_patterns = [p for p in patterns if p.get("pattern_name") not in existing_names]
                return new_patterns[:3]
    except Exception as e:
        print(f"[meta_weekly] Pattern discovery failed: {e}")

    return []


def generate_embedding(text: str) -> list[float] | None:
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": text, "keep_alive": "0"},
            )
            if resp.status_code == 200:
                return resp.json().get("embedding")
    except Exception:
        pass
    return None


def generate_weekly_reflection(context: dict) -> tuple[dict, float]:
    """Use Claude Sonnet for deeper weekly analysis. Returns (reflection, cost)."""

    # Build calibration section
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

    # Build cross-layer section
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

    # Build inference chain summary
    chain_section = ""
    chains = context.get("chains", [])
    if chains:
        depth_dist = {}
        decision_dist = {}
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

    # Build tuning performance section
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
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6-20250514",
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
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


def run():
    tracer = PipelineTracer("meta_weekly", metadata={"week_start": WEEK_START})

    try:
        # Gather all existing data
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

        # New tumbler-architecture data
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

        # Pattern template discovery
        with tracer.step("discover_patterns") as result:
            existing_patterns = get_existing_patterns()
            new_patterns = discover_patterns(chains, existing_patterns)
            result.set({"discovered": len(new_patterns)})

        # Store discovered patterns
        if new_patterns:
            with tracer.step("store_patterns") as result:
                stored_count = 0
                for pattern in new_patterns:
                    embed_text = f"{pattern.get('pattern_name', '')}: {pattern.get('pattern_description', '')}"
                    embedding = generate_embedding(embed_text)

                    pattern_data = {
                        "pattern_name": pattern["pattern_name"],
                        "pattern_description": pattern.get("pattern_description", ""),
                        "pattern_category": pattern.get("pattern_category", "signal_combination"),
                        "trigger_conditions": pattern.get("trigger_conditions", {}),
                    }
                    if embedding:
                        pattern_data["embedding"] = embedding

                    stored = _post_to_supabase("pattern_templates", pattern_data)
                    if stored:
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

        # Generate weekly reflection
        with tracer.step("generate_reflection") as result:
            reflection, claude_cost = generate_weekly_reflection(context)
            result.set({"has_adjustments": len(reflection.get("adjustments", [])), "claude_cost": claude_cost})

        # Log Claude cost
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

        # Embed
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

        # Store
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

        # Process adjustments
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

    except Exception as e:
        tracer.fail(str(e))
        print(f"[meta_weekly] Failed: {e}")
        raise


if __name__ == "__main__":
    run()
