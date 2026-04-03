#!/usr/bin/env python3
"""
Legislative Calendar — fetches upcoming House/Senate votes and committee hearings.
Runs Sunday 6 PM ET. Stores events in legislative_calendar for T2 tumbler context.
Sources: Congress.gov API, Perplexity for enrichment.
"""

import json
import os
import sys
from datetime import date

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from common import PERPLEXITY_KEY  # noqa: E402
from tracer import PipelineTracer, _post_to_supabase  # noqa: E402

CONGRESS_API_KEY = os.environ.get("CONGRESS_API_KEY", "")  # free from api.congress.gov

SECTOR_KEYWORDS = {
    "semiconductors": [
        "chip", "semiconductor", "CHIPS Act", "fab", "wafer",
        "nvidia", "intel", "amd",
    ],
    "defense": [
        "defense", "military", "NDAA", "armed services", "weapon",
        "cyber", "pentagon",
    ],
    "healthcare": [
        "health", "pharma", "FDA", "drug", "medicare", "medicaid",
        "biotech",
    ],
    "energy": [
        "energy", "oil", "gas", "renewable", "solar", "nuclear",
        "grid", "pipeline",
    ],
    "ai": [
        "artificial intelligence", "AI", "machine learning",
        "algorithm", "compute",
    ],
    "fintech": [
        "banking", "finance", "crypto", "stablecoin", "payment",
        "SEC", "CFTC",
    ],
}


def fetch_congress_hearings() -> list[dict]:
    """Fetch upcoming committee hearings from Congress.gov."""
    if not CONGRESS_API_KEY:
        print("[leg_calendar] No CONGRESS_API_KEY set, skipping Congress.gov fetch")
        return []

    events = []
    try:
        resp = httpx.get(
            "https://api.congress.gov/v3/committee-meeting",
            params={
                "api_key": CONGRESS_API_KEY,
                "format": "json",
                "limit": 50,
            },
            timeout=15.0,
        )
        if resp.status_code == 200:
            for meeting in resp.json().get("committeeMeetings", []):
                date_str = meeting.get("date", "")[:10]
                title = meeting.get("title", "")
                committee = meeting.get("committee", {}).get("name", "")

                # Classify affected sectors
                full_text = (title + " " + committee).lower()
                sectors = [
                    s for s, keywords in SECTOR_KEYWORDS.items()
                    if any(kw.lower() in full_text for kw in keywords)
                ]

                events.append({
                    "event_date": date_str,
                    "event_type": "committee_hearing",
                    "chamber": meeting.get("chamber", "").lower(),
                    "committee": committee,
                    "bill_title": title,
                    "affected_sectors": sectors,
                    "significance": "high" if sectors else "low",
                    "source_url": meeting.get("url", ""),
                })
    except Exception as e:
        print(f"[leg_calendar] Congress.gov error: {e}")
    return events


def fetch_upcoming_votes_via_perplexity() -> list[dict]:
    """Use Perplexity to identify high-impact votes scheduled in the next 30 days."""
    if not PERPLEXITY_KEY:
        return []

    try:
        resp = httpx.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {PERPLEXITY_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "sonar",
                "messages": [{"role": "user", "content":
                    "What major US Congressional votes, committee markups, "
                    "or hearings are scheduled in the next 30 days that could "
                    "significantly impact publicly traded companies? "
                    "Focus on: technology, semiconductors, AI regulation, "
                    "defense spending (NDAA), healthcare policy, energy policy, "
                    "banking/crypto regulation. "
                    "For each event: date, chamber, committee, topic, and "
                    "affected sectors. "
                    "Respond as a JSON array with fields: date, chamber, "
                    "committee, topic, sectors (array), significance "
                    "(low/medium/high/critical)."
                }],
                "max_tokens": 1000,
            },
            timeout=30.0,
        )
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            events_raw = json.loads(content)
            return [{
                "event_date": e.get("date", "")[:10],
                "event_type": "floor_vote",
                "chamber": e.get("chamber", "").lower(),
                "committee": e.get("committee", ""),
                "bill_title": e.get("topic", ""),
                "affected_sectors": e.get("sectors", []),
                "significance": e.get("significance", "medium"),
            } for e in events_raw if e.get("date")]
    except Exception as e:
        print(f"[leg_calendar] Perplexity calendar error: {e}")
    return []


def run():
    """Fetch and store upcoming legislative events."""
    tracer = PipelineTracer("legislative_calendar")
    try:
        with tracer.step("fetch_hearings") as result:
            hearings = fetch_congress_hearings()
            result.set({"count": len(hearings)})

        with tracer.step("fetch_votes_perplexity") as result:
            votes = fetch_upcoming_votes_via_perplexity()
            result.set({"count": len(votes)})

        all_events = hearings + votes
        with tracer.step("store_events") as result:
            stored = 0
            for event in all_events:
                if (
                    event.get("event_date")
                    and event.get("event_date") >= date.today().isoformat()
                ):
                    _post_to_supabase("legislative_calendar", event)
                    stored += 1
            result.set({"stored": stored})

        tracer.complete({"total_events": len(all_events)})
        print(f"[legislative_calendar] Complete. Stored {len(all_events)} events.")
    except Exception as e:
        tracer.fail(str(e))
        raise


if __name__ == "__main__":
    run()
