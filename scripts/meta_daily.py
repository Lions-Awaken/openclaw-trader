#!/usr/bin/env python3
"""
Meta-Daily Analysis — runs at 4:30 PM ET daily.

1. Query today's pipeline_runs -> health summary
2. Query today's signal_evaluations -> per-signal accuracy
3. RAG retrieve similar past days (reflections + signal patterns + catalysts)
4. Inference chain depth analysis
5. Catalyst correlation
6. Adjustment impact tracking (trades_since_applied, pnl_since_applied)
7. Claude Sonnet generates meta-reflection with all context
8. Propose strategy adjustments (auto-approve within +/-5% weight bounds)
9. Embed, store, and log cost
"""

import json
import os
import sys
from datetime import date

import httpx

# Reuse tracer for pipeline observability
sys.path.insert(0, os.path.dirname(__file__))
from tracer import PipelineTracer, _patch_supabase, _post_to_supabase, _sb_headers

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Reusable HTTP client
_client = httpx.Client(timeout=15.0)

TODAY = ""  # Reassigned at the start of run()


def sb_get(path: str, params: dict | None = None) -> list:
    """GET from Supabase REST API."""
    client = _client
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
    client = _client
    resp = client.post(
        f"{SUPABASE_URL}/rest/v1/rpc/{fn_name}",
        headers=_sb_headers(),
        json=params,
    )
    if resp.status_code == 200:
        return resp.json()
    return []


def get_pipeline_health() -> dict:
    """Today's pipeline run stats."""
    runs = sb_get("pipeline_runs", {
        "select": "pipeline_name,step_name,status,duration_ms,error_message",
        "started_at": f"gte.{TODAY}T00:00:00Z",
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


def get_signal_accuracy() -> dict:
    """Today's signal evaluation stats."""
    evals = sb_get("signal_evaluations", {
        "select": "ticker,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision",
        "scan_date": f"eq.{TODAY}",
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

    decisions = {}
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
        "checked_at": f"gte.{TODAY}T00:00:00Z",
        "passed": "eq.false",
    })


def get_todays_trades() -> list[dict]:
    """Trades made today."""
    return sb_get("trade_decisions", {
        "select": "ticker,action,pnl,outcome,signals_fired,reasoning",
        "created_at": f"gte.{TODAY}T00:00:00Z",
    })


def get_order_events() -> list[dict]:
    """Today's order events."""
    return sb_get("order_events", {
        "select": "ticker,event_type,side,qty_ordered,qty_filled,avg_fill_price",
        "created_at": f"gte.{TODAY}T00:00:00Z",
    })


def get_inference_chain_analysis() -> dict:
    """Today's inference chain depth and decision analysis."""
    chains = sb_get("inference_chains", {
        "select": "ticker,max_depth_reached,final_confidence,final_decision,stopping_reason,tumblers",
        "chain_date": f"eq.{TODAY}",
    })

    if not chains:
        return {"total": 0}

    depth_dist = {}
    decision_dist = {}
    stop_dist = {}
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


def get_todays_catalysts() -> list[dict]:
    """Catalyst events from today."""
    return sb_get("catalyst_events", {
        "select": "ticker,catalyst_type,headline,direction,magnitude,sentiment_score",
        "event_time": f"gte.{TODAY}T00:00:00Z",
        "order": "event_time.desc",
        "limit": "20",
    })


def get_catalyst_correlation(trades: list[dict], catalysts: list[dict]) -> dict:
    """Correlate today's trades with catalyst events."""
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

        # Count trades since applied
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


def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding using local Ollama."""
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


def generate_reflection(context: dict) -> tuple[dict, float]:
    """Use Claude Sonnet to generate meta-reflection. Returns (reflection, cost)."""

    # Build RAG context section
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

    # Build chain analysis section
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

    # Build catalyst correlation section
    catalyst_section = ""
    cat_corr = context.get("catalyst_correlation", {})
    if cat_corr.get("total_catalysts", 0) > 0:
        catalyst_section = f"""
### Catalyst Correlation
{cat_corr.get('trades_with_catalysts', 0)}/{cat_corr.get('total_trades', 0)} trades had catalyst support ({cat_corr.get('catalyst_driven_pct', 0)}%)
Total catalysts today: {cat_corr.get('total_catalysts', 0)}
"""

    # Build adjustment impact section
    adj_section = ""
    adj_impact = context.get("adjustment_impact", [])
    if adj_impact:
        adj_section = "\n### Strategy Adjustment Impact\n"
        for adj in adj_impact:
            adj_section += f"- {adj['parameter']}: {adj['trades_since']} trades, ${adj['pnl_since']} P&L since applied\n"

    prompt = f"""You are a trading system meta-analyst. Analyze today's trading operations and generate insights.

## Today's Data ({TODAY})

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
{rag_section}{chain_section}{catalyst_section}{adj_section}
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
        client = _client
        resp = client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            content = data["content"][0]["text"]
            # Strip markdown code fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]

            # Calculate cost
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


def get_active_profile() -> dict:
    """Load the active strategy profile."""
    rows = sb_get("strategy_profiles", {
        "select": "profile_name,self_modify_enabled,self_modify_requires_approval,self_modify_max_delta_pct",
        "active": "eq.true",
        "limit": "1",
    })
    return rows[0] if rows else {}


def auto_approve_adjustments(adjustments: list[dict]) -> list[dict]:
    """Auto-approve adjustments based on active strategy profile settings."""
    profile = get_active_profile()
    self_modify = profile.get("self_modify_enabled", False)
    needs_approval = profile.get("self_modify_requires_approval", True)
    max_delta = float(profile.get("self_modify_max_delta_pct", 5.0))

    approved = []
    for adj in adjustments:
        try:
            prev = float(adj.get("current_value", "0").replace("%", ""))
            new = float(adj.get("suggested_value", "0").replace("%", ""))
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


def run():
    global TODAY
    TODAY = date.today().isoformat()

    tracer = PipelineTracer("meta_daily", metadata={"date": TODAY})

    try:
        # Step 1: Gather data
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

        # Step 2: Gather new tumbler-architecture data
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

        # Step 3: RAG retrieval
        with tracer.step("rag_retrieve") as result:
            embed_text = (
                f"Date: {TODAY}. "
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
        }

        # Step 4: Generate reflection
        with tracer.step("generate_reflection") as result:
            reflection, claude_cost = generate_reflection(context)
            result.set({"has_adjustments": len(reflection.get("adjustments", [])), "claude_cost": claude_cost})

        # Log Claude cost
        if claude_cost > 0:
            _post_to_supabase("cost_ledger", {
                "ledger_date": TODAY,
                "category": "claude_api",
                "subcategory": "meta_daily",
                "amount": round(-claude_cost, 6),
                "description": f"Meta daily reflection for {TODAY}",
                "metadata": {"model": "claude-sonnet-4-6"},
                "pipeline_run_id": tracer.root_id,
            })

        # Step 5: Generate embedding
        with tracer.step("generate_embedding") as result:
            embed_text = (
                f"Date: {TODAY}. "
                f"Patterns: {reflection.get('patterns_observed', '')}. "
                f"Signals: {reflection.get('signal_assessment', '')}. "
                f"Issues: {reflection.get('operational_issues', '')}. "
                f"Catalysts: {reflection.get('catalyst_insights', '')}."
            )
            embedding = generate_embedding(embed_text)
            result.set({"has_embedding": embedding is not None})

        # Step 6: Store reflection
        with tracer.step("store_reflection") as result:
            reflection_data = {
                "reflection_date": TODAY,
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

        # Step 7: Process adjustments
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

    except Exception as e:
        tracer.fail(str(e))
        print(f"[meta_daily] Failed: {e}")
        raise


if __name__ == "__main__":
    from loki_logger import get_logger
    _logger = get_logger("meta_daily")
    run()
