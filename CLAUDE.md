# OpenClaw Trader

## Overview

TikTok live stream monitoring and analytics pipeline. Monitors TikTok accounts for live streams, captures events (comments, gifts, follows), generates summaries, and syncs to Supabase. Admin UI in Twilight Underground.

## Tech Stack

- **Backend**: Python 3.12 + FastAPI + TikTokLive
- **Database**: Supabase (Postgres) — production instance `uupmzaglafeiakamefit`
- **Hosting**: Fly.io (`tu-streamsaber`)
- **Observability**: Sentry (errors) + Grafana Cloud Loki (logs)
- **Frontend**: Twilight Underground admin pages (separate repo)

## Project Structure

```
openclaw-trader/
├── streamsaber/              # Fly.io app — TikTok stream monitor
│   ├── fly.toml              # Fly.io deployment config
│   ├── Dockerfile
│   ├── requirements.txt
│   └── src/
│       ├── web_dashboard.py  # FastAPI API server
│       ├── stream_monitor.py # Multi-stream monitor daemon
│       └── supabase_client.py # Supabase REST API client
├── log-shipper/              # Fly.io app — log shipping to Grafana
│   └── fly.toml
├── supabase/
│   └── migrations/           # SQL migrations
├── scripts/
│   └── scan-secrets.sh       # Secret scanner for pre-commit
└── ruff.toml                 # Python linter config
```

## Commands

```bash
# Deploy StreamSaber
cd streamsaber && fly deploy

# Check logs
fly logs -a tu-streamsaber

# Check health
curl https://tu-streamsaber.fly.dev/health

# Lint Python
ruff check streamsaber/

# Run locally (needs env vars)
cd streamsaber && uvicorn src.web_dashboard:app --host 0.0.0.0 --port 8080
```

## Hard Rules — Violations Are Bugs

- NEVER commit secrets (service role keys, API tokens, Fly tokens) — secret scanner blocks these
- NEVER push directly to main — use PR workflow
- NEVER modify production Supabase without testing first
- ALWAYS run `ruff check` before committing Python changes
- ALWAYS deploy via `fly deploy` from the `streamsaber/` directory

## Environment Variables (Fly.io Secrets)

```
SUPABASE_URL              # Production Supabase URL
SUPABASE_SERVICE_ROLE_KEY # Service role key (full access)
STREAMSABER_API_KEY       # API key for X-StreamSaber-Key header
SENTRY_DSN                # Sentry error tracking
```

## Supabase Tables

| Table | Purpose |
|-------|---------|
| `tiktok_accounts` | Monitored TikTok accounts (5 active) |
| `tiktok_stream_summaries` | Per-stream summary stats |
| `tiktok_daily_rollups` | Daily aggregated stats |
| `tiktok_stream_events` | Raw high-value events (30-day retention, pg_cron purge) |

## Fly.io Apps

| App | Purpose | Cost |
|-----|---------|------|
| `tu-streamsaber` | Stream monitor + API | ~$7/mo |
| `tu-log-shipper` | Log shipping to Grafana | ~$2/mo |

## Git Guardrails

Pre-commit and pre-push hooks are in `.githooks/`. Activated via:
```bash
git config core.hooksPath .githooks
```

- **Pre-commit**: Secret scanner + ruff linter
- **Pre-push**: Blocks direct pushes to main (use `ALLOW_MAIN_PUSH=1` for emergencies)

## API Endpoints

All routes require `X-StreamSaber-Key` header except `/health`.

| Route | Method | Purpose |
|-------|--------|---------|
| `/health` | GET | Health check (no auth) |
| `/api/status` | GET | Monitor status |
| `/api/accounts` | GET/POST | List/add accounts |
| `/api/accounts/{id}` | PUT/DELETE | Update/remove account |
| `/api/control/scan` | POST | Force scan |
| `/api/control/stop/{username}` | POST | Stop capture |
| `/api/captures` | GET | List captures from Supabase |
| `/api/captures/{id}/{stream_id}` | GET | Capture detail |
| `/api/captures/{id}/{stream_id}/events` | GET | Capture events |
| `/api/analytics/summary` | GET | Aggregated analytics |
| `/api/leaderboard` | GET | Cross-account leaderboard |
| `/api/vip/dashboard` | GET | VIP tier dashboard |
