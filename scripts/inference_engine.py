#!/usr/bin/env python3
"""
Inference Engine — 5-Tumbler Lock & Tumbler Analysis.

Called by scanner.py for each candidate scoring 3+ signals.
Iterates through 5 tumbler levels with RAG retrieval at each.
Tracks confidence, applies stopping rules, logs full chain.

Tumbler 1: Technical Foundation (pure computation)
Tumbler 2: Fundamental + Sentiment Context (Perplexity if stale)
Tumbler 3: Flow + Cross-Asset Analysis (Ollama qwen2.5:3b)
Tumbler 4: Pattern Template Matching (Claude Sonnet if warranted)
Tumbler 5: Counterfactual + Final Synthesis (Claude Sonnet)

Stopping Rules evaluated after each tumbler:
  - veto_signal: sentiment < -0.5, regime DOWN_ANY
  - confidence_floor: below threshold for current depth
  - forced_connection: delta < 0.03 (new data added nothing)
  - conflicting_signals: layers fundamentally disagree
  - insufficient_data: no RAG context available
  - resource_limit: Claude budget exhausted
  - time_limit: > 30 seconds elapsed
"""

import json
import os
import sys
import time
from datetime import date, datetime

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_API_KEY_2,
    OLLAMA_URL,
    _claude_client,
    generate_embedding,
    sb_get,
    sb_rpc,
)
from shadow_profiles import get_shadow_context
from tracer import _post_to_supabase, traced

# Default confidence thresholds (overridden by active strategy profile)
CONFIDENCE_THRESHOLDS = {
    1: 0.25,
    2: 0.40,
    3: 0.55,
    4: 0.65,
    5: 0.75,
}

FORCED_CONNECTION_DELTA = 0.03
TIME_LIMIT = 30

DECISION_THRESHOLDS = {
    "strong_enter": 0.75,
    "enter": 0.60,
    "watch": 0.45,
    "skip": 0.20,
}

TODAY = ""  # Reassigned at the start of run_inference()

# Active strategy profile — loaded at runtime
_active_profile: dict | None = None


def load_active_profile() -> dict:
    """Load the active strategy profile from Supabase."""
    global _active_profile
    rows = sb_get("strategy_profiles", {
        "select": "profile_name,min_signal_score,min_tumbler_depth,min_confidence,"
                  "max_risk_per_trade_pct,max_concurrent_positions,position_size_method,"
                  "trade_style,circuit_breakers_enabled,self_modify_enabled,"
                  "self_modify_requires_approval,annual_target_pct",
        "active": "eq.true",
        "limit": "1",
    })
    if rows:
        _active_profile = rows[0]
        # Override decision thresholds based on profile's min_confidence
        min_conf = float(_active_profile.get("min_confidence", 0.60))
        DECISION_THRESHOLDS["enter"] = min_conf
        DECISION_THRESHOLDS["strong_enter"] = min(1.0, min_conf + 0.15)
        DECISION_THRESHOLDS["watch"] = max(0.10, min_conf - 0.15)
        DECISION_THRESHOLDS["skip"] = max(0.05, min_conf - 0.40)

        # Override confidence floor thresholds based on min_tumbler_depth
        min_depth = int(_active_profile.get("min_tumbler_depth", 3))
        base_conf = float(_active_profile.get("min_confidence", 0.60))
        for d in range(1, 6):
            if d < min_depth:
                # Below min depth, use scaled-down thresholds
                CONFIDENCE_THRESHOLDS[d] = max(0.10, base_conf * (d / 5) * 0.6)
            else:
                CONFIDENCE_THRESHOLDS[d] = max(0.10, base_conf * (d / 5))

        print(f"[inference_engine] Loaded profile: {_active_profile.get('profile_name', '?')} "
              f"(min_signals={_active_profile.get('min_signal_score')}, "
              f"min_conf={min_conf}, style={_active_profile.get('trade_style')})")
    else:
        _active_profile = {}
        print("[inference_engine] No active profile found, using defaults")

    return _active_profile


def get_min_signal_score() -> int:
    """Get minimum signal score from active profile."""
    if _active_profile:
        return int(_active_profile.get("min_signal_score", 4))
    return 4


def get_todays_claude_spend() -> float:
    """Check how much Claude API budget has been used today."""
    rows = sb_get("cost_ledger", {
        "select": "amount",
        "category": "eq.claude_api",
        "ledger_date": f"eq.{TODAY}",
    })
    return abs(sum(float(r.get("amount", 0)) for r in rows))


def get_claude_budget() -> float:
    """Get daily Claude budget from config."""
    rows = sb_get("budget_config", {
        "select": "value",
        "config_key": "eq.daily_claude_budget",
    })
    if rows:
        return float(rows[0].get("value", 0.50))
    return 0.50


def get_calibration_factor(confidence: float) -> float:
    """Get calibration factor for the given confidence bucket."""
    rows = sb_get("confidence_calibration", {
        "select": "active_factors",
        "order": "calibration_week.desc",
        "limit": "1",
    })
    if not rows or not rows[0].get("active_factors"):
        return 1.0  # No calibration data yet

    factors = rows[0]["active_factors"]
    # Find appropriate bucket
    bucket_key = f"{int(confidence * 10) * 10}"  # Round to nearest 10%
    return float(factors.get(bucket_key, factors.get("default", 1.0)))


@traced("predictions")
def call_ollama_qwen(prompt: str) -> str | None:
    """Call local Ollama qwen2.5:3b for fast inference."""
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": "qwen2.5:3b",
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": 512, "temperature": 0.3},
                    "keep_alive": "0",
                },
            )
            if resp.status_code == 200:
                return resp.json().get("response", "")
    except Exception as e:
        print(f"[inference_engine] Ollama qwen error: {e}")
    return None


@traced("predictions")
def call_claude(
    prompt: str,
    max_tokens: int = 1024,
    start_time: float | None = None,
) -> tuple[str | None, float]:
    """Call Claude Sonnet with retry + key fallback. Returns (response, cost)."""
    keys_to_try = [k for k in [ANTHROPIC_API_KEY, ANTHROPIC_API_KEY_2] if k]
    if not keys_to_try:
        return None, 0.0

    RETRYABLE = {429, 529}

    for key_index, api_key in enumerate(keys_to_try):
        backoff = 2.0
        for attempt in range(3):
            if start_time is not None and (time.time() - start_time) > TIME_LIMIT - 5:
                print("[inference_engine] call_claude: time budget exhausted, aborting")
                return None, 0.0
            try:
                resp = _claude_client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-6-20250514",
                        "max_tokens": max_tokens,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["content"][0]["text"]
                    usage = data.get("usage", {})
                    cost = (usage.get("input_tokens", 0) * 3 + usage.get("output_tokens", 0) * 15) / 1_000_000
                    return content, cost
                if resp.status_code in RETRYABLE:
                    wait = min(float(resp.headers.get("retry-after", backoff)), 16.0)
                    key_label = f"key{'2' if key_index else '1'}"
                    print(f"[inference_engine] call_claude: {resp.status_code} attempt {attempt + 1}/3 ({key_label}), waiting {wait:.1f}s")
                    if attempt == 2 and key_index == 0 and ANTHROPIC_API_KEY_2:
                        print("[inference_engine] call_claude: switching to fallback key")
                        break
                    time.sleep(wait)
                    backoff = min(backoff * 2, 16.0)
                    continue
                print(f"[inference_engine] call_claude: non-retryable {resp.status_code}")
                return None, 0.0
            except httpx.TimeoutException:
                print(f"[inference_engine] call_claude: timeout attempt {attempt + 1}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 16.0)
            except Exception as e:
                print(f"[inference_engine] call_claude: error: {e}")
                return None, 0.0
    return None, 0.0


def log_cost(category: str, subcategory: str, amount: float, description: str, metadata: dict | None = None, pipeline_run_id: str | None = None):
    """Log a cost to the cost_ledger."""
    _post_to_supabase("cost_ledger", {
        "ledger_date": TODAY,
        "category": category,
        "subcategory": subcategory,
        "amount": round(-abs(amount), 6),
        "description": description,
        "metadata": metadata or {},
        "pipeline_run_id": pipeline_run_id,
    })


# ============================================================================
# Congress-mode helpers (CONGRESS_MIRROR profile only)
# ============================================================================

def get_legislative_context(ticker: str) -> dict:
    """Check legislative_calendar for upcoming events affecting this ticker's sector."""
    upcoming = sb_get("legislative_calendar", {
        "select": "event_date,event_type,committee,bill_title,affected_sectors,significance",
        "event_date": f"gte.{TODAY}",
        "order": "event_date.asc",
        "limit": "10",
    })
    # Find events where ticker's sector overlaps
    relevant = []
    for event in upcoming:
        sectors = event.get("affected_sectors", [])
        # Simple check — in production this should use the ticker's known sector
        if sectors:
            relevant.append(event)
    return {"upcoming_events": relevant[:3], "count": len(relevant)}


def get_congress_cluster_context(ticker: str) -> dict:
    """Check for recent congress clusters on this ticker."""
    clusters = sb_get("congress_clusters", {
        "select": "cluster_date,member_count,cross_chamber,members,"
                  "confidence_boost,avg_signal_score",
        "ticker": f"eq.{ticker}",
        "order": "cluster_date.desc",
        "limit": "3",
    })
    return {"clusters": clusters, "count": len(clusters)}


@traced("predictions")
def tumbler_1_technical(ticker: str, signals: dict, total_score: int) -> dict:
    """Tumbler 1: Technical Foundation — pure computation, no LLM.

    RAG: retrieve similar past signal evaluations.
    Confidence: based on signal score and past performance of similar setups.
    """
    # Base confidence from signal score (0-6 mapped to 0-0.5)
    base_confidence = min(0.5, total_score / 6 * 0.5)

    # RAG: find similar past evaluations
    embed_text = (
        f"Ticker: {ticker}. Signals: trend={signals.get('trend', {}).get('passed', False)}, "
        f"momentum={signals.get('momentum', {}).get('passed', False)}, "
        f"volume={signals.get('volume', {}).get('passed', False)}, "
        f"fundamental={signals.get('fundamental', {}).get('passed', False)}, "
        f"sentiment={signals.get('sentiment', {}).get('passed', False)}, "
        f"flow={signals.get('flow', {}).get('passed', False)}. Score: {total_score}/6."
    )
    embedding = generate_embedding(embed_text)

    rag_results = []
    rag_similarity_avg = 0.0
    if embedding:
        rag_results = sb_rpc("match_signal_evaluations", {
            "query_embedding": embedding,
            "match_threshold": 0.6,
            "match_count": 5,
        })
        if rag_results:
            rag_similarity_avg = sum(r.get("similarity", 0) for r in rag_results) / len(rag_results)

    # Adjust confidence based on similar past setups
    key_finding = f"Score {total_score}/6."
    if rag_results:
        past_enters = sum(1 for r in rag_results if r.get("decision") == "enter")
        past_success_hint = past_enters / len(rag_results)
        confidence_adj = (past_success_hint - 0.5) * 0.1  # Small adjustment
        base_confidence = max(0, min(1.0, base_confidence + confidence_adj))
        key_finding += f" {len(rag_results)} similar past setups found (avg sim: {rag_similarity_avg:.2f})."
        key_finding += f" Past enter rate: {past_enters}/{len(rag_results)}."

    return {
        "depth": 1,
        "tumbler_name": "technical_foundation",
        "confidence_after": round(base_confidence, 4),
        "rag_contexts_retrieved": len(rag_results),
        "rag_similarity_avg": round(rag_similarity_avg, 3),
        "key_finding": key_finding,
        "data_sources": ["signal_evaluations_rag"],
        "embedding": embedding,
    }


@traced("predictions")
def tumbler_2_fundamental(
    ticker: str,
    confidence: float,
    active_profile: dict | None = None,
) -> dict:
    """Tumbler 2: Fundamental + Sentiment Context.

    RAG: retrieve recent catalyst events for this ticker.
    LLM: Perplexity only if catalyst data is stale (>4 hours).
    """
    if active_profile is None:
        active_profile = _active_profile
    if active_profile and active_profile.get("is_shadow"):
        print(f"[inference_engine] Shadow context injected at T2 for {active_profile.get('shadow_type')}")
    # RAG: find recent catalyst events
    embed_text = f"Recent catalysts and news for {ticker} stock. Earnings, analyst actions, insider trades, sentiment."
    embedding = generate_embedding(embed_text)

    rag_results = []
    rag_similarity_avg = 0.0
    if embedding:
        rag_results = sb_rpc("match_catalyst_events", {
            "query_embedding": embedding,
            "match_threshold": 0.4,
            "match_count": 10,
            "filter_ticker": ticker,
        })
        if rag_results:
            rag_similarity_avg = sum(r.get("similarity", 0) for r in rag_results) / len(rag_results)

    # Analyze catalyst sentiment
    sentiment_scores = [float(r.get("sentiment_score", 0)) for r in rag_results if r.get("sentiment_score") is not None]
    avg_sentiment = sum(sentiment_scores) / len(sentiment_scores) if sentiment_scores else 0.0

    # Check for veto condition
    veto = avg_sentiment < -0.5

    # Adjust confidence
    sentiment_adj = avg_sentiment * 0.15  # Sentiment contributes up to ±15%
    catalyst_count_bonus = min(0.05, len(rag_results) * 0.01)  # More data = slight boost
    new_confidence = max(0, min(1.0, confidence + sentiment_adj + catalyst_count_bonus))

    # Identify strongest catalysts
    key_findings = []
    for cat in rag_results[:3]:
        key_findings.append(f"{cat.get('catalyst_type', 'unknown')}: {cat.get('headline', '')[:80]}")

    key_finding = f"Avg sentiment: {avg_sentiment:.2f}. {len(rag_results)} catalysts found."
    if key_findings:
        key_finding += " Top: " + "; ".join(key_findings)
    if veto:
        key_finding += " VETO: sentiment < -0.5"

    # Congress mode enhancement (CONGRESS_MIRROR profile only)
    congress_boost = 0.0
    congress_context = ""

    if active_profile and active_profile.get("profile_name") == "CONGRESS_MIRROR":
        # Get legislative calendar context
        leg_context = get_legislative_context(ticker)
        if leg_context["count"] > 0:
            upcoming = leg_context["upcoming_events"][0]
            try:
                days_until = (
                    datetime.strptime(upcoming["event_date"], "%Y-%m-%d")
                    - datetime.today()
                ).days
                if (
                    days_until <= 14
                    and upcoming.get("significance") in ("high", "critical")
                ):
                    congress_boost += 0.07
                    congress_context += (
                        f"High-impact {upcoming['event_type']} in "
                        f"{days_until} days: "
                        f"{upcoming.get('bill_title', '')}. "
                    )
            except (ValueError, KeyError):
                pass

        # Get cluster context
        cluster_ctx = get_congress_cluster_context(ticker)
        if cluster_ctx["count"] > 0:
            best_cluster = cluster_ctx["clusters"][0]
            cross = best_cluster.get("cross_chamber", False)
            boost = best_cluster.get("confidence_boost", 0.05)
            congress_boost += boost
            congress_context += (
                f"Congress cluster: {best_cluster['member_count']} "
                f"members bought "
                f"({'cross-chamber' if cross else 'same chamber'}). "
            )

        # Apply congress boost to confidence
        if congress_boost > 0:
            new_confidence = min(1.0, new_confidence + congress_boost)
            key_finding += (
                f" Congress boost: +{congress_boost:.3f}. "
                f"{congress_context}"
            )

    return {
        "depth": 2,
        "tumbler_name": "fundamental_sentiment",
        "confidence_after": round(new_confidence, 4),
        "rag_contexts_retrieved": len(rag_results),
        "rag_similarity_avg": round(rag_similarity_avg, 3),
        "key_finding": key_finding,
        "data_sources": ["catalyst_events_rag"],
        "veto": veto,
        "avg_sentiment": avg_sentiment,
    }


@traced("predictions")
def tumbler_3_flow_crossasset(
    ticker: str,
    confidence: float,
    context: dict,
    active_profile: dict | None = None,
) -> dict:
    """Tumbler 3: Flow + Cross-Asset Analysis.

    RAG: retrieve similar inference chains + past trade learnings.
    LLM: Ollama qwen2.5:3b (local, free) for cross-asset analysis.
    """
    if active_profile is None:
        active_profile = _active_profile
    # RAG: find similar past inference chains
    embed_text = f"Inference chain for {ticker}. Confidence: {confidence:.2f}. {context.get('key_finding', '')}"
    embedding = generate_embedding(embed_text)

    rag_results = []
    rag_similarity_avg = 0.0
    if embedding:
        rag_results = sb_rpc("match_inference_chains", {
            "query_embedding": embedding,
            "match_threshold": 0.5,
            "match_count": 5,
        })
        if rag_results:
            rag_similarity_avg = sum(r.get("similarity", 0) for r in rag_results) / len(rag_results)

    # RAG: also search past trade learnings for this setup type
    learnings = []
    if embedding:
        learnings = sb_rpc("match_trade_learnings", {
            "query_embedding": embedding,
            "match_threshold": 0.5,
            "match_count": 5,
        })

    # Ask qwen for cross-asset analysis, incorporating trade learnings
    past_context = ""
    for chain in rag_results[:3]:
        outcome = chain.get("actual_outcome", "unknown")
        past_context += (
            f"- {chain.get('ticker', '?')} ({chain.get('chain_date', '?')}): "
            f"confidence={chain.get('final_confidence', 0):.2f}, "
            f"decision={chain.get('final_decision', '?')}, "
            f"outcome={outcome}\n"
        )

    learning_context = ""
    for lr in learnings[:3]:
        learning_context += (
            f"- {lr.get('ticker', '?')} ({lr.get('trade_date', '?')}): "
            f"{lr.get('outcome', '?')} ({lr.get('pnl_pct', 0):+.1f}%), "
            f"accuracy={lr.get('expectation_accuracy', '?')}, "
            f"lesson={lr.get('key_lesson', '')[:80]}\n"
        )

    shadow_prefix = ""
    if active_profile and active_profile.get("is_shadow"):
        shadow_type = active_profile.get("shadow_type", "")
        shadow_prefix = get_shadow_context(shadow_type)
        if shadow_prefix:
            shadow_prefix = shadow_prefix.strip() + "\n\n"
        print(f"[inference_engine] Shadow context injected at T3 for {shadow_type}")

    qwen_prompt = f"""{shadow_prefix}Analyze this trade setup for {ticker}:
Current confidence: {confidence:.2f}
Context: {context.get('key_finding', 'No prior context.')}

Similar past inference chains:
{past_context if past_context else 'No similar past chains found.'}

Past trade learnings (actual outcomes from closed trades):
{learning_context if learning_context else 'No past trade learnings found.'}

In 2-3 sentences: Does the cross-asset context and trade history support or weaken this trade?
Respond with a JSON object: {{"adjustment": float between -0.1 and +0.1, "reasoning": "string"}}"""

    qwen_response = call_ollama_qwen(qwen_prompt)
    adjustment = 0.0
    reasoning = "Qwen analysis unavailable"

    if qwen_response:
        try:
            # Try to parse JSON from response
            json_match = qwen_response
            if "```" in qwen_response:
                json_match = qwen_response.split("```")[1].strip()
                if json_match.startswith("json"):
                    json_match = json_match[4:].strip()
            parsed = json.loads(json_match)
            adjustment = max(-0.1, min(0.1, float(parsed.get("adjustment", 0))))
            reasoning = parsed.get("reasoning", "")[:200]
        except (json.JSONDecodeError, ValueError):
            # Fallback: try to extract adjustment from text
            reasoning = qwen_response[:200]

    new_confidence = max(0, min(1.0, confidence + adjustment))

    # Check outcomes of similar past chains and learnings
    past_outcomes = [r.get("actual_outcome") for r in rag_results if r.get("actual_outcome")]
    win_count = sum(1 for o in past_outcomes if o in ("STRONG_WIN", "WIN"))
    loss_count = sum(1 for o in past_outcomes if o in ("LOSS", "STRONG_LOSS"))

    learning_wins = sum(1 for lr in learnings if lr.get("outcome") in ("STRONG_WIN", "WIN"))
    learning_losses = sum(1 for lr in learnings if lr.get("outcome") in ("LOSS", "STRONG_LOSS"))

    key_finding = f"Qwen adjustment: {adjustment:+.3f}. {reasoning}"
    if past_outcomes:
        key_finding += f" Past chains: {win_count}W/{loss_count}L."
    if learnings:
        key_finding += f" Trade learnings: {learning_wins}W/{learning_losses}L ({len(learnings)} trades)."
        # Surface the most relevant lesson
        top_lesson = learnings[0].get("key_lesson", "")
        if top_lesson:
            key_finding += f" Top lesson: {top_lesson[:80]}"

    return {
        "depth": 3,
        "tumbler_name": "flow_crossasset",
        "confidence_after": round(new_confidence, 4),
        "rag_contexts_retrieved": len(rag_results) + len(learnings),
        "rag_similarity_avg": round(rag_similarity_avg, 3),
        "key_finding": key_finding,
        "data_sources": ["inference_chains_rag", "trade_learnings_rag", "ollama_qwen"],
    }


@traced("predictions")
def tumbler_4_pattern(
    ticker: str,
    confidence: float,
    chain_context: list[dict],
    start_time: float = 0.0,
    active_profile: dict | None = None,
) -> dict:
    """Tumbler 4: Pattern Template Matching.

    RAG: match against pattern_templates.
    LLM: Claude Sonnet (only if high-conviction candidate).
    """
    if active_profile is None:
        active_profile = _active_profile
    # RAG: find matching pattern templates
    context_text = " ".join(t.get("key_finding", "") for t in chain_context)
    embed_text = f"Pattern match for {ticker}. {context_text[:300]}"
    embedding = generate_embedding(embed_text)

    rag_results = []
    rag_similarity_avg = 0.0
    matched_patterns = []
    if embedding:
        rag_results = sb_rpc("match_pattern_templates", {
            "query_embedding": embedding,
            "match_threshold": 0.5,
            "match_count": 5,
        })
        if rag_results:
            rag_similarity_avg = sum(r.get("similarity", 0) for r in rag_results) / len(rag_results)
            matched_patterns = [r for r in rag_results if r.get("times_matched", 0) >= r.get("min_occurrences_for_trust", 3)]

    # Use Claude to evaluate pattern match quality
    claude_response = None
    claude_cost = 0.0
    adjustment = 0.0

    if matched_patterns:
        patterns_desc = "\n".join(
            f"- {p.get('pattern_name', '?')}: {p.get('pattern_description', '?')[:100]} "
            f"(success: {p.get('success_rate', 0):.0f}%, matched: {p.get('times_matched', 0)}x, "
            f"sim: {p.get('similarity', 0):.2f})"
            for p in matched_patterns[:3]
        )

        shadow_prefix = ""
        if active_profile and active_profile.get("is_shadow"):
            shadow_type = active_profile.get("shadow_type", "")
            shadow_prefix = get_shadow_context(shadow_type)
            if shadow_prefix:
                shadow_prefix = shadow_prefix.strip() + "\n\n"
            print(f"[inference_engine] Shadow context injected at T4 for {shadow_type}")

        claude_prompt = f"""{shadow_prefix}You are a pattern recognition analyst. Evaluate if these known patterns match the current setup.

Ticker: {ticker}
Current confidence: {confidence:.2f}
Chain context: {context_text[:400]}

Matched patterns:
{patterns_desc}

Respond with JSON: {{"adjustment": float between -0.1 and +0.1, "best_pattern": "pattern name or null", "reasoning": "1-2 sentences"}}"""

        profile_name = active_profile.get("profile_name", "UNKNOWN") if active_profile else "UNKNOWN"
        claude_response, claude_cost = call_claude(claude_prompt, max_tokens=256, start_time=start_time)
        if claude_cost > 0:
            log_cost("claude_api", f"inference_engine_tumbler4_{profile_name}_{ticker}", claude_cost,
                     f"Pattern matching for {ticker}", {"model": "claude-sonnet-4-6", "ticker": ticker})

        if claude_response:
            try:
                if claude_response.startswith("```"):
                    claude_response = claude_response.split("\n", 1)[1].rsplit("```", 1)[0]
                parsed = json.loads(claude_response)
                adjustment = max(-0.1, min(0.1, float(parsed.get("adjustment", 0))))
            except (json.JSONDecodeError, ValueError):
                pass

    new_confidence = max(0, min(1.0, confidence + adjustment))

    key_finding = f"{len(matched_patterns)} trusted patterns matched."
    if matched_patterns:
        top = matched_patterns[0]
        key_finding += f" Best: {top.get('pattern_name', '?')} ({top.get('success_rate', 0):.0f}% success)."
    key_finding += f" Claude adjustment: {adjustment:+.3f}."

    return {
        "depth": 4,
        "tumbler_name": "pattern_matching",
        "confidence_after": round(new_confidence, 4),
        "rag_contexts_retrieved": len(rag_results),
        "rag_similarity_avg": round(rag_similarity_avg, 3),
        "key_finding": key_finding,
        "data_sources": ["pattern_templates_rag"] + (["claude_sonnet"] if claude_response else []),
        "claude_cost": claude_cost,
        "matched_pattern_ids": [p.get("id") for p in matched_patterns],
    }


@traced("predictions")
def tumbler_5_counterfactual(
    ticker: str,
    confidence: float,
    chain_context: list[dict],
    start_time: float = 0.0,
    active_profile: dict | None = None,
) -> dict:
    """Tumbler 5: Counterfactual + Final Synthesis.

    RAG: retrieve meta_reflections + past trade learnings (especially losses).
    LLM: Claude Sonnet as devil's advocate.
    Apply calibration factor for final adjusted confidence.
    """
    if active_profile is None:
        active_profile = _active_profile
    # RAG: retrieve similar meta reflections
    context_text = " ".join(t.get("key_finding", "") for t in chain_context)
    embed_text = f"Trade decision analysis for {ticker}. {context_text[:300]}"
    embedding = generate_embedding(embed_text)

    rag_results = []
    if embedding:
        rag_results = sb_rpc("match_meta_reflections", {
            "query_embedding": embedding,
            "match_threshold": 0.5,
            "match_count": 3,
        })

    # RAG: retrieve relevant past trade learnings — especially losses and misses
    trade_learnings = []
    if embedding:
        trade_learnings = sb_rpc("match_trade_learnings", {
            "query_embedding": embedding,
            "match_threshold": 0.45,
            "match_count": 5,
        })

    # Past reflections context
    reflection_context = ""
    for ref in rag_results:
        reflection_context += (
            f"- {ref.get('reflection_date', '?')}: {ref.get('patterns_observed', '')[:100]}. "
            f"Issues: {ref.get('operational_issues', 'none')[:80]}\n"
        )

    # Trade learnings — surface what went wrong in similar past trades
    learning_context = ""
    losses = [row for row in trade_learnings if row.get("outcome") in ("LOSS", "STRONG_LOSS")]
    misses = [row for row in trade_learnings if row.get("expectation_accuracy") in ("missed", "opposite")]
    relevant = sorted(losses + misses, key=lambda x: x.get("similarity", 0), reverse=True)[:4]
    # Deduplicate by id
    seen_ids: set = set()
    for row in relevant:
        lid = row.get("id")
        if lid not in seen_ids:
            seen_ids.add(lid)
            outcome = row.get("outcome", "?")
            learning_context += (
                f"- {row.get('ticker', '?')} ({row.get('trade_date', '?')}): {outcome} "
                f"({row.get('pnl_pct', 0):+.1f}%), "
                f"accuracy={row.get('expectation_accuracy', '?')}, "
                f"what_failed={row.get('what_failed', '')[:80]}, "
                f"lesson={row.get('key_lesson', '')[:60]}\n"
            )

    # Claude as devil's advocate
    shadow_prefix = ""
    if active_profile and active_profile.get("is_shadow"):
        shadow_type = active_profile.get("shadow_type", "")
        shadow_prefix = get_shadow_context(shadow_type)
        if shadow_prefix:
            shadow_prefix = shadow_prefix.strip() + "\n\n"
        print(f"[inference_engine] Shadow context injected at T5 for {shadow_type}")

    claude_prompt = f"""{shadow_prefix}You are a risk analyst playing devil's advocate. Your job is to find reasons NOT to take this trade.

Ticker: {ticker}
Current confidence: {confidence:.2f}

Tumbler chain analysis:
{chr(10).join(f"  Tumbler {t.get('depth', '?')}: {t.get('key_finding', '')}" for t in chain_context)}

Past meta-reflections:
{reflection_context if reflection_context else 'No similar past reflections found.'}

Past trade learnings (losses and missed expectations from similar setups):
{learning_context if learning_context else 'No past losses found for similar setups.'}

Be critical. What could go wrong? What are we missing? What patterns have failed us in similar setups before?

Respond with JSON:
{{"adjustment": float between -0.15 and +0.05, "risk_factors": ["string", ...], "reasoning": "2-3 sentences"}}"""

    profile_name = active_profile.get("profile_name", "UNKNOWN") if active_profile else "UNKNOWN"
    claude_response, claude_cost = call_claude(claude_prompt, max_tokens=512, start_time=start_time)
    if claude_cost > 0:
        log_cost("claude_api", f"inference_engine_tumbler5_{profile_name}_{ticker}", claude_cost,
                 f"Counterfactual analysis for {ticker}", {"model": "claude-sonnet-4-6", "ticker": ticker})

    adjustment = 0.0
    risk_factors = []
    reasoning = "Counterfactual analysis unavailable"

    if claude_response:
        try:
            if claude_response.startswith("```"):
                claude_response = claude_response.split("\n", 1)[1].rsplit("```", 1)[0]
            parsed = json.loads(claude_response)
            adjustment = max(-0.15, min(0.05, float(parsed.get("adjustment", 0))))
            risk_factors = parsed.get("risk_factors", [])
            reasoning = parsed.get("reasoning", "")[:300]
        except (json.JSONDecodeError, ValueError):
            reasoning = (claude_response or "")[:300]

    # Apply calibration factor
    raw_confidence = max(0, min(1.0, confidence + adjustment))
    calibration_factor = get_calibration_factor(raw_confidence)
    calibrated_confidence = max(0, min(1.0, raw_confidence * calibration_factor))

    key_finding = f"Devil's advocate: {reasoning}"
    if risk_factors:
        key_finding += f" Risks: {', '.join(risk_factors[:3])}."
    key_finding += f" Calibration factor: {calibration_factor:.3f}."
    key_finding += f" Raw: {raw_confidence:.3f} -> Calibrated: {calibrated_confidence:.3f}."

    return {
        "depth": 5,
        "tumbler_name": "counterfactual_synthesis",
        "confidence_after": round(calibrated_confidence, 4),
        "rag_contexts_retrieved": len(rag_results),
        "rag_similarity_avg": 0,
        "key_finding": key_finding,
        "data_sources": ["meta_reflections_rag", "trade_learnings_rag", "claude_sonnet", "calibration"],
        "claude_cost": claude_cost,
        "calibration_factor": calibration_factor,
    }


@traced("predictions")
def check_stopping_rule(
    tumbler_result: dict,
    prev_confidence: float,
    start_time: float,
    has_veto: bool = False,
    ticker: str = "",
    active_profile: dict | None = None,
    local_confidence_thresholds: dict | None = None,
) -> str | None:
    """Evaluate stopping rules after a tumbler. Returns reason or None to continue."""
    if active_profile is None:
        active_profile = _active_profile
    if local_confidence_thresholds is None:
        local_confidence_thresholds = CONFIDENCE_THRESHOLDS

    depth = tumbler_result["depth"]
    confidence = tumbler_result["confidence_after"]
    delta = abs(confidence - prev_confidence)
    elapsed = time.time() - start_time

    # Time limit
    if elapsed > TIME_LIMIT:
        return "time_limit"

    # Veto signal
    if has_veto or tumbler_result.get("veto"):
        return "veto_signal"

    # Confidence floor
    threshold = local_confidence_thresholds.get(depth, 0)
    if confidence < threshold:
        return "confidence_floor"

    # Forced connection (delta < 0.03 means new data added nothing)
    # Only apply after tumbler 4+ — tumblers 1-3 are cheap local calls,
    # don't let them gate Claude (tumblers 4-5) which is the whole point
    if depth >= 4 and delta < FORCED_CONNECTION_DELTA:
        return "forced_connection"

    # Congress signal stale (CONGRESS_MIRROR only)
    if active_profile and active_profile.get("profile_name") == "CONGRESS_MIRROR":
        if ticker:
            congress_events = sb_get("catalyst_events", {
                "select": "disclosure_freshness_score,disclosure_days_since_trade",
                "catalyst_type": "eq.congressional_trade",
                "ticker": f"eq.{ticker}",
                "order": "created_at.desc",
                "limit": "1",
            })
            if congress_events:
                freshness = float(
                    congress_events[0].get("disclosure_freshness_score") or 0.5,
                )
                days = int(
                    congress_events[0].get("disclosure_days_since_trade") or 20,
                )
                if freshness < 0.2 and days > 40:
                    return "congress_signal_stale"

    return None


def decide(confidence: float, local_decision_thresholds: dict | None = None) -> str:
    """Map final confidence to decision."""
    thresholds = local_decision_thresholds if local_decision_thresholds is not None else DECISION_THRESHOLDS
    if confidence >= thresholds["strong_enter"]:
        return "strong_enter"
    if confidence >= thresholds["enter"]:
        return "enter"
    if confidence >= thresholds["watch"]:
        return "watch"
    if confidence >= thresholds["skip"]:
        return "skip"
    return "veto"


@traced("predictions")
def run_inference(
    ticker: str,
    signals: dict,
    total_score: int,
    scan_type: str = "pre_market",
    signal_evaluation_id: str | None = None,
    pipeline_run_id: str | None = None,
    profile_override: dict | None = None,
) -> dict:
    """Execute the full tumbler chain for a ticker.

    Returns dict with: inference_chain_id, final_confidence, final_decision,
    max_depth_reached, stopping_reason, patterns_matched, tumblers

    When profile_override is provided the override dict is used as the active
    profile WITHOUT mutating module-level globals (_active_profile,
    DECISION_THRESHOLDS, CONFIDENCE_THRESHOLDS).  This is the shadow-run path.
    """
    global TODAY
    TODAY = date.today().isoformat()

    if profile_override is not None:
        active_profile = profile_override
        # Build LOCAL threshold copies — do NOT mutate module-level dicts
        min_conf = float(active_profile.get("min_confidence", 0.60))
        local_decision_thresholds = {
            "strong_enter": min(1.0, min_conf + 0.15),
            "enter": min_conf,
            "watch": max(0.10, min_conf - 0.15),
            "skip": max(0.05, min_conf - 0.40),
        }
        min_depth = int(active_profile.get("min_tumbler_depth", 3))
        local_confidence_thresholds = {
            d: max(0.10, min_conf * (d / 5) * (0.6 if d < min_depth else 1.0))
            for d in range(1, 6)
        }
        # Cap tumbler depth for shadow profiles (REGIME_WATCHER stops at T3)
        from shadow_profiles import get_max_tumbler_depth  # noqa: PLC0415
        max_depth_cap = get_max_tumbler_depth(active_profile.get("shadow_type", ""))
    else:
        # Normal path — load from DB, mutate globals as before
        active_profile = load_active_profile()
        local_decision_thresholds = DECISION_THRESHOLDS
        local_confidence_thresholds = CONFIDENCE_THRESHOLDS
        max_depth_cap = 5

    min_score = (
        int(active_profile.get("min_signal_score", 4))
        if active_profile
        else 4
    )

    # Check if this ticker meets the profile's minimum signal threshold
    if total_score < min_score:
        print(f"[inference_engine] {ticker}: score {total_score} < min {min_score} for profile {active_profile.get('profile_name', '?')}, skipping")
        return {
            "inference_chain_id": None,
            "ticker": ticker,
            "final_confidence": 0.0,
            "final_decision": "skip",
            "max_depth_reached": 0,
            "stopping_reason": "confidence_floor",
            "patterns_matched": [],
            "tumblers": [],
            "profile": active_profile.get("profile_name", "DEFAULT"),
        }

    start_time = time.time()
    tumblers = []
    catalyst_event_ids = []
    pattern_template_ids = []
    confidence = 0.0
    stopping_reason = None
    has_veto = False

    # Check Claude budget
    claude_budget = get_claude_budget()
    claude_spent = get_todays_claude_spend()
    claude_available = claude_budget - claude_spent > 0.01

    # Helper: pass local copies to check_stopping_rule on every call
    def _stop(tumbler_result: dict, prev_conf: float, veto: bool = False) -> str | None:
        return check_stopping_rule(
            tumbler_result,
            prev_conf,
            start_time,
            has_veto=veto,
            ticker=ticker,
            active_profile=active_profile,
            local_confidence_thresholds=local_confidence_thresholds,
        )

    def _finalize(reason: str) -> dict:
        return _finalize_chain(
            ticker, scan_type, tumblers, confidence, reason,
            signal_evaluation_id, catalyst_event_ids, pattern_template_ids,
            pipeline_run_id, t1.get("embedding") if tumblers else None,
            active_profile=active_profile,
            local_decision_thresholds=local_decision_thresholds,
        )

    # === TUMBLER 1: Technical Foundation ===
    t1 = tumbler_1_technical(ticker, signals, total_score)
    prev_confidence = confidence
    confidence = t1["confidence_after"]
    t1["confidence_before"] = prev_confidence
    t1["confidence_delta"] = round(confidence - prev_confidence, 4)
    t1["duration_ms"] = int((time.time() - start_time) * 1000)
    tumblers.append(t1)

    stopping_reason = _stop(t1, prev_confidence)
    if stopping_reason:
        return _finalize(stopping_reason)

    if max_depth_cap < 2:
        return _finalize("all_tumblers_clear")

    # === TUMBLER 2: Fundamental + Sentiment ===
    t2_start = time.time()
    t2 = tumbler_2_fundamental(ticker, confidence, active_profile=active_profile)
    prev_confidence = confidence
    confidence = t2["confidence_after"]
    t2["confidence_before"] = prev_confidence
    t2["confidence_delta"] = round(confidence - prev_confidence, 4)
    t2["duration_ms"] = int((time.time() - t2_start) * 1000)
    tumblers.append(t2)
    has_veto = t2.get("veto", False)

    stopping_reason = _stop(t2, prev_confidence, veto=has_veto)
    if stopping_reason:
        return _finalize(stopping_reason)

    if max_depth_cap < 3:
        return _finalize("all_tumblers_clear")

    # === TUMBLER 3: Flow + Cross-Asset ===
    t3_start = time.time()
    t3 = tumbler_3_flow_crossasset(ticker, confidence, {"key_finding": t2.get("key_finding", "")}, active_profile=active_profile)
    prev_confidence = confidence
    confidence = t3["confidence_after"]
    t3["confidence_before"] = prev_confidence
    t3["confidence_delta"] = round(confidence - prev_confidence, 4)
    t3["duration_ms"] = int((time.time() - t3_start) * 1000)
    tumblers.append(t3)

    stopping_reason = _stop(t3, prev_confidence)
    if stopping_reason:
        return _finalize(stopping_reason)

    if max_depth_cap < 4:
        return _finalize("all_tumblers_clear")

    # === TUMBLER 4: Pattern Template Matching ===
    if not claude_available:
        return _finalize("resource_limit")

    t4_start = time.time()
    t4 = tumbler_4_pattern(ticker, confidence, tumblers, start_time=start_time, active_profile=active_profile)
    prev_confidence = confidence
    confidence = t4["confidence_after"]
    t4["confidence_before"] = prev_confidence
    t4["confidence_delta"] = round(confidence - prev_confidence, 4)
    t4["duration_ms"] = int((time.time() - t4_start) * 1000)
    tumblers.append(t4)
    pattern_template_ids = t4.get("matched_pattern_ids", [])

    # Re-check budget after tumbler 4
    claude_spent = get_todays_claude_spend()
    claude_available = claude_budget - claude_spent > 0.01

    stopping_reason = _stop(t4, prev_confidence)
    if stopping_reason:
        return _finalize(stopping_reason)

    if max_depth_cap < 5:
        return _finalize("all_tumblers_clear")

    # === TUMBLER 5: Counterfactual + Final Synthesis ===
    if not claude_available:
        return _finalize("resource_limit")

    t5_start = time.time()
    t5 = tumbler_5_counterfactual(ticker, confidence, tumblers, start_time=start_time, active_profile=active_profile)
    prev_confidence = confidence
    confidence = t5["confidence_after"]
    t5["confidence_before"] = prev_confidence
    t5["confidence_delta"] = round(confidence - prev_confidence, 4)
    t5["duration_ms"] = int((time.time() - t5_start) * 1000)
    tumblers.append(t5)

    return _finalize("all_tumblers_clear")


def _finalize_chain(
    ticker: str,
    scan_type: str,
    tumblers: list[dict],
    confidence: float,
    stopping_reason: str,
    signal_evaluation_id: str | None,
    catalyst_event_ids: list[str],
    pattern_template_ids: list[str],
    pipeline_run_id: str | None,
    embedding: list[float] | None,
    active_profile: dict | None = None,
    local_decision_thresholds: dict | None = None,
) -> dict:
    """Store the inference chain and return result."""
    if active_profile is None:
        active_profile = _active_profile
    max_depth = max(t.get("depth", 0) for t in tumblers) if tumblers else 0
    decision = decide(confidence, local_decision_thresholds)

    # Build reasoning summary
    reasoning_parts = [t.get("key_finding", "") for t in tumblers if t.get("key_finding")]
    reasoning_summary = " | ".join(reasoning_parts)[:500]

    # Clean tumblers for storage (remove embedding)
    stored_tumblers = []
    for t in tumblers:
        stored = {k: v for k, v in t.items() if k not in ("embedding", "veto", "avg_sentiment", "claude_cost", "matched_pattern_ids", "calibration_factor")}
        stored_tumblers.append(stored)

    profile_name = active_profile.get("profile_name", "DEFAULT") if active_profile else "DEFAULT"
    chain_data = {
        "ticker": ticker,
        "chain_date": TODAY,
        "scan_type": scan_type,
        "max_depth_reached": max_depth,
        "final_confidence": round(confidence, 4),
        "final_decision": decision,
        "stopping_reason": stopping_reason,
        "tumblers": stored_tumblers,
        "signal_evaluation_id": signal_evaluation_id,
        "catalyst_event_ids": catalyst_event_ids,
        "pattern_template_ids": [pid for pid in pattern_template_ids if pid],
        "reasoning_summary": reasoning_summary,
        "pipeline_run_id": pipeline_run_id,
        "profile_name": profile_name,
    }
    if embedding:
        chain_data["embedding"] = embedding

    stored = _post_to_supabase("inference_chains", chain_data)
    chain_id = stored.get("id") if stored else None

    print(
        f"[inference_engine] [{profile_name}] {ticker}: depth={max_depth}, "
        f"confidence={confidence:.3f}, decision={decision}, "
        f"stop={stopping_reason}"
    )

    return {
        "inference_chain_id": chain_id,
        "ticker": ticker,
        "final_confidence": round(confidence, 4),
        "final_decision": decision,
        "max_depth_reached": max_depth,
        "stopping_reason": stopping_reason,
        "patterns_matched": [pid for pid in pattern_template_ids if pid],
        "tumblers": stored_tumblers,
        "profile": profile_name,
    }


if __name__ == "__main__":
    # Self-test with dummy signals
    print("[inference_engine] Running self-test...")
    result = run_inference(
        ticker="TEST",
        signals={
            "trend": {"passed": True},
            "momentum": {"passed": True},
            "volume": {"passed": False},
            "fundamental": {"passed": True},
            "sentiment": {"passed": True, "score": 0.3},
            "flow": {"passed": False},
        },
        total_score=4,
        scan_type="manual",
    )
    print(f"[inference_engine] Self-test result: {json.dumps(result, indent=2, default=str)}")
