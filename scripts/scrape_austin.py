#!/usr/bin/env python3
"""Scrape Austin event sources and emit public/discovered.json.

Output matches Side Quest's *internal* event schema exactly, so the app can
load the file and drop the events straight into its list with no adapter:

    {
      "id": "do512-10293",
      "title": "String",
      "date": "YYYY-MM-DD",          # single date (event start)
      "time": "7:00 PM",             # start time, "" if unknown
      "endTime": "10:00 PM",         # end time, "" if unknown
      "location": "String",
      "price": "Free" | "$25" | "",
      "description": "String",
      "category": "music|food|art|sports|social|film|comedy|outdoors|other",
      "link": "String",
      "notes": "Discovered via [Source Name]",
      "status": "want",
      "isRecurring": false,
      "recurringType": null,
      "thumbnail": "https://... (image URL) or ''",
      "addedAt": 1712345678901
    }

Sources:
  1. Do512             - https://do512.com/  (schema.org Event JSON-LD)
  2. 365 Things Austin - RSS feed
  3. RSS-Bridge feeds for a handful of public Instagram accounts

Events whose date is before today are dropped. Output is sorted by date. The
scrape is best-effort: a failure in one source is logged and skipped, it never
aborts the others.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "public" / "discovered.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; SideQuestScraper/1.0; "
    "+https://side-quest.samarthmaira9.workers.dev)"
)
REQUEST_TIMEOUT = 25  # seconds

DO512_URLS = [
    "https://do512.com/",
    "https://do512.com/events",
]

THINGS_365_RSS = "https://365thingsaustin.com/feed/"

# RSS-Bridge turns public pages into RSS. Public instances rate-limit Instagram
# hard (HTTP 429), so for reliable Instagram you must point this at your OWN
# instance signed into an IG account. Comma-separate multiple bases to try in
# order (e.g. "https://my-bridge.example/,https://rss-bridge.org/bridge01/").
RSS_BRIDGE_BASES = [
    b.strip()
    for b in (
        os.environ.get("RSS_BRIDGE_BASE") or "https://rss-bridge.org/bridge01/"
    ).split(",")
    if b.strip()
]
# Which RSS-Bridge bridges to try per account, in order. PicukiBridge reads a
# public IG mirror and often survives when InstagramBridge is rate-limited.
IG_BRIDGES = ["InstagramBridge", "PicukiBridge"]
IG_ACCOUNTS = [
    "whenwherewhataustin",
    "365thingsaustin",
    "theaustintourist",
    "emilylovesatx",
]

# Optional residential/scraping proxy (e.g. ScraperAPI). When SCRAPERAPI_KEY is
# set, every http_get() is routed through it so IP-blocked sites like Do512
# (which 403s datacenter IPs) become reachable. Without it, fetches go direct.
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "").strip()


def via_proxy(url: str) -> str:
    if not SCRAPERAPI_KEY:
        return url
    from urllib.parse import quote
    return (
        "https://api.scraperapi.com/?api_key="
        + SCRAPERAPI_KEY
        + "&country_code=us&url="
        + quote(url, safe="")
    )

# The app's valid category keys are exactly:
#   music, food, art, sports, social, film, comedy, outdoors, other
# Keyword -> category. First match wins; order matters (specific first).
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("music", ["concert", "live music", "dj", "band", "festival", "acl",
               "vinyl", "singer", "album", "residency", "gig", "sxsw music"]),
    ("comedy", ["comedy", "improv", "stand-up", "standup", "open mic"]),
    ("film", ["film", "screening", "movie", "cinema", "documentary", "premiere"]),
    ("food", ["food", "brunch", "dinner", "taco", "bbq", "restaurant", "tasting",
              "wine", "beer", "brewery", "coffee", "market", "pop-up", "popup",
              "culinary", "supper", "cook", "bites", "eats"]),
    ("outdoors", ["hike", "trail", "park", "lake", "kayak", "paddle", "bike",
                  "outdoor", "garden", "greenbelt", "swim", "camping", "nature",
                  "picnic", "sunset"]),
    ("sports", ["game", "match", " vs ", "soccer", "basketball", "football",
                "baseball", "tournament", "race", "marathon", "5k", "fitness",
                "yoga", "pickleball", "padel", "tennis", "workout", "run club"]),
    ("art", ["art", "gallery", "museum", "theatre", "theater", "exhibit", "dance",
             "poetry", "craft", "workshop", "book", "author", "mural", "design"]),
    ("social", ["nightlife", "club", "bar crawl", "happy hour", "rooftop",
                "cocktail", "party", "speakeasy", "mixer", "meetup", "fundraiser",
                "charity", "community", "networking", "celebration", "parade",
                "family", "kids", "fair", "trivia", "karaoke", "singles"]),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("scrape_austin")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def today() -> date:
    return datetime.utcnow().date()


def now_ms() -> int:
    return int(time.time() * 1000)


BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def http_get(url: str) -> requests.Response | None:
    try:
        resp = requests.get(
            via_proxy(url), headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        log.warning("GET failed for %s: %s", url, exc)
        return None


def categorize(*texts: str) -> str:
    """Pick an app category from free text via keyword match; default other."""
    blob = " " + " ".join(t for t in texts if t).lower() + " "
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in blob for kw in keywords):
            return category
    return "other"


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return dateparser.parse(str(value), fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return None


def parse_time_str(value: Any) -> str:
    """Return 'H:MM AM' if the value carries a real time component, else ''."""
    if not value:
        return ""
    try:
        dt = dateparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return ""
    if dt is None or (dt.hour == 0 and dt.minute == 0):
        return ""
    return dt.strftime("%-I:%M %p")


def normalize_price(raw: Any) -> str:
    if raw is None or raw == "":
        return ""
    text = str(raw).strip()
    if text.lower() in ("0", "0.0", "0.00", "free"):
        return "Free"
    if text.startswith("$"):
        return text
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return f"${text}"
    return text


def clean_text(html_or_text: str | None, limit: int = 600) -> str:
    if not html_or_text:
        return ""
    text = BeautifulSoup(str(html_or_text), "html.parser").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "item"


def make_event(
    *,
    source_id: str,
    title: str,
    start: date | None,
    end: date | None = None,
    time_str: str = "",
    end_time_str: str = "",
    location: str = "",
    price: str = "",
    description: str = "",
    category: str = "",
    link: str = "",
    image: str = "",
    source_name: str,
) -> dict[str, Any] | None:
    """Assemble one event in the app's internal schema, or None if unusable."""
    title = (title or "").strip()
    if not title or start is None:
        return None

    notes = f"Discovered via {source_name}"
    # The app stores a single date; note a multi-day range in the notes.
    if end and end > start:
        notes = f"Through {end.strftime('%b %-d')} · {notes}"

    return {
        "id": source_id,
        "title": title,
        "date": start.isoformat(),
        "time": time_str or "",
        "endTime": end_time_str or "",
        "location": location or "",
        "price": normalize_price(price),
        "description": description or "",
        "category": category or categorize(title, description),
        "link": link or "",
        "notes": notes,
        "status": "want",
        "isRecurring": False,
        "recurringType": None,
        "thumbnail": image or "",
        "addedAt": now_ms(),
    }


# --------------------------------------------------------------------------- #
# Source 1: Do512 (schema.org Event JSON-LD)
# --------------------------------------------------------------------------- #

def _walk_jsonld(node: Any) -> Iterable[dict]:
    """Yield every dict that looks like a schema.org Event, however nested."""
    if isinstance(node, dict):
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if any(t and "Event" in str(t) for t in types):
            yield node
        for value in node.values():
            yield from _walk_jsonld(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def _jsonld_field(obj: Any, *keys: str) -> str:
    for key in keys:
        if not isinstance(obj, dict) or key not in obj:
            continue
        val = obj[key]
        if isinstance(val, list) and val:
            val = val[0]
        if isinstance(val, dict):
            val = val.get("name") or val.get("url") or val.get("price") or ""
        if val:
            return str(val)
    return ""


def scrape_do512() -> list[dict]:
    events: dict[str, dict] = {}
    for url in DO512_URLS:
        resp = http_get(url)
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for node in _walk_jsonld(data):
                title = _jsonld_field(node, "name")
                link = _jsonld_field(node, "url") or url
                start = parse_date(node.get("startDate"))
                end = parse_date(node.get("endDate"))
                location = ""
                loc = node.get("location")
                if isinstance(loc, dict):
                    location = loc.get("name", "") or _jsonld_field(
                        loc.get("address", {}), "addressLocality"
                    )
                price = _jsonld_field(node.get("offers", {}), "price", "lowPrice")
                image = _jsonld_field(node, "image")
                description = clean_text(node.get("description"))
                m = re.search(r"(\d{4,})", link)
                suffix = m.group(1) if m else slugify(title)
                event = make_event(
                    source_id=f"do512-{suffix}",
                    title=title,
                    start=start,
                    end=end,
                    time_str=parse_time_str(node.get("startDate")),
                    end_time_str=parse_time_str(node.get("endDate")),
                    location=location,
                    price=price,
                    description=description,
                    category=categorize(title, description),
                    link=link,
                    image=image,
                    source_name="Do512",
                )
                if event:
                    events[event["id"]] = event
    log.info("Do512: %d events", len(events))
    return list(events.values())


# --------------------------------------------------------------------------- #
# RSS-based sources (365 Things Austin + Instagram via RSS-Bridge)
# --------------------------------------------------------------------------- #

def _image_from_entry(entry: Any) -> str:
    for attr in ("media_content", "media_thumbnail"):
        media = getattr(entry, attr, None)
        if media and isinstance(media, list) and media[0].get("url"):
            return media[0]["url"]
    for link in getattr(entry, "links", []) or []:
        if link.get("type", "").startswith("image") and link.get("href"):
            return link["href"]
    html = ""
    if getattr(entry, "summary", None):
        html = entry.summary
    elif getattr(entry, "content", None):
        html = entry.content[0].get("value", "")
    if html:
        img = BeautifulSoup(html, "html.parser").find("img")
        if img and img.get("src"):
            return img["src"]
    return ""


_MONTH_RE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\.?\s+(\d{1,2})\b",
    re.IGNORECASE,
)


def _entry_date(entry: Any) -> date | None:
    """Extract an explicit 'Month Day' from a post; None if there isn't one.

    Blog/IG posts rarely carry a machine date, and the RSS published date is
    just when the post went up (usually the past), which would create fake
    past-dated events. So we only trust an explicit month+day in the text, and
    roll it to next year if that day has already passed this year.
    """
    text = " ".join(filter(None, [
        getattr(entry, "title", ""),
        clean_text(getattr(entry, "summary", ""), limit=400),
    ]))
    m = _MONTH_RE.search(text)
    if not m:
        return None
    try:
        parsed = dateparser.parse(
            f"{m.group(1)} {m.group(2)}",
            default=datetime(today().year, 1, 1),
        )
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed is None:
        return None
    d = parsed.date()
    if d < today():
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:
            return None
    return d


def _scrape_feed(feed_url: str, source_name: str, id_prefix: str) -> list[dict]:
    events: dict[str, dict] = {}
    try:
        parsed = feedparser.parse(
            feed_url, agent=USER_AGENT, request_headers={"User-Agent": USER_AGENT}
        )
    except Exception as exc:
        log.warning("%s: feed parse failed: %s", source_name, exc)
        return []
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        log.warning("%s: feed unreadable (%s)", source_name,
                    getattr(parsed, "bozo_exception", "unknown"))
        return []
    for i, entry in enumerate(parsed.entries):
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "")
        start = _entry_date(entry)
        description = clean_text(getattr(entry, "summary", ""))
        guid = getattr(entry, "id", "") or link or str(i)
        m = re.search(r"(\d{4,})", guid)
        suffix = m.group(1) if m else (slugify(title) or str(i))
        event = make_event(
            source_id=f"{id_prefix}-{suffix}",
            title=title,
            start=start,
            location="Austin, TX",
            description=description,
            category=categorize(title, description),
            link=link,
            image=_image_from_entry(entry),
            source_name=source_name,
        )
        if event:
            events[event["id"]] = event
    log.info("%s: %d events", source_name, len(events))
    return list(events.values())


def scrape_365_things() -> list[dict]:
    return _scrape_feed(THINGS_365_RSS, "365 Things Austin", "365atx")


def scrape_instagram_bridges() -> list[dict]:
    """Best-effort Instagram via RSS-Bridge.

    For each account, try every configured bridge base × bridge type until one
    returns entries. Public instances usually 429; a private RSS_BRIDGE_BASE
    signed into Instagram is what makes this reliable.
    """
    events: list[dict] = []
    for account in IG_ACCOUNTS:
        got = False
        for raw_base in RSS_BRIDGE_BASES:
            base = raw_base.rstrip("/") + "/"
            for bridge in IG_BRIDGES:
                feed_url = (
                    f"{base}?action=display&bridge={bridge}"
                    f"&context=Username&u={account}&format=Atom"
                )
                found = _scrape_feed(
                    feed_url, f"Instagram @{account}", f"ig-{slugify(account)}"
                )
                if found:
                    events.extend(found)
                    got = True
                    break
            if got:
                break
    return events


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

SOURCES = [
    ("Do512", scrape_do512),
    ("365 Things Austin", scrape_365_things),
    ("Instagram/RSS-Bridge", scrape_instagram_bridges),
]


def main() -> int:
    cutoff = today()
    collected: dict[str, dict] = {}
    seen_signatures: set[str] = set()

    for name, fn in SOURCES:
        try:
            for event in fn():
                event_date = parse_date(event["date"])
                if event_date is None or event_date < cutoff:
                    continue
                if event["id"] in collected:
                    continue
                signature = f"{slugify(event['title'])}|{event['date']}"
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                collected[event["id"]] = event
        except Exception as exc:
            log.exception("Source %s failed: %s", name, exc)

    events = sorted(collected.values(), key=lambda e: (e["date"], e["title"]))

    # Safety: never overwrite a good feed with an empty one. If every source
    # failed or returned nothing (e.g. a site blocked the runner), keep the
    # last known-good discovered.json instead of publishing an empty feed.
    if not events:
        log.warning(
            "No events collected from any source — leaving existing %s untouched",
            OUTPUT_PATH,
        )
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(events, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info("Wrote %d upcoming events to %s", len(events), OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
