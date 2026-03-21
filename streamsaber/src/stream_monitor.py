"""
TikTok Live Stream Monitor — Fly.io deployment variant.

Monitors multiple TikTok users' live status and automatically captures all
stream events when they go live. Accounts are fetched from Supabase.
High-value events are buffered and flushed to Supabase in batches.
Stream summaries and daily rollups are written directly to Supabase on
capture finalization. JSON files are still written to DATA_DIR as a
crash-recovery backup.

This module is imported by web_dashboard; there is no CLI entrypoint.

States:
  IDLE         - Checking if live every N minutes (lightweight)
  CONNECTING   - Attempting WebSocket connection
  CAPTURING    - Connected and capturing events
  RECONNECTING - Lost connection, attempting to reconnect
  ENDING       - Stream ended, saving final data

Exports:
  MultiStreamMonitor, StreamCapture, SupabaseEventBuffer, State
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import supabase_client

# For web scraping host stats
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# TikTokLive imports
try:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import (
        CommentEvent,
        ConnectEvent,
        DisconnectEvent,
        FollowEvent,
        GiftEvent,
        JoinEvent,
        LikeEvent,
        ShareEvent,
    )
    TIKTOK_AVAILABLE = True

    # Try to import additional events (may not exist in all versions)
    try:
        from TikTokLive.events import ViewerCountUpdateEvent
        HAS_VIEWER_COUNT = True
    except ImportError:
        HAS_VIEWER_COUNT = False

    try:
        from TikTokLive.events import RoomUserSeqEvent
        HAS_ROOM_USER_SEQ = True
    except ImportError:
        HAS_ROOM_USER_SEQ = False

    try:
        from TikTokLive.events import EmoteChatEvent
        HAS_EMOTE_CHAT = True
    except ImportError:
        HAS_EMOTE_CHAT = False

    try:
        from TikTokLive.events import EnvelopeEvent
        HAS_ENVELOPE = True
    except ImportError:
        HAS_ENVELOPE = False

    try:
        from TikTokLive.events import SubscribeEvent
        HAS_SUBSCRIBE = True
    except ImportError:
        HAS_SUBSCRIBE = False

    try:
        from TikTokLive.events import QuestionNewEvent
        HAS_QUESTION = True
    except ImportError:
        HAS_QUESTION = False

    try:
        from TikTokLive.events import LiveEndEvent
        HAS_LIVE_END = True
    except ImportError:
        HAS_LIVE_END = False

except ImportError:
    TIKTOK_AVAILABLE = False
    HAS_VIEWER_COUNT = False
    HAS_ROOM_USER_SEQ = False
    HAS_EMOTE_CHAT = False
    HAS_ENVELOPE = False
    HAS_SUBSCRIBE = False
    HAS_QUESTION = False
    HAS_LIVE_END = False


# ---------------------------------------------------------------------------
# Paths — DATA_DIR only, used for crash-recovery JSON backup
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))


# ---------------------------------------------------------------------------
# Logging — stdout JSON structured logging (Fly captures stdout)
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line on stdout."""

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


def _setup_root_logger(level: str = "INFO"):
    """Configure all loggers for JSON stdout — captures TikTokLive, httpx, etc."""
    formatter = _JsonFormatter()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    # Apply to root logger so ALL libraries get JSON formatting
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)

    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("TikTokLive").setLevel(logging.INFO)


_setup_root_logger(os.environ.get("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------

class State(Enum):
    """Daemon states."""
    IDLE = "idle"
    CONNECTING = "connecting"
    CAPTURING = "capturing"
    RECONNECTING = "reconnecting"
    ENDING = "ending"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_get(obj: Any, *attrs, default=None) -> Any:
    """Safely get nested attributes from an object."""
    for attr in attrs:
        try:
            if obj is None:
                return default
            obj = getattr(obj, attr, None)
        except Exception:
            return default
    return obj if obj is not None else default


def extract_user(user) -> dict:
    """Extract all available user data safely."""
    if user is None:
        return {'nickname': 'Unknown', 'error': 'No user object'}

    try:
        # Get avatar URL safely
        avatar = None
        profile_pic = safe_get(user, 'profile_picture')
        if profile_pic:
            urls = safe_get(profile_pic, 'urls')
            if urls and len(urls) > 0:
                avatar = urls[0]
            elif hasattr(profile_pic, 'url'):
                avatar = profile_pic.url

        # Get badges safely
        badges = []
        badge_list = safe_get(user, 'badge_list') or safe_get(user, 'badges') or []
        for badge in badge_list:
            try:
                if hasattr(badge, 'type'):
                    badges.append(badge.type)
                elif hasattr(badge, 'name'):
                    badges.append(badge.name)
                else:
                    badges.append(str(badge))
            except Exception:
                pass

        return {
            'id': safe_get(user, 'user_id') or safe_get(user, 'id'),
            'username': safe_get(user, 'unique_id'),
            'nickname': safe_get(user, 'nickname') or 'Unknown',
            'avatar': avatar,
            'bio': safe_get(user, 'bio_description'),
            'follower_count': safe_get(user, 'follower_count', default=0),
            'following_count': safe_get(user, 'following_count', default=0),
            'is_subscriber': safe_get(user, 'is_subscriber', default=False),
            'is_moderator': safe_get(user, 'is_moderator', default=False),
            'is_new_gifter': safe_get(user, 'is_new_gifter', default=False),
            'top_gifter_rank': safe_get(user, 'top_gifter_rank'),
            'badges': badges,
        }
    except Exception as e:
        return {'nickname': str(user) if user else 'Unknown', 'error': str(e)}


def extract_gift(gift, event) -> dict:
    """Extract all available gift data safely."""
    if gift is None:
        return {'name': 'Unknown', 'error': 'No gift object'}

    try:
        # Get gift image URL safely
        image_url = None
        image = safe_get(gift, 'image')
        if image:
            if hasattr(image, 'url'):
                image_url = image.url
            elif isinstance(image, dict):
                image_url = image.get('url') or image.get('url_list', [None])[0]

        # Get count from event (not gift)
        gift_count = safe_get(event, 'repeat_count') or safe_get(event, 'count') or 1
        diamond_count = safe_get(gift, 'diamond_count') or 0

        return {
            'id': safe_get(gift, 'gift_id') or safe_get(gift, 'id'),
            'name': safe_get(gift, 'name') or 'Unknown',
            'diamond_count': diamond_count,
            'count': gift_count,
            'total_diamonds': diamond_count * gift_count,
            'image': image_url,
            'is_streakable': safe_get(gift, 'streakable', default=False),
            'streak_id': safe_get(event, 'streak_id'),
        }
    except Exception as e:
        return {'name': str(gift) if gift else 'Unknown', 'error': str(e)}


async def fetch_tiktok_profile(username: str) -> dict:
    """Fetch TikTok user profile data via web scrape."""
    if not HAS_HTTPX:
        return {'error': 'httpx not installed'}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://www.tiktok.com/@{username}",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                },
                follow_redirects=True
            )

            if resp.status_code != 200:
                return {'error': f'HTTP {resp.status_code}'}

            # Try to find the JSON data in the page
            match = re.search(
                r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.+?)</script>',
                resp.text
            )
            if match:
                data = json.loads(match.group(1))
                user_detail = data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
                user_info = user_detail.get("userInfo", {})
                stats = user_info.get("stats", {})
                user = user_info.get("user", {})

                return {
                    'follower_count': stats.get("followerCount", 0),
                    'following_count': stats.get("followingCount", 0),
                    'heart_count': stats.get("heartCount", 0),
                    'video_count': stats.get("videoCount", 0),
                    'bio': user.get("signature"),
                    'nickname': user.get("nickname"),
                    'avatar': user.get("avatarLarger") or user.get("avatarMedium"),
                    'verified': user.get("verified", False),
                }

            # Fallback: try SIGI_STATE format
            match = re.search(r'<script id="SIGI_STATE"[^>]*>(.+?)</script>', resp.text)
            if match:
                data = json.loads(match.group(1))
                user_module = data.get("UserModule", {})
                users = user_module.get("users", {})
                stats = user_module.get("stats", {})

                if username in users:
                    user = users[username]
                    user_stats = stats.get(username, {})
                    return {
                        'follower_count': user_stats.get("followerCount", 0),
                        'following_count': user_stats.get("followingCount", 0),
                        'bio': user.get("signature"),
                        'nickname': user.get("nickname"),
                        'avatar': user.get("avatarLarger"),
                    }

            return {'error': 'Could not parse profile data'}

    except Exception as e:
        return {'error': str(e)}


# ---------------------------------------------------------------------------
# SupabaseEventBuffer
# ---------------------------------------------------------------------------

class SupabaseEventBuffer:
    """Buffer high-value stream events and flush to Supabase in batches.

    Only buffers: comment, gift, follow, subscribe, share.
    Flushes when 50+ events are pending OR 30+ seconds since last flush.
    """

    HIGH_VALUE_TYPES = frozenset({"comment", "gift", "follow", "subscribe", "share"})
    FLUSH_COUNT = 50
    FLUSH_INTERVAL = 30  # seconds

    def __init__(self, account_id: str, stream_id: str):
        self.account_id = account_id
        self.stream_id = stream_id
        self._pending: List[dict] = []
        self._last_flush: float = time.monotonic()
        self._logger = logging.getLogger("streamsaber.event_buffer")

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def seconds_since_flush(self) -> float:
        return time.monotonic() - self._last_flush

    def add(self, event_type: str, event_time: str, event_data: dict) -> None:
        """Add a high-value event to the buffer.

        Only comment, gift, follow, subscribe, share are accepted.
        """
        if event_type not in self.HIGH_VALUE_TYPES:
            return

        self._pending.append({
            "tiktok_account_id": self.account_id,
            "stream_id": self.stream_id,
            "event_type": event_type,
            "event_time": event_time,
            "event_data": event_data,
        })

    def should_flush(self) -> bool:
        """Check if the buffer should be flushed."""
        if not self._pending:
            return False
        if len(self._pending) >= self.FLUSH_COUNT:
            return True
        if self.seconds_since_flush >= self.FLUSH_INTERVAL:
            return True
        return False

    async def flush(self) -> int:
        """Flush pending events to Supabase via supabase_client.insert_events().

        Returns the number of events flushed.
        """
        if not self._pending:
            return 0

        batch = self._pending[:]
        self._pending.clear()
        self._last_flush = time.monotonic()

        try:
            count = await supabase_client.insert_events(batch)
            self._logger.debug(f"Flushed {count} events for stream {self.stream_id}")
            return count
        except Exception as e:
            self._logger.error(f"Failed to flush {len(batch)} events: {e}")
            # Put events back so they can be retried
            self._pending = batch + self._pending
            return 0


# ---------------------------------------------------------------------------
# StreamCapture
# ---------------------------------------------------------------------------

class StreamCapture:
    """Manages a single stream capture session."""

    def __init__(self, username: str, stream_id: str):
        self.username = username
        self.stream_id = stream_id
        self.start_time = datetime.now()
        self.end_time: Optional[datetime] = None
        self.status = "capturing"
        self.reconnect_count = 0
        self.events = []

        # Stats
        self.total_comments = 0
        self.total_gifts = 0
        self.total_gift_diamonds = 0
        self.total_likes = 0
        self.total_follows = 0
        self.total_shares = 0
        self.total_joins = 0
        self.total_subscribes = 0
        self.peak_engagement_time: Optional[datetime] = None
        self.peak_engagement_score = 0

        # For throttling joins
        self.last_join_log_time = datetime.now()
        self.pending_joins = 0
        self.pending_join_users = []

        # Analytics tracking
        self.analytics = {
            'peak_viewers': 0,
            'viewer_counts': [],  # Time series: [{'t': timestamp, 'count': N}, ...]
            'top_gifters': {},    # {'user_id': {'nickname': X, 'total_diamonds': N, 'gift_count': N}}
            'top_commenters': {}, # {'user_id': {'nickname': X, 'count': N}}
            'unique_viewers': set(),  # Set of unique user IDs (O(1) lookup)
            'hourly_activity': {},    # {'15:00': {'comments': N, 'gifts': N, 'likes': N}}
            'host_growth': {
                'start_followers': None,
                'end_followers': None,
                'gained': 0,
                'samples': [],  # [{'t': timestamp, 'count': N}, ...]
            },
        }

        # For incremental saving
        self.last_save_time = datetime.now()
        self.save_interval_seconds = 30  # Save every 30 seconds
        self.save_event_interval = 10    # Or every 10 events

    def add_event(self, event_type: str, data: dict):
        """Add an event to the capture."""
        event = {
            "t": datetime.now().isoformat(),
            "type": event_type,
            **data
        }
        self.events.append(event)

        # Update stats
        if event_type == "comment":
            self.total_comments += 1
        elif event_type == "gift":
            self.total_gifts += 1
            self.total_gift_diamonds += data.get("gift", {}).get("total_diamonds", 0) if isinstance(data.get("gift"), dict) else data.get("diamonds", 0)
        elif event_type == "like":
            self.total_likes += data.get("count", 1)
        elif event_type == "follow":
            self.total_follows += 1
        elif event_type == "share":
            self.total_shares += 1
        elif event_type == "join" or event_type == "joins":
            self.total_joins += data.get("count", 1)
        elif event_type == "subscribe":
            self.total_subscribes += 1
        elif event_type == "viewer_count":
            count = data.get("count", 0)
            self.analytics['viewer_counts'].append({
                't': datetime.now().isoformat(),
                'count': count
            })
            if count > self.analytics['peak_viewers']:
                self.analytics['peak_viewers'] = count

        # Track peak engagement
        engagement = self.total_comments + self.total_gifts * 10 + self.total_likes
        if engagement > self.peak_engagement_score:
            self.peak_engagement_score = engagement
            self.peak_engagement_time = datetime.now()

        # Update analytics
        self._update_analytics(event_type, data)

        # Incremental save - every N events or every N seconds
        should_save = False
        if len(self.events) % self.save_event_interval == 0:
            should_save = True
        elif (datetime.now() - self.last_save_time).total_seconds() >= self.save_interval_seconds:
            should_save = True

        if should_save:
            self.save()
            self.last_save_time = datetime.now()

    def _update_analytics(self, event_type: str, data: dict):
        """Update running analytics."""
        # Get user data from event
        user_data = data.get('user', {})
        if isinstance(user_data, str):
            user_data = {'nickname': user_data}

        user_id = str(user_data.get('id') or user_data.get('username') or user_data.get('nickname', 'unknown'))

        # Track unique viewers (O(1) with set)
        if user_id and user_id != 'unknown':
            self.analytics['unique_viewers'].add(user_id)

        # Track top gifters
        if event_type == 'gift':
            gift_data = data.get('gift', {})
            total_diamonds = gift_data.get('total_diamonds', 0) if isinstance(gift_data, dict) else data.get('diamonds', 0)

            if user_id not in self.analytics['top_gifters']:
                self.analytics['top_gifters'][user_id] = {
                    'nickname': user_data.get('nickname', 'Unknown'),
                    'username': user_data.get('username'),
                    'total_diamonds': 0,
                    'gift_count': 0
                }
            self.analytics['top_gifters'][user_id]['total_diamonds'] += total_diamonds
            self.analytics['top_gifters'][user_id]['gift_count'] += 1

        # Track top commenters
        if event_type == 'comment':
            if user_id not in self.analytics['top_commenters']:
                self.analytics['top_commenters'][user_id] = {
                    'nickname': user_data.get('nickname', 'Unknown'),
                    'username': user_data.get('username'),
                    'count': 0
                }
            self.analytics['top_commenters'][user_id]['count'] += 1

        # Hourly activity
        hour = datetime.now().strftime('%H:00')
        if hour not in self.analytics['hourly_activity']:
            self.analytics['hourly_activity'][hour] = {'comments': 0, 'gifts': 0, 'likes': 0, 'joins': 0}

        if event_type == 'comment':
            self.analytics['hourly_activity'][hour]['comments'] += 1
        elif event_type == 'gift':
            self.analytics['hourly_activity'][hour]['gifts'] += 1
        elif event_type == 'like':
            self.analytics['hourly_activity'][hour]['likes'] += data.get('count', 1)
        elif event_type in ('join', 'joins'):
            self.analytics['hourly_activity'][hour]['joins'] += data.get('count', 1)

    def update_host_followers(self, count: int):
        """Update host follower count tracking."""
        if count <= 0:
            return

        host = self.analytics['host_growth']
        host['samples'].append({
            't': datetime.now().isoformat(),
            'count': count
        })

        if host['start_followers'] is None:
            host['start_followers'] = count

        host['end_followers'] = count
        host['gained'] = count - (host['start_followers'] or count)

    def finalize(self, status: str = "completed"):
        """Finalize the capture."""
        self.end_time = datetime.now()
        self.status = status

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        duration = 0
        if self.end_time:
            duration = (self.end_time - self.start_time).total_seconds()
        elif self.start_time:
            duration = (datetime.now() - self.start_time).total_seconds()

        # Sort top gifters and commenters for output
        sorted_gifters = sorted(
            self.analytics['top_gifters'].items(),
            key=lambda x: x[1]['total_diamonds'],
            reverse=True
        )[:20]  # Top 20

        sorted_commenters = sorted(
            self.analytics['top_commenters'].items(),
            key=lambda x: x[1]['count'],
            reverse=True
        )[:20]  # Top 20

        return {
            "stream_id": self.stream_id,
            "username": self.username,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": int(duration),
            "status": self.status,
            "reconnect_count": self.reconnect_count,
            "stats": {
                "total_comments": self.total_comments,
                "total_gifts": self.total_gifts,
                "total_gift_diamonds": self.total_gift_diamonds,
                "total_likes": self.total_likes,
                "total_follows": self.total_follows,
                "total_shares": self.total_shares,
                "total_joins": self.total_joins,
                "total_subscribes": self.total_subscribes,
                "peak_engagement_time": self.peak_engagement_time.isoformat() if self.peak_engagement_time else None,
            },
            "analytics": {
                "peak_viewers": self.analytics['peak_viewers'],
                "unique_viewer_count": len(self.analytics['unique_viewers']),
                "viewer_count_samples": len(self.analytics['viewer_counts']),
                "top_gifters": dict(sorted_gifters),
                "top_commenters": dict(sorted_commenters),
                "hourly_activity": self.analytics['hourly_activity'],
                "host_growth": self.analytics['host_growth'],
            },
            "events": self.events
        }

    def save(self):
        """Save capture to DATA_DIR/captures/{username}/{stream_id}.json for crash recovery."""
        captures_dir = DATA_DIR / "captures" / self.username
        captures_dir.mkdir(parents=True, exist_ok=True)
        filepath = captures_dir / f"{self.stream_id}.json"

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

        return filepath


# ---------------------------------------------------------------------------
# StreamMonitor (single account — kept for internal use by MultiStreamMonitor)
# ---------------------------------------------------------------------------

class StreamMonitor:
    """Single-stream monitor. Used internally by MultiStreamMonitor."""

    def __init__(self, username: str, settings: Optional[dict] = None):
        settings = settings or {}
        self.username = username
        self.state = State.IDLE
        self.running = False
        self.client: Optional[TikTokLiveClient] = None
        self.capture: Optional[StreamCapture] = None

        # Config values
        self.idle_interval = settings.get("idle_check_interval_minutes", 15) * 60
        self.heartbeat_interval = settings.get("live_heartbeat_interval_seconds", 60)
        self.reconnect_attempts = settings.get("reconnect_attempts", 3)
        self.reconnect_delay = settings.get("reconnect_delay_seconds", 30)
        self.offline_confirm = settings.get("offline_confirm_minutes", 5) * 60
        self.throttle_joins = settings.get("throttle_joins", True)
        self.join_log_interval = settings.get("join_log_interval_seconds", 10)

        self._stop_requested = False
        self._host_stats_task = None

        self.logger = logging.getLogger(f"streamsaber.monitor.{username}")

    def _set_state(self, new_state: State):
        """Transition to a new state."""
        old_state = self.state
        self.state = new_state
        self.logger.info(f"State: {old_state.value} -> {new_state.value}")

    async def is_live(self) -> bool:
        """Lightweight check if user is currently live."""
        if not TIKTOK_AVAILABLE:
            self.logger.error("TikTokLive not available")
            return False

        try:
            client = TikTokLiveClient(unique_id=self.username)
            is_live = await client.is_live()
            self.logger.debug(f"is_live check: {is_live}")
            return is_live
        except Exception as e:
            self.logger.warning(f"is_live check failed: {e}")
            return False

    async def fetch_host_stats(self) -> Optional[int]:
        """Fetch host's current follower count."""
        try:
            if self.client:
                try:
                    room_info = await self.client.room_info()
                    if room_info:
                        owner = safe_get(room_info, 'owner')
                        if owner:
                            follower_count = safe_get(owner, 'follower_count') or safe_get(owner, 'followers')
                            if follower_count:
                                return int(follower_count)
                except Exception:
                    pass

            profile = await fetch_tiktok_profile(self.username)
            if 'follower_count' in profile:
                return profile['follower_count']

        except Exception as e:
            self.logger.debug(f"Failed to fetch host stats: {e}")

        return None

    async def _host_stats_loop(self, interval_minutes: int = 5):
        """Background task to periodically fetch host follower count."""
        interval = interval_minutes * 60

        while self.running and self.capture:
            try:
                count = await self.fetch_host_stats()
                if count:
                    self.capture.update_host_followers(count)
                    self.logger.info(f"Host followers: {count:,}")
            except Exception as e:
                self.logger.debug(f"Host stats fetch error: {e}")

            for _ in range(interval):
                if not self.running or not self.capture:
                    return
                await asyncio.sleep(1)

    def _setup_client(self):
        """Create and configure TikTokLive client with event handlers."""
        self.client = TikTokLiveClient(unique_id=self.username)

        @self.client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent):
            self.logger.info(f"Connected to @{self.username}'s stream")
            self._set_state(State.CAPTURING)
            if self.capture:
                self.capture.add_event("connect", {
                    "room_id": safe_get(event, 'room_id'),
                })
                self.capture.save()

        @self.client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            self.logger.warning("Disconnected from stream")
            if self.capture:
                self.capture.add_event("disconnect", {})

        @self.client.on(CommentEvent)
        async def on_comment(event: CommentEvent):
            if self.capture:
                user_data = extract_user(event.user)
                emotes = []
                if hasattr(event, 'emotes') and event.emotes:
                    for e in event.emotes:
                        emotes.append({
                            'id': safe_get(e, 'id'),
                            'name': safe_get(e, 'name'),
                        })
                self.capture.add_event("comment", {
                    "text": event.comment,
                    "emotes": emotes,
                    "user": user_data,
                })

        @self.client.on(GiftEvent)
        async def on_gift(event: GiftEvent):
            is_streaking = safe_get(event, 'streaking', default=False)
            is_streakable = safe_get(event, 'gift', 'streakable', default=False)
            repeat_end = safe_get(event, 'repeat_end', default=True)
            if is_streakable and is_streaking and not repeat_end:
                return

            if self.capture:
                user_data = extract_user(event.user)
                gift_data = extract_gift(event.gift, event)
                self.capture.add_event("gift", {
                    "gift": gift_data,
                    "user": user_data,
                })
                nickname = user_data.get('nickname', 'Unknown')
                gift_name = gift_data.get('name', 'Unknown')
                count = gift_data.get('count', 1)
                diamonds = gift_data.get('total_diamonds', 0)
                self.logger.info(f"Gift: {nickname} sent {count}x {gift_name} ({diamonds} diamonds)")

        @self.client.on(LikeEvent)
        async def on_like(event: LikeEvent):
            if self.capture:
                user_data = extract_user(event.user)
                like_count = safe_get(event, 'likes') or safe_get(event, 'count') or 1
                total_likes = safe_get(event, 'total_likes') or 0
                self.capture.add_event("like", {
                    "count": like_count,
                    "total_likes": total_likes,
                    "user": user_data,
                })

        @self.client.on(FollowEvent)
        async def on_follow(event: FollowEvent):
            if self.capture:
                user_data = extract_user(event.user)
                self.capture.add_event("follow", {
                    "user": user_data,
                })
                self.logger.info(f"New follower: {user_data.get('nickname', 'Unknown')}")

        @self.client.on(ShareEvent)
        async def on_share(event: ShareEvent):
            if self.capture:
                user_data = extract_user(event.user)
                self.capture.add_event("share", {
                    "user": user_data,
                })

        @self.client.on(JoinEvent)
        async def on_join(event: JoinEvent):
            if self.capture:
                user_data = extract_user(event.user)
                if self.throttle_joins:
                    self.capture.pending_joins += 1
                    self.capture.pending_join_users.append(user_data)
                    now = datetime.now()
                    elapsed = (now - self.capture.last_join_log_time).total_seconds()
                    if elapsed >= self.join_log_interval:
                        sample_users = self.capture.pending_join_users[:5]
                        self.capture.add_event("joins", {
                            "count": self.capture.pending_joins,
                            "sample_users": sample_users,
                        })
                        self.capture.pending_joins = 0
                        self.capture.pending_join_users = []
                        self.capture.last_join_log_time = now
                else:
                    self.capture.add_event("join", {
                        "user": user_data,
                    })

        # Optional event handlers
        if HAS_VIEWER_COUNT:
            @self.client.on(ViewerCountUpdateEvent)
            async def on_viewer_count(event):
                if self.capture:
                    count = safe_get(event, 'viewer_count') or safe_get(event, 'count') or 0
                    self.capture.add_event("viewer_count", {
                        "count": count,
                    })
                    self.logger.debug(f"Viewer count: {count}")

        if HAS_ROOM_USER_SEQ:
            @self.client.on(RoomUserSeqEvent)
            async def on_room_user_seq(event):
                if self.capture:
                    count = safe_get(event, 'total') or safe_get(event, 'viewer_count') or safe_get(event, 'total_user') or 0
                    if count > 0:
                        self.capture.add_event("viewer_count", {
                            "count": count,
                        })
                        self.logger.debug(f"Viewer count (RoomUserSeq): {count}")

        if HAS_EMOTE_CHAT:
            @self.client.on(EmoteChatEvent)
            async def on_emote_chat(event):
                if self.capture:
                    user_data = extract_user(event.user)
                    emote_data = {
                        'id': safe_get(event, 'emote', 'id'),
                        'name': safe_get(event, 'emote', 'name'),
                    }
                    self.capture.add_event("emote_chat", {
                        "emote": emote_data,
                        "user": user_data,
                    })

        if HAS_ENVELOPE:
            @self.client.on(EnvelopeEvent)
            async def on_envelope(event):
                if self.capture:
                    self.capture.add_event("envelope", {
                        "type": str(type(event).__name__),
                        "data": str(event)[:500],
                    })
                    self.logger.info("Envelope/Treasure event received")

        if HAS_SUBSCRIBE:
            @self.client.on(SubscribeEvent)
            async def on_subscribe(event):
                if self.capture:
                    user_data = extract_user(event.user)
                    self.capture.add_event("subscribe", {
                        "user": user_data,
                    })
                    self.logger.info(f"New subscriber: {user_data.get('nickname', 'Unknown')}")

        if HAS_QUESTION:
            @self.client.on(QuestionNewEvent)
            async def on_question(event):
                if self.capture:
                    user_data = extract_user(event.user)
                    self.capture.add_event("question", {
                        "text": safe_get(event, 'question') or safe_get(event, 'content') or str(event),
                        "user": user_data,
                    })
                    self.logger.info(f"Q&A Question from {user_data.get('nickname', 'Unknown')}")

        if HAS_LIVE_END:
            @self.client.on(LiveEndEvent)
            async def on_live_end(event):
                self.logger.info("LiveEndEvent received - stream ended")
                if self.capture:
                    self.capture.add_event("live_end", {})

    async def _connect_and_capture(self) -> bool:
        """Attempt to connect and start capturing."""
        for attempt in range(self.reconnect_attempts):
            try:
                self._setup_client()
                self.logger.info(f"Connection attempt {attempt + 1}/{self.reconnect_attempts}")
                await self.client.connect()
                await self.client.wait_for_complete()
                self.logger.info("Connection ended")
                return True
            except Exception as e:
                error_msg = str(e)
                self.logger.warning(f"Connection attempt {attempt + 1} failed: {error_msg}")
                if "503" in error_msg or "sign" in error_msg.lower():
                    self.logger.info("Sign API error - waiting before retry")
                if attempt < self.reconnect_attempts - 1:
                    await asyncio.sleep(self.reconnect_delay)

        return False

    async def _run_idle(self):
        """IDLE state: Check if live periodically."""
        self.logger.info(f"Checking if @{self.username} is live every {self.idle_interval // 60} minutes")

        while self.running and self.state == State.IDLE:
            try:
                if await self.is_live():
                    self.logger.info(f"@{self.username} is LIVE! Starting capture...")
                    return State.CONNECTING
                else:
                    self.logger.debug(f"@{self.username} is not live")
            except Exception as e:
                self.logger.error(f"Error in idle check: {e}")

            for _ in range(self.idle_interval):
                if not self.running or self.state != State.IDLE:
                    break
                await asyncio.sleep(1)

        return self.state

    async def _run_capturing(self):
        """CAPTURING state: Capture stream events."""
        try:
            success = await self._connect_and_capture()

            if not success:
                if await self.is_live():
                    self.logger.info("Still live but can't connect - waiting 2 min")
                    await asyncio.sleep(120)
                    return State.CONNECTING
                else:
                    return State.ENDING

            await asyncio.sleep(5)
            if await self.is_live():
                self.logger.info("Connection dropped but stream still live")
                return State.RECONNECTING
            else:
                return State.ENDING

        except asyncio.CancelledError:
            self.logger.info("Capture cancelled")
            return State.ENDING

        except Exception as e:
            self.logger.error(f"Capture error: {e}")
            self.logger.debug(traceback.format_exc())
            return State.RECONNECTING

    async def _run_reconnecting(self):
        """RECONNECTING state: Attempt to reconnect after disconnect."""
        self._set_state(State.RECONNECTING)

        if self.capture:
            self.capture.reconnect_count += 1

        for attempt in range(self.reconnect_attempts):
            self.logger.info(f"Reconnection attempt {attempt + 1}/{self.reconnect_attempts}")

            try:
                if not await self.is_live():
                    self.logger.info("Stream appears to have ended")
                    await asyncio.sleep(60)
                    if not await self.is_live():
                        return State.ENDING

                self._setup_client()
                await self.client.connect()
                await self.client.wait_for_complete()
                return State.CAPTURING

            except Exception as e:
                self.logger.warning(f"Reconnect attempt {attempt + 1} failed: {e}")
                if attempt < self.reconnect_attempts - 1:
                    await asyncio.sleep(self.reconnect_delay)

        if await self.is_live():
            self.logger.info("Still live but can't reconnect - waiting 2 min")
            await asyncio.sleep(120)
            return State.RECONNECTING
        else:
            return State.ENDING

    async def _run_ending(self):
        """ENDING state: Finalize and save capture."""
        self._set_state(State.ENDING)

        if self._host_stats_task:
            self._host_stats_task.cancel()
            try:
                await self._host_stats_task
            except asyncio.CancelledError:
                pass
            self._host_stats_task = None

        if self.capture:
            final_count = await self.fetch_host_stats()
            if final_count:
                self.capture.update_host_followers(final_count)

        if self.capture:
            if self.capture.pending_joins > 0:
                self.capture.add_event("joins", {
                    "count": self.capture.pending_joins,
                    "sample_users": self.capture.pending_join_users[:5],
                })

            self.capture.finalize("completed")
            filepath = self.capture.save()

            data = self.capture.to_dict()
            stats = data["stats"]
            analytics = data["analytics"]
            duration = data["duration_seconds"]

            self.logger.info(f"Capture complete: {filepath}")
            self.logger.info(f"  Duration: {duration // 60}m {duration % 60}s")
            self.logger.info(f"  Peak viewers: {analytics['peak_viewers']}")
            self.logger.info(f"  Unique viewers: {analytics['unique_viewer_count']}")
            self.logger.info(f"  Comments: {stats['total_comments']}")
            self.logger.info(f"  Gifts: {stats['total_gifts']} ({stats['total_gift_diamonds']} diamonds)")
            self.logger.info(f"  Likes: {stats['total_likes']}")
            self.logger.info(f"  Follows: {stats['total_follows']}")
            self.logger.info(f"  Shares: {stats['total_shares']}")
            self.logger.info(f"  Subscribes: {stats['total_subscribes']}")

            if analytics['top_gifters']:
                self.logger.info("  Top gifters:")
                for uid, gifter in list(analytics['top_gifters'].items())[:5]:
                    self.logger.info(f"    {gifter['nickname']}: {gifter['total_diamonds']} diamonds")

            host_growth = analytics.get('host_growth', {})
            if host_growth.get('start_followers'):
                gained = host_growth.get('gained', 0)
                sign = '+' if gained >= 0 else ''
                self.logger.info(f"  Host growth: {host_growth['start_followers']:,} -> {host_growth['end_followers']:,} ({sign}{gained:,})")

            self.capture = None

        return State.IDLE

    async def run(self):
        """Main daemon loop."""
        self.running = True
        self.logger.info(f"Stream Monitor started for @{self.username}")
        self._set_state(State.IDLE)

        while self.running:
            try:
                if self.state == State.IDLE:
                    next_state = await self._run_idle()
                elif self.state == State.CONNECTING:
                    stream_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                    self.capture = StreamCapture(self.username, stream_id)
                    self.capture.save()
                    self.logger.info(f"Started capture session: {stream_id}")

                    self._host_stats_task = asyncio.create_task(self._host_stats_loop())

                    initial_count = await self.fetch_host_stats()
                    if initial_count:
                        self.capture.update_host_followers(initial_count)
                        self.logger.info(f"Initial host followers: {initial_count:,}")

                    next_state = await self._run_capturing()
                elif self.state == State.CAPTURING:
                    next_state = await self._run_capturing()
                elif self.state == State.RECONNECTING:
                    next_state = await self._run_reconnecting()
                elif self.state == State.ENDING:
                    next_state = await self._run_ending()
                else:
                    next_state = State.IDLE

                if next_state != self.state:
                    self.state = next_state

            except asyncio.CancelledError:
                self.logger.info("Daemon cancelled")
                break
            except Exception as e:
                self.logger.error(f"Unexpected error: {e}")
                self.logger.debug(traceback.format_exc())
                self._set_state(State.IDLE)
                await asyncio.sleep(60)

        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

        self.logger.info("Stream Monitor stopped")

    def stop(self):
        """Stop the daemon gracefully."""
        self.running = False
        self._stop_requested = True


# ---------------------------------------------------------------------------
# MultiStreamMonitor
# ---------------------------------------------------------------------------

class MultiStreamMonitor:
    """Monitor multiple TikTok Live streams simultaneously.

    Accounts are fetched from Supabase on startup and refreshed every 5 minutes.
    High-value events are buffered via SupabaseEventBuffer and flushed to
    tiktok_stream_events. Stream summaries and daily rollups are written to
    Supabase on capture finalization.
    """

    def __init__(self):
        self.running = False

        # Settings (sensible defaults for Fly.io)
        self.max_concurrent = int(os.environ.get("MAX_CONCURRENT_CAPTURES", "10"))
        self.scan_interval = int(os.environ.get("SCAN_INTERVAL_MINUTES", "5")) * 60
        self.check_delay = float(os.environ.get("CHECK_DELAY_SECONDS", "1"))
        self.priority_1_slots = int(os.environ.get("PRIORITY_1_RESERVED_SLOTS", "1"))

        self.settings = {
            "max_concurrent_captures": self.max_concurrent,
            "scan_interval_minutes": self.scan_interval // 60,
            "check_delay_seconds": self.check_delay,
            "priority_1_reserved_slots": self.priority_1_slots,
            "reconnect_attempts": int(os.environ.get("RECONNECT_ATTEMPTS", "3")),
            "reconnect_delay_seconds": int(os.environ.get("RECONNECT_DELAY_SECONDS", "30")),
            "throttle_joins": True,
            "join_log_interval_seconds": 10,
        }

        # Accounts from Supabase: [{id, tiktok_username, label, priority, ...}]
        self.accounts: List[dict] = []  # Raw rows from Supabase
        self.account_states: Dict[str, dict] = {}

        # Active captures: {username: {'task': Task, 'capture': StreamCapture, 'client': TikTokLiveClient, 'event_buffer': SupabaseEventBuffer}}
        self.active_captures: Dict[str, dict] = {}

        # Timestamp of last account refresh
        self._last_account_refresh: float = 0
        self._account_refresh_interval = 300  # 5 minutes

        self.logger = logging.getLogger("streamsaber.multi_monitor")

    async def _refresh_accounts(self):
        """Fetch active accounts from Supabase and update internal state."""
        try:
            rows = await supabase_client.fetch_active_accounts()
            self.accounts = rows

            # Build/update account_states, preserving runtime state for existing accounts
            seen_usernames = set()
            for row in rows:
                username = row["tiktok_username"]
                seen_usernames.add(username)

                if username not in self.account_states:
                    self.account_states[username] = {
                        'account_id': row["id"],
                        'is_live': False,
                        'is_capturing': False,
                        'last_checked': None,
                        'last_stream_start': None,
                        'total_streams_captured': 0,
                        'label': row.get('label', ''),
                        'priority': row.get('priority', 2),
                    }
                else:
                    # Update metadata that may have changed in Supabase
                    self.account_states[username]['account_id'] = row["id"]
                    self.account_states[username]['label'] = row.get('label', '')
                    self.account_states[username]['priority'] = row.get('priority', 2)

            # Remove accounts that are no longer active in Supabase
            # (but keep them if they have an active capture so it can finish)
            for username in list(self.account_states.keys()):
                if username not in seen_usernames and username not in self.active_captures:
                    del self.account_states[username]

            self._last_account_refresh = time.monotonic()
            self.logger.info(f"Refreshed accounts from Supabase: {len(rows)} active")

        except Exception as e:
            self.logger.error(f"Failed to refresh accounts from Supabase: {e}")

    async def _check_is_live(self, username: str) -> bool:
        """Check if a specific user is live."""
        if not TIKTOK_AVAILABLE:
            return False
        try:
            client = TikTokLiveClient(unique_id=username)
            is_live = await client.is_live()
            return is_live
        except Exception as e:
            self.logger.debug(f"[@{username}] is_live check failed: {e}")
            return False

    async def _scan_all_accounts(self):
        """Scan all accounts to check live status."""
        # Refresh accounts from Supabase if interval has elapsed
        if time.monotonic() - self._last_account_refresh >= self._account_refresh_interval:
            await self._refresh_accounts()

        self.logger.info(f"Scanning {len(self.account_states)} accounts...")

        for username in list(self.account_states.keys()):
            if not self.running:
                break

            try:
                is_live = await self._check_is_live(username)
                state = self.account_states[username]
                was_live = state['is_live']
                state['is_live'] = is_live
                state['last_checked'] = datetime.now().isoformat()

                if is_live and not was_live:
                    label = state.get('label', '')
                    self.logger.info(f"[@{username}] is LIVE! ({label})")
                elif not is_live and was_live:
                    self.logger.info(f"[@{username}] went offline")

            except Exception as e:
                self.logger.debug(f"[@{username}] scan error: {e}")

            # Delay between checks to avoid rate limiting
            await asyncio.sleep(self.check_delay)

    async def _manage_captures(self):
        """Start/stop captures based on live status and resource limits."""
        # Get list of live accounts not being captured
        live_not_capturing = []
        for username, state in self.account_states.items():
            if state['is_live'] and not state['is_capturing']:
                live_not_capturing.append((username, state['priority']))

        # Sort by priority (1 first, then 2)
        live_not_capturing.sort(key=lambda x: x[1])

        # Calculate available slots
        current_captures = len(self.active_captures)
        available_slots = self.max_concurrent - current_captures

        # Reserve slots for priority 1
        priority_1_capturing = sum(1 for u in self.active_captures
                                    if self.account_states.get(u, {}).get('priority') == 1)
        priority_1_pending = sum(1 for u, p in live_not_capturing if p == 1)

        # Start new captures
        for username, priority in live_not_capturing:
            if available_slots <= 0:
                self.logger.warning(f"[@{username}] Can't capture - max concurrent limit ({self.max_concurrent}) reached")
                continue

            # Reserve slot for priority 1 if needed
            if priority == 2:
                reserved_needed = self.priority_1_slots - priority_1_capturing
                if available_slots <= reserved_needed and priority_1_pending > 0:
                    self.logger.debug(f"[@{username}] Slot reserved for priority 1 accounts")
                    continue

            # Start capture
            self.logger.info(f"[@{username}] Starting capture...")
            task = asyncio.create_task(self._capture_stream(username))
            self.active_captures[username] = {'task': task, 'started': datetime.now()}
            self.account_states[username]['is_capturing'] = True
            self.account_states[username]['last_stream_start'] = datetime.now().isoformat()
            available_slots -= 1

        # Check for captures that should stop (account went offline)
        for username in list(self.active_captures.keys()):
            state = self.account_states.get(username, {})
            if not state.get('is_live', False) and state.get('is_capturing', False):
                # Let the capture task handle cleanup
                pass

    async def _capture_stream(self, username: str):
        """Background task to capture a single stream."""
        state = self.account_states.get(username, {})
        label = state.get('label', '')
        account_id = state.get('account_id', '')

        # Create capture session
        stream_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        capture = StreamCapture(username, stream_id)

        # Save immediately (crash-recovery backup)
        capture.save()

        # Create event buffer for Supabase
        event_buffer = SupabaseEventBuffer(account_id, stream_id)

        # Fetch initial host stats
        try:
            profile = await fetch_tiktok_profile(username)
            if profile.get('follower_count'):
                capture.update_host_followers(profile['follower_count'])
                self.logger.info(f"[@{username}] Initial followers: {profile['follower_count']:,}")
        except Exception:
            pass

        # Setup client and handlers
        client = TikTokLiveClient(unique_id=username)
        self._setup_handlers(client, capture, username, label, event_buffer)

        # Store references
        if username in self.active_captures:
            self.active_captures[username]['capture'] = capture
            self.active_captures[username]['client'] = client
            self.active_captures[username]['event_buffer'] = event_buffer

        # Background task for periodic viewer count polling
        async def poll_viewer_count():
            poll_interval = 30  # seconds
            while True:
                try:
                    await asyncio.sleep(poll_interval)
                    room = await client.room_info()
                    if room and isinstance(room, dict):
                        viewer_count = (
                            room.get('user_count') or
                            room.get('room_user_count') or
                            room.get('stats', {}).get('total_user') or
                            room.get('liveRoomStats', {}).get('userCount') or
                            0
                        )
                        if viewer_count:
                            capture.add_event("viewer_count", {"count": viewer_count, "source": "poll"})
                            self.logger.debug(f"[@{username}] Viewer count: {viewer_count}")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.debug(f"[@{username}] Viewer poll error: {e}")

        # Background task for periodic event buffer flushing
        async def flush_event_buffer():
            while True:
                try:
                    await asyncio.sleep(5)  # Check every 5 seconds
                    if event_buffer.should_flush():
                        await event_buffer.flush()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.debug(f"[@{username}] Event buffer flush error: {e}")

        viewer_poll_task = asyncio.create_task(poll_viewer_count())
        flush_task = asyncio.create_task(flush_event_buffer())

        # Connect and capture
        reconnect_attempts = self.settings.get('reconnect_attempts', 3)
        reconnect_delay = self.settings.get('reconnect_delay_seconds', 30)

        try:
            for attempt in range(reconnect_attempts):
                try:
                    self.logger.info(f"[@{username}] Connection attempt {attempt + 1}/{reconnect_attempts}")
                    await client.connect()
                    await client.wait_for_complete()
                    self.logger.info(f"[@{username}] Connection ended")
                    break
                except Exception as e:
                    self.logger.warning(f"[@{username}] Connection failed: {e}")
                    if attempt < reconnect_attempts - 1:
                        await asyncio.sleep(reconnect_delay)

            # Check if still live
            await asyncio.sleep(5)
            if await self._check_is_live(username):
                self.logger.info(f"[@{username}] Still live, reconnecting...")
        except asyncio.CancelledError:
            self.logger.info(f"[@{username}] Capture cancelled")
        except Exception as e:
            self.logger.error(f"[@{username}] Capture error: {e}")
        finally:
            # Cancel background tasks
            viewer_poll_task.cancel()
            flush_task.cancel()
            try:
                await viewer_poll_task
            except asyncio.CancelledError:
                pass
            try:
                await flush_task
            except asyncio.CancelledError:
                pass

        # Finalize capture
        await self._finalize_capture(username, capture, event_buffer)

    def _setup_handlers(
        self,
        client: TikTokLiveClient,
        capture: StreamCapture,
        username: str,
        label: str,
        event_buffer: SupabaseEventBuffer,
    ):
        """Setup event handlers for a client."""
        prefix = f"[@{username}]"

        @client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent):
            self.logger.info(f"{prefix} Connected to stream")
            capture.add_event("connect", {"room_id": safe_get(event, 'room_id')})
            capture.save()

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            self.logger.warning(f"{prefix} Disconnected")
            capture.add_event("disconnect", {})

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent):
            user_data = extract_user(event.user)
            emotes = []
            if hasattr(event, 'emotes') and event.emotes:
                for e in event.emotes:
                    emotes.append({'id': safe_get(e, 'id'), 'name': safe_get(e, 'name')})
            event_data = {"text": event.comment, "emotes": emotes, "user": user_data}
            capture.add_event("comment", event_data)
            event_buffer.add("comment", datetime.now(timezone.utc).isoformat(), event_data)

        @client.on(GiftEvent)
        async def on_gift(event: GiftEvent):
            is_streaking = safe_get(event, 'streaking', default=False)
            is_streakable = safe_get(event, 'gift', 'streakable', default=False)
            repeat_end = safe_get(event, 'repeat_end', default=True)
            if is_streakable and is_streaking and not repeat_end:
                return

            user_data = extract_user(event.user)
            gift_data = extract_gift(event.gift, event)
            event_data = {"gift": gift_data, "user": user_data}
            capture.add_event("gift", event_data)
            event_buffer.add("gift", datetime.now(timezone.utc).isoformat(), event_data)

            nickname = user_data.get('nickname', 'Unknown')
            gift_name = gift_data.get('name', 'Unknown')
            count = gift_data.get('count', 1)
            diamonds = gift_data.get('total_diamonds', 0)
            self.logger.info(f"{prefix} Gift: {nickname} sent {count}x {gift_name} ({diamonds} diamonds)")

        @client.on(LikeEvent)
        async def on_like(event: LikeEvent):
            user_data = extract_user(event.user)
            like_count = safe_get(event, 'likes') or safe_get(event, 'count') or 1
            capture.add_event("like", {"count": like_count, "user": user_data})

        @client.on(FollowEvent)
        async def on_follow(event: FollowEvent):
            user_data = extract_user(event.user)
            event_data = {"user": user_data}
            capture.add_event("follow", event_data)
            event_buffer.add("follow", datetime.now(timezone.utc).isoformat(), event_data)
            self.logger.info(f"{prefix} New follower: {user_data.get('nickname', 'Unknown')}")

        @client.on(ShareEvent)
        async def on_share(event: ShareEvent):
            user_data = extract_user(event.user)
            event_data = {"user": user_data}
            capture.add_event("share", event_data)
            event_buffer.add("share", datetime.now(timezone.utc).isoformat(), event_data)

        @client.on(JoinEvent)
        async def on_join(event: JoinEvent):
            user_data = extract_user(event.user)
            # Throttled join logging
            if self.settings.get('throttle_joins', True):
                capture.pending_joins += 1
                capture.pending_join_users.append(user_data)
                now = datetime.now()
                interval = self.settings.get('join_log_interval_seconds', 10)
                if (now - capture.last_join_log_time).total_seconds() >= interval:
                    capture.add_event("joins", {
                        "count": capture.pending_joins,
                        "sample_users": capture.pending_join_users[:5],
                    })
                    capture.pending_joins = 0
                    capture.pending_join_users = []
                    capture.last_join_log_time = now
            else:
                capture.add_event("join", {"user": user_data})

        # Optional events
        if HAS_ROOM_USER_SEQ:
            @client.on(RoomUserSeqEvent)
            async def on_room_user_seq(event):
                count = safe_get(event, 'total') or safe_get(event, 'viewer_count') or 0
                if count > 0:
                    capture.add_event("viewer_count", {"count": count})

        if HAS_SUBSCRIBE:
            @client.on(SubscribeEvent)
            async def on_subscribe(event):
                user_data = extract_user(event.user)
                event_data = {"user": user_data}
                capture.add_event("subscribe", event_data)
                event_buffer.add("subscribe", datetime.now(timezone.utc).isoformat(), event_data)
                self.logger.info(f"{prefix} New subscriber: {user_data.get('nickname', 'Unknown')}")

        if HAS_LIVE_END:
            @client.on(LiveEndEvent)
            async def on_live_end(event):
                self.logger.info(f"{prefix} Stream ended")
                capture.add_event("live_end", {})

    async def _finalize_capture(
        self,
        username: str,
        capture: StreamCapture,
        event_buffer: SupabaseEventBuffer,
    ):
        """Finalize and save a capture. Writes summary to Supabase."""
        state = self.account_states.get(username, {})
        account_id = state.get('account_id', '')

        # Fetch final host stats
        try:
            profile = await fetch_tiktok_profile(username)
            if profile.get('follower_count'):
                capture.update_host_followers(profile['follower_count'])
        except Exception:
            pass

        # Flush pending joins
        if capture.pending_joins > 0:
            capture.add_event("joins", {
                "count": capture.pending_joins,
                "sample_users": capture.pending_join_users[:5],
            })

        # Flush remaining events in the Supabase buffer
        try:
            await event_buffer.flush()
        except Exception as e:
            self.logger.error(f"[@{username}] Final event buffer flush failed: {e}")

        capture.finalize("completed")

        # Save crash-recovery JSON backup
        filepath = capture.save()

        # Build summary dict
        data = capture.to_dict()
        stats = data["stats"]
        analytics = data["analytics"]
        duration = data["duration_seconds"]

        # Prepare top_gifters / top_commenters as lists for Supabase JSON column
        top_gifters_list = [
            {**v, "user_id": k}
            for k, v in list(analytics.get("top_gifters", {}).items())[:20]
        ]
        top_commenters_list = [
            {**v, "user_id": k}
            for k, v in list(analytics.get("top_commenters", {}).items())[:20]
        ]

        summary = {
            "stream_id": capture.stream_id,
            "started_at": capture.start_time.isoformat(),
            "ended_at": capture.end_time.isoformat() if capture.end_time else None,
            "duration_seconds": duration,
            "status": capture.status,
            "total_comments": stats["total_comments"],
            "total_gifts": stats["total_gifts"],
            "total_diamonds": stats["total_gift_diamonds"],
            "total_likes": stats["total_likes"],
            "total_follows": stats["total_follows"],
            "total_shares": stats["total_shares"],
            "total_joins": stats["total_joins"],
            "peak_viewers": analytics.get("peak_viewers", 0),
            "unique_viewers": analytics.get("unique_viewer_count", 0),
            "top_gifters": top_gifters_list,
            "top_commenters": top_commenters_list,
            "hourly_activity": analytics.get("hourly_activity", {}),
            "host_growth": analytics.get("host_growth", {}),
        }

        # Write summary to Supabase
        try:
            await supabase_client.upsert_stream_summary(account_id, summary)
        except Exception as e:
            self.logger.error(f"[@{username}] Failed to upsert stream summary: {e}")

        # Recompute daily rollup for the stream's date
        try:
            date_str = capture.start_time.strftime("%Y-%m-%d")
            await supabase_client.recompute_daily_rollup(account_id, date_str)
        except Exception as e:
            self.logger.error(f"[@{username}] Failed to recompute daily rollup: {e}")

        # Log summary
        self.logger.info(f"[@{username}] Capture complete: {filepath}")
        self.logger.info(f"[@{username}]   Duration: {duration // 60}m {duration % 60}s")
        self.logger.info(f"[@{username}]   Comments: {stats['total_comments']}, Gifts: {stats['total_gifts']} ({stats['total_gift_diamonds']} diamonds)")

        # Update state
        if username in self.account_states:
            self.account_states[username]['is_capturing'] = False
            self.account_states[username]['total_streams_captured'] += 1

        # Cleanup
        if username in self.active_captures:
            del self.active_captures[username]

    async def run(self):
        """Main daemon loop for multi-stream monitoring."""
        self.running = True

        # Fetch accounts from Supabase on startup
        await self._refresh_accounts()

        self.logger.info(f"Multi-Stream Monitor started - watching {len(self.account_states)} accounts")
        self.logger.info(f"Max concurrent captures: {self.max_concurrent}")

        while self.running:
            try:
                # Scan all accounts
                await self._scan_all_accounts()

                # Manage captures (start new, check existing)
                await self._manage_captures()

                # Log status
                live_count = sum(1 for s in self.account_states.values() if s['is_live'])
                capturing_count = len(self.active_captures)
                self.logger.info(f"Status: {live_count} live, {capturing_count} capturing")

                # Wait for next scan
                for _ in range(self.scan_interval):
                    if not self.running:
                        break
                    await asyncio.sleep(1)

            except asyncio.CancelledError:
                self.logger.info("Daemon cancelled")
                break
            except Exception as e:
                self.logger.error(f"Unexpected error: {e}")
                self.logger.debug(traceback.format_exc())
                await asyncio.sleep(60)

        # Cleanup all active captures
        self.logger.info("Stopping all captures...")
        for username, data in list(self.active_captures.items()):
            if 'task' in data:
                data['task'].cancel()
                try:
                    await data['task']
                except asyncio.CancelledError:
                    pass

        self.logger.info("Multi-Stream Monitor stopped")

    def stop(self):
        """Stop the daemon gracefully."""
        self.running = False
