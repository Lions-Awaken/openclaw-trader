#!/usr/bin/env python3
"""
Catalyst Ingest — polls 6 data sources for market-moving events.

Sources:
  1. Finnhub company_news + insider_transactions (per watchlist ticker)
  2. SEC EDGAR RSS feed (8-K filings, insider forms)
  3. QuiverQuant congressional trades (STOCK Act disclosures)
  4. Perplexity deep search (breaking news for top movers)
  5. Yahoo Finance fundamentals + analyst data (yfinance)
  6. FRED macro indicators (Fed funds, yield curve, CPI, unemployment)

Runs 3x daily on weekdays: 8:30 AM, 12:15 PM, 3:50 PM ET.

Flow: Load watchlist -> poll all sources -> deduplicate (cosine > 0.95)
      -> classify type/magnitude/direction -> embed -> batch insert
"""

import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    FINNHUB_KEY,
    FRED_KEY,
    PERPLEXITY_KEY,
    _client,
    generate_embedding,
    sb_get,
    slack_notify,
)

try:
    import yfinance as yf
except ImportError:
    yf = None
from tracer import PipelineTracer, _post_to_supabase, set_active_tracer, traced

# Lookback window for news (12 hours)
LOOKBACK_HOURS = 12

# Catalyst type classification keywords
CATALYST_KEYWORDS = {
    "earnings_surprise": ["earnings", "eps", "beat", "miss", "revenue", "guidance", "quarter"],
    "analyst_action": ["upgrade", "downgrade", "price target", "initiate", "coverage", "rating"],
    "insider_transaction": ["insider", "purchase", "sale", "filing", "form 4"],
    "congressional_trade": ["congress", "senator", "representative", "stock act", "disclosure"],
    "sec_filing": ["sec", "8-k", "10-k", "10-q", "s-1", "proxy", "registration"],
    "executive_social": ["ceo", "cto", "tweet", "post", "statement"],
    "government_contract": ["contract", "awarded", "government", "defense", "dod", "nasa"],
    "product_launch": ["launch", "release", "announce", "new product", "unveil"],
    "regulatory_action": ["fda", "approval", "cleared", "regulate", "investigation", "fine", "penalty"],
    "macro_event": ["fed", "fomc", "rate", "inflation", "gdp", "jobs", "employment", "cpi", "ppi"],
    "sector_rotation": ["rotation", "sector", "flow", "institutional"],
    "supply_chain": ["supply", "shortage", "chip", "inventory", "backlog"],
    "partnership": ["partner", "collaboration", "joint venture", "deal", "agreement", "acquisition", "merger"],
}

# Direction keywords
BULLISH_KEYWORDS = [
    "beat", "upgrade", "raise", "buy", "outperform", "positive", "growth",
    "approval", "launch", "contract", "bullish", "surge", "breakout", "rally",
    "strong", "exceed", "awarded", "purchase", "insider buy",
]
BEARISH_KEYWORDS = [
    "miss", "downgrade", "lower", "sell", "underperform", "negative", "decline",
    "warning", "investigation", "fine", "recall", "bearish", "plunge", "crash",
    "weak", "disappoint", "insider sale", "penalty", "layoff",
]


def get_watchlist() -> list[str]:
    """Load active tickers for catalyst monitoring."""
    # First try trade-relevant tables
    tickers = set()

    # From signal_evaluations (recent scan targets)
    evals = sb_get("signal_evaluations", {
        "select": "ticker",
        "scan_date": f"gte.{(datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()}",
    })
    for e in evals:
        tickers.add(e["ticker"])

    # Fallback: hardcoded watchlist if no recent evals
    if not tickers:
        tickers = {"NVDA", "AAPL", "MSFT", "AMD", "GOOGL", "META", "AMZN", "TSLA", "AVGO", "SMCI"}

    return sorted(tickers)


@traced("catalysts")
def classify_catalyst(headline: str, content: str = "") -> dict:
    """Classify catalyst type, magnitude, and direction using keyword rules."""
    text = (headline + " " + content).lower()

    # Classify type
    best_type = "other"
    best_score = 0
    for ctype, keywords in CATALYST_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score = score
            best_type = ctype

    # Classify direction
    bull_count = sum(1 for kw in BULLISH_KEYWORDS if kw in text)
    bear_count = sum(1 for kw in BEARISH_KEYWORDS if kw in text)

    if bull_count > bear_count + 1:
        direction = "bullish"
        sentiment = min(1.0, bull_count * 0.2)
    elif bear_count > bull_count + 1:
        direction = "bearish"
        sentiment = max(-1.0, -bear_count * 0.2)
    elif bull_count > 0 or bear_count > 0:
        direction = "ambiguous"
        sentiment = round((bull_count - bear_count) * 0.15, 3)
    else:
        direction = "neutral"
        sentiment = 0.0

    # Classify magnitude
    extreme_words = ["crash", "surge", "plunge", "soar", "halt", "emergency", "extreme"]
    major_words = ["significant", "major", "massive", "sharp", "breaking"]
    if any(w in text for w in extreme_words):
        magnitude = "extreme"
    elif any(w in text for w in major_words):
        magnitude = "major"
    elif best_score >= 2:
        magnitude = "medium"
    else:
        magnitude = "minor"

    return {
        "catalyst_type": best_type,
        "direction": direction,
        "sentiment_score": round(sentiment, 3),
        "magnitude": magnitude,
    }


def check_duplicate(embedding: list[float], recent_embeddings: list[list[float]], threshold: float = 0.95) -> bool:
    """Check if embedding is too similar to any recent one (cosine > threshold)."""
    if not recent_embeddings:
        return False
    for existing in recent_embeddings:
        # Cosine similarity
        dot = sum(a * b for a, b in zip(embedding, existing))
        norm_a = sum(a * a for a in embedding) ** 0.5
        norm_b = sum(b * b for b in existing) ** 0.5
        if norm_a > 0 and norm_b > 0:
            cosine = dot / (norm_a * norm_b)
            if cosine > threshold:
                return True
    return False


# ============================================================================
# Congress enrichment helpers
# ============================================================================

# Simple ticker-to-sector classification for jurisdiction checks
TICKER_SECTOR_MAP = {
    "NVDA": "semiconductors", "AMD": "semiconductors", "INTC": "semiconductors",
    "AVGO": "semiconductors", "QCOM": "semiconductors", "MU": "semiconductors",
    "ARM": "semiconductors", "TSM": "semiconductors", "MRVL": "semiconductors",
    "SMCI": "semiconductors", "AAPL": "technology", "MSFT": "technology",
    "GOOGL": "technology", "META": "technology", "AMZN": "technology",
    "TSLA": "ev", "PLTR": "defense", "LMT": "defense", "RTX": "defense",
    "NOC": "defense", "GD": "defense", "BA": "aerospace",
    "JPM": "banking", "GS": "banking", "MS": "banking",
    "XOM": "energy", "CVX": "energy", "NEE": "energy",
    "UNH": "healthcare", "JNJ": "healthcare", "PFE": "healthcare",
    "LLY": "healthcare", "ABBV": "healthcare", "MRK": "healthcare",
}


def classify_ticker_sector(ticker: str) -> str:
    """Return broad sector classification for a ticker."""
    return TICKER_SECTOR_MAP.get(ticker, "unknown")


def load_politician_scores() -> dict:
    """Load politician signal scores keyed by normalized name."""
    politicians = sb_get("politician_intel", {
        "select": "full_name,signal_score,committees,sector_expertise,"
                  "chronic_late_filer,tracks_spouse,spouse_name,chamber",
    })
    return {p["full_name"].lower().strip(): p for p in politicians}


def score_disclosure_freshness(
    trade_date_str: str, disclosure_date_str: str,
) -> tuple[float, int]:
    """Score how fresh a disclosure is. Returns (freshness_score, days_since_trade)."""
    try:
        fmt = "%Y-%m-%d"
        trade_dt = datetime.strptime(trade_date_str[:10], fmt)
        disc_dt = datetime.strptime(disclosure_date_str[:10], fmt)
        days = max(0, (disc_dt - trade_dt).days)
        # Decay curve: 1-10 days = 1.0, 11-20 = 0.8, 21-30 = 0.5, 31-45 = 0.2
        if days <= 10:
            score = 1.0
        elif days <= 20:
            score = 0.8
        elif days <= 30:
            score = 0.5
        else:
            score = max(0.1, 0.2 - (days - 30) * 0.01)
        return round(score, 3), days
    except Exception:
        return 0.5, -1


@traced("catalysts")
def detect_congress_clusters(
    new_events: list[dict], politician_scores: dict, window_days: int = 14,
) -> list[dict]:
    """Detect multi-member buys of the same ticker within window_days."""
    # Group bullish congress events by ticker
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for ev in new_events:
        if (
            ev.get("catalyst_type") == "congressional_trade"
            and ev.get("direction") == "bullish"
        ):
            by_ticker[ev.get("ticker", "")].append(ev)

    clusters = []
    for ticker, events in by_ticker.items():
        if len(events) < 2:
            continue
        # Check all pairs within window
        for i, ev1 in enumerate(events):
            for ev2 in events[i + 1:]:
                try:
                    d1 = datetime.strptime(ev1["event_time"][:10], "%Y-%m-%d")
                    d2 = datetime.strptime(ev2["event_time"][:10], "%Y-%m-%d")
                    if abs((d2 - d1).days) <= window_days:
                        # Get chambers
                        p1 = politician_scores.get(
                            ev1.get("metadata", {}).get("representative", "").lower(), {},
                        )
                        p2 = politician_scores.get(
                            ev2.get("metadata", {}).get("representative", "").lower(), {},
                        )
                        cross_chamber = (
                            p1.get("chamber") != p2.get("chamber")
                            if p1 and p2 else False
                        )
                        boost = 0.10 if cross_chamber else 0.05
                        clusters.append({
                            "ticker": ticker,
                            "cluster_date": date.today().isoformat(),
                            "member_count": 2,
                            "cross_chamber": cross_chamber,
                            "members": [
                                {
                                    "name": ev1.get("metadata", {}).get(
                                        "representative", "",
                                    ),
                                    "signal_score": ev1.get("metadata", {}).get(
                                        "signal_score", 0,
                                    ),
                                },
                                {
                                    "name": ev2.get("metadata", {}).get(
                                        "representative", "",
                                    ),
                                    "signal_score": ev2.get("metadata", {}).get(
                                        "signal_score", 0,
                                    ),
                                },
                            ],
                            "confidence_boost": boost,
                        })
                except Exception:
                    continue
    return clusters


@traced("catalysts")
def fetch_finnhub_news(ticker: str, lookback_hours: int = LOOKBACK_HOURS) -> list[dict]:
    """Fetch recent company news from Finnhub."""
    if not FINNHUB_KEY:
        return []

    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    events = []
    try:
        client = _client
        resp = client.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": ticker,
                "from": from_date,
                "to": to_date,
            },
            headers={"X-Finnhub-Token": FINNHUB_KEY},
        )
        if resp.status_code == 200:
            for article in resp.json()[:10]:  # Cap at 10 per ticker
                events.append({
                    "ticker": ticker,
                    "headline": article.get("headline", ""),
                    "content": article.get("summary", ""),
                    "source": "finnhub",
                    "source_url": article.get("url", ""),
                    "event_time": datetime.fromtimestamp(
                        article.get("datetime", time.time()), tz=timezone.utc
                    ).isoformat(),
                })
    except Exception as e:
        print(f"[catalyst_ingest] Finnhub news error for {ticker}: {e}")

    return events


@traced("catalysts")
def fetch_finnhub_insiders(ticker: str) -> list[dict]:
    """Fetch insider transactions from Finnhub."""
    if not FINNHUB_KEY:
        return []

    events = []
    try:
        client = _client
        resp = client.get(
            "https://finnhub.io/api/v1/stock/insider-transactions",
            params={"symbol": ticker},
            headers={"X-Finnhub-Token": FINNHUB_KEY},
        )
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS * 4)
            for txn in data[:5]:
                filing_date = txn.get("filingDate", "")
                if filing_date:
                    try:
                        fd = datetime.strptime(filing_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        if fd < cutoff:
                            continue
                    except ValueError:
                        pass

                txn_type = txn.get("transactionType", "").lower()
                shares = txn.get("share", 0) or 0
                value = txn.get("transactionPrice", 0) or 0
                name = txn.get("name", "Unknown")

                headline = f"Insider {txn_type}: {name} — {abs(shares):.0f} shares @ ${value:.2f}"
                events.append({
                    "ticker": ticker,
                    "headline": headline,
                    "content": f"Insider transaction by {name}. Type: {txn_type}. "
                               f"Shares: {shares}. Price: ${value}. Filing: {filing_date}",
                    "source": "finnhub",
                    "source_url": "",
                    "event_time": (datetime.strptime(filing_date, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    ) if filing_date else datetime.now(timezone.utc)).isoformat(),
                })
    except Exception as e:
        print(f"[catalyst_ingest] Finnhub insiders error for {ticker}: {e}")

    return events


@traced("catalysts")
def fetch_sec_edgar_rss() -> list[dict]:
    """Fetch recent 8-K and insider filings from SEC EDGAR RSS."""
    events = []
    feeds = [
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=20&search_text=&output=atom",
    ]

    for feed_url in feeds:
        try:
            client = _client
            resp = client.get(feed_url, headers={"User-Agent": "OpenClaw Trading Bot research@openclaw.dev"})
            if resp.status_code == 200:
                # Simple XML parsing for Atom feed
                text = resp.text
                entries = text.split("<entry>")[1:]  # Skip header
                for entry in entries[:10]:
                    title_match = re.search(r"<title[^>]*>(.*?)</title>", entry, re.DOTALL)
                    link_match = re.search(r'<link[^>]*href="([^"]*)"', entry)
                    updated_match = re.search(r"<updated>(.*?)</updated>", entry)

                    if title_match:
                        title = title_match.group(1).strip()
                        # Extract ticker from title (usually in format "TICKER - 8-K")
                        ticker_match = re.search(r"([A-Z]{1,5})\s*[-—]", title)
                        events.append({
                            "ticker": ticker_match.group(1) if ticker_match else None,
                            "headline": title[:200],
                            "content": title,
                            "source": "sec_edgar",
                            "source_url": link_match.group(1) if link_match else "",
                            "event_time": updated_match.group(1) if updated_match else datetime.now(timezone.utc).isoformat(),
                        })
        except Exception as e:
            print(f"[catalyst_ingest] SEC EDGAR RSS error: {e}")

    return events


@traced("catalysts")
def fetch_quiverquant_trades(politician_scores: dict | None = None) -> list[dict]:
    """Fetch recent congressional trades from QuiverQuant with politician enrichment."""
    if politician_scores is None:
        politician_scores = {}
    events = []
    try:
        client = _client
        resp = client.get(
            "https://api.quiverquant.com/beta/live/congresstrading",
            headers={"Authorization": "Bearer free"},
        )
        if resp.status_code == 200:
            trades = resp.json()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            for trade in trades[:20]:
                report_date = trade.get("ReportDate", "")
                if report_date and report_date < cutoff[:10]:
                    continue

                ticker = trade.get("Ticker", "")
                rep_name = trade.get("Representative", "").strip()
                txn_type = trade.get("Transaction", "")
                amount = trade.get("Amount", "")

                # --- Politician enrichment ---
                rep_key = rep_name.lower()
                pol = politician_scores.get(rep_key, {})
                signal_score = pol.get("signal_score", 0.05)

                # Filter out low-signal members to save embedding cost and noise
                if politician_scores and signal_score < 0.20:
                    continue

                # Score disclosure freshness
                trade_date = (
                    trade.get("TransactionDate", "")
                    or trade.get("Date", "")
                )
                freshness_score, days_since = score_disclosure_freshness(
                    trade_date, report_date,
                )

                # Chronic late filer bonus: if usually late but this one is fresh
                if pol.get("chronic_late_filer") and days_since <= 10:
                    freshness_score = min(1.0, freshness_score + 0.20)

                # Sector jurisdiction check
                ticker_sector = classify_ticker_sector(ticker)
                sector_expertise = pol.get("sector_expertise", [])
                in_jurisdiction = any(
                    s.lower() in ticker_sector.lower() for s in sector_expertise
                )

                # Filer type multiplier
                filer_type = "member"
                if (
                    pol.get("tracks_spouse")
                    and rep_name == pol.get("spouse_name", "")
                ):
                    filer_type = "spouse"

                filer_multiplier = {
                    "member": 1.0, "spouse": 0.85, "dependent_child": 0.70,
                }.get(filer_type, 1.0)

                # Combined effective score
                effective_score = round(
                    signal_score
                    * freshness_score
                    * filer_multiplier
                    * (1.1 if in_jurisdiction else 1.0),
                    3,
                )

                # Determine direction from transaction type
                txn_lower = txn_type.lower()
                if "purchase" in txn_lower or "buy" in txn_lower:
                    direction = "bullish"
                elif "sale" in txn_lower or "sell" in txn_lower:
                    direction = "bearish"
                else:
                    direction = "neutral"

                headline = (
                    f"Congressional trade: {rep_name} — "
                    f"{txn_type} {ticker} ({amount})"
                )
                event = {
                    "ticker": ticker if ticker else None,
                    "headline": headline,
                    "catalyst_type": "congressional_trade",
                    "direction": direction,
                    "content": (
                        f"Congressional trading disclosure. "
                        f"{rep_name} {txn_type} {ticker}. "
                        f"Amount range: {amount}. Report date: {report_date}."
                    ),
                    "source": "quiverquant",
                    "source_url": "",
                    "event_time": (
                        datetime.strptime(report_date, "%Y-%m-%d").replace(
                            tzinfo=timezone.utc,
                        ) if report_date else datetime.now(timezone.utc)
                    ).isoformat(),
                    # Congress enrichment columns
                    "politician_signal_score": effective_score,
                    "disclosure_days_since_trade": days_since,
                    "disclosure_freshness_score": freshness_score,
                    "in_jurisdiction": in_jurisdiction,
                    "filer_type": filer_type,
                    "metadata": {
                        "representative": rep_name,
                        "signal_score": signal_score,
                        "freshness_score": freshness_score,
                        "days_since_trade": days_since,
                        "in_jurisdiction": in_jurisdiction,
                    },
                }
                events.append(event)
    except Exception as e:
        print(f"[catalyst_ingest] QuiverQuant error: {e}")

    return events


@traced("catalysts")
def fetch_perplexity_search(tickers: list[str]) -> list[dict]:
    """Deep search for breaking catalysts using Perplexity API."""
    if not PERPLEXITY_KEY or not tickers:
        return []

    events = []
    # Only search top 3 tickers to stay within budget
    for ticker in tickers[:3]:
        try:
            client = _client
            resp = client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={
                    "Authorization": f"Bearer {PERPLEXITY_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "sonar",
                    "messages": [
                        {"role": "user", "content": (
                            f"What are the most important breaking news, catalysts, or market-moving "
                            f"events for {ticker} stock in the last 4 hours? Include analyst actions, "
                            f"insider trades, SEC filings, earnings, product announcements. "
                            f"If nothing significant, say 'No significant catalysts'. "
                            f"Be concise, 2-3 bullet points max."
                        )}
                    ],
                    "max_tokens": 300,
                },
            )
            if resp.status_code == 200:
                resp_data = resp.json()
                content = resp_data["choices"][0]["message"]["content"]
                if "no significant" not in content.lower():
                    events.append({
                        "ticker": ticker,
                        "headline": f"Perplexity catalyst scan: {ticker}",
                        "content": content,
                        "source": "perplexity",
                        "source_url": "",
                        "event_time": datetime.now(timezone.utc).isoformat(),
                    })

                # Log cost
                usage = resp_data.get("usage", {})
                tokens_in = usage.get("prompt_tokens", 0)
                tokens_out = usage.get("completion_tokens", 0)
                # Perplexity sonar: ~$0.001/1K tokens
                cost = (tokens_in + tokens_out) * 0.001 / 1000
                if cost > 0:
                    _post_to_supabase("cost_ledger", {
                        "ledger_date": datetime.now(timezone.utc).date().isoformat(),
                        "category": "perplexity_api",
                        "subcategory": f"catalyst_ingest_{ticker}",
                        "amount": round(-cost, 6),
                        "description": f"Perplexity catalyst scan for {ticker}",
                        "metadata": {"tokens_in": tokens_in, "tokens_out": tokens_out, "model": "sonar"},
                    })

        except Exception as e:
            print(f"[catalyst_ingest] Perplexity search error for {ticker}: {e}")

    return events


# FRED series we track: (series_id, display_name, threshold_pct for "significant change")
FRED_SERIES = [
    ("FEDFUNDS", "Fed Funds Rate", 0.0),       # Any change is significant
    ("T10Y2Y", "10Y-2Y Yield Spread", 0.0),    # Inversion signal — any flip matters
    ("CPIAUCSL", "CPI (All Urban)", 0.15),      # >0.15% MoM is notable
    ("UNRATE", "Unemployment Rate", 0.1),        # >0.1pp change
    ("DGS10", "10-Year Treasury Yield", 0.05),  # >5bp move
    ("BAMLH0A0HYM2", "High Yield Spread", 0.1), # Credit stress indicator
]


@traced("catalysts")
def fetch_yfinance_signals(tickers: list[str]) -> list[dict]:
    """Fetch analyst recs, earnings dates, and fundamental flags from Yahoo Finance."""
    if yf is None:
        print("[catalyst_ingest] yfinance not installed, skipping")
        return []

    events = []
    now = datetime.now(timezone.utc)

    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
            if not info or info.get("trailingPegRatio") is None and info.get("forwardPE") is None:
                continue

            # --- Analyst target vs current price ---
            target = info.get("targetMeanPrice")
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if target and price and price > 0:
                upside_pct = ((target - price) / price) * 100
                if abs(upside_pct) >= 15:
                    direction = "bullish" if upside_pct > 0 else "bearish"
                    events.append({
                        "ticker": ticker,
                        "headline": f"{ticker}: analyst target ${target:.0f} vs price ${price:.0f} ({upside_pct:+.1f}%)",
                        "content": (
                            f"Analyst consensus target ${target:.2f} implies {upside_pct:+.1f}% "
                            f"from current ${price:.2f}. Recommendations: "
                            f"{info.get('recommendationKey', 'n/a')} "
                            f"({info.get('numberOfAnalystOpinions', 0)} analysts)."
                        ),
                        "source": "yfinance",
                        "catalyst_type": "analyst_action",
                        "direction": direction,
                        "event_time": now.isoformat(),
                        "metadata": {
                            "target_mean": target,
                            "target_low": info.get("targetLowPrice"),
                            "target_high": info.get("targetHighPrice"),
                            "current_price": price,
                            "upside_pct": round(upside_pct, 2),
                            "recommendation": info.get("recommendationKey"),
                            "num_analysts": info.get("numberOfAnalystOpinions"),
                        },
                    })

            # --- Fundamental flags ---
            forward_pe = info.get("forwardPE")
            trailing_pe = info.get("trailingPE")
            debt_equity = info.get("debtToEquity")
            peg = info.get("trailingPegRatio")

            flags = []
            if forward_pe and forward_pe > 50:
                flags.append(f"high forward P/E ({forward_pe:.1f})")
            if trailing_pe and forward_pe and trailing_pe > 0:
                pe_change = ((forward_pe - trailing_pe) / trailing_pe) * 100
                if abs(pe_change) > 20:
                    flags.append(f"P/E shift {pe_change:+.0f}% (trailing {trailing_pe:.1f} → forward {forward_pe:.1f})")
            if debt_equity and debt_equity > 200:
                flags.append(f"high debt/equity ({debt_equity:.0f}%)")
            if peg and peg < 0.5:
                flags.append(f"low PEG ratio ({peg:.2f})")

            if flags:
                events.append({
                    "ticker": ticker,
                    "headline": f"{ticker}: fundamental signals — {'; '.join(flags)}",
                    "content": (
                        f"Fundamental snapshot for {ticker}: "
                        f"P/E {trailing_pe or 'n/a'} trailing / {forward_pe or 'n/a'} forward, "
                        f"PEG {peg or 'n/a'}, debt/equity {debt_equity or 'n/a'}%, "
                        f"market cap ${info.get('marketCap', 0) / 1e9:.1f}B. "
                        f"Flags: {'; '.join(flags)}."
                    ),
                    "source": "yfinance",
                    "catalyst_type": "fundamental_shift",
                    "direction": "bearish" if any("high" in f for f in flags) else "neutral",
                    "event_time": now.isoformat(),
                    "metadata": {
                        "forward_pe": forward_pe,
                        "trailing_pe": trailing_pe,
                        "peg_ratio": peg,
                        "debt_to_equity": debt_equity,
                        "market_cap": info.get("marketCap"),
                        "flags": flags,
                    },
                })

            time.sleep(0.3)  # Rate-limit Yahoo Finance

        except Exception as e:
            print(f"[catalyst_ingest] yfinance error for {ticker}: {e}")

    return events


@traced("catalysts")
def fetch_fred_macro() -> list[dict]:
    """Fetch key macro indicators from FRED and generate events on significant changes."""
    if not FRED_KEY:
        print("[catalyst_ingest] FRED_API_KEY not set, skipping")
        return []

    events = []

    for series_id, name, threshold in FRED_SERIES:
        try:
            resp = _client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": FRED_KEY,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 2,
                },
            )
            if resp.status_code != 200:
                continue

            obs = resp.json().get("observations", [])
            if len(obs) < 2:
                continue

            latest_val = obs[0].get("value", ".")
            prev_val = obs[1].get("value", ".")
            latest_date = obs[0].get("date", "")

            # Skip missing values (FRED uses "." for missing)
            if latest_val == "." or prev_val == ".":
                continue

            latest = float(latest_val)
            prev = float(prev_val)
            change = latest - prev

            if abs(change) < threshold:
                continue

            # Determine direction based on series meaning
            if series_id in ("FEDFUNDS", "DGS10", "UNRATE", "BAMLH0A0HYM2"):
                direction = "bearish" if change > 0 else "bullish"
            elif series_id == "T10Y2Y":
                direction = "bearish" if latest < 0 else "bullish"
            else:
                direction = "bearish" if change > 0 else "bullish"  # CPI up = bearish

            events.append({
                "ticker": None,  # Macro events are market-wide
                "headline": f"FRED {name}: {latest:.3f} ({change:+.3f} from {prev:.3f})",
                "content": (
                    f"{name} ({series_id}) updated to {latest:.4f} on {latest_date}, "
                    f"previous {prev:.4f} (change: {change:+.4f}). "
                    f"{'Yield curve inverted — recession signal.' if series_id == 'T10Y2Y' and latest < 0 else ''}"
                    f"{'Credit spreads widening — risk-off signal.' if series_id == 'BAMLH0A0HYM2' and change > 0 else ''}"
                    f"{'Rate hike environment.' if series_id == 'FEDFUNDS' and change > 0 else ''}"
                ),
                "source": "fred",
                "catalyst_type": "macro_event",
                "direction": direction,
                "event_time": f"{latest_date}T00:00:00+00:00",
                "metadata": {
                    "series_id": series_id,
                    "series_name": name,
                    "latest_value": latest,
                    "previous_value": prev,
                    "change": round(change, 4),
                    "observation_date": latest_date,
                },
            })

        except Exception as e:
            print(f"[catalyst_ingest] FRED error for {series_id}: {e}")

    return events


def run():
    tracer = PipelineTracer("catalyst_ingest", metadata={"time": datetime.now(timezone.utc).isoformat()})
    set_active_tracer(tracer)

    try:
        # Step 1: Load watchlist
        with tracer.step("load_watchlist") as result:
            watchlist = get_watchlist()
            result.set({"tickers": watchlist, "count": len(watchlist)})

        all_raw_events = []
        finnhub_count = 0
        sec_count = 0
        qq_count = 0
        ppx_count = 0
        yf_count = 0
        fred_count = 0

        # Step 2: Finnhub news + insiders per ticker
        with tracer.step("fetch_finnhub", input_snapshot={"tickers": watchlist}) as result:
            for ticker in watchlist:
                all_raw_events.extend(fetch_finnhub_news(ticker))
                all_raw_events.extend(fetch_finnhub_insiders(ticker))
                time.sleep(0.2)  # Respect Finnhub rate limit (60/min)
            finnhub_count = len(all_raw_events)
            result.set({"finnhub_events": finnhub_count})

        # Step 3: SEC EDGAR RSS
        with tracer.step("fetch_sec_edgar") as result:
            sec_events = fetch_sec_edgar_rss()
            # Filter to only watchlist tickers
            for ev in sec_events:
                if ev["ticker"] and ev["ticker"] in watchlist:
                    all_raw_events.append(ev)
            sec_count = len([e for e in sec_events if e["ticker"] in watchlist])
            result.set({"sec_events": len(sec_events), "matched": sec_count})

        # Load politician scores for congress enrichment
        politician_scores = load_politician_scores()

        # Step 4: QuiverQuant congressional trades (with politician enrichment)
        with tracer.step("fetch_quiverquant") as result:
            qq_events = fetch_quiverquant_trades(politician_scores)
            for ev in qq_events:
                if ev["ticker"] and ev["ticker"] in watchlist:
                    all_raw_events.append(ev)
            qq_count = len([e for e in qq_events if e.get("ticker") in watchlist])
            result.set({"qq_events": len(qq_events), "matched": qq_count})

        # Step 5: Perplexity deep search for top movers
        with tracer.step("fetch_perplexity", input_snapshot={"tickers": watchlist[:3]}) as result:
            ppx_events = fetch_perplexity_search(watchlist)
            all_raw_events.extend(ppx_events)
            ppx_count = len(ppx_events)
            result.set({"perplexity_events": ppx_count})

        # Step 6: Yahoo Finance fundamentals + analyst data
        with tracer.step("fetch_yfinance", input_snapshot={"tickers": watchlist}) as result:
            yf_events = fetch_yfinance_signals(watchlist)
            all_raw_events.extend(yf_events)
            yf_count = len(yf_events)
            result.set({"yfinance_events": yf_count})

        # Step 7: FRED macro indicators
        with tracer.step("fetch_fred") as result:
            fred_events = fetch_fred_macro()
            all_raw_events.extend(fred_events)
            fred_count = len(fred_events)
            result.set({"fred_events": fred_count})

        print(f"[catalyst_ingest] Raw events collected: {len(all_raw_events)}")

        # Step 8: Classify, embed, deduplicate, insert
        with tracer.step("classify_embed_insert", input_snapshot={"raw_count": len(all_raw_events)}) as result:
            recent_embeddings = []
            inserted = 0
            duplicates = 0

            for raw_event in all_raw_events:
                headline = raw_event.get("headline", "")
                content = raw_event.get("content", "")
                if not headline:
                    continue

                # Classify
                classification = classify_catalyst(headline, content)

                # Embed
                embed_text = f"{headline}. {content}"[:500]
                embedding = generate_embedding(embed_text)

                # Deduplicate
                if embedding and check_duplicate(embedding, recent_embeddings):
                    duplicates += 1
                    continue

                if embedding:
                    recent_embeddings.append(embedding)
                    # Cap at 100 to prevent unbounded memory growth on Jetson
                    if len(recent_embeddings) > 100:
                        recent_embeddings = recent_embeddings[-100:]

                # Determine affected tickers
                affected = []
                if raw_event.get("ticker"):
                    affected.append(raw_event["ticker"])

                # Build record — prefer raw event catalyst_type/direction if set
                record = {
                    "ticker": raw_event.get("ticker"),
                    "catalyst_type": raw_event.get("catalyst_type") or classification["catalyst_type"],
                    "headline": headline[:200],
                    "source": raw_event["source"],
                    "source_url": raw_event.get("source_url", ""),
                    "event_time": raw_event.get("event_time", datetime.now(timezone.utc).isoformat()),
                    "magnitude": classification["magnitude"],
                    "direction": raw_event.get("direction") or classification["direction"],
                    "sentiment_score": classification["sentiment_score"],
                    "affected_tickers": affected,
                    "content": embed_text,
                    "metadata": raw_event.get("metadata", {}),
                }

                # Carry through congress enrichment columns if present
                for col in (
                    "politician_signal_score",
                    "disclosure_days_since_trade",
                    "disclosure_freshness_score",
                    "in_jurisdiction",
                    "filer_type",
                ):
                    if col in raw_event:
                        record[col] = raw_event[col]

                if embedding:
                    record["embedding"] = embedding

                stored = _post_to_supabase("catalyst_events", record)
                if stored:
                    inserted += 1

            result.set({"inserted": inserted, "duplicates": duplicates, "total_raw": len(all_raw_events)})

        # Step 9: Detect congress clusters
        with tracer.step("detect_congress_clusters") as result:
            congress_events = [
                e for e in all_raw_events
                if e.get("catalyst_type") == "congressional_trade"
                or e.get("source") == "quiverquant"
            ]
            clusters = detect_congress_clusters(
                congress_events, politician_scores,
            )
            for cluster in clusters:
                _post_to_supabase("congress_clusters", cluster)
            result.set({"clusters_detected": len(clusters)})

        tracer.complete({"total_inserted": inserted, "total_duplicates": duplicates})
        print(f"[catalyst_ingest] Complete. Inserted: {inserted}, Duplicates: {duplicates}")
        slack_notify(
            f"*Catalyst Ingest complete* — `{inserted}` events inserted, `{duplicates}` dupes skipped\n"
            f"Sources: finnhub `{finnhub_count}` · sec `{sec_count}` · quiverquant `{qq_count}` · perplexity `{ppx_count}` · yfinance `{yf_count}` · fred `{fred_count}`"
        )

    except Exception as e:
        tracer.fail(str(e))
        print(f"[catalyst_ingest] Failed: {e}")
        slack_notify(f"*Catalyst Ingest FATAL*: {e}")
        raise


if __name__ == "__main__":
    from loki_logger import get_logger
    _logger = get_logger("catalyst_ingest")
    run()
