#!/usr/bin/env python3
"""
Catalyst Ingest — polls 4 data sources for market-moving events.

Sources:
  1. Finnhub company_news + insider_transactions (per watchlist ticker)
  2. SEC EDGAR RSS feed (8-K filings, insider forms)
  3. QuiverQuant congressional trades (STOCK Act disclosures)
  4. Perplexity deep search (breaking news for top movers)

Runs 3x daily on weekdays: 8:30 AM, 12:15 PM, 3:50 PM ET.

Flow: Load watchlist -> poll all sources -> deduplicate (cosine > 0.95)
      -> classify type/magnitude/direction -> embed -> batch insert
"""

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from tracer import PipelineTracer, _post_to_supabase, _sb_headers

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
PERPLEXITY_KEY = os.environ.get("PERPLEXITY_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

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


def sb_get(path: str, params: dict | None = None) -> list:
    """GET from Supabase REST API."""
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers=_sb_headers(),
            params=params or {},
        )
        if resp.status_code == 200:
            return resp.json()
    return []


def get_watchlist() -> list[str]:
    """Load active tickers from tiktok_accounts (used as watchlist source)."""
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


def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding via Ollama, then release model memory."""
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": text, "keep_alive": "0"},
            )
            if resp.status_code == 200:
                return resp.json().get("embedding")
    except Exception:
        pass
    return None


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


def fetch_finnhub_news(ticker: str, lookback_hours: int = LOOKBACK_HOURS) -> list[dict]:
    """Fetch recent company news from Finnhub."""
    if not FINNHUB_KEY:
        return []

    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")

    events = []
    try:
        with httpx.Client(timeout=15.0) as client:
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


def fetch_finnhub_insiders(ticker: str) -> list[dict]:
    """Fetch insider transactions from Finnhub."""
    if not FINNHUB_KEY:
        return []

    events = []
    try:
        with httpx.Client(timeout=15.0) as client:
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


def fetch_sec_edgar_rss() -> list[dict]:
    """Fetch recent 8-K and insider filings from SEC EDGAR RSS."""
    events = []
    feeds = [
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=20&search_text=&output=atom",
    ]

    for feed_url in feeds:
        try:
            with httpx.Client(timeout=15.0) as client:
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


def fetch_quiverquant_trades() -> list[dict]:
    """Fetch recent congressional trades from QuiverQuant."""
    events = []
    try:
        with httpx.Client(timeout=15.0) as client:
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
                    name = trade.get("Representative", "Unknown")
                    txn_type = trade.get("Transaction", "")
                    amount = trade.get("Amount", "")

                    headline = f"Congressional trade: {name} — {txn_type} {ticker} ({amount})"
                    events.append({
                        "ticker": ticker if ticker else None,
                        "headline": headline,
                        "content": f"Congressional trading disclosure. {name} {txn_type} {ticker}. "
                                   f"Amount range: {amount}. Report date: {report_date}.",
                        "source": "quiverquant",
                        "source_url": "",
                        "event_time": (datetime.strptime(report_date, "%Y-%m-%d").replace(
                            tzinfo=timezone.utc
                        ) if report_date else datetime.now(timezone.utc)).isoformat(),
                    })
    except Exception as e:
        print(f"[catalyst_ingest] QuiverQuant error: {e}")

    return events


def fetch_perplexity_search(tickers: list[str]) -> list[dict]:
    """Deep search for breaking catalysts using Perplexity API."""
    if not PERPLEXITY_KEY or not tickers:
        return []

    events = []
    # Only search top 3 tickers to stay within budget
    for ticker in tickers[:3]:
        try:
            with httpx.Client(timeout=30.0) as client:
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


def run():
    tracer = PipelineTracer("catalyst_ingest", metadata={"time": datetime.now(timezone.utc).isoformat()})

    try:
        # Step 1: Load watchlist
        with tracer.step("load_watchlist") as result:
            watchlist = get_watchlist()
            result.set({"tickers": watchlist, "count": len(watchlist)})

        all_raw_events = []

        # Step 2: Finnhub news + insiders per ticker
        with tracer.step("fetch_finnhub", input_snapshot={"tickers": watchlist}) as result:
            for ticker in watchlist:
                all_raw_events.extend(fetch_finnhub_news(ticker))
                all_raw_events.extend(fetch_finnhub_insiders(ticker))
                time.sleep(0.2)  # Respect Finnhub rate limit (60/min)
            result.set({"finnhub_events": len(all_raw_events)})

        # Step 3: SEC EDGAR RSS
        with tracer.step("fetch_sec_edgar") as result:
            sec_events = fetch_sec_edgar_rss()
            # Filter to only watchlist tickers
            for ev in sec_events:
                if ev["ticker"] and ev["ticker"] in watchlist:
                    all_raw_events.append(ev)
            result.set({"sec_events": len(sec_events), "matched": len([e for e in sec_events if e["ticker"] in watchlist])})

        # Step 4: QuiverQuant congressional trades
        with tracer.step("fetch_quiverquant") as result:
            qq_events = fetch_quiverquant_trades()
            for ev in qq_events:
                if ev["ticker"] and ev["ticker"] in watchlist:
                    all_raw_events.append(ev)
            result.set({"qq_events": len(qq_events), "matched": len([e for e in qq_events if e.get("ticker") in watchlist])})

        # Step 5: Perplexity deep search for top movers
        with tracer.step("fetch_perplexity", input_snapshot={"tickers": watchlist[:3]}) as result:
            ppx_events = fetch_perplexity_search(watchlist)
            all_raw_events.extend(ppx_events)
            result.set({"perplexity_events": len(ppx_events)})

        print(f"[catalyst_ingest] Raw events collected: {len(all_raw_events)}")

        # Step 6: Classify, embed, deduplicate, insert
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

                # Build record
                record = {
                    "ticker": raw_event.get("ticker"),
                    "catalyst_type": classification["catalyst_type"],
                    "headline": headline[:200],
                    "source": raw_event["source"],
                    "source_url": raw_event.get("source_url", ""),
                    "event_time": raw_event.get("event_time", datetime.now(timezone.utc).isoformat()),
                    "magnitude": classification["magnitude"],
                    "direction": classification["direction"],
                    "sentiment_score": classification["sentiment_score"],
                    "affected_tickers": affected,
                    "content": embed_text,
                    "metadata": {},
                }
                if embedding:
                    record["embedding"] = embedding

                stored = _post_to_supabase("catalyst_events", record)
                if stored:
                    inserted += 1

            result.set({"inserted": inserted, "duplicates": duplicates, "total_raw": len(all_raw_events)})

        tracer.complete({"total_inserted": inserted, "total_duplicates": duplicates})
        print(f"[catalyst_ingest] Complete. Inserted: {inserted}, Duplicates: {duplicates}")

    except Exception as e:
        tracer.fail(str(e))
        print(f"[catalyst_ingest] Failed: {e}")
        raise


if __name__ == "__main__":
    run()
