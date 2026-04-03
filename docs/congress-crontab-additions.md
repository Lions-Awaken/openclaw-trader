# Congress Mirror Crontab Additions

Add these entries to ridley's crontab (`crontab -e` on ridley).
Ridley is in PDT. All times below are PDT (crontab native).

## Legislative Calendar Refresh

Runs Sunday 3:00 PM PDT (6:00 PM ET). Before meta_weekly (4:00 PM PDT).

```
# Legislative calendar refresh -- Sunday 6:00 PM ET (15:00 PDT)
0 15 * * 0  source ~/.bashrc && cd ~/openclaw-trader && python scripts/legislative_calendar.py >> /tmp/legislative_calendar.log 2>&1
```

## Politician Intel Refresh

Runs first Sunday of each month at 2:00 PM PDT (5:00 PM ET). Refreshes signal scores.

```
# Politician intel refresh -- 1st Sunday of month 5:00 PM ET (14:00 PDT)
0 14 1-7 * 0  source ~/.bashrc && cd ~/openclaw-trader && python scripts/seed_politician_intel.py >> /tmp/seed_politician.log 2>&1
```

## Full Crontab Context (existing + new)

```
# Existing schedules (PDT on ridley):
# 5:30  M-F  catalyst_ingest.py (8:30 AM ET)
# 6:35  M-F  scanner.py         (9:35 AM ET)
# 6:00-12:45 M-F position_manager.py every 30m (9:00 AM-3:45 PM ET)
# 9:15  M-F  catalyst_ingest.py (12:15 PM ET)
# 9:30  M-F  scanner.py         (12:30 PM ET)
# 12:50 M-F  catalyst_ingest.py (3:50 PM ET)
# 13:30 M-F  meta_daily.py      (4:30 PM ET)
# 16:00 Sun  meta_weekly.py     (7:00 PM ET)
# 16:30 Sun  calibrator.py      (7:30 PM ET)

# NEW:
# 15:00 Sun  legislative_calendar.py  (6:00 PM ET)
# 14:00 1st-Sun  seed_politician_intel.py (5:00 PM ET)
```
