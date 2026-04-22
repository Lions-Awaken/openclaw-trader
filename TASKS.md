# Task Board

> Managed by the Orchestrator. All agents read and write here.
> Status labels: [READY] . [BLOCKED: TASK-XX] . [IN PROGRESS] . [DONE] . [FAILED]

---

## Ridley Cleanup — Post-SDK-Flash Recovery (2026-04-21)

**Source:** After the Apr-20 JetPack 6.2.2 reflash we pushed additional SDK-package installs on
ridley and rebooted (current `uptime` = 1d 2h). Cleanup mode — hardware is fine, Ollama runs on
GPU (4 GB VRAM, 29/29 layers, `MAXN_SUPER`). **Do not touch Ollama.** But state around Ollama
did not survive, and today (Tue 2026-04-21 PDT) produced zero usable trading output.

**Observed failure modes (2026-04-21 22:30 PDT):**
1. `~/.openclaw-env` on ridley is truncated to 1094 bytes with **no** `ALPACA_KEY`, `LOKI_URL`,
   or `SUPABASE_URL`. Every cron fails on its first `sb_get` with `Request URL is missing an
   'http://' or 'https://' protocol`.
2. `/etc/timezone = Etc/UTC`. The crontab declares `TZ=America/Los_Angeles` but cron is not
   honoring it → every schedule fires **7 h early**. Today's `scanner` ran at 23:35 PDT Mon;
   `shadow_mark_to_market` ran at 11:00 PDT during market hours instead of 18:00 PDT.
3. Tracer root-row write fails 3/3 retries → `pipeline_runs` has zero inserts for the PDT-today
   window (latest row is 2026-04-21 04:56 UTC = 21:56 PDT Mon).
4. Loki `{app="openclaw-trader"}` stats for the full PDT day: **0 streams, 0 entries, 0 bytes.**
   The Apr-19 TASK-424 regression has returned.
5. `system_monitor.service` is not installed (`Unit system_monitor.service could not be found`).
   The daemon is running from somewhere — `system_monitor.log` is growing — but the last
   `system_stats` insert is Apr 21 10:09 UTC (~16 h heartbeat gap). The crontab still carries
   the `# NOTE: system_monitor.py @reboot disabled — 'collectors' module missing from repo`
   marker from the original reflash.
6. Today's health check (run at 05:00 UTC = 22:00 PDT Mon): `PASS 26  FAIL 15  WARN 8  SKIP 10`.
   Representative FAIL: `[1204] Shadow divergences flowing — 0 shadow_divergences in 48h`.
7. Today's trading: `order_events = 0`, `trade_decisions = 0`, `inference_chains = 1` (manual).

**Mission goal:** End state — ridley crontab firing on PDT wallclock, all required env vars
sourced, tracer writing root rows, Loki shipping events, `system_monitor` systemd-managed and
heartbeating into Supabase every 5 s, preflight back to `72/72`. One subsequent cron cycle
produces fresh rows in `pipeline_runs`, `data_quality_checks`, `system_stats`, and Loki.

**Non-goals:** No architecture changes. No schema changes. No crontab regeneration (manifest.py
entries are correct — only the TZ interpretation is broken). No Ollama touching. No Fly.io
dashboard redeploy (it's on ridley:9090 and healthy).

---

### Wave 1 — Stop-the-bleeding foundation (serial)

### TASK-CLEANUP-01 . GEORDI . [DONE]
**Goal:** Set ridley's system timezone to `America/Los_Angeles` so cron honors the `TZ=`
directive at the top of the crontab.
**Acceptance:**
- `ssh ridley 'timedatectl | grep -i "time zone"'` shows `America/Los_Angeles (PDT, -0700)`
- `ssh ridley 'date'` prints PDT wallclock
- `sudo systemctl restart cron` on ridley (cron caches TZ at daemon start — mandatory)
- First post-restart entry in `/var/log/syslog` for one of the openclaw crons fires at the
  PDT wallclock matching the manifest.py schedule (e.g., `position_manager` on the next `:00`
  or `:30` PDT boundary, not 7 h earlier)
**Commands:** `sudo timedatectl set-timezone America/Los_Angeles && sudo systemctl restart cron`
**Rollback:** `sudo timedatectl set-timezone Etc/UTC && sudo systemctl restart cron`
**Output artifact:** `timedatectl` output + first PDT-aligned cron syslog line.
**Depends on:** nothing

### TASK-CLEANUP-02 . GEORDI . [DONE]
**Goal:** Rebuild `~/.openclaw-env` on ridley with every env var `scripts/common.py` reads,
pulling values from the claudefleet vault. **Subshell-only — no secret value is ever printed
to stdout or surfaces in Claude's transcript.**
**Scope adjusted after /engage re-probe (2026-04-22 06:03 UTC):** Env file already has 15 keys
(ALPACA_API_KEY, ALPACA_SECRET_KEY, ANTHROPIC_API_KEY, DASHBOARD_KEY, FINNHUB_API_KEY,
FLY_TO_RIDLEY_TOKEN, FRED_API_KEY, OLLAMA_URL, PERPLEXITY_API_KEY, SENTRY_DSN,
SESSION_SIGNING_SALT, SLACK_BOT_TOKEN, SLACK_CHANNEL, SUPABASE_SERVICE_KEY, SUPABASE_URL).
**Actual gap: LOKI_URL, LOKI_USER, LOKI_API_KEY** (Loki triplet missing → no events shipped).
Optional: ANTHROPIC_API_KEY_2 (backup key; if absent from vault, skip — primary works).
**Required vars (13 from `common.py` + 3 Loki):**
`SUPABASE_URL, SUPABASE_SERVICE_KEY, ANTHROPIC_API_KEY, ANTHROPIC_API_KEY_2, ALPACA_API_KEY,
ALPACA_SECRET_KEY, PERPLEXITY_API_KEY, FINNHUB_API_KEY, FRED_API_KEY, SENTRY_DSN,
SLACK_BOT_TOKEN, SLACK_CHANNEL, OLLAMA_URL, LOKI_URL, LOKI_USER, LOKI_API_KEY`
**Vault paths** (per `~/.claude/CLAUDE.md`):
- `supabase.instances.openclaw-trader.{url,service_role_key}` → SUPABASE_*
- `anthropic.api_key` → ANTHROPIC_API_KEY (ANTHROPIC_API_KEY_2 — if not in vault, flag to WORF)
- `alpaca.{api_key,secret_key}` → ALPACA_*
- `market_data.{finnhub,perplexity,fred}.api_key` → *_API_KEY (FRED_API_KEY may be absent; flag)
- `sentry.dsns.openclaw` → SENTRY_DSN
- `slack.{bot_token,channel_id}` → SLACK_*
- `grafana_loki.{url,username,token}` → LOKI_*
- `OLLAMA_URL` = literal `http://localhost:11434` (not a secret; hard-code)
**Mechanism:** On mother_brain, build env file inside a subshell using `vault-show` piped
through `python3 -c` that writes to a temp file in `/dev/shm` with `chmod 600`, `scp` to
ridley, `shred` the source. Never `echo` or `cat` a secret to the terminal.
**Acceptance:**
- `ssh ridley 'ls -l ~/.openclaw-env'` shows `-rw------- … 16+ lines`
- `ssh ridley 'bash -c ". ~/.openclaw-env && env | grep -cE \"^(SUPABASE_URL|ALPACA_API_KEY|LOKI_URL|ANTHROPIC_API_KEY|FINNHUB_API_KEY)=\""'` returns `5`
- `ssh ridley 'bash -c ". ~/.openclaw-env && python3 -c \"import os; assert os.environ[\\\"SUPABASE_URL\\\"].startswith(\\\"https://\\\"); print(\\\"ok\\\")\""'` returns `ok`
**Rollback:** keep a timestamped backup `~/.openclaw-env.bak.20260421` before overwrite.
**Output artifact:** Env var count (number), SUPABASE_URL length (integer), file permissions
string. **No values in the artifact.**
**Depends on:** TASK-CLEANUP-01

### TASK-CLEANUP-03 . WORF . [DONE — PASS WITH FOLLOW-UPS]
**Goal:** Read-only audit that the CLEANUP-02 restore didn't leak any secret into the session
transcript, `~/.claude.json`, shell history, or git.
**Acceptance:**
- `rg -n 'glsa_|sk-ant-[A-Za-z0-9_-]{20,}|sbp_[A-Za-z0-9]{20,}|xoxb-|AK[A-Z0-9]{16}' ~/.claude/ 2>/dev/null` returns zero matches
- `git -C /home/mother_brain/projects/openclaw-trader status --short` has no new files containing secrets
- `ssh ridley 'tail -80 ~/.bash_history'` contains no full secret values (vault-show + subshell
  piping is permitted since values never enter history)
- New `~/.openclaw-env` has exactly `chmod 600` and owner `ridley:ridley`
**Output artifact:** Pass/fail table (5 checks × pass/fail) + grep counts.
**Depends on:** TASK-CLEANUP-02

---

### Wave 2 — Verify primary paths (parallel after W1)

### TASK-CLEANUP-04 . GEORDI . [DONE]
**Goal:** Hand-fire `system_check.py --mode health` then `catalyst_ingest.py` on ridley to prove
env → tracer → Supabase round-trips. Outside market hours — no trading risk.
**Acceptance:**
- `ssh ridley 'cd ~/openclaw-trader && . ~/.openclaw-env && python3 scripts/system_check.py --mode health'` finishes with `FAIL ≤ 2` (some WARNs for data staleness are acceptable; SKIPs on 7-day windows are fine)
- `ssh ridley 'cd ~/openclaw-trader && . ~/.openclaw-env && python3 scripts/catalyst_ingest.py'` exits `0`
- Neither log tail contains `sb_get error: Request URL is missing` lines
- Supabase query `SELECT pipeline_name, MAX(started_at) FROM pipeline_runs GROUP BY 1` shows
  fresh rows (within the last 10 minutes) for both `health_check` and `catalyst_ingest`
**Output artifact:** Before/after FAIL counts for health_check + fresh pipeline_run_ids.
**Depends on:** TASK-CLEANUP-02

### TASK-CLEANUP-05 . DATA . [DONE]
**Goal:** Confirm tracer root-row persistence is healed — the "Root row write failed
(attempt 3/3)" pattern is gone.
**Acceptance:**
- SQL: `SELECT pipeline_name, step_name, status, COUNT(*) FROM pipeline_runs WHERE started_at > now() - interval '30 minutes' GROUP BY 1,2,3 ORDER BY 1,2` shows for each fresh pipeline at least one root row (`step_name='root'`, `status='success'`) plus ≥1 child step row (proves FK chain healed)
- Zero rows with `status='failed'` where `error_message ILIKE '%FK constraint%'`
- `SELECT COUNT(*) FROM data_quality_checks WHERE checked_at > now() - interval '30 minutes'` returns ≥1 (catalyst_ingest writes quality checks)
**Output artifact:** Representative pipeline_run_id with intact root→child chain + 30-min row counts per pipeline.
**Depends on:** TASK-CLEANUP-04

### TASK-CLEANUP-06 . GEORDI . [DONE]
**Goal:** Verify Loki shipping is functional end-to-end.
**Acceptance:**
- Grafana Loki stats for `{app="openclaw-trader"}` over the last 15 min after CLEANUP-04
  returns non-zero `streams`, `entries`, `bytes`
- LogQL `{app="openclaw-trader"} | json` returns ≥5 entries with top-level fields
  `timestamp`, `level`, `project="openclaw-trader"`, `message`, and nested `metadata.*`
- LogQL metric `sum(count_over_time({app="openclaw-trader"}[5m]))` returns ≥5 over the window
- `{app="openclaw-trader", script="system_monitor"}` returns at least one `daemon_start` or
  `service_up` event (flush=True path, proves CLEANUP-08 kick)
**Output artifact:** Sample JSON-parsed entry + 5-minute count + LogQL queries used.
**Depends on:** TASK-CLEANUP-02

---

### Wave 3 — system_monitor restoration (parallel start with W2)

### TASK-CLEANUP-07 . GEORDI . [DONE — superseded by PR #55 on 2026-04-20]
**Goal:** Restore `scripts/collectors.py` — the psutil + tegrastats + `/sys` wrapper module
that `system_monitor.py` imports. Wiped during reflash; the crontab header still carries the
`collectors module missing` note.
**Resolution:** Already shipped in PR #55 merged 2026-04-20 (commit `cd4e5ba`). File present
at `/home/ridley/openclaw-trader/scripts/collectors.py` (8063 bytes, 234 LOC). Verified
2026-04-21 23:03 PDT during /engage re-probe. The crontab header note is stale (follow-up in
CLEANUP-08-B).
**Acceptance:**
- File exists at `scripts/collectors.py` and is committed to `main`
- Public surface functions present: `get_cpu_metrics()`, `get_memory_metrics()`,
  `get_gpu_metrics()` (reads `/sys/class/thermal/...`, falls back on non-Jetson hosts),
  `get_ollama_metrics()` (queries `http://localhost:11434/api/ps`), `get_disk_metrics()`,
  `get_process_metrics()`, `get_power_mode()` (via `nvpmodel -q` or `/etc/nvpmodel.conf`)
- Every return key is one of the existing `system_stats` columns
  (`cpu_percent, cpu_freq_mhz, cpu_cores, load_avg_1m, load_avg_5m, mem_total_mb, mem_used_mb,
  mem_available_mb, mem_percent, gpu_load_pct, gpu_temp_c, cpu_temp_c, disk_root_pct,
  disk_nvme_pct, disk_nvme_used_gb, process_count, openclaw_mem_mb, ollama_mem_mb,
  ollama_running, ollama_models, ollama_vram_mb, power_mode, uptime_seconds`)
- Collectors return numeric / bool / None on any exception — never raise
- `python3 -c "from scripts.collectors import get_cpu_metrics; print(get_cpu_metrics())"`
  succeeds on mother_brain and ridley
- PR #XX merged to `main`; CI green
- The `# NOTE: system_monitor.py @reboot disabled — 'collectors' module missing` banner
  removed from the crontab header (in the same PR or a follow-up edit to `manifest.py`)
**Output artifact:** PR URL, commit hash, and a column→collector mapping table.
**Depends on:** nothing (runs parallel with W1/W2)

### TASK-CLEANUP-08 . GEORDI . [DONE — superseded by Apr-20 restoration]
**Goal:** Install a systemd unit for `system_monitor.py` and enable it.
**Resolution:** Live as **systemd user unit** `openclaw-system-monitor.service` at
`/home/ridley/.config/systemd/user/openclaw-system-monitor.service`. Verified during /engage
re-probe 2026-04-22 06:03 UTC: `active (running)` with PID 17642, enabled. Companion service
`openclaw-dashboard.service` also live (uvicorn on :9090). The earlier report that
"system_monitor.service is not installed" used the wrong service name.

### TASK-CLEANUP-08-B . GEORDI . [BLOCKED: TASK-CLEANUP-02]
**Goal:** Remove the stale `# NOTE: system_monitor.py @reboot disabled — 'collectors' module
missing from repo` block from the generated crontab header. collectors.py has shipped and
the daemon is systemd-managed; the note is misleading.
**Acceptance:**
- `ssh ridley 'crontab -l | grep -c "collectors.*missing"'` returns `0`
- `scripts/manifest.py` no longer emits the disabled banner
- Change committed to `main` and deployed
**Output artifact:** manifest.py diff hash + post-regen crontab snippet.
**Depends on:** TASK-CLEANUP-02
**Depends on:** TASK-CLEANUP-07, TASK-CLEANUP-02

---

### Wave 4 — Integration rehearsal (after W2 + W3)

### TASK-CLEANUP-09 . DATA . [DEFERRED — next natural fire is 05:00 PDT Wed 2026-04-22]
**Goal:** Observe a natural cron fire end-to-end to prove the fleet pipeline behaves
identically to the Apr-10 baseline.
**Acceptance:** Cron at 05:00 PDT Wed will produce a fresh `pipeline_runs` root + child
chain. Until then, hand-fire (CLEANUP-04) provided equivalent end-to-end proof: fresh
`catalyst_ingest` root row with 26 child steps + 0 FK failures + Loki events flowing.
Marking DEFERRED (not DONE) because natural-fire verification is strictly stronger than
hand-fire, but the session-level verdict is already conclusive.
**Output artifact:** Will append a block to PROGRESS.md after the 05:00 PDT fire with the
observed pipeline_run_id + Loki count.
**Depends on:** time.

### TASK-CLEANUP-10 . PICARD . [DONE]
**Resolution:** `PASS 72 FAIL 0 WARN 0 SKIP 0 TOTAL 72` — `FLIGHT DIRECTOR .......... ALL
STATIONS GO` at 2026-04-22 ~07:10 UTC after fixing the systemd EnvironmentFile regression
(Geordi's `export` prefix was rejected by systemd 249, so two user-unit ExecStart lines were
rewritten to `/bin/bash -c '. ~/.openclaw-env && exec <original>'`). Env restoration
verified: both dashboard and system_monitor processes now have 34 env vars including
`DASHBOARD_KEY` and `LOKI_API_KEY`.
**Goal:** Re-establish the `72/72` preflight baseline from the Apr-20 restoration.
**Acceptance:**
- `ssh ridley 'cd ~/openclaw-trader && . ~/.openclaw-env && python3 scripts/system_check.py --mode preflight'` returns `PASS 72 FAIL 0 WARN ≤3 SKIP 0 TOTAL 72`
- `run_id` captured; full output tail saved to PROGRESS.md
- Any FAIL → do NOT mark DONE; spawn a CLEANUP-1X follow-up task for each and leave this one
  IN PROGRESS
**Output artifact:** Preflight summary line + run_id + output tail.
**Depends on:** TASK-CLEANUP-09

---

### Wave 5 — Documentation + announcement

### TASK-CLEANUP-11 . GEORDI . [DONE]
**Goal:** Write `docs/runbooks/ridley-post-flash-checklist.md` so the next reflash is a
15-minute operation instead of a multi-session recovery.
**Acceptance:** Runbook covers these sections, each with copy-pasteable commands:
- `1. Timezone` — `timedatectl set-timezone America/Los_Angeles; systemctl restart cron` and
  why the crontab `TZ=` directive alone is insufficient
- `2. Env file` — 16-var shape, vault paths table, subshell-only assembly pattern, `chmod 600`
- `3. Repo clone + hooks path` — already documented in RR-03; link it
- `4. Python deps` — already documented in RR-04; link it
- `5. Collectors + system_monitor` — systemd unit block, `systemctl enable --now` command,
  verification query
- `6. Post-fix verification` — pipeline_runs freshness SQL, Loki stats query, system_stats
  cadence SQL
- `7. Known gotchas` — `fly` CLI not installed on mother_brain; dashboard lives on ridley:9090
  not Fly (verify via `ssh ridley 'tail logs/dashboard.log'`); Ollama GPU check:
  `journalctl -u ollama | grep "offloaded .* layers to GPU"`
**Output artifact:** Runbook path + word count + section list in PROGRESS.md.
**Depends on:** TASK-CLEANUP-08

### TASK-CLEANUP-12 . PICARD . [IN PROGRESS — posted after merge]
**Goal:** Post completion to Ten Forward + notify Slack + close the task group in PROGRESS.md.
**Acceptance:**
- Reply posted to `[Project Log] openclaw-trader` thread (id `pGx8rJ8H65rkp-AdpPn8N`) using
  standard `Shipped / Blocked / Decisions / Next` schema; body mentions 7 failure modes,
  the 11 recovery tasks, `preflight 72/72`, collectors PR, runbook URL
- `@picard` mentioned in body for claudefleet indexing
- Slack `#all-lions-awaken` 1-line notification via `bash ~/.claude/hooks/slack_notify.sh`
- PROGRESS.md appended with a `## Ridley Cleanup (2026-04-21)` completion block matching the
  convention used by TASK-RR-14
**Output artifact:** Forum post ID + Slack message_ts + PROGRESS.md block line range.
**Depends on:** TASK-CLEANUP-10, TASK-CLEANUP-11

---

## Ridley Restoration — Bring openclaw back online on fresh JetPack 6.2.2 (2026-04-20)

**Source:** ridley (Jetson Orin Nano 8GB) was reflashed tonight with JetPack 6.2.2 / L4T 36.5 to
fix long-standing Ollama CUDA failures. SD card rootfs is clean — zero openclaw state remains.
Supabase data survives (inference_chains, cost_ledger, etc.). NVMe drive data is intact
(/mnt/nvme/stream-saber). Fly dashboard `openclaw-trader-dash` has all required secrets.

**Data-driven diagnosis of prior breakdown** (Supabase query results, 2026-04-20):
- 919 lifetime inference_chains; 10% cleared all 5 tumblers
- Apr 7-10: 19-20% clear rate (system healthy)
- Apr 11-14: cleared drops to 0 (tumblers break)
- Apr 15-20: 5-10 chains/day, all dying at `time_limit` or `confidence_floor`
- $0.17 lifetime Claude spend — budget was NEVER the bottleneck (`resource_limit` = 0 trips)
- Root cause: T3 (Ollama qwen2.5:3b) started returning None → `confidence_floor`/`time_limit` cascade

**Mission goal:** By end of session — fresh ridley running the full cron schedule, qwen2.5:3b
reliably serving T3, scanner completing chains to `all_tumblers_clear` at the Apr-10 baseline
(~19% clear rate). Alpaca paper trading enabled. Cron active.

**Non-goals:** No architecture changes. No local embeddings migration. No T5 two-stage refactor.
Those are Phase 2 after stability is proven.

---

### Wave 1 — Ridley base setup (parallel-safe)

### TASK-RR-01 . GEORDI . [DONE]
**Goal:** Install Python pip + dev tooling on ridley so `pip install -r requirements.txt` works.
**Acceptance:**
- `python3 -m pip --version` returns a version (currently: `No module named pip`)
- `python3 -m venv --help` works (for optional venv)
- git is already installed (confirmed in survey)
**Commands:** `sudo apt-get install -y python3-pip python3-venv python3-dev build-essential`
**Output artifact:** pip version in PROGRESS.md, any package install warnings.
**Depends on:** nothing

### TASK-RR-02 . GEORDI . [DONE]
**Goal:** Pull the production Ollama models onto ridley. qwen2.5:3b is the T3 workhorse;
nomic-embed-text is for catalyst RAG (already referenced in manifest.py as "Ollama embed").
**Acceptance:**
- `ollama list` shows `qwen2.5:3b` and `nomic-embed-text`
- `curl -s http://localhost:11434/api/tags` returns both models
- Smoke test: `ollama run qwen2.5:3b "Say ok"` returns non-empty response in <20s
- Confirm 29/29 GPU layers offloaded (grep `offloaded.*layers to GPU` in journalctl)
**Commands:** `ollama pull qwen2.5:3b && ollama pull nomic-embed-text`
**Output artifact:** Model sizes + journalctl line showing GPU offload.
**Depends on:** nothing

### TASK-RR-03 . GEORDI . [DONE]
**Goal:** Clone openclaw-trader repo to `/home/ridley/openclaw-trader` (canonical path per
CLAUDE.md deploy flow) at the current HEAD (commit `078a00b`).
**Acceptance:**
- `~/openclaw-trader/scripts/manifest.py` exists
- `git log -1 --oneline` shows commit `078a00b`
- `git config core.hooksPath .githooks` configured
- Remote is `https://github.com/Lions-Awaken/openclaw-trader.git` (HTTPS, no auth needed for
  public repo; if private, use PAT — see RR-04 notes)
**Rollback:** `rm -rf ~/openclaw-trader` and re-clone.
**Output artifact:** git log -1 output + hooks path confirmation.
**Depends on:** nothing

---

### Wave 2 — Python dependencies + secret materialization (after W1)

### TASK-RR-04 . GEORDI . [DONE]
**Goal:** Install Python dependencies from `requirements.txt` into ridley's system Python
(matches pre-reflash setup — requirements.txt is pinned to what was working on ridley).
**Acceptance:**
- `cd ~/openclaw-trader && pip3 install -r requirements.txt` exits 0
- `python3 -c "import httpx, sentry_sdk, yfinance, alpaca, finnhub, pandas, fastapi, anthropic"` succeeds
- `pip3 freeze | grep -E "^(httpx|alpaca|yfinance)"` shows exact pinned versions
**Risk:** Python 3.10 on ridley vs 3.14 `__pycache__` on mother_brain — pinned versions in
requirements.txt are 3.10-compatible (verified).
**Output artifact:** Full `pip3 freeze` output comparison to requirements.txt.
**Depends on:** TASK-RR-01, TASK-RR-03

### TASK-RR-05 . WORF . [DONE]
**Goal:** Inventory env-var gaps. Compare `common.py` required vars against Fly secrets +
claudefleet vault. Document which secrets need transfer and which paths will carry them.
**Required env vars** (from `common.py` grep):
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_API_KEY_2`,
`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `PERPLEXITY_API_KEY`, `FINNHUB_API_KEY`,
`FRED_API_KEY`, `SENTRY_DSN`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL`, `OLLAMA_URL`
**Known sources:**
- Fly `openclaw-trader-dash` secrets: SUPABASE_URL, SUPABASE_SERVICE_KEY, ANTHROPIC_API_KEY,
  ALPACA_API_KEY, ALPACA_SECRET_KEY, FINNHUB_API_KEY, SENTRY_DSN (confirmed in Fly secrets list)
- Vault: anthropic, alpaca, market_data.finnhub, market_data.perplexity, slack, sentry.dsns
**Acceptance:** Written gap table: each env var → source path or [MISSING]. Flag any vars that
require human action (e.g., FRED_API_KEY if not in either vault or Fly).
**Output artifact:** Gap table in PROGRESS.md.
**Depends on:** nothing

### TASK-RR-06 . GEORDI . [DONE]
**Goal:** Materialize `/home/ridley/.openclaw-env` on ridley with all required env vars.
Secrets must NEVER be printed to mother_brain stdout — use SSH pipe pattern:
`fly ssh console -a <app> -C 'sh -c "env | grep -E ^(VAR1|VAR2|...)"' 2>/dev/null | ssh ridley 'cat > ~/.openclaw-env && chmod 600 ~/.openclaw-env'`
For vault-sourced secrets: `~/.config/claudefleet/vault-show | python3 -c "..." | ssh ridley 'cat >> ~/.openclaw-env'`
(vault-show output consumed in pipe, never printed).
**Acceptance:**
- `ssh ridley 'ls -la ~/.openclaw-env'` shows mode 600, owned by ridley:ridley
- `ssh ridley 'wc -l ~/.openclaw-env'` shows N lines where N >= 11 (required vars)
- `ssh ridley 'source ~/.openclaw-env && python3 -c "import os; print(bool(os.environ[\"SUPABASE_URL\"]))"'` returns `True`
- NO secret values ever appear in mother_brain Bash tool output
**Risk:** A failure in the pipe chain could leak secrets to stderr. Redirect stderr carefully.
**Rollback:** `ssh ridley 'rm -f ~/.openclaw-env'`.
**Output artifact:** Line count + checksum of env file (no values).
**Depends on:** TASK-RR-04, TASK-RR-05

### TASK-RR-07 . DATA . [DONE]
**Goal:** Alpaca paper-trading posture confirmation — verify `ALPACA_BASE_URL` defaults to
paper endpoint in common.py, and that `alpaca.paper_trade` flag in vault is `"true"`.
**Acceptance:**
- grep in common.py: default Alpaca base URL = `https://paper-api.alpaca.markets` (paper)
- vault value for `alpaca.paper_trade` is `"true"` (string)
- Brian has explicitly confirmed paper-only for this restore (logged in PROGRESS.md)
**Output artifact:** Confirmation note in PROGRESS.md with grep evidence.
**Depends on:** nothing

---

### Wave 3 — Connectivity verification (after W2)

### TASK-RR-08 . GEORDI . [DONE]
**Goal:** Run a SMOKE TEST of every external service from ridley — Supabase, Alpaca (paper),
Anthropic, Finnhub, Ollama local, Slack. Non-destructive reads only.
**Acceptance:** A single test script produces this table:
```
Supabase:   ✓ 200 OK  (GET /rest/v1/inference_chains?limit=1)
Alpaca:     ✓ 200 OK  (GET /v2/account) — paper account, status=ACTIVE
Anthropic:  ✓ valid (HEAD /v1/messages with small probe — or call_claude with max_tokens=1)
Finnhub:    ✓ 200 OK (quote probe for SPY)
Ollama:     ✓ qwen2.5:3b responds to "ok"
Slack:      ✓ auth.test returns ok=true
```
Write script to `scripts/preflight_restore.py` (new, temp — can be removed after).
**Output artifact:** Full smoke test output in PROGRESS.md.
**Depends on:** TASK-RR-06

### TASK-RR-09 . GEORDI . [DONE]
**Goal:** Run `scripts/system_check.py --mode preflight` — the NASA go/no-go preflight
simulator (17 groups, synthetic data, Mission Readiness score). Entire pipeline with synthetic
data, no real trades.
**Acceptance:**
- Preflight completes without Python exceptions
- Mission Readiness score ≥ 85% (threshold for go/no-go)
- All 5 tumblers execute on synthetic data (T3 calls qwen, returns real output)
- Exit code 0
**Rollback:** No trades are placed in preflight mode — nothing to roll back.
**Output artifact:** Preflight output + Mission Readiness score in PROGRESS.md.
**Depends on:** TASK-RR-08

---

### Wave 4 — Cron + background services (after W3)

### TASK-RR-10 . GEORDI . [DONE]
**Goal:** Generate crontab from canonical `scripts/manifest.py` + `EVENT_TRIGGERED`, install on
ridley. Wrap every entry to source `~/.openclaw-env` and cd into project dir.
**Cron wrapper pattern:**
```
<SCHEDULE> cd /home/ridley/openclaw-trader && . ~/.openclaw-env && python3 <SCRIPT> <ARGS> >> /home/ridley/openclaw-trader/logs/<NAME>.log 2>&1
```
Include all 17 MANIFEST entries + the 2 EVENT_TRIGGERED cron entries (ollama_watchdog ×4,
system_monitor @reboot).
**Script-specific args** (from CLAUDE.md cron table):
- `system_check.py --mode health` for 05:00 weekday run
- `catalyst_ingest.py` no args
- `ingest_signals.py form4` for 06:00, `ingest_signals.py options` for 07:00
- `scanner.py` no args
- `meta_analysis.py daily` for 13:30 weekday, `meta_analysis.py weekly` for 16:00 Sunday
**Acceptance:**
- `crontab -l | wc -l` ≥ 20 (all entries)
- `crontab -l | grep -c scanner` == 2 (6:35 + 9:30)
- `crontab -l | grep -c catalyst_ingest` == 3 (5:30 + 9:00 + 12:50)
- `crontab -l | grep ollama_watchdog` returns 4 matches
- `mkdir -p /home/ridley/openclaw-trader/logs` before first cron fires
**Rollback:** `crontab -r` removes all cron entries.
**Output artifact:** Full installed crontab copy-pasted into PROGRESS.md (no secrets to worry
about since it's just schedules).
**Depends on:** TASK-RR-09

### TASK-RR-11 . GEORDI . [SKIPPED: collectors/ module missing from repo]
**Goal:** Start `system_monitor.py` as a persistent daemon via systemd user service (not @reboot
cron — more robust). Ensures hardware metrics → `system_stats` table even when cron isn't firing.
**Acceptance:**
- `systemctl --user status openclaw-system-monitor` shows active (running)
- After 60s, new rows appear in Supabase `system_stats` table
- Service restarts automatically if the Python process dies (Restart=always)
**Rollback:** `systemctl --user stop openclaw-system-monitor && systemctl --user disable ...`
**Output artifact:** systemd unit file contents + first system_stats row timestamp.
**Depends on:** TASK-RR-10

---

### Wave 5 — Production smoke test (after W4)

### TASK-RR-12 . GEORDI . [DONE]
**Goal:** Run one REAL `scanner.py` invocation manually (simulating a live 6:35 AM run). Verify
chain completes all tumblers and writes to `inference_chains` with `stopping_reason` ≠ `time_limit` or `confidence_floor` for at least some tickers.
**Acceptance:**
- scanner.py exits 0 after completing its watchlist (or within 5 min)
- Query Supabase: `SELECT stopping_reason, COUNT(*) FROM inference_chains WHERE created_at > now() - interval '10 min'` returns at least some rows with `all_tumblers_clear` OR `forced_connection` (= tumbler saved cost, healthy). If ALL rows are `time_limit`/`confidence_floor`, system is NOT healthy — halt and investigate T3 output.
- `pipeline_runs` table has a row for this scanner invocation with status=success
- T3 tumbler output contains real qwen text (not None) in at least one chain (query `tumblers` jsonb column)
**Rollback:** Paper trading — no real-money risk. Any positions opened are Alpaca paper.
**Output artifact:** Summary of chain outcomes: N total, X cleared, Y forced_connection, Z floor/timeout. Paste in PROGRESS.md.
**Depends on:** TASK-RR-11

### TASK-RR-13 . GEORDI . [DONE — operationally pass; watchdog doesn't write pipeline_runs (follow-on: add @traced)]
**Goal:** Run `ollama_watchdog.py` manually to verify memory-recovery path works (the thing
that was failing pre-reflash).
**Acceptance:**
- Script exits 0 within 30s
- Output contains "Ollama healthy" (success case) OR a clean restart sequence
- New row in `pipeline_runs` for `ollama_watchdog` (first one EVER — pre-reflash had 0 runs logged)
**Rollback:** None needed; worst case the watchdog restarts Ollama, which causes a 10-20s blip.
**Output artifact:** Watchdog output + pipeline_runs row.
**Depends on:** TASK-RR-12

---

### Wave 6 — Handoff + announcement

### TASK-RR-14 . PICARD . [DONE]
**Goal:** Final integration verification + fleet announcement.
Verification queries:
1. `SELECT COUNT(*) FROM inference_chains WHERE created_at > now() - interval '1 hour'` — should have rows
2. `SELECT COUNT(*) FROM pipeline_runs WHERE started_at > now() - interval '1 hour'` — rows from scanner + watchdog
3. `SELECT COUNT(*) FROM system_stats WHERE created_at > now() - interval '15 min'` — from system_monitor daemon
4. `crontab -l | wc -l` on ridley
Write a comprehensive PROGRESS.md entry covering:
- Reflash → restoration timeline
- Mission Readiness preflight score
- Chain outcome summary (cleared vs floor/timeout)
- Links to: `docs/kb/jetson-orin-nano-flashing.md`, this TASKS.md section
Post an announcement to Slack `#all-lions-awaken`:
```
openclaw-trader restored to ridley (fresh JetPack 6.2.2).
Preflight score: <X>/100. First scanner: <Y>/<Z> chains cleared.
Full summary: <PROGRESS.md link>
```
**Acceptance:** All 4 queries return expected row counts. Slack announcement posted. PROGRESS.md has restoration entry with timestamps.
**Output artifact:** Slack message timestamp + PROGRESS.md entry.
**Depends on:** TASK-RR-13

---

### Risk Register — Ridley Restoration

| Risk | Likelihood | Mitigation |
|---|---|---|
| Secret leak during env materialization | LOW | SSH pipe pattern, stderr redirect, no printing to tool output |
| Python 3.10 on ridley vs 3.14 `__pycache__` from mother_brain | LOW | requirements.txt pinned to 3.10-compatible versions; .pyc will recompile |
| qwen still OOMs after reflash | MEDIUM | If TASK-RR-09 preflight fails on T3, halt. Investigate via `journalctl -u ollama` before proceeding. Tonight's inference test showed 29/29 GPU offload working. |
| GitHub repo is private and HTTPS clone fails | LOW | Fallback: use `gh auth login` or PAT in HTTPS URL. Should take <5 min if it happens. |
| Alpaca account accidentally points at live trading | LOW | TASK-RR-07 verifies paper_trade flag before cron activation |
| Cron fires before env file is populated | LOW | TASK-RR-10 depends on TASK-RR-06 (env) and TASK-RR-09 (preflight pass). Hard gate. |
| FRED_API_KEY or OTHER_VAR missing from both vault and Fly | MEDIUM | TASK-RR-05 surfaces gap early. Brian can paste-add to vault in one go. |
| "Different mode" boot issue recurs | LOW | Cause was missing ridley user — fixed via SD-card chroot. Confirmed in tonight's session. |

---

### Estimated execution time (all tasks)

| Wave | Parallel tasks | Serial time |
|---|---|---|
| 1 | RR-01 + RR-02 + RR-03 | ~8 min (ollama pull is the pacesetter) |
| 2 | RR-04, RR-05, RR-07 (mostly parallel) → RR-06 (gated) | ~5-10 min |
| 3 | RR-08 → RR-09 | ~5 min |
| 4 | RR-10 → RR-11 | ~5 min |
| 5 | RR-12 → RR-13 | ~3-5 min (scanner depends on watchlist size) |
| 6 | RR-14 | ~2 min |
| **Total** | | **~30-40 min** |

---

## Typography System + Interactive Workflow Page Extraction

Source: User request 2026-04-18 — two parallel overhauls driven by the
Adversarial Ensemble Interactive Workflow being cramped inside its
collapsed dropdown and the informational text ("59 automated checks…")
being illegibly small in Orbitron.

**Design decisions:**
- **Label font (unchanged):** Orbitron for headers, titles, menu/nav
  items, pill labels, button text, badges, KPI values, stat labels.
- **Reading font (new):** Inter (Google Fonts) for all informational,
  explanatory, instructional, and descriptive body text. Inter is
  purpose-built for screen UI + technical documentation, great at small
  sizes with a tall x-height and open apertures.
- **Body color:** `#e8eaed` (Material Design on-dark recommendation) —
  off-white with neutral tone, optimal contrast without harshness.
- **Body size:** ~0.9rem (just a hair smaller than header size). Current
  gray description text is ~0.75rem and hard to read; new default
  ~0.9rem, line-height 1.55 for readable paragraphs.
- **Workflow page route:** `GET /workflow` returns `workflow.html` with
  no dashboard chrome — full viewport canvas for the interactive
  diagram. HOW IT WORKS button still navigates to `#section-about` on
  the Dashboard; a secondary link from there opens the standalone page.

---

## Wave 1 — Typography Foundation (blocks everything else)

### TASK-TYPO-01 . FRONTEND-AGENT . [DONE]
**Goal:** Establish the Inter-based typography system. In
`dashboard/theme.css`:
- Add `@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');` at the top
- Define CSS variables in `:root`:
  - `--font-label: 'Orbitron', sans-serif;` (existing usage pattern)
  - `--font-body: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;`
  - `--text-body: #e8eaed;`
  - `--text-body-dim: #a8abb2;` (for secondary/metadata lines)
- New utility class `.body-text`:
  ```css
  .body-text {
    font-family: var(--font-body);
    font-size: 0.9rem;
    font-weight: 400;
    color: var(--text-body);
    letter-spacing: 0.02em;
    line-height: 1.55;
  }
  ```
- Variant `.body-text-sm` at 0.8rem for metadata/footnotes
- DO NOT modify any existing styles yet — purely add. Ruff/lint clean.
**Acceptance:** Both fonts load (visible in network tab); `.body-text`
class renders correctly when temporarily added to any element; no
existing styles changed or broken.
**Output artifact:** CSS diff in PROGRESS.md.
**Depends on:** nothing

---

## Wave 2 — Prove-out in How It Works (validates the system before mass rollout)

### TASK-TYPO-02 . FRONTEND-AGENT . [DONE]
**Goal:** Apply `.body-text` class to all informational paragraphs and
descriptions inside `#section-about` (How It Works) in
`dashboard/index.html`:
- Every `<p>` tag inside the section
- The 20 workflow step descriptions (populated by JS — update the
  render function in the workflow widget so its `.wf-description` /
  description element uses `.body-text` class)
- "59 automated checks across 13 groups…" and all similar metadata
  lines currently rendered in gray Orbitron
- Keep Orbitron for: step titles (HEALTH CHECK, CATALYST, etc.), group
  labels, node labels, button labels, "STEP X OF Y" counter
**Acceptance:** Informational text in How It Works reads comfortably at
0.9rem in Inter; all structural labels remain in Orbitron; no layout
breakage.
**Output artifact:** Before/after screenshot + list of updated
selectors in PROGRESS.md.
**Depends on:** TASK-TYPO-01

---

## Wave 3 — Standalone Workflow Page Extraction

### TASK-WF-ROUTE-01 . BACKEND-AGENT . [BLOCKED: TASK-TYPO-02]
**Goal:** Add `GET /workflow` route in `dashboard/server.py`. Serves a
new standalone file `dashboard/workflow.html`. Auth-protected with
`_require_auth` matching the `/` and `/systems` routes. Returns the
file content with `text/html` content type.
**Acceptance:** Authenticated `curl localhost:9090/workflow` returns
HTML 200; unauthenticated returns 401/redirect to /login; route
registered in server.py route list.
**Output artifact:** Route definition in PROGRESS.md.
**Depends on:** TASK-TYPO-02

### TASK-WF-PAGE-01 . FRONTEND-AGENT . [BLOCKED: TASK-WF-ROUTE-01]
**Goal:** Create `dashboard/workflow.html` — standalone full-viewport
version of the Adversarial Ensemble Interactive Workflow. Extract:
- All `.wf-*` CSS (currently inline in index.html ~lines 1389-1680)
- `WF_STEPS` constant + all rendering JS (currently ~lines 1788-2200)
- The `.wf-shell` HTML markup
Remove the `.card` container restraint. Layout rules:
- Full 100vw × 100vh viewport (no dashboard sidebar/header chrome)
- Small back button top-left → returns to `/#about`
- Diagram SVG spans full width; nodes spread with 2x current spacing
- Detail panel sits beside or below the diagram with generous padding
- Controls (Prev/Restart/Play/Stop/Next + step dots) pinned to bottom
- Informational text uses `.body-text` (from Wave 1)
- Structural labels stay Orbitron
**Acceptance:** `/workflow` renders a full-page diagram with all 20
steps visible and readable; play/pause/step/restart all work; back
button returns to /#about; no JS console errors; performance smooth.
**Output artifact:** Screenshot + node-spacing measurements in
PROGRESS.md.
**Depends on:** TASK-WF-ROUTE-01

### TASK-WF-LINK-01 . FRONTEND-AGENT . [BLOCKED: TASK-WF-PAGE-01]
**Goal:** In `#section-about`, replace the inline 20-step diagram with
a single prominent "Open Interactive Workflow →" CTA button linking to
`/workflow`. Keep the narrative text (What is Parallax, Cron Timeline,
Scanner Pipeline, Ensemble table, Calibration Loop, Infrastructure,
Circuit Breakers) in place. Remove the inline `.wf-shell` markup + the
CSS/JS that powered it (now lives in workflow.html). Verify no orphan
references to wf-* IDs remain in index.html.
**Acceptance:** #section-about no longer contains the 20-step
interactive diagram; CTA button visible and opens /workflow; no JS
errors from missing wf-* element references.
**Output artifact:** Lines removed from index.html in PROGRESS.md.
**Depends on:** TASK-WF-PAGE-01

---

## Wave 4 — App-Wide Typography Rollout

### TASK-TYPO-03 . FRONTEND-AGENT . [DONE]
**Goal:** Audit every section in `dashboard/index.html` (Dashboard,
Pipeline, Trade Log, Positions, Replay, Ensemble, Performance,
Economics, About). For each, identify informational/explanatory text
(paragraph descriptions, empty-state messages, table row reasoning,
tooltips, error messages, chart labels). Apply `.body-text` or
`.body-text-sm` class. Keep Orbitron for: h1/h2 headers, nav pills,
button labels, KPI values (the big dollar amounts), stat labels,
badge/pill text, column headers in tables.

Also update JS render functions that build innerHTML with inline
font-family / font-size to remove those inline overrides and rely on
the class instead.

Run through this list:
- Dashboard: Market Regime action text, Recent Trades reasoning column
- Pipeline: run detail JSON snapshots, timeline day labels
- Trade Log: trade reasoning, what_worked, improvement fields
- Positions: empty state text, error messages
- Replay: chain reasoning, waterfall step descriptions, OHLCV loading states
- Ensemble: shadow profile metadata, divergence reasoning
- Performance: leaderboard descriptions, position row details
- Economics: budget descriptions, chart legend labels
**Acceptance:** Every informational text block uses Inter at the new
size; structural labels untouched; zero font-family overrides remain
in inline styles where the default would suffice.
**Output artifact:** Per-section checklist of changes in PROGRESS.md.
**Depends on:** TASK-TYPO-02

### TASK-TYPO-04 . FRONTEND-AGENT . [DONE]
**Goal:** Apply the same typography rollout to
`dashboard/systems-console.html` (metrics dashboards, alerts, status
lines) and `dashboard/login.html` (error messages, expiry notices).
Same rules: Orbitron for structure, Inter for prose.
**Acceptance:** Systems console and login pages match the main
dashboard's typography pattern.
**Output artifact:** Screenshot diffs in PROGRESS.md.
**Depends on:** TASK-TYPO-03

---

## Wave 5 — Verification

### TASK-UI-VERIFY-01 . PICARD . [BLOCKED: TASK-TYPO-04, TASK-WF-LINK-01]
**Goal:** Final integration sweep:
1. Load dashboard; click every tab; verify headers are Orbitron, body
   text is Inter at 0.9rem, color is #e8eaed
2. Navigate to `/workflow`; verify full-page standalone diagram works
3. Navigate to `/systems`; verify typography rollout applied
4. Check `/login` typography
5. Hard-refresh on Fly prod; verify all changes deployed
6. Deploy to Fly.io
**Acceptance:** All pages render correctly with unified typography. No
JS errors. Fly.io reachable with new changes.
**Output artifact:** Completion summary + screenshots of each page in
PROGRESS.md.
**Depends on:** TASK-TYPO-04, TASK-WF-LINK-01

---

## Dead Code Audit — Tier 2/3 Follow-up

Source: In-session dead code audit (2026-04-18)
Status: Tier 1 complete (3 dead scripts + 1 doc deleted). Tier 2 and 3
deferred until after Monday's data comes in so we can see which
endpoints/tables the system actually uses under live load.

### TASK-DEAD-T2 . DB-AGENT . [BLOCKED: post-Monday-data]
**Goal:** Drop 4-5 unused Supabase tables after verifying no external readers. Candidates:
- `llm_inferences` (0 rows — likely superseded by pipeline_runs tracing)
- `research_memories` (0 rows — no writer found in codebase)
- `data_quality_checks` (0 rows — orphaned)
- `predictions` (3 stale rows — related to removed `prediction_accuracy` feature)
- `strategy_adjustments` (0 rows — meta_analysis proposes them but never writes; check if feature is alive)
Before dropping: grep all scripts + dashboard routes for each table name. Verify no Grafana/external tool reads them.
**Acceptance:** Dropped tables return "relation does not exist". No scripts or endpoints broken.
**Output artifact:** Migration + removal rationale in PROGRESS.md.
**Depends on:** Monday data collection (verify nothing starts writing to these tables)

### TASK-DEAD-T3 . BACKEND-AGENT . [BLOCKED: post-Monday-data]
**Goal:** Remove 12 orphaned backend endpoints from `dashboard/routes/*.py`. No frontend consumer, no chat tool, no external caller identified. Endpoints to remove:
- `trading.py`: `/api/rag/status`, `/api/rag/coverage`, `/api/rag/activity`, `/api/trade-learnings/stats`, `/api/trade-learnings/{id}`, `/api/logs/domains`, `/api/logs/domain/{name}`
- `ensemble.py`: `/api/shadow/kronos/latest`, `/api/shadow/positions/{id}`
- `health.py`: `/api/health/flight-status`
- `replay.py`: `/api/replay/outcome`
- `chat.py`: `POST /api/trades/{id}/reasoning`
Before removing: scan index.html + systems-console.html + chat tool dispatch for each URL. If any are used by a planned future feature, leave with a TODO comment.
**Acceptance:** All 12 endpoints removed. Dashboard still loads cleanly. No 404s on any tab.
**Output artifact:** List of removed endpoints in PROGRESS.md.
**Depends on:** TASK-DEAD-T2 (do DB first so no endpoint tries to query a dropped table mid-cleanup)

---

## Parallax Dashboard Alignment Audit

Source: In-session diagnostic + full data audit (2026-04-18)
Goal: Every tab, every KPI, every workflow description in the Parallax dashboard
must be 100% accurate to the system as it actually works today.

Context: Session diagnostics revealed T3/T4 tumblers were dead (0.000 delta),
regime was 15 days stale, pattern_templates was empty, and several dashboard
sections showed stale or incorrect data. Fixes deployed (keep_alive, templates,
regime cron, watchdog). Now aligning the dashboard to match reality.

---

## Wave 1 — Header KPIs + Dashboard Tab (no dependencies)

### TASK-DA-01 . FRONTEND-AGENT . [READY]
**Goal:** Audit and fix the 5 header KPIs (Equity, Cash, Total P&L, Win Rate, Trades) and the Dashboard tab content (regime widget, recent trades table). Specific issues to verify/fix:
- (A) Total P&L hardcodes `startingCapital = 100000`. Verify this matches the Alpaca paper account actual starting balance. If it doesn't, fetch starting capital from the account or make it configurable.
- (B) Win Rate and Trades show data from `account_performance` VIEW which aggregates ALL trade_decisions with no time window. The staleness indicator (`perf-stale-tag`) was added — verify it renders correctly and shows "last trade Xd ago" when stale.
- (C) Regime widget — verify the `age_days` staleness tag renders (yellow >1d, red >3d). Verify the regime badge CSS classes (`regime-UP_LOWVOL`, `regime-DOWN_ANY`, etc.) all exist and render correctly.
- (D) Recent trades table — verify `buildTradeTable()` renders correctly for all action types (BUY, SELL, CLOSE, STOP_OUT). Check that outcome coloring works (STRONG_WIN green, LOSS red, etc.).
- (E) The 60-second auto-refresh — verify `setInterval(loadDashboard, 60000)` doesn't create duplicate staleness tags or accumulate DOM elements on repeated refresh.
**Acceptance:** All 5 KPIs show correct live data. Regime staleness tag visible. No DOM element accumulation on 60s refresh cycles. Trade table renders all action types correctly.
**Output artifact:** Screenshot of working header KPIs + list of any fixes applied.
**Depends on:** nothing

---

## Wave 2 — Data Tabs (parallel — each touches different sections)

### TASK-DA-02 . FRONTEND-AGENT . [READY]
**Goal:** Audit Pipeline tab. Verify:
- (A) Pipeline health score (`/api/pipeline/health`) — does the score calculation (successes/total * 100) match reality? With 27 daily runs, what score shows?
- (B) Pipeline filter dropdown — verify it lists all current pipeline_names from `pipeline_runs` (scanner, catalyst_ingest, position_manager, ingest_form4, ingest_options_flow, shadow_position_opener, shadow_mark_to_market, shadow_performance_rollup, meta_daily, daily_report, health_check). If filter options are hardcoded, update to match actual pipelines.
- (C) Pipeline DAG viewer — click a run, verify the nested step tree renders correctly with status colors and duration.
- (D) 7-day timeline — verify it shows the correct daily bar chart of successes/failures.
**Acceptance:** All 4 pipeline sub-sections render with current data. Filter dropdown shows all active pipeline names. DAG viewer expands correctly.
**Output artifact:** List of fixes applied to Pipeline tab.
**Depends on:** nothing

### TASK-DA-03 . FRONTEND-AGENT . [READY]
**Goal:** Audit Trade Log tab. Verify:
- (A) `GET /api/trades` returns the 35 trade_decisions rows. Trade Log should display them all (current limit is 50, so all 35 fit).
- (B) Expandable detail rows — verify clicking a trade expands to show reasoning, what_worked, improvement fields.
- (C) Column display — verify entry_price, exit_price, pnl, hold_days, signals_fired, outcome all render correctly. Some rows have null exit_price/pnl (open entries) — verify those display gracefully.
- (D) Outcome badge coloring — STRONG_WIN, WIN, LOSS, STRONG_LOSS, null should each have distinct styling.
- (E) Trade actions — BUY entries show entry info, SELL/CLOSE/STOP_OUT show exit info. Verify the table doesn't mix these confusingly.
**Acceptance:** All 35 trade_decisions visible. Expandable detail works. No display errors for null fields. Outcome badges correctly colored.
**Output artifact:** List of fixes applied to Trade Log tab.
**Depends on:** nothing

### TASK-DA-04 . FRONTEND-AGENT . [READY]
**Goal:** Audit Positions tab. Verify:
- (A) `GET /api/positions` returns live Alpaca data. Currently 0 open positions — verify the empty state message renders ("No open positions. Cash is a position.").
- (B) When positions DO exist, verify the position card layout shows: symbol, qty, avg_entry, current_price, unrealized_pl, unrealized_plpc, market_value. Color-code P&L.
- (C) Verify this tab works correctly when Alpaca API is down (should show error state, not crash).
**Acceptance:** Empty state renders. Position cards render correctly with test data (or verify structure in code). Error handling present.
**Output artifact:** Confirmation of Positions tab status.
**Depends on:** nothing

### TASK-DA-05 . FRONTEND-AGENT . [READY]
**Goal:** Audit Health tab. Verify:
- (A) `GET /api/health/latest` returns the most recent health check run with all check groups. After our path-resolution fixes (check_302, check_1002, port 8000→9090), verify these checks now pass in the dashboard display.
- (B) Health lights flow diagram — verify all 8 groups render in pipeline order (INFRA → DATABASE → CRONS → SIGNALS → TUMBLERS → ENSEMBLE → LOGGING → DASHBOARD). Clicking a light should expand detail.
- (C) "RUN NOW" button — verify it triggers `POST /api/health/run`, polls for results, and updates the display. Verify the poll timeout (40 × 3s = 2 min) is sufficient.
- (D) History strip — verify it shows the last N runs with correct coloring (all green for passes, red for failures).
- (E) 30-second auto-refresh — verify it works and doesn't duplicate DOM elements.
**Acceptance:** Health lights render with latest check data. RUN NOW triggers and displays results. History strip visible. Auto-refresh works.
**Output artifact:** Screenshot of Health tab + list of any fixes.
**Depends on:** nothing

### TASK-DA-06 . FRONTEND-AGENT . [READY]
**Goal:** Audit Replay tab. Verify:
- (A) `GET /api/replay/dates` — currently hardcoded to `profile_name=eq.CONGRESS_MIRROR`. Verify dates populate the date picker. With 908 inference chains, there should be multiple dates available.
- (B) Session selector (morning/midday) — verify both sessions return candidates.
- (C) Candidate grid — verify ticker, score, decision, confidence, shadow_dissent_count render correctly.
- (D) Click a candidate → verify modal opens with: OHLCV candlestick chart (LightweightCharts), tumbler waterfall diagram, shadow comparison table.
- (E) **Note for future:** Replay is hardcoded to CONGRESS_MIRROR. When multi-profile support is added, this will need a profile selector dropdown. Flag this as a future task, don't change it now.
**Acceptance:** Replay dates load. Candidates render. Modal opens with chart + waterfall + shadows. No JS errors.
**Output artifact:** Confirmation of Replay tab status + future profile-selector note.
**Depends on:** nothing

---

## Wave 3 — Ensemble + Performance Tabs (sequential — both touch shadow data)

### TASK-DA-07 . FRONTEND-AGENT . [READY]
**Goal:** Audit Ensemble tab (3 shadow sections + signal feed). Verify:
- (A) Shadow Scoreboard (`/api/shadow/profiles`) — verify all 6 shadow profiles render: SKEPTIC, CONTRARIAN, REGIME_WATCHER, OPTIONS_FLOW, FORM4_INSIDER, KRONOS_TECHNICALS. Each card should show fitness_score, dwm_weight, conditional_brier, times_correct/dissented, last_graded_at. All profiles currently have fitness_score=0.0 (except REGIME_WATCHER at 1.0) — verify this displays correctly.
- (B) Unanimous Dissent (`/api/shadow/unanimous?days=30`) — verify the query works and renders red-bordered cards when all shadows disagree with live. May be empty — verify empty state renders.
- (C) Divergence History (`/api/shadow/divergences?days=30`) — 93 divergences exist (Apr 7-15). Verify the table renders with correct columns and date sorting.
- (D) Signal Sources — Options Flow (`/api/signals/options-flow?days=7`) is empty (0 rows). Form 4 (`/api/signals/form4?days=30`) should have data. Verify both empty states render correctly and fitness bar chart shows all profiles.
**Acceptance:** All 4 sections of Ensemble tab render. 6 shadow profiles visible. Divergence table has data. Empty states handled gracefully.
**Output artifact:** List of fixes applied to Ensemble tab.
**Depends on:** nothing

### TASK-DA-08 . FRONTEND-AGENT . [READY]
**Goal:** Audit Performance tab (shadow P&L tracking). Current state:
- `shadow_performance` table: 0 rows (rollup hasn't produced data yet)
- `shadow_positions`: 9 positions (8 open, 1 closed as of Apr 17)
Verify:
- (A) Leaderboard (`/api/shadow/leaderboard`) — with only 9 positions (mostly open), the leaderboard will be sparse. Verify it renders correctly with minimal data. Verify P&L calculations work for open positions (unrealized based on current_price vs entry_price).
- (B) Weekly chart (`/api/shadow/performance?weeks=12`) — will return empty array. Verify the empty state message renders: "No weekly performance data yet."
- (C) Open positions grid (`/api/shadow/positions?status=open`) — should show the 8 open shadow positions. Verify columns: Ticker, Agent, Entry Date, Entry Price, Current Price, P&L%, Days Held, Divergent. Verify color coding works.
- (D) **Note:** The first rollup will run Sunday Apr 19. After that, weekly chart should start populating. Flag this for post-rollup verification.
**Acceptance:** Leaderboard renders (even with sparse data). Empty weekly chart shows correct message. Open positions grid shows 8 positions with correct columns.
**Output artifact:** Confirmation of Performance tab + post-rollup note.
**Depends on:** nothing

---

## Wave 4 — Economics + How It Works (parallel)

### TASK-DA-09 . FRONTEND-AGENT . [READY]
**Goal:** Audit Economics tab. Verify:
- (A) Summary KPIs (`/api/economics/summary?days=30`) — verify net P&L, trading P&L, costs, ROI render correctly. With -$1,021 in real trades and ~$0.72/day in API costs, verify the numbers make sense.
- (B) Cost breakdown (`/api/economics/breakdown?days=30`) — verify the table shows categories (claude_api, hosting, etc.) with correct totals.
- (C) History chart (`/api/economics/history?days=90`) — verify the canvas chart renders 3 lines (Trading P&L, Costs, Net) with correct data. Chart uses custom canvas rendering (no library) — verify it doesn't error.
- (D) Budget caps (`/api/budget/config`) — verify the editable budget grid renders. Current budgets: daily_claude_budget. Verify clicking to edit works (uses browser `prompt()`). Verify today's spend displays next to each cap.
**Acceptance:** All 4 Economics sub-sections render. Chart draws correctly. Budget caps editable.
**Output artifact:** Confirmation of Economics tab status.
**Depends on:** nothing

### TASK-DA-10 . FRONTEND-AGENT . [READY]
**Goal:** Audit "How It Works" tab for accuracy against current system. This is the most critical alignment task — the tab contains detailed technical descriptions that must match reality. Verify and fix:
- (A) **"What is Parallax?"** section — update any remaining "openclaw-trader" text. Update the description: currently says "39-ticker AI infrastructure watchlist" but the active profile is CONGRESS_MIRROR which watches congressional trades, not a fixed AI watchlist. Rewrite to accurately describe the current system: multi-profile architecture with CONGRESS_MIRROR as active profile, 5-tumbler inference engine, 6-shadow adversarial ensemble.
- (B) **Daily Cron Timeline table** — verify it matches the actual crontab on ridley. Current crons include: system_check 5:00, regime 5:15, ollama_watchdog 5:25/6:30/8:55/12:45, catalyst_ingest 5:30/9:00/12:50, ingest_form4 6:00, scanner 6:35/9:30, ingest_options 7:00, position_manager every 30m 6-12:45, shadow_opener 7:15/10:30, meta_daily 13:30, daily_report 14:00, shadow_mtm 18:00. Update the table to match.
- (C) **Scanner Pipeline / T1-T5 tumbler chain** — verify descriptions match current inference_engine.py. Key facts: T1 is technical scoring, T2 is fundamental+sentiment, T3 is flow/cross-asset via Ollama qwen2.5:3b, T4 is pattern template matching via Claude Haiku (now has 24 seeded templates), T5 is counterfactual synthesis via Claude Sonnet (has never fired — note this). Confidence thresholds: 0.25, 0.40, 0.55, 0.65, calibrated. Decision thresholds: strong_enter >=0.75, enter >=0.60, watch >=0.45, skip >=0.20, veto <0.20.
- (D) **Adversarial Ensemble** — verify the 6 shadow agent descriptions match shadow_profiles.py. Verify budget gate tiers are accurate. Verify DWM formula description matches calibrator.py.
- (E) **Calibration Loop** — verify steps match actual calibrator.py weekly flow.
- (F) **Meta Daily Reflection** — verify description matches meta_analysis.py daily flow.
- (G) **Infrastructure section** — update to reflect current stack: ridley (Jetson Orin Nano 8GB), motherbrain (orchestrator), Supabase, Alpaca paper, Fly.io (auto-stop dashboard + log shipper), Ollama qwen2.5:3b + nomic-embed-text, Claude API (Haiku for T4, Sonnet for T5 + meta).
- (H) **Circuit Breakers** — verify the list matches scanner.py's actual circuit breakers (consecutive losses, daily drawdown). Remove any that don't exist.
- (I) **Workflow widget** — the 20-step interactive diagram. Verify step descriptions match reality. If any steps describe deprecated features (e.g., removed tabs, deleted scripts), update them.
**Acceptance:** Every description in How It Works matches the actual codebase. No references to deprecated features, wrong watchlist sizes, or incorrect architecture. Cron timeline matches ridley's crontab.
**Output artifact:** Detailed list of all text changes made in How It Works tab.
**Depends on:** nothing

---

## Wave 5 — Telemetry Sidebar + Integration Test

### TASK-DA-11 . FRONTEND-AGENT . [READY]
**Goal:** Audit the telemetry sidebar (right-side persistent panel). Verify:
- (A) NYSE latency (`/api/health/latency`) — verify the gauge renders with color thresholds (<50ms green, <120ms cyan, <250ms yellow, 250+ red).
- (B) Supabase latency (timed fetch to `/api/pipeline/health`) — verify the gauge renders.
- (C) Market status — verify the client-side ET calculation correctly shows OPEN/PRE-MARKET/AFTER-HOURS/CLOSED with correct times and colors.
- (D) Claude budget (`/api/budget/config`) — verify it shows today's spend vs daily cap.
- (E) Pipeline health score — verify it shows the same score as Pipeline tab.
- (F) 15-second auto-refresh — verify `setInterval(updateTelemetry, 15000)` works without accumulating DOM elements.
**Acceptance:** All 5 telemetry widgets render. Auto-refresh works. No DOM accumulation.
**Output artifact:** Confirmation of telemetry sidebar status.
**Depends on:** nothing

### TASK-DA-12 . PICARD . [BLOCKED: TASK-DA-01 through TASK-DA-11]
**Goal:** Final integration verification. For each of the 10 tabs + header KPIs:
1. Load the dashboard on ridley:9090
2. Click through every tab
3. Verify no JS console errors
4. Verify all data matches what Supabase returns
5. Verify auto-refresh timers work (Dashboard 60s, Health 30s, Telemetry 15s)
6. Take a screenshot of each tab
7. Deploy latest to Fly.io
8. Verify Fly.io dashboard matches ridley
9. Post completion summary to Slack
**Acceptance:** All 10 tabs functional. Zero JS errors. Fly.io deployed and working. Slack summary posted.
**Output artifact:** Per-tab screenshots + completion summary in PROGRESS.md.
**Depends on:** All DA tasks

---

## Completed — Prior Sessions

### Parallax Rebrand (2026-04-18): Visual rename only . [DONE]
### Tumbler Calibration (2026-04-18): keep_alive + pattern templates + regime restart . [DONE]
### Ollama Watchdog (2026-04-17): 4x daily auto-restart + compact_memory . [DONE]
### Dashboard Data Audit (2026-04-16): health check fixes + staleness indicators . [DONE]
### Shadow P&L Tracker (2026-04-14): TASK-SP-01 through TASK-SP-06 . [DONE]
### Streamline Consolidation (2026-04-13): TASK-SLIM-01 through TASK-SLIM-09 . [DONE]
### Pre-Launch Remediation (2026-04-11/12): TASK-FIX-01 through TASK-FIX-17 . [DONE]
### Workflow AI Assistant (2026-04-11): TASK-WF-01 through TASK-WF-05 . [DONE]
### Kronos Shadow Agent (2026-04-09/10): TASK-K00 through TASK-K06 . [DONE]
### Full Preflight Coverage + Mission Readiness (2026-04-09): TASK-PF-01 through TASK-PF-03 + Group Q . [DONE]
### Optimization Audit (2026-04-08): TASK-OPT-01 through TASK-OPT-11 . [DONE]
### System Simulator + Enhanced Health Check (2026-04-07): TASK-SIM-01 through TASK-SIM-10 . [DONE]
### Health Monitor + Signal Diversification (2026-04-07): TASK-HM/SD-01 through TASK-INT-01 . [DONE]
