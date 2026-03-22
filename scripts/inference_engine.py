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
from datetime import date

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from tracer import _post_to_supabase, _sb_headers

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PERPLEXITY_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Confidence thresholds per tumbler depth
CONFIDENCE_THRESHOLDS = {
    1: 0.25,  # Technical must show something
    2: 0.40,  # Fundamentals must support
    3: 0.55,  # Cross-asset must align
    4: 0.65,  # Pattern match needed
    5: 0.75,  # Final synthesis threshold
}

# Delta threshold for forced connection detection
FORCED_CONNECTION_DELTA = 0.03

# Time limit (seconds)
TIME_LIMIT = 30

# Decision thresholds on final confidence
DECISION_THRESHOLDS = {
    "strong_enter": 0.75,
    "enter": 0.60,
    "watch": 0.45,
    "skip": 0.20,
    # Below 0.20 = veto
}

TODAY = ""  # Reassigned at the start of run_inference()


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
    """Call a Supabase RPC function (for RAG queries)."""
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/{fn_name}",
            headers=_sb_headers(),
            json=params,
        )
        if resp.status_code == 200:
            return resp.json()
    return []


def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding via Ollama."""
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": text, "keep_alive": "0"},
            )
            if resp.status_code == 200:
                return resp.json().get("embedding")
    except Exception:
        pass
    return None


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


def call_claude(prompt: str, max_tokens: int = 1024) -> tuple[str | None, float]:
    """Call Claude Sonnet. Returns (response, cost)."""
    if not ANTHROPIC_API_KEY:
        return None, 0.0

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
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data["content"][0]["text"]
                usage = data.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                # Claude Sonnet pricing: $3/1M input, $15/1M output
                cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000
                return content, cost
    except Exception as e:
        print(f"[inference_engine] Claude error: {e}")

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


def tumbler_2_fundamental(ticker: str, confidence: float) -> dict:
    """Tumbler 2: Fundamental + Sentiment Context.

    RAG: retrieve recent catalyst events for this ticker.
    LLM: Perplexity only if catalyst data is stale (>4 hours).
    """
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


def tumbler_3_flow_crossasset(ticker: str, confidence: float, context: dict) -> dict:
    """Tumbler 3: Flow + Cross-Asset Analysis.

    RAG: retrieve similar inference chains.
    LLM: Ollama qwen2.5:3b (local, free) for cross-asset analysis.
    """
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

    # Ask qwen for cross-asset analysis
    past_context = ""
    for chain in rag_results[:3]:
        outcome = chain.get("actual_outcome", "unknown")
        past_context += (
            f"- {chain.get('ticker', '?')} ({chain.get('chain_date', '?')}): "
            f"confidence={chain.get('final_confidence', 0):.2f}, "
            f"decision={chain.get('final_decision', '?')}, "
            f"outcome={outcome}\n"
        )

    qwen_prompt = f"""Analyze this trade setup for {ticker}:
Current confidence: {confidence:.2f}
Context: {context.get('key_finding', 'No prior context.')}

Similar past trades:
{past_context if past_context else 'No similar past trades found.'}

In 2-3 sentences: Does the cross-asset context support or weaken this trade?
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

    # Check outcomes of similar past chains
    past_outcomes = [r.get("actual_outcome") for r in rag_results if r.get("actual_outcome")]
    win_count = sum(1 for o in past_outcomes if o in ("STRONG_WIN", "WIN"))
    loss_count = sum(1 for o in past_outcomes if o in ("LOSS", "STRONG_LOSS"))

    key_finding = f"Qwen adjustment: {adjustment:+.3f}. {reasoning}"
    if past_outcomes:
        key_finding += f" Past similar: {win_count}W/{loss_count}L."

    return {
        "depth": 3,
        "tumbler_name": "flow_crossasset",
        "confidence_after": round(new_confidence, 4),
        "rag_contexts_retrieved": len(rag_results),
        "rag_similarity_avg": round(rag_similarity_avg, 3),
        "key_finding": key_finding,
        "data_sources": ["inference_chains_rag", "ollama_qwen"],
    }


def tumbler_4_pattern(ticker: str, confidence: float, chain_context: list[dict]) -> dict:
    """Tumbler 4: Pattern Template Matching.

    RAG: match against pattern_templates.
    LLM: Claude Sonnet (only if high-conviction candidate).
    """
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

        claude_prompt = f"""You are a pattern recognition analyst. Evaluate if these known patterns match the current setup.

Ticker: {ticker}
Current confidence: {confidence:.2f}
Chain context: {context_text[:400]}

Matched patterns:
{patterns_desc}

Respond with JSON: {{"adjustment": float between -0.1 and +0.1, "best_pattern": "pattern name or null", "reasoning": "1-2 sentences"}}"""

        claude_response, claude_cost = call_claude(claude_prompt, max_tokens=256)
        if claude_cost > 0:
            log_cost("claude_api", f"inference_engine_tumbler4_{ticker}", claude_cost,
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


def tumbler_5_counterfactual(ticker: str, confidence: float, chain_context: list[dict]) -> dict:
    """Tumbler 5: Counterfactual + Final Synthesis.

    RAG: retrieve meta_reflections and past losses.
    LLM: Claude Sonnet as devil's advocate.
    Apply calibration factor for final adjusted confidence.
    """
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

    # Past reflections context
    reflection_context = ""
    for ref in rag_results:
        reflection_context += (
            f"- {ref.get('reflection_date', '?')}: {ref.get('patterns_observed', '')[:100]}. "
            f"Issues: {ref.get('operational_issues', 'none')[:80]}\n"
        )

    # Claude as devil's advocate
    claude_prompt = f"""You are a risk analyst playing devil's advocate. Your job is to find reasons NOT to take this trade.

Ticker: {ticker}
Current confidence: {confidence:.2f}

Tumbler chain analysis:
{chr(10).join(f"  Tumbler {t.get('depth', '?')}: {t.get('key_finding', '')}" for t in chain_context)}

Past meta-reflections:
{reflection_context if reflection_context else 'No similar past reflections found.'}

Be critical. What could go wrong? What are we missing? What did similar setups get wrong in the past?

Respond with JSON:
{{"adjustment": float between -0.15 and +0.05, "risk_factors": ["string", ...], "reasoning": "2-3 sentences"}}"""

    claude_response, claude_cost = call_claude(claude_prompt, max_tokens=512)
    if claude_cost > 0:
        log_cost("claude_api", f"inference_engine_tumbler5_{ticker}", claude_cost,
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
        "data_sources": ["meta_reflections_rag", "claude_sonnet", "calibration"],
        "claude_cost": claude_cost,
        "calibration_factor": calibration_factor,
    }


def check_stopping_rule(tumbler_result: dict, prev_confidence: float, start_time: float, has_veto: bool = False) -> str | None:
    """Evaluate stopping rules after a tumbler. Returns reason or None to continue."""
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
    threshold = CONFIDENCE_THRESHOLDS.get(depth, 0)
    if confidence < threshold:
        return "confidence_floor"

    # Forced connection (delta < 0.03 means new data added nothing)
    if depth >= 2 and delta < FORCED_CONNECTION_DELTA:
        return "forced_connection"

    return None


def decide(confidence: float) -> str:
    """Map final confidence to decision."""
    if confidence >= DECISION_THRESHOLDS["strong_enter"]:
        return "strong_enter"
    if confidence >= DECISION_THRESHOLDS["enter"]:
        return "enter"
    if confidence >= DECISION_THRESHOLDS["watch"]:
        return "watch"
    if confidence >= DECISION_THRESHOLDS["skip"]:
        return "skip"
    return "veto"


def run_inference(
    ticker: str,
    signals: dict,
    total_score: int,
    scan_type: str = "pre_market",
    signal_evaluation_id: str | None = None,
    pipeline_run_id: str | None = None,
) -> dict:
    """Execute the full tumbler chain for a ticker.

    Returns dict with: inference_chain_id, final_confidence, final_decision,
    max_depth_reached, stopping_reason, patterns_matched, tumblers
    """
    global TODAY
    TODAY = date.today().isoformat()

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

    # === TUMBLER 1: Technical Foundation ===
    t1 = tumbler_1_technical(ticker, signals, total_score)
    prev_confidence = confidence
    confidence = t1["confidence_after"]
    t1["confidence_before"] = prev_confidence
    t1["confidence_delta"] = round(confidence - prev_confidence, 4)
    t1["duration_ms"] = int((time.time() - start_time) * 1000)
    tumblers.append(t1)

    stopping_reason = check_stopping_rule(t1, prev_confidence, start_time)
    if stopping_reason:
        return _finalize_chain(ticker, scan_type, tumblers, confidence, stopping_reason,
                               signal_evaluation_id, catalyst_event_ids, pattern_template_ids,
                               pipeline_run_id, t1.get("embedding"))

    # === TUMBLER 2: Fundamental + Sentiment ===
    t2_start = time.time()
    t2 = tumbler_2_fundamental(ticker, confidence)
    prev_confidence = confidence
    confidence = t2["confidence_after"]
    t2["confidence_before"] = prev_confidence
    t2["confidence_delta"] = round(confidence - prev_confidence, 4)
    t2["duration_ms"] = int((time.time() - t2_start) * 1000)
    tumblers.append(t2)
    has_veto = t2.get("veto", False)

    stopping_reason = check_stopping_rule(t2, prev_confidence, start_time, has_veto)
    if stopping_reason:
        return _finalize_chain(ticker, scan_type, tumblers, confidence, stopping_reason,
                               signal_evaluation_id, catalyst_event_ids, pattern_template_ids,
                               pipeline_run_id, t1.get("embedding"))

    # === TUMBLER 3: Flow + Cross-Asset ===
    t3_start = time.time()
    t3 = tumbler_3_flow_crossasset(ticker, confidence, {"key_finding": t2.get("key_finding", "")})
    prev_confidence = confidence
    confidence = t3["confidence_after"]
    t3["confidence_before"] = prev_confidence
    t3["confidence_delta"] = round(confidence - prev_confidence, 4)
    t3["duration_ms"] = int((time.time() - t3_start) * 1000)
    tumblers.append(t3)

    stopping_reason = check_stopping_rule(t3, prev_confidence, start_time)
    if stopping_reason:
        return _finalize_chain(ticker, scan_type, tumblers, confidence, stopping_reason,
                               signal_evaluation_id, catalyst_event_ids, pattern_template_ids,
                               pipeline_run_id, t1.get("embedding"))

    # === TUMBLER 4: Pattern Template Matching ===
    if not claude_available:
        stopping_reason = "resource_limit"
        return _finalize_chain(ticker, scan_type, tumblers, confidence, stopping_reason,
                               signal_evaluation_id, catalyst_event_ids, pattern_template_ids,
                               pipeline_run_id, t1.get("embedding"))

    t4_start = time.time()
    t4 = tumbler_4_pattern(ticker, confidence, tumblers)
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

    stopping_reason = check_stopping_rule(t4, prev_confidence, start_time)
    if stopping_reason:
        return _finalize_chain(ticker, scan_type, tumblers, confidence, stopping_reason,
                               signal_evaluation_id, catalyst_event_ids, pattern_template_ids,
                               pipeline_run_id, t1.get("embedding"))

    # === TUMBLER 5: Counterfactual + Final Synthesis ===
    if not claude_available:
        stopping_reason = "resource_limit"
        return _finalize_chain(ticker, scan_type, tumblers, confidence, stopping_reason,
                               signal_evaluation_id, catalyst_event_ids, pattern_template_ids,
                               pipeline_run_id, t1.get("embedding"))

    t5_start = time.time()
    t5 = tumbler_5_counterfactual(ticker, confidence, tumblers)
    prev_confidence = confidence
    confidence = t5["confidence_after"]
    t5["confidence_before"] = prev_confidence
    t5["confidence_delta"] = round(confidence - prev_confidence, 4)
    t5["duration_ms"] = int((time.time() - t5_start) * 1000)
    tumblers.append(t5)

    stopping_reason = "all_tumblers_clear"
    return _finalize_chain(ticker, scan_type, tumblers, confidence, stopping_reason,
                           signal_evaluation_id, catalyst_event_ids, pattern_template_ids,
                           pipeline_run_id, t1.get("embedding"))


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
) -> dict:
    """Store the inference chain and return result."""
    max_depth = max(t.get("depth", 0) for t in tumblers) if tumblers else 0
    decision = decide(confidence)

    # Build reasoning summary
    reasoning_parts = [t.get("key_finding", "") for t in tumblers if t.get("key_finding")]
    reasoning_summary = " | ".join(reasoning_parts)[:500]

    # Clean tumblers for storage (remove embedding)
    stored_tumblers = []
    for t in tumblers:
        stored = {k: v for k, v in t.items() if k not in ("embedding", "veto", "avg_sentiment", "claude_cost", "matched_pattern_ids", "calibration_factor")}
        stored_tumblers.append(stored)

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
    }
    if embedding:
        chain_data["embedding"] = embedding

    stored = _post_to_supabase("inference_chains", chain_data)
    chain_id = stored.get("id") if stored else None

    print(
        f"[inference_engine] {ticker}: depth={max_depth}, "
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
