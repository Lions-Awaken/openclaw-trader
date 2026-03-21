"""
StreamSaber Web Dashboard — FastAPI backend for Fly.io deployment.

API-only server (no HTML serving). All UI is handled by the React frontend.
Authentication via X-StreamSaber-Key header.

Usage:
    uvicorn streamsaber.src.web_dashboard:app --host 0.0.0.0 --port 8080
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional

import sentry_sdk
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from . import supabase_client
from .stream_monitor import MultiStreamMonitor

log = logging.getLogger("streamsaber.dashboard")

# ============================================================================
# Sentry
# ============================================================================

SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.environ.get("FLY_APP_NAME", "development"),
        release=os.environ.get("FLY_IMAGE_REF", "dev"),
        traces_sample_rate=0.2,
        profiles_sample_rate=0.1,
        enable_tracing=True,
        send_default_pii=False,
        integrations=[
            LoggingIntegration(
                level=logging.INFO,        # Breadcrumbs from INFO+
                event_level=logging.ERROR,  # Events from ERROR+
            ),
        ],
    )
    log.info("Sentry initialized")

# ============================================================================
# Configuration
# ============================================================================

STREAMSABER_API_KEY = os.environ.get("STREAMSABER_API_KEY", "dev-key")
PORT = int(os.environ.get("STREAMSABER_PORT", "8080"))

# ============================================================================
# Auth Dependency
# ============================================================================

async def verify_api_key(request: Request):
    """Verify the X-StreamSaber-Key header matches our API key."""
    key = request.headers.get("X-StreamSaber-Key", "")
    if key != STREAMSABER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# ============================================================================
# App Setup
# ============================================================================

app = FastAPI(
    title="StreamSaber API",
    description="TikTok Live Stream Monitor API",
    version="2.0.0",
)

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://twilightunderground.com",
        "https://www.twilightunderground.com",
        "https://twilight-underground.vercel.app",
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# Global monitor instance
monitor: Optional[MultiStreamMonitor] = None
monitor_task: Optional[asyncio.Task] = None
start_time: Optional[datetime] = None

# ============================================================================
# Pydantic Models
# ============================================================================

class AccountCreate(BaseModel):
    tiktok_username: str
    priority: int = 2
    label: str = ""

class AccountUpdate(BaseModel):
    is_active: Optional[bool] = None
    priority: Optional[int] = None
    label: Optional[str] = None

# ============================================================================
# Lifecycle Events
# ============================================================================

@app.on_event("startup")
async def startup():
    """Start the stream monitor as a background task."""
    global monitor, monitor_task, start_time
    start_time = datetime.now()
    monitor = MultiStreamMonitor()
    monitor_task = asyncio.create_task(monitor.run())


@app.on_event("shutdown")
async def shutdown():
    """Gracefully stop the monitor."""
    if monitor:
        monitor.stop()
    if monitor_task:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

# ============================================================================
# Helpers
# ============================================================================

def _get_live_capture_stats(username: str) -> Optional[dict]:
    """Get stats for an active capture by username."""
    if not monitor or username not in monitor.active_captures:
        return None

    capture_data = monitor.active_captures[username]
    capture = capture_data.get("capture")
    if not capture:
        return None

    started = capture_data.get("started")
    duration = (datetime.now() - started).total_seconds() if started else 0

    return {
        "stream_id": capture.stream_id,
        "duration_seconds": int(duration),
        "total_comments": capture.total_comments,
        "total_gifts": capture.total_gifts,
        "total_gift_diamonds": capture.total_gift_diamonds,
        "total_likes": capture.total_likes,
        "total_follows": capture.total_follows,
        "total_joins": capture.total_joins,
        "event_count": len(capture.events),
    }


def _find_live_capture_by_account(account_id: str):
    """Find an active capture whose account matches account_id.

    Returns (username, capture_data) or (None, None).
    """
    if not monitor:
        return None, None
    for username, cap_data in monitor.active_captures.items():
        capture = cap_data.get("capture")
        if capture and getattr(capture, "account_id", None) == account_id:
            return username, cap_data
    return None, None

# ============================================================================
# Routes — Health (no auth)
# ============================================================================

@app.get("/health")
async def health():
    """Health check — no auth required."""
    return {"status": "ok", "version": "2.0.0"}

# ============================================================================
# Routes — Status
# ============================================================================

@app.get("/api/status", dependencies=[Depends(verify_api_key)])
async def get_status():
    """Get current monitor status."""
    if not monitor:
        return {"running": False, "error": "Monitor not started"}

    uptime = (datetime.now() - start_time).total_seconds() if start_time else 0

    live_count = sum(1 for s in monitor.account_states.values() if s.get("is_live"))
    capturing_count = len(monitor.active_captures)

    return {
        "running": monitor.running,
        "uptime_seconds": int(uptime),
        "total_accounts": len(monitor.account_states),
        "enabled_accounts": len([
            a for a in monitor.accounts if a.get("is_active", True)
        ]),
        "live_count": live_count,
        "capturing_count": capturing_count,
    }

# ============================================================================
# Routes — Accounts
# ============================================================================

@app.get("/api/accounts", dependencies=[Depends(verify_api_key)])
async def get_accounts():
    """List all monitored accounts with live status."""
    if not monitor:
        return []

    accounts = []
    for acc in monitor.accounts:
        username = acc.get("tiktok_username") or acc.get("username", "")
        state = monitor.account_states.get(username, {})

        account_data = {
            "id": acc.get("id"),
            "tiktok_username": username,
            "label": acc.get("label", ""),
            "priority": acc.get("priority", 2),
            "is_active": acc.get("is_active", True),
            "is_live": state.get("is_live", False),
            "is_capturing": state.get("is_capturing", False),
            "last_checked": state.get("last_checked"),
            "total_streams_captured": state.get("total_streams_captured", 0),
        }

        # Add live capture stats if actively capturing
        if state.get("is_capturing"):
            account_data["capture"] = _get_live_capture_stats(username)

        accounts.append(account_data)

    return accounts


@app.post("/api/accounts", dependencies=[Depends(verify_api_key)])
async def add_account(account: AccountCreate):
    """Add a new account to monitor."""
    if not monitor:
        raise HTTPException(status_code=503, detail="Monitor not running")

    # Check if already exists in memory
    for acc in monitor.accounts:
        uname = acc.get("tiktok_username") or acc.get("username", "")
        if uname == account.tiktok_username:
            raise HTTPException(status_code=400, detail="Account already exists")

    # Persist to Supabase
    row = await supabase_client.create_account({
        "tiktok_username": account.tiktok_username,
        "priority": account.priority,
        "label": account.label,
        "is_active": True,
    })

    # Add to in-memory monitor
    new_account = {
        "id": row.get("id"),
        "tiktok_username": account.tiktok_username,
        "priority": account.priority,
        "label": account.label,
        "is_active": True,
    }
    monitor.accounts.append(new_account)
    monitor.account_states[account.tiktok_username] = {
        "is_live": False,
        "is_capturing": False,
        "last_checked": None,
        "last_stream_start": None,
        "total_streams_captured": 0,
        "label": account.label,
        "priority": account.priority,
    }

    return {"status": "created", "tiktok_username": account.tiktok_username, "id": row.get("id")}


@app.put("/api/accounts/{account_id}", dependencies=[Depends(verify_api_key)])
async def update_account(account_id: str, updates: AccountUpdate):
    """Update account settings."""
    # Build Supabase patch payload
    patch: Dict = {}
    if updates.is_active is not None:
        patch["is_active"] = updates.is_active
    if updates.priority is not None:
        patch["priority"] = updates.priority
    if updates.label is not None:
        patch["label"] = updates.label

    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    await supabase_client.update_account(account_id, patch)

    # Update in-memory state
    if monitor:
        for acc in monitor.accounts:
            if acc.get("id") == account_id:
                acc.update(patch)
                username = acc.get("tiktok_username") or acc.get("username", "")
                if username in monitor.account_states:
                    if updates.priority is not None:
                        monitor.account_states[username]["priority"] = updates.priority
                    if updates.label is not None:
                        monitor.account_states[username]["label"] = updates.label
                break

    return {"status": "updated", "account_id": account_id}


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(verify_api_key)])
async def delete_account(account_id: str):
    """Delete an account from monitoring."""
    await supabase_client.delete_account(account_id)

    # Remove from in-memory monitor
    if monitor:
        removed_username = None
        for acc in monitor.accounts:
            if acc.get("id") == account_id:
                removed_username = acc.get("tiktok_username") or acc.get("username", "")
                break

        if removed_username:
            monitor.accounts = [
                a for a in monitor.accounts if a.get("id") != account_id
            ]
            monitor.account_states.pop(removed_username, None)

            # Stop capture if active
            if removed_username in monitor.active_captures:
                cap_data = monitor.active_captures[removed_username]
                task = cap_data.get("task")
                if task:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    return {"status": "deleted", "account_id": account_id}

# ============================================================================
# Routes — Control
# ============================================================================

@app.post("/api/control/scan", dependencies=[Depends(verify_api_key)])
async def force_scan():
    """Force an immediate scan of all accounts."""
    if not monitor:
        raise HTTPException(status_code=503, detail="Monitor not running")

    asyncio.create_task(monitor._scan_all_accounts())
    asyncio.create_task(monitor._manage_captures())

    return {
        "status": "scan_triggered",
        "accounts_to_check": len(monitor.account_states),
    }


@app.post("/api/control/stop/{username}", dependencies=[Depends(verify_api_key)])
async def stop_capture(username: str):
    """Stop capturing a specific stream."""
    if not monitor:
        raise HTTPException(status_code=503, detail="Monitor not running")

    if username not in monitor.active_captures:
        raise HTTPException(status_code=404, detail="No active capture for this account")

    cap_data = monitor.active_captures[username]
    task = cap_data.get("task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return {"status": "stopped", "username": username}

# ============================================================================
# Routes — Captures
# ============================================================================

@app.get("/api/captures", dependencies=[Depends(verify_api_key)])
async def list_captures(limit: int = 20, account_id: Optional[str] = None):
    """List recent captures from Supabase."""
    account_ids = [account_id] if account_id else None
    summaries = await supabase_client.query_summaries(
        account_ids=account_ids,
        days=365,
        limit=limit,
    )
    return {"captures": summaries, "total": len(summaries)}


@app.get("/api/captures/{account_id}/{stream_id}", dependencies=[Depends(verify_api_key)])
async def get_capture(account_id: str, stream_id: str):
    """Get capture detail. Includes live stats if the stream is currently active."""
    summary = await supabase_client.get_summary(account_id, stream_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Capture not found")

    result = dict(summary)

    # Check if this stream is currently live
    username, cap_data = _find_live_capture_by_account(account_id)
    if username and cap_data:
        capture = cap_data.get("capture")
        if capture and capture.stream_id == stream_id:
            started = cap_data.get("started")
            duration = (datetime.now() - started).total_seconds() if started else 0
            result["live"] = {
                "is_live": True,
                "duration_seconds": int(duration),
                "total_comments": capture.total_comments,
                "total_gifts": capture.total_gifts,
                "total_gift_diamonds": capture.total_gift_diamonds,
                "total_likes": capture.total_likes,
                "total_follows": capture.total_follows,
                "total_joins": capture.total_joins,
                "event_count": len(capture.events),
            }

    return result


@app.get("/api/captures/{account_id}/{stream_id}/events", dependencies=[Depends(verify_api_key)])
async def get_capture_events(
    account_id: str,
    stream_id: str,
    page: int = 1,
    per_page: int = 100,
    types: str = "",
):
    """Get events for a capture. Live streams serve from memory; past streams from Supabase."""
    per_page = min(per_page, 1000)

    # Check if this stream is currently live and serve from memory
    username, cap_data = _find_live_capture_by_account(account_id)
    if username and cap_data:
        capture = cap_data.get("capture")
        if capture and capture.stream_id == stream_id:
            events = list(capture.events)

            # Filter by type if specified
            if types:
                type_list = [t.strip() for t in types.split(",")]
                events = [e for e in events if e.get("type") in type_list]

            total = len(events)
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page

            return {
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": (total + per_page - 1) // per_page if total > 0 else 0,
                "events": events[start_idx:end_idx],
                "source": "live",
            }

    # Past stream — query from Supabase
    event_type = None
    if types:
        # If multiple types, we query all and filter; single type goes to Supabase
        type_list = [t.strip() for t in types.split(",")]
        event_type = type_list[0] if len(type_list) == 1 else None

    offset = (page - 1) * per_page
    events = await supabase_client.query_events(
        account_id=account_id,
        stream_id=stream_id,
        event_type=event_type,
        limit=per_page,
        offset=offset,
    )

    # If we had multiple types and could only pass one, filter client-side
    if types and event_type is None:
        type_list = [t.strip() for t in types.split(",")]
        events = [e for e in events if e.get("event_type") in type_list]

    # Get total count for pagination
    total = await supabase_client.count_events(
        account_id=account_id,
        stream_id=stream_id,
    )

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if total > 0 else 0,
        "events": events,
        "source": "supabase",
    }

# ============================================================================
# Routes — Analytics
# ============================================================================

@app.get("/api/analytics/summary", dependencies=[Depends(verify_api_key)])
@limiter.limit("10/minute")
async def get_analytics_summary(request: Request, days: int = 7):
    """Aggregated analytics computed from stream summaries in Supabase."""
    summaries = await supabase_client.query_summaries(days=days, limit=1000)

    totals = {
        "streams": 0,
        "diamonds": 0,
        "gifts": 0,
        "comments": 0,
        "likes": 0,
        "follows": 0,
        "shares": 0,
        "duration_seconds": 0,
    }
    by_account: Dict[str, dict] = {}
    all_gifters: Dict[str, dict] = {}

    for s in summaries:
        acct_id = s.get("tiktok_account_id", "")
        diamonds = s.get("total_diamonds", 0)
        gifts = s.get("total_gifts", 0)
        comments = s.get("total_comments", 0)
        likes = s.get("total_likes", 0)
        follows = s.get("total_follows", 0)
        shares = s.get("total_shares", 0)
        duration = s.get("duration_seconds", 0)

        # Skip very short captures
        if duration < 60:
            continue

        totals["streams"] += 1
        totals["diamonds"] += diamonds
        totals["gifts"] += gifts
        totals["comments"] += comments
        totals["likes"] += likes
        totals["follows"] += follows
        totals["shares"] += shares
        totals["duration_seconds"] += duration

        if acct_id not in by_account:
            by_account[acct_id] = {
                "account_id": acct_id,
                "streams": 0,
                "diamonds": 0,
                "gifts": 0,
                "comments": 0,
                "duration_seconds": 0,
            }
        by_account[acct_id]["streams"] += 1
        by_account[acct_id]["diamonds"] += diamonds
        by_account[acct_id]["gifts"] += gifts
        by_account[acct_id]["comments"] += comments
        by_account[acct_id]["duration_seconds"] += duration

        # Aggregate top gifters from each summary
        top_gifters_raw = s.get("top_gifters", [])
        if isinstance(top_gifters_raw, str):
            try:
                top_gifters_raw = json.loads(top_gifters_raw)
            except (json.JSONDecodeError, TypeError):
                top_gifters_raw = []

        for g in (top_gifters_raw or []):
            uid = g.get("user_id") or g.get("username", "")
            if not uid:
                continue
            if uid not in all_gifters:
                all_gifters[uid] = {
                    "user_id": uid,
                    "nickname": g.get("nickname", "Unknown"),
                    "username": g.get("username", "Unknown"),
                    "total_diamonds": 0,
                    "gift_count": 0,
                    "accounts": set(),
                }
            all_gifters[uid]["total_diamonds"] += g.get("total_diamonds", 0)
            all_gifters[uid]["gift_count"] += g.get("gift_count", g.get("count", 0))
            all_gifters[uid]["accounts"].add(acct_id)

    by_account_list = sorted(by_account.values(), key=lambda x: x["diamonds"], reverse=True)

    top_gifters_list = []
    for g in sorted(all_gifters.values(), key=lambda x: x["total_diamonds"], reverse=True)[:20]:
        top_gifters_list.append({
            "user_id": g["user_id"],
            "nickname": g["nickname"],
            "username": g["username"],
            "total_diamonds": g["total_diamonds"],
            "gift_count": g["gift_count"],
            "accounts": list(g["accounts"]),
        })

    # Build recent streams list from summaries
    recent_streams = []
    for s in summaries[:30]:
        if s.get("duration_seconds", 0) < 60:
            continue
        recent_streams.append({
            "account_id": s.get("tiktok_account_id"),
            "stream_id": s.get("stream_id"),
            "started_at": s.get("started_at"),
            "duration_seconds": s.get("duration_seconds", 0),
            "stats": {
                "total_diamonds": s.get("total_diamonds", 0),
                "total_gifts": s.get("total_gifts", 0),
                "total_comments": s.get("total_comments", 0),
                "total_follows": s.get("total_follows", 0),
                "total_likes": s.get("total_likes", 0),
                "peak_viewers": s.get("peak_viewers", 0),
            },
        })

    return {
        "period_days": days,
        "totals": totals,
        "by_account": by_account_list,
        "top_gifters": top_gifters_list,
        "recent_streams": recent_streams,
    }

# ============================================================================
# Routes — Leaderboard
# ============================================================================

@app.get("/api/leaderboard", dependencies=[Depends(verify_api_key)])
async def get_leaderboard(days: int = 30):
    """Leaderboard aggregated from daily rollups in Supabase."""
    rollups = await supabase_client.query_daily_rollups(days=days)

    account_stats: Dict[str, dict] = {}
    for r in rollups:
        acct_id = r.get("tiktok_account_id", "")
        if acct_id not in account_stats:
            account_stats[acct_id] = {
                "account_id": acct_id,
                "streams": 0,
                "diamonds": 0,
                "gifts": 0,
                "followers": 0,
                "total_duration_seconds": 0,
                "peak_viewers": 0,
            }
        account_stats[acct_id]["streams"] += r.get("stream_count", 0)
        account_stats[acct_id]["diamonds"] += r.get("total_diamonds", 0)
        account_stats[acct_id]["gifts"] += r.get("total_gifts", 0)
        account_stats[acct_id]["followers"] += r.get("total_follows", 0)
        account_stats[acct_id]["total_duration_seconds"] += r.get("total_duration_seconds", 0)
        account_stats[acct_id]["peak_viewers"] = max(
            account_stats[acct_id]["peak_viewers"],
            r.get("avg_peak_viewers", 0),
        )

    leaderboard = sorted(account_stats.values(), key=lambda x: x["diamonds"], reverse=True)

    for i, entry in enumerate(leaderboard):
        entry["rank"] = i + 1
        entry["avg_diamonds_per_stream"] = (
            round(entry["diamonds"] / entry["streams"]) if entry["streams"] > 0 else 0
        )
        entry["avg_stream_duration"] = (
            round(entry["total_duration_seconds"] / entry["streams"]) if entry["streams"] > 0 else 0
        )

    total_streams = sum(a["streams"] for a in leaderboard)
    total_diamonds = sum(a["diamonds"] for a in leaderboard)

    return {
        "leaderboard": leaderboard,
        "period_days": days,
        "total_streams": total_streams,
        "total_diamonds": total_diamonds,
    }

# ============================================================================
# Routes — VIP Dashboard
# ============================================================================

@app.get("/api/vip/dashboard", dependencies=[Depends(verify_api_key)])
async def get_vip_dashboard():
    """VIP viewer tracking — aggregate top_gifters across all summaries."""
    summaries = await supabase_client.query_summaries(days=365, limit=2000)

    now = datetime.now(timezone.utc)
    all_viewers: Dict[str, dict] = {}

    for s in summaries:
        capture_date_str = s.get("started_at")
        capture_date = None
        if capture_date_str:
            try:
                capture_date = datetime.fromisoformat(capture_date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        acct_id = s.get("tiktok_account_id", "")

        top_gifters_raw = s.get("top_gifters", [])
        if isinstance(top_gifters_raw, str):
            try:
                top_gifters_raw = json.loads(top_gifters_raw)
            except (json.JSONDecodeError, TypeError):
                top_gifters_raw = []

        if isinstance(top_gifters_raw, dict):
            gifter_items = list(top_gifters_raw.items())
        elif isinstance(top_gifters_raw, list):
            gifter_items = [
                (g.get("user_id", str(i)), g) for i, g in enumerate(top_gifters_raw)
            ]
        else:
            continue

        for uid, gdata in gifter_items:
            if not uid:
                continue
            if uid not in all_viewers:
                all_viewers[uid] = {
                    "user_id": uid,
                    "nickname": gdata.get("nickname", "Unknown"),
                    "total_diamonds": 0,
                    "gift_count": 0,
                    "stream_count": 0,
                    "first_seen": capture_date,
                    "last_seen": capture_date,
                    "accounts": set(),
                }

            all_viewers[uid]["total_diamonds"] += gdata.get(
                "total_diamonds", gdata.get("diamonds", 0)
            )
            all_viewers[uid]["gift_count"] += gdata.get(
                "gift_count", gdata.get("count", 1)
            )
            all_viewers[uid]["stream_count"] += 1
            all_viewers[uid]["accounts"].add(acct_id)

            if capture_date:
                first = all_viewers[uid]["first_seen"]
                last = all_viewers[uid]["last_seen"]
                if first is None or capture_date < first:
                    all_viewers[uid]["first_seen"] = capture_date
                if last is None or capture_date > last:
                    all_viewers[uid]["last_seen"] = capture_date

    # Categorize into tiers
    whale_tier = []
    supporter_tier = []
    fan_tier = []
    at_risk = []
    rising_stars = []

    for v in all_viewers.values():
        diamonds = v["total_diamonds"]
        last_seen = v["last_seen"]
        stream_count = v["stream_count"]

        days_inactive = (now - last_seen).days if last_seen else 999
        is_active = days_inactive <= 7
        is_at_risk = days_inactive >= 14 and diamonds >= 1000

        viewer_data = {
            "user_id": v["user_id"],
            "nickname": v["nickname"],
            "total_diamonds": diamonds,
            "gift_count": v["gift_count"],
            "stream_count": stream_count,
            "first_seen": v["first_seen"].isoformat() if v["first_seen"] else None,
            "last_seen": v["last_seen"].isoformat() if v["last_seen"] else None,
            "days_inactive": days_inactive,
            "is_active": is_active,
            "accounts": list(v["accounts"]),
        }

        if diamonds >= 10000:
            whale_tier.append(viewer_data)
            if is_at_risk:
                at_risk.append(viewer_data)
        elif diamonds >= 1000:
            supporter_tier.append(viewer_data)
            if is_at_risk:
                at_risk.append(viewer_data)
        elif diamonds >= 100:
            fan_tier.append(viewer_data)

        if stream_count <= 3 and diamonds >= 500:
            rising_stars.append(viewer_data)

    whale_tier.sort(key=lambda x: x["total_diamonds"], reverse=True)
    supporter_tier.sort(key=lambda x: x["total_diamonds"], reverse=True)
    fan_tier.sort(key=lambda x: x["total_diamonds"], reverse=True)
    at_risk.sort(key=lambda x: x["total_diamonds"], reverse=True)
    rising_stars.sort(key=lambda x: x["total_diamonds"], reverse=True)

    return {
        "whale_tier": whale_tier[:20],
        "supporter_tier": supporter_tier[:30],
        "fan_tier": fan_tier[:50],
        "at_risk": at_risk[:10],
        "rising_stars": rising_stars[:10],
        "totals": {
            "whales": len(whale_tier),
            "supporters": len(supporter_tier),
            "fans": len(fan_tier),
            "at_risk_count": len(at_risk),
        },
    }

# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    print(f"Starting StreamSaber API on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
