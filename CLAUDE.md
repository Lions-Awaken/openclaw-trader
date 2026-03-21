# OpenClaw Trader

## Overview

Infrastructure, migrations, and ops tooling for the TikTok live stream monitoring pipeline. The StreamSaber backend app itself lives in the Twilight Underground repo (`streamsaber/`). This repo holds supporting infrastructure.

## Project Structure

```
openclaw-trader/
├── log-shipper/              # Fly.io app — log shipping to Grafana Cloud
│   └── fly.toml
├── supabase/
│   └── migrations/           # SQL migrations for TikTok tables
├── scripts/
│   └── scan-secrets.sh       # Secret scanner for pre-commit
├── .githooks/                # Git guardrails (pre-commit, pre-push)
├── ruff.toml                 # Python linter config
└── CLAUDE.md
```

## Related Repos

| Repo | What lives there |
|------|-----------------|
| **twilight-underground** | `streamsaber/` backend app, `relay/` stream relay, frontend admin pages |
| **openclaw-trader** (this) | Supabase migrations, log-shipper, infra tooling |

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
