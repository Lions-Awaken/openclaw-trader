# Database Separation: openclaw-trader from Twilight Underground

## Background

openclaw-trader tables were accidentally migrated to both:
- **openclaw-trader** Supabase (`vpollvsbtushbiapoflr`) — the correct database, has real data
- **Twilight Underground** Supabase (`uupmzaglafeiakamefit`) — duplicate tables, all empty (0 rows)

## What These Scripts Do

### `01_cleanup_tu_database.sql`
Drops the empty openclaw tables, RAG functions, and pg_cron jobs from the
Twilight Underground database. Only affects tables with 0 rows — the script
includes safety checks that abort if any table has data.

### `02_rollback_tu_database.sql`
Recreates everything that `01_cleanup_tu_database.sql` dropped.
Use this if anything breaks after cleanup.

## How to Run

### Forward (cleanup):
```bash
# Review first, then run against TU database
psql "$TU_DATABASE_URL" -f 01_cleanup_tu_database.sql
```

### Rollback:
```bash
# Recreates all dropped objects in TU database
psql "$TU_DATABASE_URL" -f 02_rollback_tu_database.sql
```

## Verification

After running cleanup, verify:
1. TU app (`tu-streamsaber`) still works — it should not use any of these tables
2. openclaw-trader dashboard still works — it should point to `vpollvsbtushbiapoflr`
3. All cron scripts still work — they should point to `vpollvsbtushbiapoflr`

## Tables Removed from TU

| Table | Rows in TU | Rows in openclaw DB |
|-------|-----------|-------------------|
| `pipeline_runs` | 0 | 0 |
| `order_events` | 0 | 0 |
| `data_quality_checks` | 0 | 0 |
| `signal_evaluations` | 0 | 0 |
| `meta_reflections` | 0 | 0 |
| `strategy_adjustments` | 0 | 0 |
| `catalyst_events` | 0 | 0 |
| `pattern_templates` | 0 | 0 |
| `inference_chains` | 0 | 0 |
| `cost_ledger` | 0 | 0 |
| `budget_config` | 2 | 2 |
| `confidence_calibration` | 0 | 0 |
| `tuning_profiles` | 1 | 1 |
| `tuning_telemetry` | 0 | 0 |
| `stack_heartbeats` | 2 | 2 |
| `magic_link_tokens` | 0 | 1 |
| `strategy_profiles` | 2 | 2 |
