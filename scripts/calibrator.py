#!/usr/bin/env python3
"""
Calibrator — Weekly calibration & outcome grading.

Runs Sunday 7:30 PM ET (after meta_weekly).

1. Grade ungraded inference chains against actual trade outcomes
2. Compute calibration factors per confidence bucket
3. Calculate Brier score and calibration error
4. Fill catalyst_events price follow-up data
5. Update pattern template match counts and success rates
6. Store calibration row
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from tracer import PipelineTracer, _patch_supabase, _post_to_supabase, _sb_headers

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ALPACA_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_DATA_BASE = "https://data.alpaca.markets"

# Reusable HTTP client
_client = httpx.Client(timeout=15.0)

TODAY = date.today()  # Reassigned at the start of run()
WEEK_START = ""  # Reassigned at the start of run()


def sb_get(path: str, params: dict | None = None) -> list:
    client = _client
    resp = client.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=_sb_headers(),
        params=params or {},
    )
    if resp.status_code == 200:
        return resp.json()
    return []


def get_trade_outcomes() -> dict[str, dict]:
    """Get trade outcomes indexed by ticker+date for matching."""
    trades = sb_get("trade_decisions", {
        "select": "ticker,action,pnl,outcome,created_at",
        "created_at": f"gte.{WEEK_START}T00:00:00Z",
        "order": "created_at.desc",
    })

    outcomes = {}
    for t in trades:
        ticker = t["ticker"]
        trade_date = t["created_at"][:10]
        pnl = float(t.get("pnl", 0) or 0)
        outcome = t.get("outcome", "")

        key = f"{ticker}_{trade_date}"
        if key not in outcomes:
            outcomes[key] = {"pnl": pnl, "outcome": outcome}

    return outcomes


def grade_chains(trade_outcomes: dict[str, dict]) -> tuple[int, int]:
    """Grade ungraded inference chains by matching to trade outcomes."""
    ungraded = sb_get("inference_chains", {
        "select": "id,ticker,chain_date,final_decision,final_confidence",
        "actual_outcome": "is.null",
        "chain_date": f"gte.{WEEK_START}",
    })

    graded = 0
    total = len(ungraded)

    for chain in ungraded:
        ticker = chain["ticker"]
        chain_date = chain["chain_date"]
        decision = chain["final_decision"]

        # Look for a trade within 5 days of the chain
        matched_outcome = None
        for day_offset in range(6):
            check_date = (datetime.strptime(chain_date, "%Y-%m-%d") + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            key = f"{ticker}_{check_date}"
            if key in trade_outcomes:
                matched_outcome = trade_outcomes[key]
                break

        if matched_outcome:
            # Map outcome
            pnl = matched_outcome["pnl"]
            if pnl > 50:
                actual = "STRONG_WIN"
            elif pnl > 0:
                actual = "WIN"
            elif pnl > -10:
                actual = "SCRATCH"
            elif pnl > -50:
                actual = "LOSS"
            else:
                actual = "STRONG_LOSS"

            _patch_supabase("inference_chains", chain["id"], {
                "actual_outcome": actual,
                "actual_pnl": pnl,
            })
            graded += 1
        elif decision in ("skip", "veto"):
            # Skipped/vetoed — check if we missed a winner
            # For now, mark as SCRATCH (didn't trade)
            _patch_supabase("inference_chains", chain["id"], {
                "actual_outcome": "SCRATCH",
                "actual_pnl": 0,
            })
            graded += 1

    return graded, total


def compute_calibration_buckets() -> tuple[list[dict], dict]:
    """Compute stated vs actual confidence calibration buckets."""
    # Get all graded chains from last 30 days
    cutoff = (TODAY - timedelta(days=30)).isoformat()
    chains = sb_get("inference_chains", {
        "select": "final_confidence,final_decision,actual_outcome,actual_pnl,max_depth_reached",
        "chain_date": f"gte.{cutoff}",
        "actual_outcome": "not.is.null",
    })

    if not chains:
        return [], {}

    # Group by confidence buckets (0-10%, 10-20%, ..., 90-100%)
    buckets_data: dict[str, list] = {}
    for chain in chains:
        conf = float(chain.get("final_confidence", 0))
        bucket = int(conf * 10) * 10  # 0, 10, 20, ..., 90
        bucket_key = str(bucket)

        if bucket_key not in buckets_data:
            buckets_data[bucket_key] = []

        is_win = chain.get("actual_outcome") in ("STRONG_WIN", "WIN")
        buckets_data[bucket_key].append({
            "confidence": conf,
            "is_win": is_win,
            "pnl": float(chain.get("actual_pnl", 0) or 0),
            "depth": chain.get("max_depth_reached", 0),
        })

    # Compute per-bucket stats
    buckets = []
    active_factors = {}

    for bucket_key in sorted(buckets_data.keys()):
        entries = buckets_data[bucket_key]
        count = len(entries)
        if count == 0:
            continue

        stated_avg = sum(e["confidence"] for e in entries) / count
        actual_win_rate = sum(1 for e in entries if e["is_win"]) / count
        overconfident = stated_avg > actual_win_rate

        # Calibration factor: actual / stated (clamped)
        if stated_avg > 0:
            calibration_factor = round(min(1.5, max(0.5, actual_win_rate / stated_avg)), 3)
        else:
            calibration_factor = 1.0

        active_factors[bucket_key] = calibration_factor

        buckets.append({
            "bucket": f"{bucket_key}-{int(bucket_key) + 10}%",
            "stated_avg": round(stated_avg, 3),
            "actual_win_rate": round(actual_win_rate, 3),
            "count": count,
            "calibration_factor": calibration_factor,
            "overconfident": overconfident,
        })

    # Compute depth factors
    depth_data: dict[int, list] = {}
    for chain in chains:
        depth = chain.get("max_depth_reached", 0)
        if depth not in depth_data:
            depth_data[depth] = []
        is_win = chain.get("actual_outcome") in ("STRONG_WIN", "WIN")
        depth_data[depth].append({
            "confidence": float(chain.get("final_confidence", 0)),
            "is_win": is_win,
        })

    depth_factors = {}
    for depth, entries in sorted(depth_data.items()):
        count = len(entries)
        avg_conf = sum(e["confidence"] for e in entries) / count
        actual_rate = sum(1 for e in entries if e["is_win"]) / count
        depth_factors[f"depth_{depth}"] = {
            "avg_confidence": round(avg_conf, 3),
            "actual_win_rate": round(actual_rate, 3),
            "count": count,
        }

    return buckets, {"active_factors": active_factors, "depth_factors": depth_factors}


def compute_brier_score(chains: list[dict]) -> tuple[float, float, float]:
    """Compute Brier score, calibration error, and overconfidence bias."""
    if not chains:
        return 0.0, 0.0, 0.0

    brier_sum = 0.0
    cal_error_sum = 0.0
    overconfidence_sum = 0.0

    for chain in chains:
        conf = float(chain.get("final_confidence", 0))
        is_win = 1.0 if chain.get("actual_outcome") in ("STRONG_WIN", "WIN") else 0.0

        # Brier score: (predicted - actual)^2
        brier_sum += (conf - is_win) ** 2
        cal_error_sum += abs(conf - is_win)
        overconfidence_sum += conf - is_win

    n = len(chains)
    return (
        round(brier_sum / n, 4),
        round(cal_error_sum / n, 4),
        round(overconfidence_sum / n, 4),
    )


def fill_catalyst_prices() -> int:
    """Fill price follow-up data for catalyst events that are old enough."""
    # Get catalysts from 1-5 days ago that don't have prices filled
    one_day_ago = (TODAY - timedelta(days=1)).isoformat()

    catalysts = sb_get("catalyst_events", {
        "select": "id,ticker,event_time,price_at_event",
        "event_time": f"lte.{one_day_ago}T23:59:59Z",
        "price_1d_after": "is.null",
        "ticker": "not.is.null",
        "limit": "50",
    })

    updated = 0
    for cat in catalysts:
        ticker = cat.get("ticker")
        if not ticker:
            continue

        event_time = cat.get("event_time", "")
        if not event_time:
            continue

        try:
            event_date = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        # Fetch historical bars from Alpaca
        prices = _get_price_history(ticker, event_date)
        if not prices:
            continue

        update_data = {}
        if prices.get("at_event"):
            update_data["price_at_event"] = prices["at_event"]
        if prices.get("1d_after"):
            update_data["price_1d_after"] = prices["1d_after"]
        if prices.get("5d_after"):
            update_data["price_5d_after"] = prices["5d_after"]

        # Compute impact
        if prices.get("at_event") and prices.get("1d_after"):
            impact = (prices["1d_after"] - prices["at_event"]) / prices["at_event"] * 100
            update_data["actual_impact_pct"] = round(impact, 3)

        if update_data:
            _patch_supabase("catalyst_events", cat["id"], update_data)
            updated += 1

    return updated


def _get_price_history(ticker: str, event_date: datetime) -> dict:
    """Fetch historical prices around event date from Alpaca."""
    if not ALPACA_KEY or not ALPACA_SECRET:
        return {}

    try:
        start = event_date.strftime("%Y-%m-%d")
        end = (event_date + timedelta(days=7)).strftime("%Y-%m-%d")

        client = _client
        resp = client.get(
            f"{ALPACA_DATA_BASE}/v2/stocks/{ticker}/bars",
            headers={
                "APCA-API-KEY-ID": ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            params={
                "start": start,
                "end": end,
                "timeframe": "1Day",
                "limit": 10,
            },
        )
        if resp.status_code == 200:
            bars = resp.json().get("bars", [])
            if not bars:
                return {}

            prices = {}
            if len(bars) >= 1:
                prices["at_event"] = float(bars[0].get("c", 0))  # Close on event day
            if len(bars) >= 2:
                prices["1d_after"] = float(bars[1].get("c", 0))
            if len(bars) >= 5:
                prices["5d_after"] = float(bars[4].get("c", 0))
            elif len(bars) >= 3:
                prices["5d_after"] = float(bars[-1].get("c", 0))

            return prices
    except Exception as e:
        print(f"[calibrator] Price history error for {ticker}: {e}")

    return {}


def update_pattern_templates() -> int:
    """Update pattern template match counts based on graded chains."""
    # Get chains from last 30 days that matched patterns
    cutoff = (TODAY - timedelta(days=30)).isoformat()
    chains = sb_get("inference_chains", {
        "select": "pattern_template_ids,actual_outcome,actual_pnl",
        "chain_date": f"gte.{cutoff}",
        "actual_outcome": "not.is.null",
    })

    # Aggregate per pattern template
    pattern_stats: dict[str, dict] = {}
    for chain in chains:
        pattern_ids = chain.get("pattern_template_ids", [])
        if not pattern_ids:
            continue
        outcome = chain.get("actual_outcome", "")
        pnl = float(chain.get("actual_pnl", 0) or 0)
        is_correct = outcome in ("STRONG_WIN", "WIN")

        for pid in pattern_ids:
            if not pid:
                continue
            if pid not in pattern_stats:
                pattern_stats[pid] = {"matched": 0, "correct": 0, "total_return": 0.0}
            pattern_stats[pid]["matched"] += 1
            if is_correct:
                pattern_stats[pid]["correct"] += 1
            pattern_stats[pid]["total_return"] += pnl

    # Update each pattern template
    updated = 0
    for pid, stats in pattern_stats.items():
        avg_return = stats["total_return"] / stats["matched"] if stats["matched"] > 0 else 0
        template_conf = stats["correct"] / stats["matched"] if stats["matched"] > 0 else 0

        _patch_supabase("pattern_templates", pid, {
            "times_matched": stats["matched"],
            "times_correct": stats["correct"],
            "avg_return_pct": round(avg_return, 3),
            "template_confidence": round(template_conf, 4),
            "last_matched_at": datetime.now(timezone.utc).isoformat(),
        })
        updated += 1

    return updated


def run():
    global TODAY, WEEK_START
    TODAY = date.today()
    WEEK_START = (TODAY - timedelta(days=7)).isoformat()

    tracer = PipelineTracer("calibrator", metadata={"week_start": WEEK_START})

    try:
        # Step 1: Get trade outcomes
        with tracer.step("get_trade_outcomes") as result:
            trade_outcomes = get_trade_outcomes()
            result.set({"outcomes": len(trade_outcomes)})

        # Step 2: Grade ungraded chains
        with tracer.step("grade_chains") as result:
            graded, total = grade_chains(trade_outcomes)
            result.set({"graded": graded, "total_ungraded": total})

        # Step 3: Compute calibration buckets
        with tracer.step("compute_calibration") as result:
            buckets, factors = compute_calibration_buckets()
            result.set({"buckets": len(buckets)})

        # Step 4: Compute Brier score
        with tracer.step("compute_brier") as result:
            cutoff = (TODAY - timedelta(days=30)).isoformat()
            all_graded = sb_get("inference_chains", {
                "select": "final_confidence,actual_outcome",
                "chain_date": f"gte.{cutoff}",
                "actual_outcome": "not.is.null",
            })
            brier, cal_error, overconfidence = compute_brier_score(all_graded)
            result.set({"brier": brier, "cal_error": cal_error, "overconfidence": overconfidence, "graded_count": len(all_graded)})

        # Step 5: Store calibration row
        with tracer.step("store_calibration") as result:
            cal_data = {
                "calibration_week": TODAY.isoformat(),
                "buckets": buckets,
                "total_predictions": len(all_graded),
                "total_graded": graded,
                "brier_score": brier,
                "calibration_error": cal_error,
                "overconfidence_bias": overconfidence,
                "active_factors": factors.get("active_factors", {}),
                "depth_factors": factors.get("depth_factors", {}),
                "pipeline_run_id": tracer.root_id,
            }
            stored = _post_to_supabase("confidence_calibration", cal_data)
            result.set({"stored": stored is not None})

        # Step 6: Fill catalyst event prices
        with tracer.step("fill_catalyst_prices") as result:
            updated_catalysts = fill_catalyst_prices()
            result.set({"updated": updated_catalysts})

        # Step 7: Update pattern templates
        with tracer.step("update_pattern_templates") as result:
            updated_patterns = update_pattern_templates()
            result.set({"updated": updated_patterns})

        tracer.complete({
            "graded": graded,
            "brier_score": brier,
            "calibration_error": cal_error,
            "overconfidence_bias": overconfidence,
            "catalysts_updated": updated_catalysts,
            "patterns_updated": updated_patterns,
        })
        print(
            f"[calibrator] Complete. Graded: {graded}/{total}. "
            f"Brier: {brier:.4f}. Cal error: {cal_error:.4f}. "
            f"Overconfidence: {overconfidence:.4f}"
        )

    except Exception as e:
        tracer.fail(str(e))
        print(f"[calibrator] Failed: {e}")
        raise


if __name__ == "__main__":
    run()
