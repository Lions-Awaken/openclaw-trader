#!/usr/bin/env python3
"""
Post-Trade RAG Ingestion — triggers every time a trade closes.

Call this immediately after a position exits (fill or stop-out):

    python post_trade_analysis.py \
        --ticker NVDA \
        --entry 875.50 \
        --exit 901.25 \
        --hold_days 3 \
        [--chain_id <uuid>]

What it does:
  1. Retrieves the original inference chain for context on what we expected
  2. Fetches market context (SPY/QQQ move during hold period)
  3. Retrieves active catalysts during the hold period
  4. Calls Claude to generate a structured post-mortem comparing
     expectation vs reality
  5. Embeds the post-mortem via Ollama
  6. Stores in trade_learnings table for future RAG retrieval
  7. Back-fills actual_outcome + actual_pnl on inference_chains row

Future inference chains RAG-search trade_learnings at:
  - Tumbler 3: "what happened when we took similar flow setups before?"
  - Tumbler 5: "what did we get wrong in comparable counterfactuals?"
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    ALPACA_DATA,
    ANTHROPIC_API_KEY,
    _client,
    alpaca_headers,
    generate_embedding,
    sb_get,
)
from tracer import (
    PipelineTracer,
    _patch_supabase,
    _post_to_supabase,
    set_active_tracer,
    traced,
)

TODAY = date.today().isoformat()


# ============================================================================
# Data fetchers
# ============================================================================

@traced("economics")
def fetch_inference_chain(chain_id: str | None, ticker: str, trade_date: str) -> dict | None:
    """Get the inference chain that led to this trade."""
    if chain_id:
        rows = sb_get("inference_chains", {"id": f"eq.{chain_id}"})
        return rows[0] if rows else None

    # Fall back to latest chain for this ticker on or before trade_date
    rows = sb_get("inference_chains", {
        "select": "id,ticker,chain_date,max_depth_reached,final_confidence,final_decision,"
                  "stopping_reason,tumblers,reasoning_summary,catalyst_event_ids",
        "ticker": f"eq.{ticker}",
        "chain_date": f"lte.{trade_date}",
        "final_decision": "in.(strong_enter,enter)",
        "order": "chain_date.desc",
        "limit": "1",
    })
    return rows[0] if rows else None


@traced("economics")
def fetch_market_context(ticker: str, entry_date: str, exit_date: str) -> dict:
    """Fetch SPY/QQQ bars for the hold period to give market backdrop context."""
    context = {}
    for sym in ["SPY", "QQQ", ticker]:
        try:
            resp = _client.get(
                f"{ALPACA_DATA}/v2/stocks/{sym}/bars",
                headers=alpaca_headers(),
                params={
                    "timeframe": "1Day",
                    "start": entry_date,
                    "end": exit_date,
                    "limit": "30",
                },
            )
            if resp.status_code == 200:
                bars = resp.json().get("bars", [])
                if bars:
                    first_close = float(bars[0].get("c", 0))
                    last_close = float(bars[-1].get("c", 0))
                    move_pct = round((last_close - first_close) / first_close * 100, 2) if first_close else 0
                    context[sym] = {
                        "move_pct": move_pct,
                        "bars": len(bars),
                        "first_close": first_close,
                        "last_close": last_close,
                    }
        except Exception as e:
            print(f"[post_trade] Market context for {sym}: {e}")

    return context


@traced("economics")
def fetch_active_catalysts(ticker: str, entry_date: str, exit_date: str) -> list:
    """Get catalysts that were active during the hold period."""
    rows = sb_get("catalyst_events", {
        "select": "catalyst_type,headline,direction,magnitude,sentiment_score,event_time,actual_impact_pct",
        "or": f"(ticker.eq.{ticker},affected_tickers.cs.{{{ticker}}})",
        "event_time": f"gte.{entry_date}T00:00:00Z",
        "order": "event_time.asc",
        "limit": "15",
    })
    return rows


@traced("economics")
def call_claude_postmortem(prompt: str) -> tuple[str | None, float]:
    """Call Claude Sonnet for post-mortem analysis. Returns (response, cost)."""
    if not ANTHROPIC_API_KEY:
        return None, 0.0
    try:
        resp = _client.post(
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
            usage = data.get("usage", {})
            cost = (usage.get("input_tokens", 0) * 3 + usage.get("output_tokens", 0) * 15) / 1_000_000
            return content, cost
    except Exception as e:
        print(f"[post_trade] Claude error: {e}")
    return None, 0.0


def log_cost(amount: float, ticker: str, description: str, metadata: dict):
    _post_to_supabase("cost_ledger", {
        "ledger_date": TODAY,
        "category": "claude_api",
        "subcategory": f"post_trade_analysis_{ticker}",
        "amount": round(-abs(amount), 6),
        "description": description,
        "metadata": metadata,
    })


# ============================================================================
# Core analysis
# ============================================================================

def classify_outcome(pnl: float, entry_price: float) -> tuple[str, float]:
    """Map P&L to STRONG_WIN/WIN/SCRATCH/LOSS/STRONG_LOSS and pnl_pct."""
    if entry_price <= 0:
        return "SCRATCH", 0.0
    pnl_pct = (pnl / entry_price) * 100  # per-share pnl as percent of entry price
    if pnl_pct >= 5:
        return "STRONG_WIN", round(pnl_pct, 3)
    if pnl_pct >= 1:
        return "WIN", round(pnl_pct, 3)
    if pnl_pct >= -1:
        return "SCRATCH", round(pnl_pct, 3)
    if pnl_pct >= -3:
        return "LOSS", round(pnl_pct, 3)
    return "STRONG_LOSS", round(pnl_pct, 3)


def build_postmortem_prompt(
    ticker: str,
    entry_price: float,
    exit_price: float,
    pnl: float,
    pnl_pct: float,
    outcome: str,
    hold_days: int,
    chain: dict | None,
    market_ctx: dict,
    catalysts: list,
) -> str:
    chain_summary = ""
    expected_confidence = 0.0

    if chain:
        expected_confidence = float(chain.get("final_confidence", 0))
        chain_summary = f"""
ORIGINAL INFERENCE CHAIN:
  Decision: {chain.get('final_decision', '?')} (confidence: {expected_confidence:.2f})
  Depth reached: {chain.get('max_depth_reached', 0)}/5 tumblers
  Stopping reason: {chain.get('stopping_reason', '?')}
  Reasoning: {chain.get('reasoning_summary', 'N/A')[:400]}"""
        # Pull tumbler key findings if available
        tumblers = chain.get("tumblers", [])
        if tumblers:
            chain_summary += "\n  Tumbler findings:"
            for t in tumblers:
                chain_summary += f"\n    T{t.get('depth', '?')}: {t.get('key_finding', '')[:120]}"

    spy_ctx = market_ctx.get("SPY", {})
    qqq_ctx = market_ctx.get("QQQ", {})
    ticker_ctx = market_ctx.get(ticker, {})

    market_section = f"""
MARKET CONTEXT DURING HOLD ({hold_days} day{'s' if hold_days != 1 else ''}):
  SPY: {spy_ctx.get('move_pct', 'N/A')}%
  QQQ: {qqq_ctx.get('move_pct', 'N/A')}%
  {ticker}: {ticker_ctx.get('move_pct', 'N/A')}%"""

    catalyst_section = ""
    if catalysts:
        catalyst_section = "\nACTIVE CATALYSTS DURING HOLD:"
        for c in catalysts[:8]:
            catalyst_section += (
                f"\n  [{c.get('magnitude','?')} {c.get('direction','?')}] "
                f"{c.get('catalyst_type','?')}: {c.get('headline','')[:100]}"
            )
    else:
        catalyst_section = "\nACTIVE CATALYSTS DURING HOLD: None found in database"

    return f"""You are an expert trading post-mortem analyst. A trade just closed. Generate a structured learning record.

TRADE SUMMARY:
  Ticker: {ticker}
  Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f}
  P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)
  Outcome: {outcome}
  Hold: {hold_days} day{'s' if hold_days != 1 else ''}
{chain_summary}
{market_section}
{catalyst_section}

Analyze this trade. Compare what we expected vs what happened. Be specific and concrete.

Respond with a single JSON object (no markdown fences):
{{
  "expected_direction": "bullish" | "bearish" | "neutral",
  "actual_direction": "bullish" | "bearish" | "flat",
  "expectation_accuracy": "met" | "exceeded" | "missed" | "opposite",
  "actual_move_pct": <float, the ticker's actual % move during hold>,
  "catalyst_match": "<1-2 sentences: did expected catalysts materialize? what actually drove the move?>",
  "pattern_effectiveness": "<1-2 sentences: did the setup pattern hold up? what broke down?>",
  "key_variance": "<1-2 sentences: the single biggest delta between expectation and reality>",
  "what_worked": "<1-2 sentences: what signal or analysis was correct?>",
  "what_failed": "<1-2 sentences: what analysis was wrong or what did we miss?>",
  "key_lesson": "<1 sentence: the most actionable lesson for future similar setups>",
  "setup_conditions": {{
    "technical_signals": "<brief description of technical setup>",
    "fundamental_context": "<brief description of fundamental backdrop>",
    "regime": "<market regime at entry if known>"
  }},
  "exit_conditions": {{
    "trigger": "target_hit" | "stop_loss" | "time_exit" | "manual" | "signal_reversal",
    "note": "<brief description of why/how we exited>"
  }}
}}"""


def run(
    ticker: str,
    entry_price: float,
    exit_price: float,
    hold_days: int,
    chain_id: str | None = None,
    entry_date: str | None = None,
    pipeline_run_id: str | None = None,
) -> dict | None:
    """Run the full post-trade analysis and ingest into trade_learnings."""
    print(f"\n[post_trade] Analyzing closed trade: {ticker} "
          f"${entry_price:.2f} → ${exit_price:.2f} ({hold_days}d)")

    tracer = PipelineTracer("post_trade_analysis", metadata={"ticker": ticker})
    set_active_tracer(tracer)

    try:
        pnl = exit_price - entry_price
        outcome, pnl_pct = classify_outcome(pnl, entry_price)
        trade_date = entry_date or TODAY
        exit_date = (
            datetime.fromisoformat(trade_date) + timedelta(days=hold_days)
        ).date().isoformat()

        print(f"[post_trade] Outcome: {outcome} ({pnl_pct:+.2f}%)")

        # === 1. Retrieve original inference chain ===
        chain = fetch_inference_chain(chain_id, ticker, trade_date)
        if chain:
            print(f"[post_trade] Found inference chain: depth={chain.get('max_depth_reached')}, "
                  f"conf={chain.get('final_confidence'):.3f}")
        else:
            print("[post_trade] No inference chain found — proceeding with market context only")

        # === 2. Market context ===
        market_ctx = fetch_market_context(ticker, trade_date, exit_date)
        print(f"[post_trade] Market context: SPY={market_ctx.get('SPY', {}).get('move_pct', 'N/A')}%, "
              f"QQQ={market_ctx.get('QQQ', {}).get('move_pct', 'N/A')}%")

        # === 3. Active catalysts ===
        catalysts = fetch_active_catalysts(ticker, trade_date, exit_date)
        print(f"[post_trade] Found {len(catalysts)} active catalysts during hold")

        # === 4. Claude post-mortem ===
        prompt = build_postmortem_prompt(
            ticker, entry_price, exit_price, pnl, pnl_pct, outcome,
            hold_days, chain, market_ctx, catalysts,
        )

        print("[post_trade] Calling Claude for post-mortem analysis...")
        t0 = time.time()
        raw_response, claude_cost = call_claude_postmortem(prompt)
        elapsed = time.time() - t0

        analysis = {}
        if raw_response:
            try:
                analysis = json.loads(raw_response)
                print(f"[post_trade] Claude analysis complete ({elapsed:.1f}s, ${claude_cost:.4f})")
            except json.JSONDecodeError:
                # Try to extract JSON from response
                if "{" in raw_response:
                    try:
                        start = raw_response.index("{")
                        end = raw_response.rindex("}") + 1
                        analysis = json.loads(raw_response[start:end])
                        print(f"[post_trade] Claude analysis extracted ({elapsed:.1f}s)")
                    except (json.JSONDecodeError, ValueError):
                        print("[post_trade] Could not parse Claude response, using defaults")
        else:
            print("[post_trade] Claude unavailable — storing with factual data only")

        if claude_cost > 0:
            log_cost(claude_cost, ticker, f"Post-mortem analysis for {ticker} {outcome}",
                     {"model": "claude-sonnet-4-6", "ticker": ticker, "outcome": outcome})

        # === 5. Build content string for embedding ===
        key_lesson = analysis.get("key_lesson", "")
        what_worked = analysis.get("what_worked", "")
        what_failed = analysis.get("what_failed", "")
        key_variance = analysis.get("key_variance", "")

        chain_summary_for_embed = ""
        if chain:
            chain_summary_for_embed = f"Confidence {chain.get('final_confidence', 0):.2f}. {chain.get('reasoning_summary', '')[:200]}"

        content = (
            f"Trade: {ticker}. Outcome: {outcome} ({pnl_pct:+.2f}%). "
            f"Hold: {hold_days}d. Entry ${entry_price:.2f} exit ${exit_price:.2f}. "
            f"Expectation: {analysis.get('expected_direction', 'bullish')} with {chain.get('final_confidence', 0) if chain else 0:.2f} confidence. "
            f"Reality: {analysis.get('actual_direction', '?')} {analysis.get('actual_move_pct', 0):+.2f}%. "
            f"Accuracy: {analysis.get('expectation_accuracy', '?')}. "
            f"Catalysts: {analysis.get('catalyst_match', 'unknown')}. "
            f"What worked: {what_worked}. "
            f"What failed: {what_failed}. "
            f"Key variance: {key_variance}. "
            f"Lesson: {key_lesson}. "
            f"Chain: {chain_summary_for_embed}"
        )

        # === 6. Generate embedding ===
        print("[post_trade] Generating embedding...")
        embedding = generate_embedding(content)
        if embedding:
            print(f"[post_trade] Embedding generated ({len(embedding)} dims)")
        else:
            print("[post_trade] Embedding failed — storing without vector")

        # === 7. Store in trade_learnings ===
        expected_confidence = float(chain.get("final_confidence", 0)) if chain else 0.0
        actual_move = float(analysis.get("actual_move_pct", 0) or 0)

        record: dict = {
            "ticker": ticker,
            "trade_date": trade_date,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
            "outcome": outcome,
            "hold_days": hold_days,
            "expected_direction": analysis.get("expected_direction", "bullish"),
            "expected_confidence": round(expected_confidence, 4),
            "actual_direction": analysis.get("actual_direction", "flat"),
            "actual_move_pct": round(actual_move, 3),
            "expectation_accuracy": analysis.get("expectation_accuracy", "missed"),
            "inference_chain_id": chain.get("id") if chain else None,
            "signal_score": chain.get("signal_score") if chain else None,
            "tumbler_depth": chain.get("max_depth_reached") if chain else None,
            "stopping_reason": chain.get("stopping_reason") if chain else None,
            "catalyst_match": analysis.get("catalyst_match", ""),
            "pattern_effectiveness": analysis.get("pattern_effectiveness", ""),
            "key_variance": analysis.get("key_variance", ""),
            "what_worked": what_worked,
            "what_failed": what_failed,
            "key_lesson": key_lesson,
            "setup_conditions": analysis.get("setup_conditions", {}),
            "exit_conditions": analysis.get("exit_conditions", {}),
            "market_context": market_ctx,
            "active_catalysts": [
                {"type": c.get("catalyst_type"), "headline": c.get("headline", "")[:100],
                 "direction": c.get("direction"), "sentiment": c.get("sentiment_score")}
                for c in catalysts[:8]
            ],
            "content": content,
            "metadata": {
                "claude_cost": round(claude_cost, 6),
                "analysis_duration_s": round(elapsed, 1),
            },
        }
        if pipeline_run_id:
            record["pipeline_run_id"] = pipeline_run_id
        if embedding:
            record["embedding"] = embedding

        stored = _post_to_supabase("trade_learnings", record)
        learning_id = stored.get("id") if stored else None
        print(f"[post_trade] Stored trade_learnings row: {learning_id}")

        # === 8. Back-fill inference chain outcome ===
        if chain and chain.get("id"):
            outcome_map = {
                "STRONG_WIN": "STRONG_WIN",
                "WIN": "WIN",
                "SCRATCH": "SCRATCH",
                "LOSS": "LOSS",
                "STRONG_LOSS": "STRONG_LOSS",
            }
            patched = _patch_supabase("inference_chains", chain["id"], {
                "actual_outcome": outcome_map.get(outcome, outcome),
                "actual_pnl": round(pnl, 4),
            })
            if patched:
                print(f"[post_trade] Back-filled inference_chain {chain['id'][:8]}... with outcome {outcome}")

        print(f"[post_trade] Done. Learning ID: {learning_id}")
        result = {
            "learning_id": learning_id,
            "ticker": ticker,
            "outcome": outcome,
            "pnl_pct": pnl_pct,
            "key_lesson": key_lesson,
            "expectation_accuracy": analysis.get("expectation_accuracy", "?"),
        }
        tracer.complete({"outcome": outcome, "pnl_pct": pnl_pct, "learning_id": learning_id})
        return result

    except Exception as e:
        tracer.fail(str(e))
        print(f"[post_trade] FATAL: {e}")
        raise


# ============================================================================
# CLI entry point
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Post-trade RAG ingestion")
    parser.add_argument("--ticker", required=True, type=str.upper, help="Ticker symbol")
    parser.add_argument("--entry", required=True, type=float, help="Entry price")
    parser.add_argument("--exit", required=True, type=float, help="Exit price")
    parser.add_argument("--hold_days", required=True, type=int, help="Days held")
    parser.add_argument("--chain_id", default=None, help="Inference chain UUID (optional)")
    parser.add_argument("--entry_date", default=None, help="Entry date YYYY-MM-DD (default: today)")
    parser.add_argument("--pipeline_run_id", default=None, help="Parent pipeline run UUID")

    args = parser.parse_args()

    result = run(
        ticker=args.ticker,
        entry_price=args.entry,
        exit_price=args.exit,
        hold_days=args.hold_days,
        chain_id=args.chain_id,
        entry_date=args.entry_date,
        pipeline_run_id=args.pipeline_run_id,
    )
    if result:
        print(f"\n[post_trade] Result: {json.dumps(result, indent=2, default=str)}")
