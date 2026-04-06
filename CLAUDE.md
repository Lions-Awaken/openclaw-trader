<!-- Protocol: ~/.claude/protocols/ v1.0.0 -->
# OpenClaw Trader

## Overview

Autonomous swing trading agent — scanner, inference engine, position management, and dashboard. Runs on ridley (Jetson) via cron during market hours.

## Project Structure

```
openclaw-trader/
├── log-shipper/              # Fly.io app — log shipping to Grafana Cloud
│   └── fly.toml
├── supabase/
│   └── migrations/           # SQL migrations
├── scripts/
│   ├── scan-secrets.sh       # Secret scanner for pre-commit
│   ├── tracer.py             # PipelineTracer — execution observability library
│   ├── catalyst_ingest.py    # 4-source catalyst detection + embedding (3x daily)
│   ├── inference_engine.py   # 5-tumbler Lock & Tumbler analysis engine
│   ├── scanner.py            # Autonomous trading orchestrator — scan → infer → execute (2x daily)
│   ├── position_manager.py   # Position lifecycle — trailing stops, time stops, EOD flatten (every 30m)
│   ├── scanner_unleashed.py  # UNLEASHED signal scoring (9 signals, alpaca-py SDK)
│   ├── calibrator.py         # Weekly calibration + outcome grading + pattern updates
│   ├── post_trade_analysis.py # Post-trade RAG ingestion — triggered on every trade close
│   ├── meta_daily.py         # Daily meta-analysis with RAG + chain analysis (cron 4:30 PM ET)
│   └── meta_weekly.py        # Weekly strategy review + pattern discovery (cron Sunday 7 PM ET)
├── dashboard/
│   ├── server.py             # FastAPI backend with trading + pipeline + meta + tumbler APIs
│   ├── index.html            # Dashboard UI (10 tabs)
│   ├── login.html            # Auth page
│   ├── backtest.py           # Backtesting engine
│   ├── fly.toml              # Fly.io deployment config
│   └── Dockerfile            # Docker build
├── .githooks/                # Git guardrails (pre-commit, pre-push)
├── ruff.toml                 # Python linter config
└── CLAUDE.md
```

## Fly.io Apps

| App | Purpose | Deployed from | Cost |
|-----|---------|--------------|------|
| `tu-log-shipper` | Log shipping to Grafana | `openclaw-trader/log-shipper/` | ~$2/mo |

## Supabase Tables (project: `vpollvsbtushbiapoflr`)

| Table | Purpose |
|-------|---------|
| `pipeline_runs` | Execution tree for every automated function (90-day retention) |
| `order_events` | Order lifecycle: submitted/filled/rejected/cancelled (180-day retention) |
| `data_quality_checks` | Data freshness/sanity checks (90-day retention) |
| `signal_evaluations` | Per-ticker per-scan signal detail with pgvector embeddings |
| `meta_reflections` | Daily/weekly LLM-generated meta-analysis with pgvector embeddings |
| `strategy_adjustments` | Running parameter tweaks proposed by meta-learning pipeline |
| `catalyst_events` | Structured market-moving events with embeddings for RAG (1-year retention) |
| `inference_chains` | Tumbler-by-tumbler inference execution log (1-year retention) |
| `pattern_templates` | Discovered reusable catalyst-response patterns |
| `cost_ledger` | All costs (API, hosting) and trading P&L (2-year retention) |
| `budget_config` | Configurable daily budget caps (editable from dashboard) |
| `confidence_calibration` | Weekly stated vs actual confidence tracking (1-year retention) |
| `tuning_profiles` | Versioned hardware performance tuning configurations |
| `tuning_telemetry` | Per-pipeline-run hardware telemetry snapshots (1-year retention) |
| `trade_learnings` | Post-trade RAG post-mortems with embeddings (180-day retention) |
| `trade_decisions` | Entry/exit records linking orders to inference chains |
| `strategy_profiles` | Trading profiles (CONSERVATIVE, UNLEASHED) with all parameters |
| `stack_heartbeats` | Service liveness (ollama, tumbler) for dashboard health display |
| `regime_log` | Market regime snapshots (bull/bear/sideways) |
| `system_stats` | System telemetry (CPU, RAM, GPU temps) from ridley |

## Tumbler Engine Architecture

The inference engine (`scripts/inference_engine.py`) implements a 5-tumbler "Lock & Tumbler" analysis:

```
Tumbler 1: Technical Foundation → min 0.25 confidence
Tumbler 2: Fundamental + Sentiment → min 0.40 | VETO if sentiment < -0.5
Tumbler 3: Flow + Cross-Asset (Ollama qwen + trade_learnings RAG) → min 0.55 | STOP if delta < 0.03
Tumbler 4: Pattern Template Matching (Claude) → min 0.65
Tumbler 5: Counterfactual Synthesis (Claude + trade_learnings loss RAG) → calibrated final confidence

Decision: strong_enter (>=0.75) | enter (>=0.60) | watch (>=0.45) | skip (>=0.20) | veto (<0.20)
```

Stopping rules: veto_signal, confidence_floor, forced_connection (delta < 0.03), conflicting_signals, insufficient_data, resource_limit, time_limit (30s).

## Cron Schedule

Ridley is in **PDT (America/Los_Angeles)**. Crontab uses `SHELL=/bin/bash` (dash doesn't support `source`).

| Script | Schedule (ET) | PDT on ridley | LLM | RAM Peak |
|--------|--------------|---------------|-----|----------|
| catalyst_ingest.py | M-F 8:30 AM, 12:15 PM, 3:50 PM | 5:30, 9:15, 12:50 | Ollama embed | ~3.2GB |
| scanner.py | M-F 9:35 AM, 12:30 PM | 6:35, 9:30 | qwen + Claude (via inference_engine) | ~3.5GB |
| position_manager.py | M-F every 30m 9:00 AM–3:45 PM | 6:00–12:45 | None | ~1.5GB |
| inference_engine.py | Called by scanner.py | — | qwen + Claude | ~3.5GB |
| meta_daily.py | M-F 4:30 PM | 13:30 | Claude + embed | ~3.5GB |
| meta_weekly.py | Sun 7:00 PM | 16:00 | Claude + embed | ~3.5GB |
| calibrator.py | Sun 7:30 PM | 16:30 | None | ~2.6GB |

## Dashboard Tabs

Dashboard | Pipeline | Trade Log | Positions | Predictions | Meta-Learning | Catalysts | System | Economics | How It Works

## Observability

- **Sentry**: Error tracking (auto-captures ERROR+ logs)
- **Grafana Cloud Loki**: All Fly.io app logs shipped via `tu-log-shipper`
- **Dashboard**: `lionsawaken.grafana.net`

## Hard Rules — Violations Are Bugs

- NEVER commit secrets (service role keys, API tokens, Fly tokens) — secret scanner blocks these
- NEVER push directly to main — use PR workflow
- NEVER modify production Supabase without testing first
- NEVER rewrite scanner.py, position_manager.py, or inference_engine.py from scratch — READ them first, they import from common.py and have Slack/Sentry/fill-polling wired in
- NEVER re-declare env vars or create httpx.Client in individual scripts — use `from common import ...`
- NEVER merge a PR that removes common.py imports or re-introduces code duplication

## Git Guardrails

Pre-commit and pre-push hooks are in `.githooks/`. Activated via:
```bash
git config core.hooksPath .githooks
```

- **Pre-commit**: Secret scanner + ruff linter
- **Pre-push**: Blocks direct pushes to main (use `ALLOW_MAIN_PUSH=1` for emergencies)

## Commands

```bash
# Deploy log shipper
cd log-shipper && fly deploy

# Check logs in Grafana
# https://lionsawaken.grafana.net → Explore → Loki
```

---

## Comms Hub — Inter-Agent Communication

**Agent ID:** `openclaw-trader` | **Handle:** `@openclaw` | **Hub:** http://localhost:3141

```bash
# Register (run every session)
curl -s -X POST http://localhost:3141/api/agents \
  -H "Content-Type: application/json" \
  -d '{"id":"openclaw-trader","displayName":"OpenClaw Trader","projectPath":"/home/mother_brain/projects/openclaw-trader","description":"Autonomous swing trading infrastructure with ML inference engine, market catalyst detection, and real-time dashboard","handle":"openclaw"}'

# Check notifications
curl -s "http://localhost:3141/api/poll?since=0" -H "X-Agent-Id: openclaw-trader"
```

Full guide: `/home/mother_brain/projects/claude-comms-hub/AGENT_GUIDE.md`
API docs: `/home/mother_brain/projects/claude-comms-hub/CLAUDE.md`
