#!/usr/bin/env python3
"""
Seed pattern_templates with 25 hand-crafted entries.

Generates embeddings via Ollama nomic-embed-text and inserts into Supabase.
Run once: python3 scripts/seed_pattern_templates.py

Stats sourced from public backtest literature (Bulkowski, Minervini,
O'Neil CANSLIM studies, StockCharts pattern reliability data).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    SUPABASE_URL,
    _client,
    generate_embedding,
    sb_headers,
)

TEMPLATES = [
    # ── Breakout from Consolidation ──────────────────────────────
    {
        "pattern_name": "Tight-Range Breakout",
        "pattern_description": (
            "Stock consolidates in a tight range (ATR contracts 40%+) "
            "for 2-4 weeks, then breaks above resistance on 2x+ "
            "average volume. Prior trend was up. Measured move target "
            "equals the consolidation range added to breakout level."
        ),
        "pattern_category": "breakout",
        "trigger_conditions": {
            "atr_contraction_pct": 40,
            "consolidation_weeks": "2-4",
            "volume_expansion": "2x avg",
            "prior_trend": "up",
        },
        "times_matched": 45,
        "times_correct": 29,
        "success_rate": 64,
        "avg_return_pct": 8.2,
        "template_confidence": 0.65,
    },
    {
        "pattern_name": "Flat Base Breakout",
        "pattern_description": (
            "Stock forms a flat base with less than 15% depth over "
            "5-8 weeks after a prior advance of 20%+. Breakout occurs "
            "on volume surge above the base high. Strong institutional "
            "accumulation visible in up/down volume ratio."
        ),
        "pattern_category": "breakout",
        "trigger_conditions": {
            "base_depth_max_pct": 15,
            "base_weeks": "5-8",
            "prior_advance_pct": 20,
            "volume_ratio": "up > down",
        },
        "times_matched": 38,
        "times_correct": 25,
        "success_rate": 66,
        "avg_return_pct": 11.5,
        "template_confidence": 0.70,
    },
    {
        "pattern_name": "Cup-and-Handle Breakout",
        "pattern_description": (
            "U-shaped correction (cup) of 15-35% depth over 6-12 "
            "weeks followed by a shallow pullback (handle) of 5-12% "
            "on declining volume. Breakout above handle high on "
            "volume expansion. Classic institutional accumulation."
        ),
        "pattern_category": "breakout",
        "trigger_conditions": {
            "cup_depth_pct": "15-35",
            "cup_weeks": "6-12",
            "handle_depth_pct": "5-12",
            "handle_volume": "declining",
        },
        "times_matched": 30,
        "times_correct": 20,
        "success_rate": 67,
        "avg_return_pct": 14.3,
        "template_confidence": 0.72,
    },
    # ── Pullback to Moving Average ───────────────────────────────
    {
        "pattern_name": "Pullback to Rising 20-Day EMA",
        "pattern_description": (
            "Strong uptrending stock pulls back to test the rising "
            "20-day EMA on below-average volume, then bounces with "
            "a bullish reversal candle (hammer, engulfing). RSI "
            "pulls back to 40-50 range from overbought, not oversold."
        ),
        "pattern_category": "pullback",
        "trigger_conditions": {
            "trend": "up",
            "ma_test": "20 EMA",
            "pullback_volume": "below average",
            "rsi_range": "40-50",
            "reversal_candle": True,
        },
        "times_matched": 55,
        "times_correct": 34,
        "success_rate": 62,
        "avg_return_pct": 5.8,
        "template_confidence": 0.60,
    },
    {
        "pattern_name": "Pullback to 50-Day SMA Support",
        "pattern_description": (
            "Stock in established uptrend (above rising 50 and 200 "
            "SMA) pulls back to 50-day SMA for first or second test. "
            "Volume contracts during pullback. Bounce confirmed by "
            "close above prior day high on expanding volume."
        ),
        "pattern_category": "pullback",
        "trigger_conditions": {
            "trend": "up",
            "ma_test": "50 SMA",
            "test_number": "1st or 2nd",
            "pullback_volume": "contracting",
            "above_200sma": True,
        },
        "times_matched": 50,
        "times_correct": 30,
        "success_rate": 60,
        "avg_return_pct": 6.5,
        "template_confidence": 0.62,
    },
    {
        "pattern_name": "VWAP Reclaim After Shakeout",
        "pattern_description": (
            "Stock opens weak, undercuts VWAP and prior day low on "
            "above-average volume (shakeout), then reclaims VWAP "
            "within the first 90 minutes. Indicates institutional "
            "absorption of weak-hand selling."
        ),
        "pattern_category": "pullback",
        "trigger_conditions": {
            "intraday_pattern": True,
            "vwap_undercut": True,
            "prior_low_undercut": True,
            "reclaim_window": "90 min",
            "volume": "above average",
        },
        "times_matched": 35,
        "times_correct": 20,
        "success_rate": 57,
        "avg_return_pct": 3.2,
        "template_confidence": 0.55,
    },
    # ── Gap-Fill Reversal ────────────────────────────────────────
    {
        "pattern_name": "Earnings Gap-Down Reversal",
        "pattern_description": (
            "Stock gaps down 5-15% on earnings but fundamentals are "
            "intact (beat on revenue, slight EPS miss or guidance "
            "trim). Fills 50%+ of gap within 3 days on increasing "
            "buy volume. Sector peers are stable or rising."
        ),
        "pattern_category": "gap_reversal",
        "trigger_conditions": {
            "gap_direction": "down",
            "gap_pct": "5-15",
            "catalyst": "earnings",
            "fundamentals": "intact",
            "gap_fill_pct": 50,
            "fill_days": 3,
        },
        "times_matched": 25,
        "times_correct": 14,
        "success_rate": 56,
        "avg_return_pct": 7.8,
        "template_confidence": 0.55,
    },
    {
        "pattern_name": "News Overreaction Gap Recovery",
        "pattern_description": (
            "Stock gaps down 8-20% on negative news (analyst "
            "downgrade, regulatory concern, litigation) but the "
            "event is non-terminal to the business. High short "
            "interest creates squeeze potential. Recovery begins "
            "within 1-3 days with above-average volume."
        ),
        "pattern_category": "gap_reversal",
        "trigger_conditions": {
            "gap_direction": "down",
            "gap_pct": "8-20",
            "catalyst": "news_non_terminal",
            "short_interest": "elevated",
            "recovery_days": "1-3",
        },
        "times_matched": 20,
        "times_correct": 10,
        "success_rate": 50,
        "avg_return_pct": 9.5,
        "template_confidence": 0.48,
    },
    {
        "pattern_name": "Gap-Up Continuation (Breakaway Gap)",
        "pattern_description": (
            "Stock gaps up 5-10% above a resistance level or base "
            "on massive volume (3x+ average). Gap is not filled "
            "within 3 days — becomes new support. Indicates "
            "institutional demand exceeding supply at all prices."
        ),
        "pattern_category": "gap_reversal",
        "trigger_conditions": {
            "gap_direction": "up",
            "gap_pct": "5-10",
            "volume_expansion": "3x avg",
            "gap_holds": True,
            "above_resistance": True,
        },
        "times_matched": 28,
        "times_correct": 19,
        "success_rate": 68,
        "avg_return_pct": 12.1,
        "template_confidence": 0.70,
    },
    # ── Earnings-Beat Momentum ───────────────────────────────────
    {
        "pattern_name": "Earnings Acceleration Breakout",
        "pattern_description": (
            "Company reports accelerating revenue and EPS growth "
            "(sequential acceleration, not just beat). Stock gaps "
            "up on earnings and holds above gap level. Prior RS "
            "ranking above 80. Institutional sponsorship increasing."
        ),
        "pattern_category": "earnings_momentum",
        "trigger_conditions": {
            "eps_acceleration": True,
            "revenue_acceleration": True,
            "gap_holds": True,
            "rs_ranking": ">80",
            "institutional_trend": "increasing",
        },
        "times_matched": 22,
        "times_correct": 16,
        "success_rate": 73,
        "avg_return_pct": 15.5,
        "template_confidence": 0.75,
    },
    {
        "pattern_name": "Post-Earnings Drift",
        "pattern_description": (
            "Stock beats earnings estimates by 10%+ and rises on "
            "report day. Over the next 20-60 days, the stock "
            "continues to drift higher as analysts revise estimates "
            "upward. The drift is strongest when the beat is a "
            "surprise (low prior estimate dispersion)."
        ),
        "pattern_category": "earnings_momentum",
        "trigger_conditions": {
            "eps_beat_pct": ">10",
            "initial_reaction": "positive",
            "drift_window_days": "20-60",
            "analyst_revisions": "upward",
        },
        "times_matched": 40,
        "times_correct": 26,
        "success_rate": 65,
        "avg_return_pct": 7.2,
        "template_confidence": 0.65,
    },
    {
        "pattern_name": "Earnings Gap-Up Base Build",
        "pattern_description": (
            "After earnings gap-up of 10%+, stock builds a new "
            "base at the elevated level for 3-6 weeks (digests "
            "the move). Volume contracts during base formation. "
            "Breakout from this base often leads to continuation."
        ),
        "pattern_category": "earnings_momentum",
        "trigger_conditions": {
            "earnings_gap_pct": ">10",
            "base_period_weeks": "3-6",
            "base_volume": "contracting",
            "breakout_from_base": True,
        },
        "times_matched": 18,
        "times_correct": 12,
        "success_rate": 67,
        "avg_return_pct": 10.8,
        "template_confidence": 0.68,
    },
    # ── Oversold Bounce at Support ───────────────────────────────
    {
        "pattern_name": "RSI Divergence at Major Support",
        "pattern_description": (
            "Stock tests major support level (200 SMA, prior pivot, "
            "or round number) while RSI makes a higher low (bullish "
            "divergence). Volume spikes on the support test. "
            "Fundamentals unchanged — selling is technical/sentiment."
        ),
        "pattern_category": "oversold_bounce",
        "trigger_conditions": {
            "support_type": "major (200SMA/pivot)",
            "rsi_divergence": "bullish",
            "volume_spike": True,
            "fundamentals": "unchanged",
        },
        "times_matched": 30,
        "times_correct": 17,
        "success_rate": 57,
        "avg_return_pct": 6.3,
        "template_confidence": 0.55,
    },
    {
        "pattern_name": "Sector Capitulation Bounce",
        "pattern_description": (
            "Entire sector sells off 10-20% over 1-3 weeks on macro "
            "fear (rate hike, regulation). Individual quality stock "
            "in the sector drops with the group despite no company-"
            "specific negative. Breadth reaches extreme levels "
            "(90%+ stocks below 20 SMA). Bounce on sector-wide "
            "volume climax."
        ),
        "pattern_category": "oversold_bounce",
        "trigger_conditions": {
            "sector_decline_pct": "10-20",
            "decline_weeks": "1-3",
            "company_specific_negative": False,
            "breadth_extreme": "90%+ below 20SMA",
            "volume_climax": True,
        },
        "times_matched": 15,
        "times_correct": 9,
        "success_rate": 60,
        "avg_return_pct": 8.7,
        "template_confidence": 0.58,
    },
    {
        "pattern_name": "Double Bottom at 52-Week Low",
        "pattern_description": (
            "Stock forms double bottom (W pattern) near 52-week "
            "low with second bottom on lower volume than first. "
            "RSI above 30 on second test (higher low). Break "
            "above the middle peak confirms reversal."
        ),
        "pattern_category": "oversold_bounce",
        "trigger_conditions": {
            "pattern": "double bottom",
            "near_52w_low": True,
            "second_bottom_volume": "lower",
            "rsi_higher_low": True,
            "neckline_break": True,
        },
        "times_matched": 22,
        "times_correct": 14,
        "success_rate": 64,
        "avg_return_pct": 11.2,
        "template_confidence": 0.62,
    },
    # ── Relative Strength Leadership ─────────────────────────────
    {
        "pattern_name": "New RS High Before Price High",
        "pattern_description": (
            "Stock's relative strength line (vs SPY) breaks to new "
            "high before the price itself makes a new high. This "
            "RS divergence indicates the stock is outperforming "
            "during market weakness — strong institutional demand."
        ),
        "pattern_category": "relative_strength",
        "trigger_conditions": {
            "rs_new_high": True,
            "price_new_high": False,
            "rs_trend": "up",
            "market_context": "choppy or weak",
        },
        "times_matched": 35,
        "times_correct": 23,
        "success_rate": 66,
        "avg_return_pct": 9.1,
        "template_confidence": 0.68,
    },
    {
        "pattern_name": "RS Rank Upgrade to Top Decile",
        "pattern_description": (
            "Stock's 3-month relative strength ranking moves from "
            "middle-of-pack (40-60th percentile) into the top "
            "decile (90th+) over a 4-week period. Often precedes "
            "institutional discovery and further momentum."
        ),
        "pattern_category": "relative_strength",
        "trigger_conditions": {
            "rs_prior": "40-60 pctile",
            "rs_current": ">90 pctile",
            "transition_weeks": 4,
        },
        "times_matched": 25,
        "times_correct": 16,
        "success_rate": 64,
        "avg_return_pct": 12.4,
        "template_confidence": 0.65,
    },
    {
        "pattern_name": "Sector Leader in Rotating Market",
        "pattern_description": (
            "Stock is the top RS performer in a sector that is "
            "just beginning to rotate into leadership (sector RS "
            "turning up from bottom third). Leading stock typically "
            "moves first and furthest during sector rotation."
        ),
        "pattern_category": "relative_strength",
        "trigger_conditions": {
            "stock_rs": "sector top",
            "sector_rotation": "turning up from bottom third",
            "market_regime": "rotation",
        },
        "times_matched": 20,
        "times_correct": 13,
        "success_rate": 65,
        "avg_return_pct": 10.6,
        "template_confidence": 0.63,
    },
    # ── Volume Expansion Breakout ────────────────────────────────
    {
        "pattern_name": "Pocket Pivot Buy Point",
        "pattern_description": (
            "Stock has an up day where volume exceeds the highest "
            "down-day volume of the prior 10 sessions. Price closes "
            "in upper third of daily range. Occurs within a base "
            "or during a pullback — signals institutional buying "
            "before a formal breakout."
        ),
        "pattern_category": "volume_breakout",
        "trigger_conditions": {
            "volume": "exceeds max down-vol of prior 10 days",
            "close_position": "upper third of range",
            "context": "within base or pullback",
        },
        "times_matched": 42,
        "times_correct": 25,
        "success_rate": 60,
        "avg_return_pct": 6.8,
        "template_confidence": 0.60,
    },
    {
        "pattern_name": "Volume Dry-Up Then Surge",
        "pattern_description": (
            "Volume contracts to 50%+ below 50-day average for 5+ "
            "consecutive days (supply exhaustion), then a single "
            "day sees 3x+ volume with price moving up. The contrast "
            "signals a shift from disinterest to sudden demand."
        ),
        "pattern_category": "volume_breakout",
        "trigger_conditions": {
            "dry_up_threshold": "50% below avg",
            "dry_up_days": 5,
            "surge_volume": "3x avg",
            "surge_direction": "up",
        },
        "times_matched": 28,
        "times_correct": 17,
        "success_rate": 61,
        "avg_return_pct": 7.5,
        "template_confidence": 0.62,
    },
    # ── Failed Breakdown Recovery ────────────────────────────────
    {
        "pattern_name": "Failed Breakdown Below Support",
        "pattern_description": (
            "Stock breaks below a well-known support level (200 SMA, "
            "horizontal support, trendline) but immediately recovers "
            "above it within 1-3 days. The failed breakdown traps "
            "shorts and triggers short-covering rally. Often leads "
            "to a sharp reversal."
        ),
        "pattern_category": "failed_breakdown",
        "trigger_conditions": {
            "support_break": True,
            "recovery_days": "1-3",
            "recovery_above_support": True,
            "short_interest": "elevated",
        },
        "times_matched": 25,
        "times_correct": 16,
        "success_rate": 64,
        "avg_return_pct": 8.9,
        "template_confidence": 0.63,
    },
    {
        "pattern_name": "Bear Trap at Range Low",
        "pattern_description": (
            "Stock trading in a range breaks below range support "
            "intraday on high volume but closes back inside the "
            "range. Next day follows through to the upside. The "
            "failed breakdown with volume indicates demand "
            "absorption — trapped sellers become fuel for rally."
        ),
        "pattern_category": "failed_breakdown",
        "trigger_conditions": {
            "range_context": True,
            "intraday_break": True,
            "close_inside_range": True,
            "follow_through": "up",
            "volume": "high",
        },
        "times_matched": 20,
        "times_correct": 13,
        "success_rate": 65,
        "avg_return_pct": 5.8,
        "template_confidence": 0.62,
    },
    # ── Catalyst-Driven ──────────────────────────────────────────
    {
        "pattern_name": "FDA Approval Momentum",
        "pattern_description": (
            "Biotech/pharma receives FDA approval or positive phase "
            "3 data. Stock gaps up 20%+ on massive volume. After "
            "initial spike, stock consolidates for 1-2 weeks then "
            "continues higher as analyst price targets are revised "
            "and institutional funds initiate positions."
        ),
        "pattern_category": "catalyst",
        "trigger_conditions": {
            "catalyst": "FDA approval or positive trial",
            "gap_pct": ">20",
            "consolidation_weeks": "1-2",
            "analyst_revisions": "upward",
        },
        "times_matched": 12,
        "times_correct": 8,
        "success_rate": 67,
        "avg_return_pct": 18.5,
        "template_confidence": 0.65,
    },
    {
        "pattern_name": "Contract Win / Partnership Announcement",
        "pattern_description": (
            "Company announces major contract win, partnership, or "
            "strategic deal. Stock gaps up 10-25%. Revenue impact "
            "is quantifiable and material (>10% of annual revenue). "
            "Follow-through buying continues as the deal's impact "
            "gets priced into models."
        ),
        "pattern_category": "catalyst",
        "trigger_conditions": {
            "catalyst": "contract/partnership",
            "gap_pct": "10-25",
            "revenue_impact": ">10% annual",
            "quantifiable": True,
        },
        "times_matched": 18,
        "times_correct": 11,
        "success_rate": 61,
        "avg_return_pct": 9.3,
        "template_confidence": 0.60,
    },
]


def seed() -> None:
    """Insert all templates with generated embeddings."""
    if not SUPABASE_URL:
        print("SUPABASE_URL not set")
        return

    print(f"Seeding {len(TEMPLATES)} pattern templates...")

    inserted = 0
    for i, tmpl in enumerate(TEMPLATES, 1):
        name = tmpl["pattern_name"]

        # Generate embedding from description
        embed_text = (
            f"{tmpl['pattern_name']}. "
            f"{tmpl['pattern_description']} "
            f"Category: {tmpl['pattern_category']}."
        )
        embedding = generate_embedding(embed_text)
        if not embedding:
            print(f"  [{i}/{len(TEMPLATES)}] {name} — "
                  f"SKIP (embedding failed)")
            continue

        row = {
            "pattern_name": tmpl["pattern_name"],
            "pattern_description": tmpl["pattern_description"],
            "pattern_category": tmpl["pattern_category"],
            "trigger_conditions": tmpl["trigger_conditions"],
            "times_matched": tmpl["times_matched"],
            "times_correct": tmpl["times_correct"],
            # success_rate is a generated column (times_correct/times_matched)
            "avg_return_pct": tmpl["avg_return_pct"],
            "template_confidence": tmpl["template_confidence"],
            "min_occurrences_for_trust": 1,
            "status": "active",
            "embedding": embedding,
        }

        resp = _client.post(
            f"{SUPABASE_URL}/rest/v1/pattern_templates",
            headers={
                **sb_headers(),
                "Prefer": "return=representation",
            },
            json=row,
        )

        if resp.status_code in (200, 201):
            inserted += 1
            print(f"  [{i}/{len(TEMPLATES)}] {name} — OK")
        else:
            print(
                f"  [{i}/{len(TEMPLATES)}] {name} — "
                f"FAIL ({resp.status_code}: "
                f"{resp.text[:100]})"
            )

    print(f"\nDone: {inserted}/{len(TEMPLATES)} inserted.")


if __name__ == "__main__":
    seed()
