"""
AI Chat & Trade Reasoning API — /api/chat, /api/trades/{id}/reasoning

Claude-powered chat with full trading context via tool use.
Streaming SSE response with multi-turn tool dispatch loop.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import anthropic
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from shared import (
    ALPACA_BASE,
    ALPACA_KEY,
    ALPACA_SECRET,
    ANTHROPIC_API_KEY,
    SUPABASE_URL,
    _require_auth,
    _validate_uuid,
    clamp_days,
    get_http,
    sb_headers,
)

router = APIRouter()

# ============================================================================
# Chat tool definitions
# ============================================================================

CHAT_TOOLS = [
    {"name": "get_account", "description": "Get current Alpaca account: equity, cash, buying power, portfolio value, paper/live status.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_positions", "description": "Get all open positions with entry price, current price, unrealized P&L, and quantity.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_trades", "description": "Get recent trade decisions with entry/exit prices, P&L, outcome, signals, reasoning.", "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "Max trades to return (default 20)"}}, "required": []}},
    {"name": "get_inference_chains", "description": "Get tumbler-by-tumbler inference chains: depth reached, confidence, decision (enter/watch/skip/veto), stopping reason, reasoning summary.", "input_schema": {"type": "object", "properties": {"ticker": {"type": "string", "description": "Filter by ticker symbol (e.g. AAPL)"}, "days": {"type": "integer", "description": "Lookback days (default 7)"}}, "required": []}},
    {"name": "get_signal_evaluations", "description": "Get per-ticker signal scores: trend, momentum, volume, fundamental, sentiment, flow — each scored 0 or 1, with total score and reasoning.", "input_schema": {"type": "object", "properties": {"ticker": {"type": "string", "description": "Filter by ticker"}, "days": {"type": "integer", "description": "Lookback days (default 7)"}}, "required": []}},
    {"name": "get_catalysts", "description": "Get recent catalyst events: market-moving news with ticker, type, headline, direction (bullish/bearish), magnitude, sentiment score.", "input_schema": {"type": "object", "properties": {"ticker": {"type": "string", "description": "Filter by ticker"}, "days": {"type": "integer", "description": "Lookback days (default 7)"}}, "required": []}},
    {"name": "get_meta_reflections", "description": "Get daily/weekly meta-analysis reflections: AI-generated strategy reviews with patterns observed, pipeline health, adjustments proposed.", "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "description": "Lookback days (default 14)"}}, "required": []}},
    {"name": "get_trade_learnings", "description": "Get post-trade analysis (post-mortems): what worked, what failed, key lessons, tumbler depth, expectation accuracy, catalyst match analysis.", "input_schema": {"type": "object", "properties": {"ticker": {"type": "string", "description": "Filter by ticker"}, "days": {"type": "integer", "description": "Lookback days (default 60)"}, "outcome": {"type": "string", "description": "Filter by outcome: WIN, STRONG_WIN, LOSS, STRONG_LOSS, SCRATCH"}}, "required": []}},
    {"name": "get_economics", "description": "Get economics summary: trading P&L, API costs (Claude, Perplexity), budget usage, cost breakdown by category.", "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "description": "Lookback days (default 30)"}}, "required": []}},
    {"name": "get_pipeline_health", "description": "Get pipeline health: success rate, recent run status per pipeline (scanner, catalyst_ingest, position_manager, etc), failure details.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_regime", "description": "Get current market regime (UP_LOWVOL, UP_HIGHVOL, DOWN_ANY, SIDEWAYS) and recent regime history.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_calibration", "description": "Get confidence calibration: Brier score, overconfidence bias, stated vs actual confidence buckets.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_strategy_profiles", "description": "Get all strategy profiles (CONSERVATIVE, UNLEASHED, etc) with parameters: min confidence, max risk, position sizing, trade style, hold days.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_sitrep", "description": "Get the full decision intelligence briefing: trades enriched with inference chains, signals, and catalysts. Best for comprehensive analysis.", "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "description": "Lookback days (default 30)"}}, "required": []}},
]

# ============================================================================
# Workflow context for step-aware chat
# ============================================================================

WORKFLOW_CONTEXT: dict[int, dict[str, str]] = {
    1: {"title": "HEALTH CHECK", "group": "pre-market", "description": "59 automated checks across 13 groups validating every integration point in the system. Runs at 5:00 AM PDT before anything else fires.", "data_in": "Supabase tables, Ollama API, Alpaca API, crontab, file system", "data_out": "system_health table rows (one per check, grouped by run_id)", "db_table": "system_health", "cost": "Free (one cheap Claude Haiku call for API canary)", "parameters": "Check groups: infrastructure, database, crons, signals, tumblers, ensemble, logging, dashboard, claude_api, crontab_drift, output_quality, data_freshness, historical_regression", "limitations": "Runs on ridley so it can test local services, but can't test the Fly.io dashboard deployment. Dashboard API checks (I-group) require the local dashboard to be running.", "improvements": "Could add a Fly.io health check that tests the production deployment. Could add network latency checks to external APIs.", "connections": "Runs before all other pipelines. If health check fails, Slack alert fires. Results visible in the dashboard Health tab and preflight simulator."},
    2: {"title": "CATALYST INGEST", "group": "pre-market", "description": "5-source market catalyst detection. Fetches news, filings, congressional trades, price signals, and macro indicators. Embeds each event via Ollama for RAG retrieval in later tumblers.", "data_in": "Finnhub API (news + insiders), SEC EDGAR (filings), QuiverQuant (congressional trades), yfinance (price signals), FRED (macro indicators)", "data_out": "catalyst_events rows with embeddings, congress_clusters", "db_table": "catalyst_events", "cost": "Free (all sources are free-tier APIs, Ollama embedding is local)", "parameters": "Lookback hours: 8 (configurable). Watchlist: active profile tickers + recent signal_evaluations tickers. Duplicate detection: cosine similarity threshold 0.95.", "limitations": "QuiverQuant has been returning 0 events consistently — may be rate-limited or the API changed. SEC EDGAR returns matched=0 for most runs. Perplexity was removed to save cost.", "improvements": "Could add alternative congressional data sources. Could add earnings calendar integration. Could weight catalyst freshness more aggressively.", "connections": "Feeds T2 tumbler (catalyst boost). Must complete before scanner runs. 3x daily schedule (5:30, 9:00, 12:50 PDT)."},
    3: {"title": "FORM 4 INGEST", "group": "pre-market", "description": "SEC EDGAR Form 4 insider purchase filings. Scores by total value, ownership change, cluster count (multiple insiders buying same week), and filer title (CEO/CFO = strongest).", "data_in": "SEC EDGAR EFTS API, target tickers from active profile + AI infrastructure watchlist", "data_out": "form4_signals rows with score in raw_data", "db_table": "form4_signals", "cost": "Free (SEC EDGAR is public)", "parameters": "Scoring: total_value ($1M+=3, $500K+=2, $100K+=1), ownership_pct_change (>10%=3, >5%=2, >1%=1), cluster_count (per additional buyer: +2, max +4), filer_title (CEO/CFO/Chairman=+2, VP/Director=+1)", "limitations": "SEC EDGAR has rate limits (10 requests/sec with User-Agent). Filing delay: 2 business days from trade date. Sales are filtered out (only purchases).", "improvements": "Could track filing patterns over time (insiders who file fast = higher conviction). Could correlate with earnings dates.", "connections": "Feeds scanner enrichment (_enrich_with_form4). Scanner adds form4_insider_score and form4_purchase_count to each candidate's signals dict."},
    4: {"title": "SCANNER SETUP", "group": "scanner", "description": "Loads the active trading profile (CONGRESS_MIRROR), checks circuit breakers (VIX, drawdown, consecutive losses), verifies Alpaca account equity and buying power, builds the 39-ticker watchlist.", "data_in": "strategy_profiles table, Alpaca account API, market clock", "data_out": "Profile config, equity/buying_power values, circuit breaker status", "db_table": "pipeline_runs", "cost": "Free", "parameters": "Circuit breakers: VIX > 28 (stand down), drawdown > 15%, 3 consecutive losses. Max concurrent positions: 3. Max risk per trade: 5%.", "limitations": "Watchlist is semi-static (39 tickers). Doesn't dynamically discover new tickers based on momentum or catalysts.", "improvements": "Dynamic watchlist expansion based on catalyst volume. Sector rotation detection to shift focus.", "connections": "Gates the entire scanner pipeline. If circuit breaker trips, scanner exits without scanning."},
    5: {"title": "T1 — SIGNAL SCORING", "group": "scanner", "description": "39 watchlist tickers scored against 6 binary signals: Trend (SMA20 > SMA50), Momentum (RSI 30-70 + MACD), Volume (relative volume > 1.2), Catalyst (recent catalyst_events), Sentiment (positive catalyst sentiment), Flow (institutional flow indicators). Score 0-6, only tickers >= 3 advance.", "data_in": "Alpaca price bars (60 days), SPY bars for benchmark, catalyst_events for sentiment", "data_out": "signal_evaluations rows, candidate list with scores and signal details", "db_table": "signal_evaluations", "cost": "Free (pure computation, no API calls)", "parameters": "Min signal score: 3 (from active profile). SMA periods: 20, 50. RSI period: 14. Volume lookback: 20 days.", "limitations": "Binary signals (pass/fail) lose nuance. A ticker at RSI 31 scores the same as RSI 60. No weighting between signals.", "improvements": "Continuous signal scoring instead of binary. Signal weighting based on historical predictive power. Sector-relative scoring.", "connections": "First tumbler in the chain. Output feeds enrichment and T2. Writes to signal_evaluations for dashboard display."},
    6: {"title": "SIGNAL ENRICHMENT", "group": "scanner", "description": "Candidates enriched with options flow data (3-day lookback, bullish/bearish/net from options_flow_signals) and Form 4 insider purchases (14-day lookback, score + count from form4_signals).", "data_in": "options_flow_signals table, form4_signals table, candidate list from T1", "data_out": "Candidates with additional signal keys: options_flow_bullish, options_flow_bearish, options_flow_net, form4_insider_score, form4_purchase_count", "db_table": "signal_evaluations", "cost": "Free (Supabase queries only)", "parameters": "Options flow lookback: 3 days. Form 4 lookback: 14 days. Options flow: count bullish/bearish sentiment. Form 4: tiered scoring by value + cluster.", "limitations": "Options flow data is currently empty (Unusual Whales API not connected). Form 4 data depends on ingest_signals.py running correctly.", "improvements": "Connect Unusual Whales API for live options flow. Add dark pool print detection. Correlate form4 timing with earnings.", "connections": "Runs after T1, before T2. Adds data that T2/T3 can reference in their analysis."},
    7: {"title": "T2 — FUNDAMENTAL ANALYSIS", "group": "tumbler", "description": "RAG-powered fundamental analysis. Retrieves similar past inference chains and trade learnings via pgvector similarity search. Congressional trade disclosures get confidence boost if filed within 40 days. Hard veto if sentiment score < -0.5.", "data_in": "Candidate signals, pgvector embeddings from inference_chains and trade_learnings, catalyst_events, congress_clusters", "data_out": "Updated confidence (+-sentiment_adj), veto flag, catalyst_bonus, congress_boost", "db_table": "inference_chains", "cost": "Free (RAG retrieval only, no LLM call)", "parameters": "Sentiment adjustment: avg_sentiment * 0.15. Catalyst bonus: +-0.05. Congress boost: +-0.07 (if high-impact legislative event < 14 days). Veto threshold: sentiment < -0.5.", "limitations": "RAG quality depends on historical data volume. New tickers with no history get no RAG context. Congress data depends on QuiverQuant (currently returning 0).", "improvements": "Add earnings surprise correlation. Weight RAG results by recency. Add sector-level sentiment aggregation.", "connections": "Second tumbler. Receives confidence from T1. Can veto (kills the chain). Output feeds T3. Shadow context injected here for shadow profiles."},
    8: {"title": "T3 — FLOW & CROSS-ASSET", "group": "tumbler", "description": "First LLM call — Ollama qwen2.5:3b running locally on ridley's Jetson GPU. Analyzes how this setup compares to past chains and outcomes. Shadow profile system prompts are injected here for adversarial analysis.", "data_in": "Candidate data, T1+T2 results, RAG context (past chains + trade learnings), shadow system prompt (if shadow profile)", "data_out": "Confidence adjustment +-0.10, qwen analysis text", "db_table": "inference_chains", "cost": "Free (local Ollama, no API cost)", "parameters": "Model: qwen2.5:3b. Temperature: 0.3. Max tokens (num_predict): 512. Adjustment range: +-0.10.", "limitations": "qwen2.5:3b is a 3B parameter model — limited reasoning depth compared to Claude. Can't do complex multi-step analysis. Response quality varies.", "improvements": "Could upgrade to qwen2.5:7b if RAM allows (would need to unload during Kronos runs). Could add structured output parsing for more reliable adjustments.", "connections": "Third tumbler. REGIME_WATCHER stops here (max_tumbler_depth=3). This is where shadow profiles diverge from live — the adversarial system prompts change qwen's analysis."},
    9: {"title": "T4 — PATTERN MATCH", "group": "tumbler", "description": "Claude Haiku matches the current setup against known pattern templates with documented outcomes. Evaluates quality and coherence of the full trade thesis built by T1-T3.", "data_in": "Full tumbler chain context (T1-T3 results), pattern_templates from Supabase (with similarity matching)", "data_out": "Confidence adjustment +-0.10, matched pattern IDs, thesis quality assessment", "db_table": "inference_chains", "cost": "~$0.001 per call (Claude Haiku)", "parameters": "Model: claude-haiku-4-5-20251001. Max tokens: 256. Temperature: 0.3. Pattern match threshold: similarity >= 0.5.", "limitations": "Pattern template library is small — only patterns discovered by the weekly calibrator. Haiku has limited context window for complex pattern matching.", "improvements": "Could pre-compute pattern embeddings for faster matching. Could use a larger pattern library from historical backtesting. Could let the meta-learner create patterns from unanimous dissent events.", "connections": "Fourth tumbler. Only fires when Claude budget allows (budget gate). Shadow profiles get their adversarial prompts injected here too."},
    10: {"title": "T5 — COUNTERFACTUAL", "group": "tumbler", "description": "Claude Sonnet as devil's advocate — constructs the strongest argument AGAINST the trade. Asymmetric adjustment: can drop confidence by 0.15 but raise it only 0.05. Applies calibration factor from weekly calibrator.", "data_in": "Full chain context (T1-T4), meta_reflections (RAG), trade_learnings (losses/misses)", "data_out": "Final calibrated confidence, risk factors, counterfactual analysis", "db_table": "inference_chains", "cost": "~$0.005 per call (Claude Sonnet)", "parameters": "Model: claude-sonnet-4-6-20250514. Max tokens: 512. Temperature: 0.3. Adjustment range: -0.15 to +0.05 (asymmetric bearish bias). Calibration factor applied after raw adjustment.", "limitations": "Most expensive tumbler. Budget gate may skip this at low budget. The asymmetric adjustment means T5 is structurally bearish — by design, but limits bullish conviction.", "improvements": "Could A/B test symmetric vs asymmetric adjustment. Could use Haiku for a cheaper counterfactual with less depth. Could add market regime awareness to the counterfactual prompt.", "connections": "Fifth and final tumbler. If all 5 complete: stopping_reason = 'all_tumblers_clear'. Calibration factor from calibrator.py applied to raw confidence. Final decision: strong_enter (>=0.75), enter (>=0.60), watch (>=0.45), skip (>=0.20), veto (<0.20)."},
    11: {"title": "EXECUTION GATE", "group": "execution", "description": "Trade executes ONLY if: decision = enter/strong_enter, confidence >= 0.60, and >= 3 tumblers completed. ATR-based position sizing with 5% max risk per trade. Market buy + stop-loss order.", "data_in": "Final inference result (decision, confidence, stopping_reason), Alpaca account (equity, buying_power, positions)", "data_out": "trade_decisions row, order_events rows (market buy + stop-loss)", "db_table": "trade_decisions", "cost": "Free (Alpaca paper trading)", "parameters": "Min confidence: 0.60. Min tumbler depth: 3. Max risk: 5% of equity. Position sizing: ATR-based (14-period ATR x 2 for stop distance). Max concurrent positions: 3.", "limitations": "Paper trading only — no real money at risk. Alpaca paper trading doesn't perfectly simulate real market conditions (fills are instant, no slippage).", "improvements": "Could implement limit orders instead of market orders. Could add time-of-day execution preferences. Could implement partial position sizing based on confidence level.", "connections": "End of the live inference chain. Only fires for the live profile (CONGRESS_MIRROR), never for shadow profiles. Writes to trade_decisions and order_events."},
    12: {"title": "BUDGET GATE", "group": "shadow", "description": "Controls which shadow agents run based on remaining Claude API budget. Three tiers: >= 40% = all 6 shadows, 20-40% = cheap shadows only (Regime Watcher + Form 4 + Kronos), < 20% = Kronos only (zero API cost).", "data_in": "cost_ledger (today's Claude spend), budget_config (daily_claude_budget)", "data_out": "Filtered list of shadow profiles to run", "db_table": "cost_ledger", "cost": "Free", "parameters": "Tier 1 threshold: 40%. Tier 2 threshold: 20%. Cheap profiles: REGIME_WATCHER (Ollama only, stops at T3), FORM4_INSIDER, KRONOS_TECHNICALS (local GPU, zero API cost).", "limitations": "Binary tier system — doesn't partially reduce shadow depth. Could run all shadows but cap at T3 for budget savings.", "improvements": "Continuous budget allocation instead of tiers. Priority queue — run highest-DWM-weight shadows first. Adaptive tier thresholds based on time of day.", "connections": "Gates the shadow inference loop. Determines which of the 6 shadow profiles actually execute."},
    13: {"title": "SKEPTIC", "group": "shadow", "description": "Maximally conservative adversarial reviewer. Requires overwhelming evidence to approve entry. Heavily penalizes momentum-chasing. If a stock moved > 3% in 3 days, demands additional justification.", "data_in": "Same candidates as live profile, full tumbler chain with SKEPTIC system prompt injected", "data_out": "shadow_divergences row if decision differs from live", "db_table": "shadow_divergences", "cost": "Claude API (runs full T1-T5 chain)", "parameters": "Grading metric: Conditional Brier Score. System prompt: immutable, never modified by meta-learner. Full tumbler depth (5).", "limitations": "Structurally bearish — almost always says skip. High dissent rate may dilute signal quality.", "improvements": "Could add a confidence-weighted dissent (strong skip vs weak skip). Could track which specific tumblers cause SKEPTIC to diverge.", "connections": "Runs after live inference. Divergences recorded for weekly calibrator grading. DWM weight determines how much the meta-learner listens to SKEPTIC's dissent."},
    14: {"title": "CONTRARIAN", "group": "shadow", "description": "Assumes the trade is wrong. Overweights sector rotation signals, institutional distribution (volume without price progress), and divergence between price and fundamentals.", "data_in": "Same candidates, CONTRARIAN system prompt injected into tumblers", "data_out": "shadow_divergences row on disagreement", "db_table": "shadow_divergences", "cost": "Claude API (full T1-T5)", "parameters": "Grading metric: Regime-Conditional IC. System prompt: immutable. Full tumbler depth (5).", "limitations": "Expected to be wrong during strong trends (momentum carries). Most useful during regime transitions.", "improvements": "Could dynamically adjust contrarian weight based on market regime (higher weight in choppy markets, lower in trends).", "connections": "Same as SKEPTIC. Calibrator grades on regime-conditional information coefficient."},
    15: {"title": "REGIME WATCHER", "group": "shadow", "description": "Ignores the ticker entirely. Only question: 'Is this a good time to enter ANY long position?' Evaluates SPY trend, VIX, yield curve, credit spreads, sector rotation breadth.", "data_in": "Macro data only — ignores individual ticker signals", "data_out": "shadow_divergences row on disagreement", "db_table": "shadow_divergences", "cost": "Free (stops at T3, Ollama only — no Claude API calls)", "parameters": "Max tumbler depth: 3 (T1 + T2 + T3 only). Grading metric: Detection Latency. System prompt: immutable.", "limitations": "No Claude analysis — limited to Ollama qwen's macro reasoning. Can't do deep counterfactual analysis of macro risks.", "improvements": "Could add FRED macro indicators as direct T2 input. Could track regime change detection accuracy over time.", "connections": "Cheapest LLM shadow — survives budget tier 2. High enter rate (bullish bias) — consistently wants to enter when others don't."},
    16: {"title": "OPTIONS FLOW", "group": "shadow", "description": "Momentum-focused. Primary signal: unusual options activity — sweeps, blocks, dark pool prints. Alpha decay fast (1-5 day window). Graded on 5-day forward return.", "data_in": "Same candidates + options_flow_signals enrichment data", "data_out": "shadow_divergences row on disagreement", "db_table": "shadow_divergences", "cost": "Claude API (full T1-T5)", "parameters": "Alpha decay: 1-5 days. Grading metric: 5-day forward return. System prompt: immutable. Full tumbler depth (5).", "limitations": "Options flow data is currently empty (Unusual Whales API not connected). This shadow is making decisions without its primary signal source.", "improvements": "CRITICAL: Connect Unusual Whales API to actually feed options flow data. Without it, this shadow is essentially running blind on the options dimension.", "connections": "Depends on ingest_options_flow for data. Currently running without its key data source."},
    17: {"title": "FORM 4 INSIDER", "group": "shadow", "description": "Fundamentals-anchored. Primary signal: Form 4 purchase filings by CEOs, CFOs, board members within 14 days. Cluster buys weighted heavily. CFOs = strongest signal.", "data_in": "Same candidates + form4_signals enrichment data", "data_out": "shadow_divergences row on disagreement", "db_table": "shadow_divergences", "cost": "Claude API (full T1-T5)", "parameters": "Holding period: up to 15 days. Grading metric: 15-day forward return. System prompt: immutable. Full tumbler depth (5).", "limitations": "Form 4 filing delay is 2 business days — signal is always slightly stale. Cluster detection is simple (same-week buys).", "improvements": "Could weight by insider's historical accuracy (some CFOs are consistently right). Could track filing speed anomalies (chronic late filers suddenly filing fast).", "connections": "Depends on ingest_form4 for data. Best when cluster buys are detected."},
    18: {"title": "KRONOS TECHNICALS", "group": "shadow", "description": "Pure price pattern agent using Kronos financial time series foundation model. 252 daily OHLCV candles -> 50 Monte Carlo paths -> bullish probability at 10-day horizon. No news, no fundamentals — only price.", "data_in": "yfinance daily OHLCV bars (252 days), Kronos-small model weights", "data_out": "shadow_divergences row with bullish_prob in shadow_confidence", "db_table": "shadow_divergences", "cost": "Free (local Jetson GPU inference, ~25 seconds per ticker)", "parameters": "Model: NeoQuasar/Kronos-small (24.7M params). Prediction length: 15 bars. Monte Carlo paths: 50. Horizon bar: 10. Bullish threshold: 0.60. Bearish threshold: 0.40. Max candidates: top 5 by score.", "limitations": "25 seconds per ticker limits to top 5 candidates. Ollama must unload before Kronos loads (shared GPU memory). Model is price-only — blind to fundamental catalysts.", "improvements": "Could run overnight batch on all watchlist tickers. Could ensemble Kronos predictions with a trend-following model. Could fine-tune on the specific AI infrastructure sector.", "connections": "Survives all budget tiers (zero API cost). First live run: April 10, 2026. Graded on directional accuracy at 10-day horizon."},
    19: {"title": "CALIBRATOR", "group": "calibration", "description": "Sunday weekly. Grades all ungraded shadow divergences from past 30 days. Each shadow type has its own grading metric. Updates fitness_score and DWM weight in strategy_profiles.", "data_in": "shadow_divergences (ungraded), inference_chains (actual outcomes), price history (for Kronos directional accuracy)", "data_out": "Updated fitness_score, dwm_weight, conditional_brier, times_correct, times_dissented in strategy_profiles. Updated shadow_was_right, actual_outcome, actual_pnl in shadow_divergences.", "db_table": "strategy_profiles", "cost": "Free (computation only)", "parameters": "DWM formula: new_weight = 1.0 x (1 + 0.5 x (fitness - median_fitness)), clamped [0.05, 3.0]. Alpha: 0.5. Lookback: 30 days. Grading metrics: SKEPTIC=Brier, CONTRARIAN=IC, REGIME_WATCHER=latency, KRONOS=directional_accuracy_10d.", "limitations": "Weekly cadence means DWM weights update slowly. 30-day window means old regime data can dilute current accuracy.", "improvements": "Could run daily for faster adaptation. Could use exponential decay instead of hard 30-day cutoff. Could weight recent divergences more heavily.", "connections": "Reads shadow_divergences, writes strategy_profiles. DWM weights influence meta daily reflection priority. Runs after meta_weekly."},
    20: {"title": "META DAILY", "group": "meta", "description": "1:30 PM weekdays. Claude Sonnet reviews the day's shadow divergences. Identifies unanimous dissent (all 6 shadows disagreed with live on a ticker). Output becomes RAG context for future T2 tumbler calls.", "data_in": "shadow_divergences (today), pipeline_health, signal_accuracy, trades, catalysts, chain analysis", "data_out": "meta_reflections row with signal_assessment, operational_issues, adjustments, embedding", "db_table": "meta_reflections", "cost": "~$0.02 per call (Claude Sonnet + Ollama embedding)", "parameters": "Model: Claude Sonnet. Unanimous dissent: all active shadows disagreed on same ticker. RAG: retrieves similar past days for context.", "limitations": "Depends on Claude API budget being available. If budget exhausted, reflection fails with 'Unable to assess'. No automated adjustment execution — proposed adjustments require human approval.", "improvements": "Auto-execute adjustments within +-5% bounds. Add Kronos directional predictions as additional meta context. Track which adjustments were approved vs rejected.", "connections": "Final pipeline step each day. Writes to meta_reflections with embedding. Future T2 calls retrieve these reflections via RAG. The feedback loop: shadows -> divergences -> calibrator -> DWM weights -> meta daily -> RAG context -> future T2 analysis."},
}

CHAT_SYSTEM_PROMPT = """You are the OpenClaw Trader AI co-pilot — deeply embedded in an autonomous swing trading system built on adversarial AI ensemble architecture.

## System Architecture

OpenClaw runs on ridley (NVIDIA Jetson Orin Nano 8GB) with these components:
- **Scanner**: Runs 2x daily (6:35 AM, 9:30 AM PDT). 39-ticker AI infrastructure watchlist. 5-tumbler inference chain (T1-T5).
- **Adversarial Ensemble**: 6 shadow agents run in parallel with the live profile, recording disagreements as training data.
- **DWM Calibrator**: Weekly grading of shadow divergences. Fitness = correct/dissented. Weight formula: 1.0 x (1 + 0.5 x (fitness - median)), clamped [0.05, 3.0].
- **Kronos**: Pure price pattern forecasting via Kronos-small (24.7M params) on Jetson GPU. 50 Monte Carlo paths, 15-bar horizon.
- **Meta Daily**: Claude Sonnet reflects on shadow divergences. Unanimous dissent = HIGH PRIORITY.

## The 5-Tumbler Chain
- T1: Signal scoring (6 binary signals, pure math, free)
- T2: Fundamental analysis (RAG + catalyst boost + Congress, free)
- T3: Flow & cross-asset (Ollama qwen2.5:3b, local GPU, free)
- T4: Pattern matching (Claude Haiku, ~$0.001)
- T5: Counterfactual synthesis (Claude Sonnet, ~$0.005, asymmetric bearish bias)

## The 6 Shadow Agents
1. SKEPTIC — maximally conservative, Conditional Brier Score
2. CONTRARIAN — assumes trade is wrong, Regime-Conditional IC
3. REGIME_WATCHER — macro only (stops at T3), Detection Latency
4. OPTIONS_FLOW — unusual options activity, 5-day forward return
5. FORM4_INSIDER — SEC insider purchases, 15-day forward return
6. KRONOS_TECHNICALS — pure OHLCV price patterns (local GPU, 10-day directional accuracy)

## Budget Gate
- >= 40% remaining: all 6 shadows
- 20-40%: Regime Watcher + Form 4 + Kronos
- < 20%: Kronos only (zero API cost)

## Execution Gate
Trade executes only if: decision = enter/strong_enter, confidence >= 0.60, >= 3 tumblers completed. ATR-based sizing, 5% max risk.

## Infrastructure
- ridley: Jetson Orin Nano 8GB (scanner, Ollama, Kronos GPU inference)
- motherbrain: Orchestrator (Picard)
- Supabase: PostgreSQL + pgvector (project: vpollvsbtushbiapoflr)
- Alpaca: Paper trading API
- Fly.io: Dashboard (openclaw-trader-dash.fly.dev)

## Your Persona
You are a knowledgeable co-pilot who:
- Answers with specific data — use your tools to look things up, don't guess
- Knows every detail of the architecture and can explain any component in depth
- Is willing to challenge design decisions and suggest improvements
- Thinks about edge cases, failure modes, and optimization opportunities
- Speaks like an engineering partner, not a tutorial
- When the user is viewing a specific workflow step, you have deep context about that step and can discuss its internals, limitations, and potential improvements

Keep responses concise and data-driven. Use tables for comparisons. The user built this system — speak as a peer."""


# ============================================================================
# Chat tool dispatch
# ============================================================================


async def _chat_tool_dispatch(name: str, input_data: dict) -> str:
    """Execute a chat tool and return JSON string result."""
    client = get_http()
    try:
        if name == "get_account":
            resp = await client.get(
                f"{ALPACA_BASE}/v2/account",
                headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            )
            if resp.status_code == 200:
                d = resp.json()
                return json.dumps({
                    "equity": d.get("equity"),
                    "cash": d.get("cash"),
                    "buying_power": d.get("buying_power"),
                    "portfolio_value": d.get("portfolio_value"),
                    "status": d.get("status"),
                    "paper": d.get("account_number", "").startswith("PA"),
                })
            return json.dumps({"error": f"Alpaca {resp.status_code}"})

        if name == "get_positions":
            resp = await client.get(
                f"{ALPACA_BASE}/v2/positions",
                headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            )
            if resp.status_code == 200:
                return json.dumps([
                    {
                        "symbol": p.get("symbol"),
                        "qty": p.get("qty"),
                        "avg_entry": p.get("avg_entry_price"),
                        "current_price": p.get("current_price"),
                        "unrealized_pl": p.get("unrealized_pl"),
                        "unrealized_plpc": round(float(p.get("unrealized_plpc", 0)) * 100, 2),
                        "side": p.get("side"),
                    }
                    for p in resp.json()
                ])
            return json.dumps([])

        if name == "get_trades":
            limit = min(input_data.get("limit", 20), 50)
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/trade_decisions",
                headers=sb_headers(),
                params={"select": "id,ticker,action,entry_price,exit_price,pnl,outcome,signals_fired,hold_days,reasoning,what_worked,improvement,created_at", "order": "created_at.desc", "limit": str(limit)},
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_inference_chains":
            days = clamp_days(input_data.get("days", 7), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            params: dict = {
                "select": "id,ticker,chain_date,max_depth_reached,final_confidence,final_decision,stopping_reason,tumblers,reasoning_summary,actual_outcome,actual_pnl,created_at",
                "chain_date": f"gte.{cutoff}",
                "order": "created_at.desc",
                "limit": "50",
            }
            if input_data.get("ticker"):
                params["ticker"] = f"eq.{input_data['ticker'].upper()}"
            resp = await client.get(f"{SUPABASE_URL}/rest/v1/inference_chains", headers=sb_headers(), params=params)
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_signal_evaluations":
            days = clamp_days(input_data.get("days", 7), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            params = {
                "select": "id,ticker,scan_date,scan_type,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision,reasoning,created_at",
                "scan_date": f"gte.{cutoff}",
                "order": "created_at.desc",
                "limit": "50",
            }
            if input_data.get("ticker"):
                params["ticker"] = f"eq.{input_data['ticker'].upper()}"
            resp = await client.get(f"{SUPABASE_URL}/rest/v1/signal_evaluations", headers=sb_headers(), params=params)
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_catalysts":
            days = clamp_days(input_data.get("days", 7), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            params = {
                "select": "id,ticker,catalyst_type,headline,direction,magnitude,sentiment_score,event_time",
                "event_time": f"gte.{cutoff}",
                "order": "event_time.desc",
                "limit": "50",
            }
            if input_data.get("ticker"):
                params["ticker"] = f"eq.{input_data['ticker'].upper()}"
            resp = await client.get(f"{SUPABASE_URL}/rest/v1/catalyst_events", headers=sb_headers(), params=params)
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_meta_reflections":
            days = clamp_days(input_data.get("days", 14), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/meta_reflections",
                headers=sb_headers(),
                params={"select": "id,reflection_type,reflection_date,patterns_observed,pipeline_health_score,adjustments_proposed,trade_count,win_rate,created_at", "reflection_date": f"gte.{cutoff}", "order": "reflection_date.desc", "limit": "20"},
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_trade_learnings":
            days = clamp_days(input_data.get("days", 60), 180)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            params = {
                "select": "id,ticker,trade_date,entry_price,exit_price,pnl,pnl_pct,outcome,hold_days,expected_direction,expected_confidence,actual_direction,actual_move_pct,expectation_accuracy,catalyst_match,key_variance,what_worked,what_failed,key_lesson,tumbler_depth,created_at",
                "trade_date": f"gte.{cutoff}",
                "order": "trade_date.desc",
                "limit": "50",
            }
            if input_data.get("ticker"):
                params["ticker"] = f"eq.{input_data['ticker'].upper()}"
            if input_data.get("outcome"):
                params["outcome"] = f"eq.{input_data['outcome']}"
            resp = await client.get(f"{SUPABASE_URL}/rest/v1/trade_learnings", headers=sb_headers(), params=params)
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_economics":
            days = clamp_days(input_data.get("days", 30), 365)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/cost_ledger",
                headers=sb_headers(),
                params={"select": "cost_type,source,amount,currency,description,created_at", "created_at": f"gte.{cutoff}T00:00:00Z", "order": "created_at.desc", "limit": "100"},
            )
            rows = resp.json() if resp.status_code == 200 else []
            summary: dict = {}
            total = 0.0
            for r in rows:
                ct = r.get("cost_type", "other")
                amt = float(r.get("amount", 0))
                summary[ct] = summary.get(ct, 0.0) + amt
                total += amt
            return json.dumps({"total": round(total, 2), "by_type": {k: round(v, 2) for k, v in summary.items()}, "recent": rows[:20]})

        if name == "get_pipeline_health":
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/pipeline_runs",
                headers=sb_headers(),
                params={"select": "pipeline_name,status,error_message,started_at,completed_at", "order": "started_at.desc", "limit": "30"},
            )
            rows = resp.json() if resp.status_code == 200 else []
            by_pipeline: dict = {}
            for r in rows:
                pn = r.get("pipeline_name", "unknown")
                if pn not in by_pipeline:
                    by_pipeline[pn] = {"total": 0, "ok": 0, "failed": 0, "last_status": r.get("status"), "last_error": r.get("error_message")}
                by_pipeline[pn]["total"] += 1
                if r.get("status") == "completed":
                    by_pipeline[pn]["ok"] += 1
                elif r.get("status") == "failed":
                    by_pipeline[pn]["failed"] += 1
            return json.dumps(by_pipeline)

        if name == "get_regime":
            from pathlib import Path as _Path
            regime_file = _Path.home() / ".openclaw/workspace/memory/regime-current.json"
            current = json.loads(regime_file.read_text()) if regime_file.exists() else {"regime": "UNKNOWN"}
            return json.dumps({"current": current, "history": []})

        if name == "get_calibration":
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/confidence_calibration",
                headers=sb_headers(),
                params={"order": "week_start.desc", "limit": "8"},
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_strategy_profiles":
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/strategy_profiles",
                headers=sb_headers(),
                params={"select": "id,profile_name,description,active,min_signal_score,min_tumbler_depth,min_confidence,max_risk_per_trade_pct,max_concurrent_positions,position_size_method,trade_style,max_hold_days,circuit_breakers_enabled,created_at", "order": "created_at.asc"},
            )
            return json.dumps(resp.json() if resp.status_code == 200 else [])

        if name == "get_sitrep":
            days = clamp_days(input_data.get("days", 30), 90)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            trades_r, chains_r = await asyncio.gather(
                client.get(f"{SUPABASE_URL}/rest/v1/trade_decisions", headers=sb_headers(), params={"select": "id,ticker,action,entry_price,exit_price,pnl,outcome,signals_fired,hold_days,reasoning,created_at", "created_at": f"gte.{cutoff}", "order": "created_at.desc", "limit": "30"}),
                client.get(f"{SUPABASE_URL}/rest/v1/inference_chains", headers=sb_headers(), params={"select": "id,ticker,chain_date,max_depth_reached,final_confidence,final_decision,stopping_reason,reasoning_summary,created_at", "chain_date": f"gte.{cutoff[:10]}", "order": "created_at.desc", "limit": "50"}),
            )
            return json.dumps({
                "trades": trades_r.json() if trades_r.status_code == 200 else [],
                "chains": chains_r.json() if chains_r.status_code == 200 else [],
            })

        return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ============================================================================
# Chat endpoint — streaming SSE
# ============================================================================


@router.post("/api/chat")
async def chat_endpoint(request: Request, oc_session: str | None = Cookie(None)):
    """Streaming AI chat with full trading context via tool use."""
    _require_auth(request, oc_session)
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="Claude API key not configured")

    body = await request.json()
    messages = body.get("messages", [])
    current_step = body.get("current_step")
    current_step_index = body.get("current_step_index", 0)
    if not messages:
        raise HTTPException(status_code=400, detail="No messages provided")

    messages = messages[-20:]
    system_prompt = CHAT_SYSTEM_PROMPT

    if current_step and current_step_index is not None:
        step_num = current_step_index + 1
        deep_context = WORKFLOW_CONTEXT.get(step_num, {})
        if deep_context:
            step_title = current_step.get("title", "")
            system_prompt = CHAT_SYSTEM_PROMPT + f"""

## CURRENT WORKFLOW STEP — The user is viewing Step {step_num}: {step_title}

You have deep context about this step. When the user asks questions, prioritize this context:

- **Description**: {deep_context.get('description', '')}
- **Data In**: {deep_context.get('data_in', '')}
- **Data Out**: {deep_context.get('data_out', '')}
- **DB Table**: {deep_context.get('db_table', '')}
- **Cost**: {deep_context.get('cost', '')}
- **Parameters**: {deep_context.get('parameters', '')}
- **Known Limitations**: {deep_context.get('limitations', '')}
- **Potential Improvements**: {deep_context.get('improvements', '')}
- **Connections**: {deep_context.get('connections', '')}

If the user asks about this step, answer with specificity — reference parameters, limitations, and connections to adjacent steps. If they ask about improvements, be honest about what could be better and suggest concrete changes. If they ask a general question unrelated to this step, use your full system knowledge."""

    claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    async def generate():
        conv = list(messages)
        max_tool_rounds = 5

        for _round in range(max_tool_rounds + 1):
            try:
                collected_text = ""
                tool_use_blocks: list = []

                async with claude.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=system_prompt,
                    messages=conv,
                    tools=CHAT_TOOLS,
                ) as stream:
                    async for event in stream:
                        if hasattr(event, "type"):
                            if event.type == "content_block_delta" and hasattr(event, "delta"):
                                if getattr(event.delta, "type", "") == "text_delta":
                                    chunk = event.delta.text
                                    collected_text += chunk
                                    yield f"data: {json.dumps({'type': 'text', 'text': chunk})}\n\n"

                    final_message = await stream.get_final_message()

                if final_message.stop_reason == "tool_use":
                    for block in final_message.content:
                        if block.type == "tool_use":
                            tool_use_blocks.append(block)

                    for tb in tool_use_blocks:
                        yield f"data: {json.dumps({'type': 'tool_call', 'name': tb.name, 'input': tb.input})}\n\n"

                    tool_results = []
                    for tb in tool_use_blocks:
                        result = await _chat_tool_dispatch(tb.name, tb.input)
                        tool_results.append({"type": "tool_result", "tool_use_id": tb.id, "content": result})

                    def _clean_block(b):
                        d = b.model_dump()
                        d.pop("parsed_output", None)
                        return d

                    conv.append({"role": "assistant", "content": [_clean_block(b) for b in final_message.content]})
                    conv.append({"role": "user", "content": tool_results})
                    continue
                else:
                    break

            except anthropic.APIError as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
                break

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ============================================================================
# Trade reasoning — AI post-mortem on a specific trade
# ============================================================================

_reasoning_rate_tracker: dict[str, list[float]] = {}
_REASONING_MAX_PER_HOUR = 10
_REASONING_WINDOW = 3600


def _check_reasoning_rate_limit() -> bool:
    now = time.time()
    window_start = now - _REASONING_WINDOW
    calls = _reasoning_rate_tracker.get("__global__", [])
    calls = [t for t in calls if t > window_start]
    _reasoning_rate_tracker["__global__"] = calls
    return len(calls) >= _REASONING_MAX_PER_HOUR


def _record_reasoning_call() -> None:
    calls = _reasoning_rate_tracker.get("__global__", [])
    calls.append(time.time())
    _reasoning_rate_tracker["__global__"] = calls


@router.post("/api/trades/{trade_id}/reasoning")
async def get_trade_reasoning(
    trade_id: str,
    request: Request,
    oc_session: str | None = Cookie(None),
):
    _require_auth(request, oc_session)
    trade_id = _validate_uuid(trade_id)
    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="No Supabase connection")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="Claude API key not configured")

    client = get_http()
    trade_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_decisions", headers=sb_headers(), params={"id": f"eq.{trade_id}"}
    )
    if trade_resp.status_code != 200 or not trade_resp.json():
        return JSONResponse({"error": "Trade not found"}, status_code=404)
    trade = trade_resp.json()[0]

    metadata = trade.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, ValueError):
            metadata = {}
    if "ai_reasoning" in metadata:
        return {"reasoning": metadata["ai_reasoning"], "cached": True}

    if _check_reasoning_rate_limit():
        return JSONResponse({"error": "Reasoning rate limit exceeded (10/hour). Try again later."}, status_code=429)

    ticker = trade.get("ticker", "")
    inference_chain_id = trade.get("inference_chain_id")
    entry_order_id = trade.get("entry_order_id")
    stop_order_id = trade.get("stop_order_id")

    created_at_str = trade.get("created_at") or datetime.now(timezone.utc).isoformat()
    try:
        trade_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        trade_dt = datetime.now(timezone.utc)
    signal_start = (trade_dt - timedelta(days=1)).isoformat()
    catalyst_start = (trade_dt - timedelta(hours=48)).isoformat()

    async def _fetch_chain():
        if not inference_chain_id:
            return None
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/inference_chains",
                headers=sb_headers(),
                params={"id": f"eq.{inference_chain_id}"},
            )
            rows = r.json() if r.status_code == 200 else []
            return rows[0] if rows else None
        except Exception:
            return None

    async def _fetch_signals():
        if not ticker:
            return []
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/signal_evaluations",
                headers=sb_headers(),
                params={"select": "ticker,scan_date,trend,momentum,volume,fundamental,sentiment,flow,total_score,decision,reasoning", "ticker": f"eq.{ticker}", "created_at": f"gte.{signal_start}", "order": "created_at.desc", "limit": "3"},
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _fetch_catalysts():
        if not ticker:
            return []
        try:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/catalyst_events",
                headers=sb_headers(),
                params={"select": "catalyst_type,headline,magnitude,direction,sentiment_score,event_time", "ticker": f"eq.{ticker}", "event_time": f"gte.{catalyst_start}", "order": "event_time.desc", "limit": "10"},
            )
            return r.json() if r.status_code == 200 else []
        except Exception:
            return []

    async def _fetch_orders():
        order_ids = [oid for oid in [entry_order_id, stop_order_id] if oid]
        if not order_ids:
            return []
        try:
            results = []
            for oid in order_ids:
                r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/order_events",
                    headers=sb_headers(),
                    params={"order_id": f"eq.{oid}", "limit": "5"},
                )
                if r.status_code == 200:
                    results.extend(r.json())
            return results
        except Exception:
            return []

    chain, signals, catalysts, _ = await asyncio.gather(
        _fetch_chain(), _fetch_signals(), _fetch_catalysts(), _fetch_orders()
    )

    action = trade.get("action", "UNKNOWN")
    qty = trade.get("qty") or trade.get("quantity") or "?"
    entry_price = trade.get("entry_price") or "?"
    pnl = trade.get("pnl")
    outcome = trade.get("outcome") or "UNKNOWN"
    confidence = trade.get("confidence") or "?"
    decision = trade.get("decision") or trade.get("reasoning") or "?"
    profile_name = trade.get("profile_name") or trade.get("tuning_profile_id") or "?"

    chain_text = "No inference chain available."
    if chain:
        tumblers = chain.get("tumblers") or []
        if isinstance(tumblers, list) and tumblers:
            tumbler_lines = []
            for i, t in enumerate(tumblers, 1):
                if isinstance(t, dict):
                    name = t.get("name") or t.get("tumbler") or f"Tumbler {i}"
                    conf = t.get("confidence") or t.get("score") or "?"
                    summary = t.get("summary") or t.get("reasoning") or t.get("result") or ""
                    tumbler_lines.append(f"  [{i}] {name}: confidence={conf}  {summary}")
                else:
                    tumbler_lines.append(f"  [{i}] {t}")
            stopping = chain.get("stopping_reason") or "completed"
            max_depth = chain.get("max_depth_reached") or len(tumblers)
            chain_text = "\n".join(tumbler_lines) + f"\n  Stopping reason: {stopping}\n  Max depth reached: {max_depth}"
        elif chain.get("reasoning_summary"):
            chain_text = chain["reasoning_summary"]

    signal_text = "No signal data available."
    if signals:
        sig = signals[0]
        signal_text = (
            f"Trend: {sig.get('trend', '?')}, Momentum: {sig.get('momentum', '?')}, Volume: {sig.get('volume', '?')}\n"
            f"Fundamental: {sig.get('fundamental', '?')}, Sentiment: {sig.get('sentiment', '?')}, Flow: {sig.get('flow', '?')}\n"
            f"Total: {sig.get('total_score', '?')}/6"
        )

    catalyst_text = "No catalysts recorded in the 48h window."
    if catalysts:
        catalyst_text = "\n".join(
            f"  - [{c.get('catalyst_type', 'unknown')}] {c.get('headline', '')} | {c.get('direction', '?')} | magnitude={c.get('magnitude', '?')}"
            for c in catalysts[:8]
        )

    pnl_str = f"${pnl}" if pnl is not None else "open/unknown"

    prompt = f"""You are analyzing a trade made by OpenClaw, an autonomous swing trading system.

TRADE DETAILS:
- Ticker: {ticker}
- Action: {action} {qty} shares
- Entry Price: ${entry_price} on {created_at_str[:10]}
- P&L: {pnl_str} ({outcome})
- Confidence: {confidence}
- Decision: {decision}
- Profile: {profile_name}

INFERENCE CHAIN (tumbler-by-tumbler reasoning):
{chain_text}

SIGNAL SCORES:
{signal_text}

CATALYSTS (48h before entry):
{catalyst_text}

Explain in plain language:
1. What was the primary thesis for this trade?
2. Which catalysts and signals were most influential?
3. How did each tumbler contribute to the final decision?
4. Was the reasoning sound given the available data?
5. If the trade lost money, what went wrong? If profitable, was it for the right reasons?"""

    _record_reasoning_call()
    try:
        claude_sync = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = claude_sync.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        reasoning_text = message.content[0].text if message.content else "No reasoning generated."
    except anthropic.APIError as e:
        return JSONResponse({"error": f"Claude API error: {e}"}, status_code=502)
    except Exception:
        return JSONResponse({"error": "Failed to generate reasoning"}, status_code=500)

    updated_metadata = dict(metadata)
    updated_metadata["ai_reasoning"] = reasoning_text
    try:
        await client.patch(
            f"{SUPABASE_URL}/rest/v1/trade_decisions",
            headers={**sb_headers(), "Content-Type": "application/json"},
            params={"id": f"eq.{trade_id}"},
            json={"metadata": updated_metadata},
        )
    except Exception:
        pass

    return {"reasoning": reasoning_text, "cached": False}
