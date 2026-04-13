"""
Trade Replay Viewer API — /api/replay/*

Provides historical scan data replay: scan dates, candidate chains,
tumbler waterfall, shadow comparison, outcomes, and OHLCV bars.
"""

from collections import defaultdict
from datetime import datetime, timedelta

import yfinance as yf
from fastapi import APIRouter, Cookie, Request
from shared import (
    SUPABASE_URL,
    _require_auth,
    _validate_date,
    _validate_ticker,
    _validate_uuid,
    get_http,
    sb_headers,
)

router = APIRouter()

# In-memory OHLCV cache — keyed by "{ticker}_{date}", capped at 100 entries
_ohlcv_cache: dict[str, list] = {}


@router.get("/api/replay/dates")
async def get_replay_dates(request: Request, oc_session: str | None = Cookie(None)):
    """Return distinct dates with CONGRESS_MIRROR scan data, most recent first."""
    _require_auth(request, oc_session)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={
            "select": "created_at,ticker,scan_type",
            "profile_name": "eq.CONGRESS_MIRROR",
            "order": "created_at.desc",
            "limit": "2000",
        },
    )
    if resp.status_code != 200:
        return []
    rows = resp.json()
    by_date: dict[str, dict] = defaultdict(lambda: {"tickers": set(), "sessions": set()})
    for r in rows:
        d = r["created_at"][:10]
        by_date[d]["tickers"].add(r.get("ticker") or "")
        by_date[d]["sessions"].add(r.get("scan_type") or "")
    result = [
        {
            "date": d,
            "candidate_count": len(v["tickers"]),
            "session_count": len(v["sessions"]),
        }
        for d, v in sorted(by_date.items(), reverse=True)
    ]
    return result[:90]


@router.get("/api/replay/candidates")
async def get_replay_candidates(
    request: Request,
    date: str,
    session: str = "morning",
    oc_session: str | None = Cookie(None),
):
    """Return CONGRESS_MIRROR inference chains for a date, optionally filtered by session."""
    _require_auth(request, oc_session)
    date = _validate_date(date)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers={**sb_headers(), "Prefer": "count=exact"},
        params={
            "select": "id,ticker,final_decision,final_confidence,max_depth_reached,stopping_reason,profile_name,scan_type,created_at",
            "profile_name": "eq.CONGRESS_MIRROR",
            "created_at": f"gte.{date}T00:00:00Z",
            "order": "final_confidence.desc",
            "limit": "200",
        },
    )
    if resp.status_code != 200:
        return []
    chains = [c for c in resp.json() if c["created_at"][:10] == date]

    # Filter by session: morning = before 15:00 UTC (8 AM PT), midday = 15:00+
    if session == "morning":
        chains = [c for c in chains if c["created_at"][11:13] < "15"]
    elif session == "midday":
        chains = [c for c in chains if c["created_at"][11:13] >= "15"]

    # Fetch shadow dissent counts for these chains
    chain_ids = [c["id"] for c in chains]
    dissent_counts: dict[str, int] = {}
    if chain_ids:
        id_list = ",".join(chain_ids)
        div_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/shadow_divergences",
            headers=sb_headers(),
            params={
                "select": "live_chain_id",
                "live_chain_id": f"in.({id_list})",
                "limit": "500",
            },
        )
        if div_resp.status_code == 200:
            for d in div_resp.json():
                cid = d.get("live_chain_id")
                if cid:
                    dissent_counts[cid] = dissent_counts.get(cid, 0) + 1

    result = []
    for c in chains:
        result.append(
            {
                "chain_id": c["id"],
                "ticker": c["ticker"],
                "total_score": c.get("total_score", 0),
                "final_decision": c["final_decision"],
                "final_confidence": c["final_confidence"],
                "max_depth_reached": c.get("max_depth_reached"),
                "stopping_reason": c.get("stopping_reason"),
                "profile_name": c["profile_name"],
                "shadow_dissent_count": dissent_counts.get(c["id"], 0),
                "date": date,
            }
        )
    return result


@router.get("/api/replay/chain")
async def get_replay_chain(
    request: Request,
    chain_id: str,
    oc_session: str | None = Cookie(None),
):
    """Return the full inference_chains row (including tumblers JSONB) for a chain ID."""
    _require_auth(request, oc_session)
    chain_id = _validate_uuid(chain_id)
    if not SUPABASE_URL:
        return {}
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={"select": "*", "id": f"eq.{chain_id}"},
    )
    if resp.status_code != 200 or not resp.json():
        return {}
    return resp.json()[0]


@router.get("/api/replay/shadows")
async def get_replay_shadows(
    request: Request,
    ticker: str,
    date: str,
    oc_session: str | None = Cookie(None),
):
    """Return all profile chains for a ticker on a date, with divergence metadata."""
    _require_auth(request, oc_session)
    ticker = _validate_ticker(ticker)
    date = _validate_date(date)
    if not SUPABASE_URL:
        return []
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inference_chains",
        headers=sb_headers(),
        params={
            "select": "id,profile_name,final_decision,final_confidence,max_depth_reached,stopping_reason,tumblers,created_at",
            "ticker": f"eq.{ticker}",
            "created_at": f"gte.{date}T00:00:00Z",
            "order": "profile_name.asc",
            "limit": "20",
        },
    )
    if resp.status_code != 200:
        return []
    chains = [c for c in resp.json() if c["created_at"][:10] == date]

    # Fetch divergence metadata keyed by shadow_chain_id
    chain_ids = [c["id"] for c in chains]
    div_map: dict[str, dict] = {}
    if chain_ids:
        id_list = ",".join(chain_ids)
        div_resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/shadow_divergences",
            headers=sb_headers(),
            params={
                "select": "shadow_chain_id,first_diverged_at_tumbler,shadow_was_right",
                "shadow_chain_id": f"in.({id_list})",
                "limit": "50",
            },
        )
        if div_resp.status_code == 200:
            for d in div_resp.json():
                div_map[d["shadow_chain_id"]] = d

    result = []
    for c in chains:
        div = div_map.get(c["id"], {})
        result.append(
            {
                "profile_name": c["profile_name"],
                "final_decision": c["final_decision"],
                "final_confidence": c["final_confidence"],
                "max_depth_reached": c.get("max_depth_reached"),
                "stopping_reason": c.get("stopping_reason"),
                "tumblers": c.get("tumblers"),
                "first_diverged_at_tumbler": div.get("first_diverged_at_tumbler"),
                "shadow_was_right": div.get("shadow_was_right"),
            }
        )
    return result


@router.get("/api/replay/outcome")
async def get_replay_outcome(
    request: Request,
    ticker: str,
    date: str,
    oc_session: str | None = Cookie(None),
):
    """Return the trade_learnings row for a ticker on a date, if a trade was executed."""
    _require_auth(request, oc_session)
    ticker = _validate_ticker(ticker)
    date = _validate_date(date)
    if not SUPABASE_URL:
        return None
    client = get_http()
    resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/trade_learnings",
        headers=sb_headers(),
        params={
            "select": "*",
            "ticker": f"eq.{ticker}",
            "created_at": f"gte.{date}T00:00:00Z",
            "order": "created_at.desc",
            "limit": "1",
        },
    )
    if resp.status_code != 200 or not resp.json():
        return None
    row = resp.json()[0]
    if row["created_at"][:10] != date:
        return None
    return row


@router.get("/api/replay/ohlcv")
async def get_replay_ohlcv(
    request: Request,
    ticker: str,
    date: str,
    oc_session: str | None = Cookie(None),
):
    """Return 90 days of daily OHLCV bars ending on the given date (yfinance, in-memory cache)."""
    _require_auth(request, oc_session)
    ticker = _validate_ticker(ticker)
    date = _validate_date(date)
    cache_key = f"{ticker}_{date}"
    if cache_key in _ohlcv_cache:
        return _ohlcv_cache[cache_key]

    try:
        end_date = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        df = yf.download(
            ticker, end=end_date, period="90d", interval="1d", progress=False, auto_adjust=True
        )
        if df.empty:
            return []

        result = []
        for idx, row in df.iterrows():
            result.append(
                {
                    "time": idx.strftime("%Y-%m-%d"),
                    "open": round(float(row["Open"]), 2),
                    "high": round(float(row["High"]), 2),
                    "low": round(float(row["Low"]), 2),
                    "close": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]),
                }
            )

        _ohlcv_cache[cache_key] = result
        # Cap cache at 100 entries — evict oldest key
        if len(_ohlcv_cache) > 100:
            oldest = next(iter(_ohlcv_cache))
            del _ohlcv_cache[oldest]

        return result
    except Exception as exc:
        print(f"[replay/ohlcv] {ticker} {date}: {exc}")
        return []
