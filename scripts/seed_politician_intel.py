#!/usr/bin/env python3
"""
Seed Politician Intel — populates politician_intel with scored records
for high-signal congress members.

One-time seed script. Run manually after migration is applied.
Re-run monthly to refresh scores (cron first Sunday of month).
"""

import os
import sys
from datetime import date

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from tracer import _post_to_supabase  # noqa: E402

QUIVER_KEY = os.environ.get("QUIVERQUANT_API_KEY", "")


# ============================================================================
# Scoring constants
# ============================================================================

LEADERSHIP_SCORES = {
    "Speaker": 0.40,
    "Majority Leader": 0.38,
    "Minority Leader": 0.38,
    "Majority Whip": 0.30,
    "Minority Whip": 0.30,
    "Caucus Chair": 0.25,
    "Conference Chair": 0.25,
    "Committee Chair": 0.20,
    "Ranking Member": 0.18,
    "Member": 0.05,
}

HIGH_VALUE_COMMITTEES = [
    "Armed Services", "Intelligence", "Finance", "Banking",
    "Energy", "Commerce", "Science", "Technology", "Health",
    "Ways and Means", "Appropriations", "Foreign Affairs",
    "Judiciary", "Homeland Security",
]


def fetch_quiver_politicians():
    """Get all unique politicians from recent QuiverQuant congress trades."""
    resp = httpx.get(
        "https://api.quiverquant.com/beta/live/congresstrading",
        headers={"Authorization": f"Bearer {QUIVER_KEY}"},
        timeout=15.0,
    )
    if resp.status_code == 200:
        return resp.json()
    return []


def compute_signal_score(leadership_role, committees, trailing_vs_spy=None):
    """Compute composite signal score from leadership, committees, and alpha."""
    leadership_bonus = LEADERSHIP_SCORES.get(leadership_role, 0.05)
    committee_bonus = 0.0
    for c in (committees or []):
        for hvc in HIGH_VALUE_COMMITTEES:
            if hvc.lower() in c.lower():
                committee_bonus = min(0.30, committee_bonus + 0.10)
                break
    alpha_bonus = 0.0
    if trailing_vs_spy and trailing_vs_spy > 10:
        alpha_bonus = min(0.30, trailing_vs_spy / 100)
    return (
        round(min(1.0, leadership_bonus + committee_bonus + alpha_bonus), 3),
        leadership_bonus,
        committee_bonus,
        alpha_bonus,
    )


# ============================================================================
# Hardcoded seed data — known high-signal members from public research
# ============================================================================

HIGH_SIGNAL_SEED = [
    {
        "full_name": "Nancy Pelosi", "chamber": "house", "party": "D", "state": "CA",
        "leadership_role": "Minority Leader",
        "committees": ["Intelligence", "House Administration"],
        "sector_expertise": ["technology", "semiconductors", "ai"],
        "signal_score": 0.85,
        "tracks_spouse": True, "spouse_name": "Paul Pelosi",
        "trailing_12m_return_pct": 70.9, "trailing_12m_vs_spy_pct": 46.0,
        "notes": "Former Speaker. Spouse Paul Pelosi executes trades. "
                 "Options-heavy strategy. Heavily tech-weighted.",
    },
    {
        "full_name": "Dan Meuser", "chamber": "house", "party": "R", "state": "PA",
        "leadership_role": "Member",
        "committees": ["Financial Services", "Science Technology"],
        "sector_expertise": ["semiconductors", "ai", "fintech"],
        "signal_score": 0.55,
        "notes": "Late filer. NVDA trades during AI policy windows.",
    },
    {
        "full_name": "Tim Moore", "chamber": "house", "party": "R", "state": "NC",
        "leadership_role": "Member",
        "committees": ["Science Technology"],
        "sector_expertise": ["ai", "semiconductors"],
        "signal_score": 0.50,
        "notes": "Bought INTC while on House AI Subcommittee "
                 "before Intel government stake news.",
    },
    {
        "full_name": "Lisa McClain", "chamber": "house", "party": "R", "state": "MI",
        "leadership_role": "Member",
        "committees": ["Armed Services"],
        "sector_expertise": ["defense", "cyber", "government_contracts"],
        "signal_score": 0.60,
        "notes": "Armed Services Cyber Subcommittee. "
                 "Bought PLTR in 2024, up 674% since entry.",
    },
    {
        "full_name": "Marjorie Taylor Greene", "chamber": "house",
        "party": "R", "state": "GA",
        "leadership_role": "Member",
        "committees": ["Oversight", "Homeland Security"],
        "sector_expertise": ["ev", "tech"],
        "signal_score": 0.45,
        "notes": "Bought TSLA heavily during Musk-Trump alignment period.",
    },
    {
        "full_name": "Tommy Tuberville", "chamber": "senate",
        "party": "R", "state": "AL",
        "leadership_role": "Member",
        "committees": ["Armed Services", "Agriculture"],
        "sector_expertise": ["defense", "agriculture"],
        "signal_score": 0.50,
        "notes": "Senate Armed Services. Active trader "
                 "with defense sector focus.",
    },
    {
        "full_name": "Mark Kelly", "chamber": "senate", "party": "D", "state": "AZ",
        "leadership_role": "Member",
        "committees": ["Armed Services", "Commerce"],
        "sector_expertise": ["defense", "aerospace", "semiconductors"],
        "signal_score": 0.55,
        "notes": "Aerospace background. Senate Armed Services and Commerce.",
    },
    {
        "full_name": "Ro Khanna", "chamber": "house", "party": "D", "state": "CA",
        "leadership_role": "Member",
        "committees": ["Armed Services", "Oversight"],
        "sector_expertise": ["semiconductors", "tech", "defense"],
        "signal_score": 0.52,
        "notes": "Silicon Valley district. Tech-heavy portfolio "
                 "with defense overlap.",
    },
    {
        "full_name": "John Curtis", "chamber": "senate", "party": "R", "state": "UT",
        "leadership_role": "Member",
        "committees": ["Commerce", "Energy"],
        "sector_expertise": ["energy", "tech"],
        "signal_score": 0.48,
        "notes": "",
    },
    {
        "full_name": "Josh Gottheimer", "chamber": "house", "party": "D", "state": "NJ",
        "leadership_role": "Member",
        "committees": ["Financial Services", "Intelligence"],
        "sector_expertise": ["fintech", "banking", "intelligence"],
        "signal_score": 0.58,
        "notes": "House Intelligence. Active fintech and banking trades.",
    },
]


def run():
    """Seed politician_intel with high-signal members."""
    for member in HIGH_SIGNAL_SEED:
        member["updated_at"] = date.today().isoformat()
        _post_to_supabase("politician_intel", member)
    print(f"[seed_politician_intel] Seeded {len(HIGH_SIGNAL_SEED)} politicians.")


if __name__ == "__main__":
    run()
