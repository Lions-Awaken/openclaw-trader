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
│   ├── manifest.py           # Function manifest — canonical registry of all scheduled/triggered functions
│   ├── tracer.py             # PipelineTracer — execution observability library
│   ├── common.py             # Shared imports: Supabase, Alpaca, Slack, embeddings
│   ├── shadow_profiles.py    # Immutable adversarial system prompts (5 profiles)
│   ├── catalyst_ingest.py    # 6-source catalyst detection + embedding (3x daily)
│   ├── inference_engine.py   # 5-tumbler Lock & Tumbler analysis engine
│   ├── scanner.py            # Autonomous trading orchestrator — scan → enrich → infer → shadow → execute (2x daily)
│   ├── position_manager.py   # Position lifecycle — trailing stops, time stops, EOD flatten (every 30m)
│   ├── health_check.py       # 44-check pre-market system health (8 groups, writes system_health table)
│   ├── ingest_signals.py     # Consolidated Form 4 + options flow ingest (form4 @ 6AM, options @ 7AM weekdays)
│   ├── calibrator.py         # Weekly calibration + outcome grading + shadow profile DWM weighting
│   ├── post_trade_analysis.py # Post-trade RAG ingestion — triggered on every trade close
│   └── meta_analysis.py      # Daily + weekly meta-analysis with RAG + chain + shadow divergence context
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

## Function Manifest

**Canonical source:** `scripts/manifest.py`

Every scheduled and event-triggered function in the system is registered in the manifest. Health checks diff the manifest against `pipeline_runs` to detect silent failures — if it's not in the manifest, the system can't tell you it didn't run.

### Convention: Update the Manifest on Every Change

When you add, remove, or modify any of the following, you MUST update `scripts/manifest.py`:
- A new cron-scheduled script
- A new crontab entry on ridley
- A new `PipelineTracer` pipeline_name
- A new expected step_name within an existing pipeline
- A change to an existing schedule or dependency

**Format:** Add a `ManifestEntry` to `MANIFEST` (cron) or `EVENT_TRIGGERED` (on-demand). Fields: name, script, pipeline_name, schedule, schedule_desc, expected_steps, criticality, dependencies.

### Deploy Checklist

After writing scripts that run on ridley via cron:
1. Update `scripts/manifest.py` with new entries
2. Commit and push on mother_brain
3. SSH to ridley: `cd ~/openclaw-trader && git pull`
4. Verify: `crontab -l | grep <script_name>`

Forgetting step 3 means crons fire but scripts don't exist on ridley.

## Cron Schedule

Ridley is in **PDT (America/Los_Angeles)**. Crontab uses `SHELL=/bin/bash` (dash doesn't support `source`).

| Script | PDT on ridley | pipeline_name | LLM | RAM Peak |
|--------|--------------|---------------|-----|----------|
| health_check.py | 5:00 M-F | health_check (writes system_health) | None | ~2GB |
| catalyst_ingest.py | 5:30, 9:00, 12:50 M-F | catalyst_ingest | Ollama embed | ~3.2GB |
| ingest_signals.py form4 | 6:00 M-F | ingest | None | ~1.5GB |
| scanner.py | 6:35, 9:30 M-F | scanner | qwen + Claude | ~3.5GB |
| ingest_signals.py options | 7:00 M-F | ingest | None | ~1.5GB |
| position_manager.py | every 30m 6:00–12:45 M-F | position_manager | None | ~1.5GB |
| meta_analysis.py daily | 13:30 M-F | meta_daily | Claude + embed | ~3.5GB |
| meta_analysis.py weekly | 16:00 Sun | meta_weekly | Claude + embed | ~3.5GB |
| calibrator.py | 16:30 Sun | calibrator | None | ~2.6GB |
| heartbeat.py | every 5m | heartbeat | None | ~0.5GB |

## Dashboard Tabs

Dashboard | Pipeline | Trade Log | Positions | Predictions | Meta-Learning | Catalysts | System | Economics | Health | Signals | How It Works

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
- NEVER add a cron entry or new pipeline script without updating `scripts/manifest.py`
- NEVER write scripts to ridley crons without committing, pushing, and pulling on ridley

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
