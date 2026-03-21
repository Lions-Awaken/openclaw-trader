"""
Supabase REST API client for StreamSaber.

All Supabase interactions go through this module. Uses httpx for async HTTP
and the service_role key for full access.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger("streamsaber.supabase")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def _headers(*, prefer: str = "") -> dict:
    """Build auth headers for Supabase REST API."""
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _rest(table: str) -> str:
    """Build Supabase REST URL for a table."""
    return f"{SUPABASE_URL}/rest/v1/{table}"


# ---------------------------------------------------------------------------
# Shared async client (created lazily, reused across calls)
# ---------------------------------------------------------------------------
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

async def fetch_active_accounts() -> list[dict]:
    """Load active monitored accounts from tiktok_accounts table.

    Returns list of dicts with keys: id, tiktok_username, label, priority, is_active
    """
    client = _get_client()
    resp = await client.get(
        _rest("tiktok_accounts"),
        headers=_headers(),
        params={
            "select": "id,tiktok_username,label,priority,is_active,user_id,marketplace_id",
            "is_active": "eq.true",
        },
    )
    resp.raise_for_status()
    return resp.json()


async def create_account(data: dict) -> dict:
    """Create a new tiktok_account."""
    client = _get_client()
    resp = await client.post(
        _rest("tiktok_accounts"),
        headers=_headers(prefer="return=representation"),
        json=data,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else {}


async def update_account(account_id: str, data: dict) -> None:
    """Update a tiktok_account by id."""
    client = _get_client()
    resp = await client.patch(
        _rest("tiktok_accounts"),
        headers=_headers(prefer="return=minimal"),
        params={"id": f"eq.{account_id}"},
        json=data,
    )
    resp.raise_for_status()


async def delete_account(account_id: str) -> None:
    """Delete a tiktok_account by id."""
    client = _get_client()
    resp = await client.delete(
        _rest("tiktok_accounts"),
        headers=_headers(),
        params={"id": f"eq.{account_id}"},
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Stream Events (raw events → tiktok_stream_events)
# ---------------------------------------------------------------------------

async def insert_events(events: list[dict]) -> int:
    """Batch insert raw events into tiktok_stream_events.

    Each event dict should have: tiktok_account_id, stream_id, event_type,
    event_time, event_data
    """
    if not events:
        return 0

    client = _get_client()
    resp = await client.post(
        _rest("tiktok_stream_events"),
        headers=_headers(prefer="return=minimal"),
        json=events,
    )
    resp.raise_for_status()
    return len(events)


async def query_events(
    account_id: Optional[str] = None,
    stream_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Query raw events from tiktok_stream_events."""
    client = _get_client()
    params: dict = {
        "select": "*",
        "order": "event_time.desc",
        "limit": str(limit),
        "offset": str(offset),
    }
    if account_id:
        params["tiktok_account_id"] = f"eq.{account_id}"
    if stream_id:
        params["stream_id"] = f"eq.{stream_id}"
    if event_type:
        params["event_type"] = f"eq.{event_type}"

    resp = await client.get(
        _rest("tiktok_stream_events"),
        headers=_headers(prefer="count=exact"),
        params=params,
    )
    resp.raise_for_status()

    # Extract total count from Content-Range header
    return resp.json()


async def count_events(
    account_id: Optional[str] = None,
    stream_id: Optional[str] = None,
) -> int:
    """Count events matching filters."""
    client = _get_client()
    params: dict = {
        "select": "id",
        "limit": "0",
    }
    if account_id:
        params["tiktok_account_id"] = f"eq.{account_id}"
    if stream_id:
        params["stream_id"] = f"eq.{stream_id}"

    resp = await client.get(
        _rest("tiktok_stream_events"),
        headers=_headers(prefer="count=exact"),
        params=params,
    )
    resp.raise_for_status()

    # Parse count from Content-Range header: "0-0/123"
    content_range = resp.headers.get("content-range", "")
    if "/" in content_range:
        total = content_range.split("/")[-1]
        if total != "*":
            return int(total)
    return 0


# ---------------------------------------------------------------------------
# Stream Summaries
# ---------------------------------------------------------------------------

async def upsert_stream_summary(account_id: str, summary: dict) -> None:
    """Upsert a stream summary to tiktok_stream_summaries."""
    client = _get_client()
    row = {
        "tiktok_account_id": account_id,
        "stream_id": summary["stream_id"],
        "started_at": summary.get("started_at"),
        "ended_at": summary.get("ended_at"),
        "duration_seconds": summary.get("duration_seconds", 0),
        "status": summary.get("status", "completed"),
        "total_comments": summary.get("total_comments", 0),
        "total_gifts": summary.get("total_gifts", 0),
        "total_diamonds": summary.get("total_diamonds", 0),
        "total_likes": summary.get("total_likes", 0),
        "total_follows": summary.get("total_follows", 0),
        "total_shares": summary.get("total_shares", 0),
        "total_joins": summary.get("total_joins", 0),
        "peak_viewers": summary.get("peak_viewers", 0),
        "unique_viewers": summary.get("unique_viewers", 0),
        "top_gifters": json.dumps(summary.get("top_gifters", [])),
        "top_commenters": json.dumps(summary.get("top_commenters", [])),
        "hourly_activity": json.dumps(summary.get("hourly_activity", {})),
        "host_growth": json.dumps(summary.get("host_growth", {})),
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }

    resp = await client.post(
        _rest("tiktok_stream_summaries"),
        headers=_headers(prefer="resolution=merge-duplicates,return=minimal"),
        json=row,
    )
    resp.raise_for_status()
    log.info(f"Upserted summary for stream {summary['stream_id']}")


async def query_summaries(
    account_ids: Optional[list[str]] = None,
    days: int = 30,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Query stream summaries from Supabase."""
    client = _get_client()
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    params: dict = {
        "select": "*",
        "order": "started_at.desc",
        "started_at": f"gte.{cutoff}",
        "limit": str(limit),
        "offset": str(offset),
    }
    if account_ids:
        params["tiktok_account_id"] = f"in.({','.join(account_ids)})"

    resp = await client.get(
        _rest("tiktok_stream_summaries"),
        headers=_headers(prefer="count=exact"),
        params=params,
    )
    resp.raise_for_status()
    return resp.json()


async def get_summary(account_id: str, stream_id: str) -> Optional[dict]:
    """Get a single stream summary."""
    client = _get_client()
    resp = await client.get(
        _rest("tiktok_stream_summaries"),
        headers=_headers(),
        params={
            "tiktok_account_id": f"eq.{account_id}",
            "stream_id": f"eq.{stream_id}",
            "limit": "1",
        },
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Daily Rollups
# ---------------------------------------------------------------------------

async def recompute_daily_rollup(account_id: str, date_str: str) -> None:
    """Recompute daily rollup for an account + date by aggregating summaries."""
    client = _get_client()

    # Fetch all summaries for this account + date
    resp = await client.get(
        _rest("tiktok_stream_summaries"),
        headers=_headers(),
        params={
            "select": "total_diamonds,total_gifts,total_comments,total_likes,total_follows,duration_seconds,peak_viewers,unique_viewers,top_gifters",
            "tiktok_account_id": f"eq.{account_id}",
            "started_at": f"gte.{date_str}T00:00:00Z",
            "and": f"(started_at.lt.{date_str}T23:59:59Z)",
        },
    )
    resp.raise_for_status()
    summaries = resp.json()

    if not summaries:
        return

    count = len(summaries)
    total_diamonds = sum(s.get("total_diamonds", 0) for s in summaries)
    total_gifts = sum(s.get("total_gifts", 0) for s in summaries)
    total_comments = sum(s.get("total_comments", 0) for s in summaries)
    total_likes = sum(s.get("total_likes", 0) for s in summaries)
    total_follows = sum(s.get("total_follows", 0) for s in summaries)
    total_duration = sum(s.get("duration_seconds", 0) for s in summaries)
    peak_viewers = [s.get("peak_viewers", 0) for s in summaries]
    unique_viewers = [s.get("unique_viewers", 0) for s in summaries]

    avg_peak = sum(peak_viewers) / count
    avg_unique = sum(unique_viewers) / count

    # Find top gifter of the day
    top_gifter = None
    best_diamonds = 0
    for s in summaries:
        gifters = s.get("top_gifters", [])
        if isinstance(gifters, str):
            gifters = json.loads(gifters)
        for g in (gifters or []):
            if g.get("total_diamonds", 0) > best_diamonds:
                best_diamonds = g["total_diamonds"]
                top_gifter = {
                    "nickname": g.get("nickname", ""),
                    "username": g.get("username", ""),
                    "total_diamonds": g["total_diamonds"],
                }

    rollup = {
        "tiktok_account_id": account_id,
        "date": date_str,
        "stream_count": count,
        "total_duration_seconds": total_duration,
        "total_diamonds": total_diamonds,
        "total_gifts": total_gifts,
        "total_comments": total_comments,
        "total_likes": total_likes,
        "total_follows": total_follows,
        "avg_peak_viewers": round(avg_peak, 1),
        "avg_unique_viewers": round(avg_unique, 1),
        "top_gifter": json.dumps(top_gifter) if top_gifter else None,
    }

    resp = await client.post(
        _rest("tiktok_daily_rollups"),
        headers=_headers(prefer="resolution=merge-duplicates,return=minimal"),
        json=rollup,
    )
    resp.raise_for_status()
    log.info(f"Recomputed daily rollup for {account_id} on {date_str}")


# ---------------------------------------------------------------------------
# Leaderboard / Analytics helpers
# ---------------------------------------------------------------------------

async def query_daily_rollups(
    account_ids: Optional[list[str]] = None,
    days: int = 30,
) -> list[dict]:
    """Query daily rollups for analytics."""
    client = _get_client()
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

    params: dict = {
        "select": "*",
        "order": "date.desc",
        "date": f"gte.{cutoff}",
    }
    if account_ids:
        params["tiktok_account_id"] = f"in.({','.join(account_ids)})"

    resp = await client.get(
        _rest("tiktok_daily_rollups"),
        headers=_headers(),
        params=params,
    )
    resp.raise_for_status()
    return resp.json()


async def get_account_by_username(username: str) -> Optional[dict]:
    """Find a tiktok_account by username."""
    client = _get_client()
    resp = await client.get(
        _rest("tiktok_accounts"),
        headers=_headers(),
        params={
            "tiktok_username": f"eq.{username}",
            "limit": "1",
        },
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None
