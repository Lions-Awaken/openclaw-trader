# OpenClaw Trader

## Overview

Infrastructure, migrations, and ops tooling for the autonomous swing trading agent. The StreamSaber backend app itself lives in the Twilight Underground repo (`streamsaber/`). This repo holds supporting infrastructure, the Tumbler inference engine, and the dashboard.

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
│   ├── calibrator.py         # Weekly calibration + outcome grading + pattern updates
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

## Related Repos

| Repo | What lives there |
|------|-----------------|
| **twilight-underground** | `streamsaber/` backend app, `relay/` stream relay, frontend admin pages |
| **openclaw-trader** (this) | Supabase migrations, log-shipper, infra tooling, tumbler engine, dashboard |

## Fly.io Apps

| App | Purpose | Deployed from | Cost |
|-----|---------|--------------|------|
| `tu-streamsaber` | Stream monitor + API | `twilight-underground/streamsaber/` | ~$7/mo |
| `tu-log-shipper` | Log shipping to Grafana | `openclaw-trader/log-shipper/` | ~$2/mo |

## Supabase Tables (production: `uupmzaglafeiakamefit`)

| Table | Purpose |
|-------|---------|
| `tiktok_accounts` | Monitored TikTok accounts (5 active) |
| `tiktok_stream_summaries` | Per-stream summary stats |
| `tiktok_daily_rollups` | Daily aggregated stats |
| `tiktok_stream_events` | Raw high-value events (30-day retention, pg_cron purge) |
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

## Tumbler Engine Architecture

The inference engine (`scripts/inference_engine.py`) implements a 5-tumbler "Lock & Tumbler" analysis:

```
Tumbler 1: Technical Foundation → min 0.25 confidence
Tumbler 2: Fundamental + Sentiment → min 0.40 | VETO if sentiment < -0.5
Tumbler 3: Flow + Cross-Asset (Ollama qwen) → min 0.55 | STOP if delta < 0.03
Tumbler 4: Pattern Template Matching (Claude) → min 0.65
Tumbler 5: Counterfactual Synthesis (Claude) → calibrated final confidence

Decision: strong_enter (>=0.75) | enter (>=0.60) | watch (>=0.45) | skip (>=0.20) | veto (<0.20)
```

Stopping rules: veto_signal, confidence_floor, forced_connection (delta < 0.03), conflicting_signals, insufficient_data, resource_limit, time_limit (30s).

## Cron Schedule

| Script | Schedule (ET) | LLM | RAM Peak |
|--------|--------------|-----|----------|
| catalyst_ingest.py | M-F 8:30 AM, 12:15 PM, 3:50 PM | Ollama embed | ~3.2GB |
| inference_engine.py | Called by scanner | qwen + Claude | ~3.5GB |
| meta_daily.py | M-F 4:30 PM | Claude + embed | ~3.5GB |
| meta_weekly.py | Sun 7:00 PM | Claude + embed | ~3.5GB |
| calibrator.py | Sun 7:30 PM | None | ~2.6GB |

## Dashboard Tabs

Dashboard | Pipeline | Trade Log | Positions | Predictions | Meta-Learning | Catalysts | System | Economics | How It Works

## Observability

- **Sentry**: Error tracking on StreamSaber backend (auto-captures ERROR+ logs)
- **Grafana Cloud Loki**: All Fly.io app logs shipped via `tu-log-shipper`
- **Dashboard**: `lionsawaken.grafana.net` — query with `{app="tu-streamsaber"}`

## Hard Rules — Violations Are Bugs

- NEVER commit secrets (service role keys, API tokens, Fly tokens) — secret scanner blocks these
- NEVER push directly to main — use PR workflow
- NEVER modify production Supabase without testing first

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

# Check StreamSaber health
curl https://tu-streamsaber.fly.dev/health

# Check logs in Fly
fly logs -a tu-streamsaber

# Check logs in Grafana
# https://lionsawaken.grafana.net → Explore → Loki → {app="tu-streamsaber"}
```
