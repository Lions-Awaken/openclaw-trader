# Ridley post-flash checklist

> What to do after (re)flashing the Jetson Orin Nano or any SDK-package install that clears state. Runbook target: 15-minute restore, zero surprises.

Last verified: 2026-04-22 after the 2026-04-20 JetPack 6.2.2 reflash + 2026-04-21 SDK-package reinstall.

---

## 1. Timezone (cron killer)

JetPack ships with `/etc/timezone=Etc/UTC`. The generated crontab declares `TZ=America/Los_Angeles` at the top, but `cron` caches its TZ at daemon start and doesn't honor the in-crontab directive. Every cron fires 7 h early until you fix this.

```bash
sudo timedatectl set-timezone America/Los_Angeles
sudo systemctl restart cron
systemctl --user restart openclaw-system-monitor openclaw-dashboard
```

Verify:
- `timedatectl | grep "Time zone"` → `America/Los_Angeles (PDT, -0700)`
- First cron entry in `/var/log/syslog` fires at the PDT wallclock, not the UTC wallclock

---

## 2. Env file (`~/.openclaw-env`)

All cron entries source this file. It must contain **16 keys** and each must be `export`-qualified so child Python processes inherit them. A bare `VAR=value` in a dot-sourced file sets shell-local only — Python sees empty strings and every Supabase call errors out with `Request URL is missing an 'http://' or 'https://' protocol`.

### Required keys
```
export SUPABASE_URL=https://vpollvsbtushbiapoflr.supabase.co
export SUPABASE_SERVICE_KEY=<vault:.supabase.instances.openclaw-trader.service_role_key>
export ANTHROPIC_API_KEY=<vault:.anthropic.api_key>
export ALPACA_API_KEY=<vault:.alpaca.api_key>
export ALPACA_SECRET_KEY=<vault:.alpaca.secret_key>
export PERPLEXITY_API_KEY=<vault:.market_data.perplexity.api_key>
export FINNHUB_API_KEY=<vault:.market_data.finnhub.api_key>
export FRED_API_KEY=<vault:.market_data.fred.api_key>
export SENTRY_DSN=<vault:.sentry.dsns.openclaw>
export SLACK_BOT_TOKEN=<vault:.slack.bot_token>
export SLACK_CHANNEL=<vault:.slack.channel_id>
export OLLAMA_URL=http://localhost:11434
export LOKI_URL=<vault:.grafana_loki.url>
export LOKI_USER=<vault:.grafana_loki.username>
export LOKI_API_KEY=<vault:.grafana_loki.token>
export DASHBOARD_KEY=<rotated per session>
export FLY_TO_RIDLEY_TOKEN=<must match Fly secret on openclaw-trader-dash>
export SESSION_SIGNING_SALT=<rotated per session>
```

### Subshell-only assembly pattern

**Never** `cat`, `grep`, `head`, `tail`, `cat -A`, `od`, `xxd`, or `hexdump` the env file. Those utilities stream secret values to stdout and into session transcripts. See `protocols/agent-conventions.md#secrets-hygiene-no-debug-utilities`.

Safe shape:
```bash
(
  umask 077
  ~/.config/claudefleet/vault-show | python3 -c '
import sys, json
v = json.load(sys.stdin)
lines = [
    f"export SUPABASE_URL={v[\"supabase\"][\"instances\"][\"openclaw-trader\"][\"url\"]}",
    f"export SUPABASE_SERVICE_KEY={v[\"supabase\"][\"instances\"][\"openclaw-trader\"][\"service_role_key\"]}",
    # ... etc per above
]
print("\n".join(lines))
' > /dev/shm/env.$$
  scp /dev/shm/env.$$ ridley:/tmp/env.new
  shred -u /dev/shm/env.$$
)
ssh ridley 'mv /tmp/env.new ~/.openclaw-env && chmod 600 ~/.openclaw-env'
```

### Verification
```bash
ssh ridley 'wc -l ~/.openclaw-env && ls -la ~/.openclaw-env'
# expect 18 lines, -rw-------, owner ridley:ridley

ssh ridley 'bash -c ". ~/.openclaw-env && python3 -c \"import os; assert os.environ.get(\\\"SUPABASE_URL\\\",\\\"\\\").startswith(\\\"https://\\\"); print(\\\"ok\\\")\""'
# expect: ok
```

---

## 3. Repo clone + git hooks

```bash
ssh ridley 'git clone https://github.com/Lions-Awaken/openclaw-trader.git ~/openclaw-trader && \
            cd ~/openclaw-trader && git config core.hooksPath .githooks && \
            mkdir -p logs'
```

---

## 4. Python dependencies

```bash
ssh ridley 'cd ~/openclaw-trader && sudo apt-get install -y python3-pip python3-venv python3-dev build-essential && pip3 install -r requirements.txt'
```

Target Python version: 3.10 (matches what the SDK ships). If the SDK brings 3.12 or 3.14 on a future flash, verify the pinned requirements still resolve and test `import httpx, sentry_sdk, yfinance, alpaca, finnhub, pandas, fastapi, anthropic` before trusting crons.

---

## 5. Ollama + models

Pull the production models and confirm GPU offload:

```bash
ssh ridley 'ollama pull qwen2.5:3b && ollama pull nomic-embed-text'
ssh ridley 'journalctl -u ollama --since "5 minutes ago" | grep "offloaded .* layers to GPU"'
# expect: offloaded 29/29 layers to GPU
```

If GPU layers aren't offloading, check `nvpmodel -q` → `MAXN_SUPER` and inspect tegrastats. The Apr-2026 JetPack 6.2.2 was the release that finally got this reliably working.

---

## 6. Collectors module + systemd units

`scripts/collectors.py` ships in the repo as of commit `cd4e5ba` (PR #55). If it's absent, `system_monitor.py` can't import and the daemon silently fails.

Two systemd **user units** (not system units) own the long-running processes:
- `/home/ridley/.config/systemd/user/openclaw-dashboard.service` — FastAPI dashboard on :9090
- `/home/ridley/.config/systemd/user/openclaw-system-monitor.service` — stats streamer writing `system_stats` every 5 s

```bash
ssh ridley 'systemctl --user enable --now openclaw-dashboard openclaw-system-monitor'
ssh ridley 'systemctl --user is-active openclaw-dashboard openclaw-system-monitor'
# expect: active / active
```

Do NOT try to install these as system units (`/etc/systemd/system/`) or as `@reboot` cron entries. User units are the supported path post-reflash.

---

## 7. Crontab

Install from the canonical source:

```bash
ssh ridley 'cd ~/openclaw-trader && python3 scripts/manifest.py --install-crontab'
```

(Or regenerate via whatever helper you ship.) The resulting crontab should have 30 entries (17 MANIFEST + 4 ollama_watchdog + anacron stuff). **Do not hand-edit.** If you add a stale note to the crontab, future-you will spend 30 min figuring out why a note contradicts reality. Drop a TODO in `manifest.py` instead.

---

## 8. Post-fix verification queries

### pipeline_runs freshness
```sql
SELECT pipeline_name, MAX(started_at) AS latest, COUNT(*) AS n
FROM pipeline_runs
WHERE started_at > now() - interval '2 hours'
GROUP BY pipeline_name ORDER BY latest DESC;
```
Every pipeline that should have fired in the last window appears here. Expect empty rows for pipelines whose cron hasn't fired yet.

### system_stats cadence
```sql
SELECT MAX(collected_at) AS latest,
       now() - MAX(collected_at) AS gap
FROM system_stats;
```
Gap should be ≤10 s. Anything larger means `openclaw-system-monitor` has stalled.

### Loki shipping
```logql
{app="openclaw-trader"} | json
```
Should return fresh entries with top-level `timestamp`, `level`, `project="openclaw-trader"`, `message`, and nested `metadata.*`.

---

## 9. Known gotchas

- **`fly` CLI is not part of the base system-install** on mother_brain. Install via `curl -fsSL https://fly.io/install.sh | sh` then add `~/.fly/bin` to `PATH`. Auth once with `fly auth login`.
- **Supabase CLI is not part of the base** either. `curl -sfL https://github.com/supabase/cli/releases/latest/download/supabase_linux_amd64.tar.gz | tar -xz -C ~/.local/bin supabase && chmod +x ~/.local/bin/supabase`.
- **Dashboard lives on ridley:9090**, not Fly. Verify via `ssh ridley 'tail -n 20 openclaw-trader/logs/dashboard.log'`. Fly hosts `openclaw-trader-dash` only as an edge/proxy — the app itself runs on ridley behind Tailscale Funnel.
- **Do not `cat` the env file.** Seriously. If you want to check line shape, use `awk -F= '{print $1}'` or `cut -d= -f1` — keeps values out of stdout.
- **Scanner prints local time**, not UTC. If the timezone is wrong, scanner's banner `[scanner] Starting scan — <time>` will mislead you into thinking the wallclock is right. Trust `/etc/timezone`, not the banner.

---

## 10. When things go sideways

Start with `scripts/system_check.py --mode health` — 59-point coverage. `FAIL > 2` = something structural is broken; walk the output top to bottom. The script is faster than any manual probe you'll do in panic mode.

Escalate to `scripts/system_check.py --mode preflight` for NASA-style go/no-go with synthetic data. Target: `72/72 GO`. If you can't hit 72/72, do not ship to prod.

Slack + Ten Forward disclosure is mandatory for any secret exposure. See `protocols/agent-conventions.md#secrets-hygiene` for the full rule set.
