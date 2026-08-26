#!/usr/bin/env python3
"""
Fetch recent Instagram posts via an Apify actor task, classify them as events
using Google Gemini 2.5 Flash (free tier), and merge results into
public/discovered.json using the app's exact internal schema.

Required GitHub Secrets:
  APIFY_API_TOKEN     – Apify account API token
  APIFY_ACTOR_TASK_ID – ID of your Apify task running an Instagram scraper
                        (recommended actor: sones/instagram-posts-scraper-lowcost)
  GEMINI_API_KEY      – Google AI Studio key (free at aistudio.google.com)

Setup steps:
  1. Create an Apify account → build a Task using actor
     "sones/instagram-posts-scraper-lowcost" targeting your IG accounts.
     Note the Task ID from the task's URL.
  2. Get a Gemini API key from https://aistudio.google.com/apikey
  3. Add all three as Secrets in your repo's Settings → Secrets → Actions.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, date, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "public" / "discovered.json"

APIFY_TOKEN   = os.environ.get("APIFY_API_TOKEN", "").strip()
APIFY_TASK_ID = os.environ.get("APIFY_ACTOR_TASK_ID", "").strip()
GEMINI_KEY    = os.environ.get("GEMINI_API_KEY", "").strip()

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

LOOKBACK_HOURS  = 48   # include posts this many hours old
MAX_POSTS       = 100  # cap before Gemini calls
GEMINI_DELAY_S  = 6    # seconds between calls (free tier = 10 RPM)

VALID_CATEGORIES = frozenset({
    "music", "food", "art", "sports", "social",
    "film", "comedy", "outdoors", "other",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("ig_events")


# --------------------------------------------------------------------------- #
# Apify helpers
# --------------------------------------------------------------------------- #

def fetch_apify_posts() -> list[dict]:
    """Return posts from the last successful run of the configured Apify task."""
    url = (
        f"https://api.apify.com/v2/actor-tasks/{APIFY_TASK_ID}"
        f"/runs/last/dataset/items"
        f"?token={APIFY_TOKEN}&status=SUCCEEDED&limit={MAX_POSTS}"
        f"&fields=shortCode,id,caption,displayUrl,timestamp,"
        f"takenAtTimestamp,url,ownerUsername,images"
    )
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        posts = resp.json()
        log.info("Apify returned %d posts total", len(posts))
        return posts if isinstance(posts, list) else []
    except requests.RequestException as exc:
        log.error("Apify fetch failed: %s", exc)
        return []


def filter_recent(posts: list[dict]) -> list[dict]:
    """Keep only posts published within LOOKBACK_HOURS."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    result = []
    for p in posts:
        raw_ts = p.get("timestamp") or p.get("takenAtTimestamp")
        if not raw_ts:
            result.append(p)   # no timestamp → include defensively
            continue
        try:
            # Apify uses ISO strings ("2025-08-20T12:00:00.000Z") or epoch ints
            if isinstance(raw_ts, (int, float)):
                dt = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            if dt >= cutoff:
                result.append(p)
        except (ValueError, TypeError, OSError):
            result.append(p)
    log.info("%d posts after %dh recency filter", len(result), LOOKBACK_HOURS)
    return result


# --------------------------------------------------------------------------- #
# Gemini helpers
# --------------------------------------------------------------------------- #

# JSON schema for structured output — must match app's internal schema exactly.
_TODAY_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%d")

GEMINI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_event": {
            "type": "BOOLEAN",
            "description": (
                "True ONLY if ALL of these are satisfied: "
                "(1) the post announces ONE specific named event or a recurring happy hour/weekly special, "
                "(2) it includes a concrete date (day+month or relative like 'this Friday') OR is a recurring special, "
                "(3) it includes a real venue name or address. "
                "Set to FALSE for: roundup/listicle posts ('10 things to do', 'things to do in [month]', 'events this weekend' without a single specific event), "
                "generic brand posts, lifestyle content, vague 'coming soon' without a date, "
                "or posts that reference multiple unrelated events."
            ),
        },
        "is_happy_hour": {
            "type": "BOOLEAN",
            "description": (
                "True if the post is primarily about a happy hour, daily special, "
                "or recurring drink/food deal (e.g. mentions 'happy hour', 'HH', "
                "'daily special', 'every day', 'EVERYDAY', or lists drink/food prices "
                "with a recurring time window). False for one-time events."
            ),
        },
        "title": {
            "type": "STRING",
            "description": (
                "Short name. For happy hours use the venue name, e.g. 'Martini'. "
                "For events use the event name, e.g. 'Jazz Night at Rainey St Bar'."
            ),
        },
        "date": {
            "type": "STRING",
            "description": (
                f"Full calendar date as YYYY-MM-DD. Today is {_TODAY_ISO}. "
                "Infer the year if the post says 'this Friday' or 'Aug 23'. "
                "For recurring happy hours with no specific date, use today's date. "
                "Empty string only if truly no date or recurrence is mentioned."
            ),
        },
        "time": {
            "type": "STRING",
            "description": (
                "Start time as '7:00 PM'. For 'from open' or 'opening time' use empty string. "
                "Empty string if unknown."
            ),
        },
        "endTime": {
            "type": "STRING",
            "description": "End time as '10:00 PM'. Empty string if unknown.",
        },
        "recurring_days": {
            "type": "STRING",
            "description": (
                "Which days this recurs. Use 'everyday' if the post says EVERYDAY, "
                "'every day', or 7 days a week. Use 'weekdays' for Mon–Fri. "
                "Otherwise list abbreviated days comma-separated: 'Mon,Tue,Wed,Thu,Fri'. "
                "Empty string for one-time events."
            ),
        },
        "location": {
            "type": "STRING",
            "description": "Venue name and/or address. Empty string if unknown.",
        },
        "deal": {
            "type": "STRING",
            "description": (
                "ALL drink and food specials listed, one per line. "
                "Example: '$7 martinis\\n$5 apps/snacks\\n$10 burger'. "
                "For non-happy-hour events use the price field instead; leave this empty."
            ),
        },
        "price": {
            "type": "STRING",
            "description": (
                "'Free', '$15', '$10-$20', etc. for ticketed one-time events. "
                "Empty string for happy hours (use deal field instead) or if unknown."
            ),
        },
        "description": {
            "type": "STRING",
            "description": "1-2 sentence event summary. Max 300 characters.",
        },
        "category": {
            "type": "STRING",
            "enum": sorted(VALID_CATEGORIES),
            "description": "Best-matching category from the enum.",
        },
    },
    "required": ["is_event"],
}

_SYSTEM_PROMPT = (
    "You are an event detection assistant for Side Quest, an Austin TX events app. "
    "Analyze the Instagram post below and extract structured event data. "
    "Set is_event=false for roundup posts ('things to do in [month]', '10 events this weekend', "
    "'best places to...') — these list multiple things and do not describe a single specific event. "
    "A valid event has ONE name, ONE date, and ONE venue. "
    "Pay special attention to happy hours: capture ALL deal lines (one per line in the deal field), "
    "the exact end time (e.g. '7:00 PM' from 'open-7pm'), and recurrence ('everyday' if EVERYDAY). "
    "Return JSON only — no markdown, no explanation.\n\n"
    f"Today's date is {_TODAY_ISO} (Austin, TX local time)."
)


def _fetch_image_b64(url: str) -> tuple[str, str] | None:
    """Download image, return (base64_string, mime_type) or None on failure."""
    if not url:
        return None
    try:
        resp = requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        return base64.b64encode(resp.content).decode(), mime
    except Exception as exc:
        log.debug("Image fetch failed (%s): %s", url[:60], exc)
        return None


def gemini_classify(post: dict) -> dict | None:
    """Call Gemini 2.5 Flash; return extracted fields dict or None on failure."""
    raw_caption = post.get("caption") or ""
    caption = (raw_caption if isinstance(raw_caption, str) else str(raw_caption)).strip()
    if not caption:
        return None

    parts: list[dict] = [
        {"text": f"{_SYSTEM_PROMPT}\n\n---\nInstagram caption:\n{caption[:2500]}"},
    ]

    # Attach the post image when available; fall back to caption-only if it fails
    img_url = (post.get("displayUrl") or
               ((post.get("images") or [{}])[0].get("url") or ""))
    img = _fetch_image_b64(img_url)
    if img:
        b64, mime = img
        parts.append({"inlineData": {"mimeType": mime, "data": b64}})
        log.debug("  image attached (%.1f kB)", len(b64) * 3 / 4 / 1024)
    else:
        log.debug("  caption-only (no usable image)")

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": GEMINI_SCHEMA,
            "temperature": 0.1,
        },
    }

    try:
        resp = requests.post(
            f"{GEMINI_ENDPOINT}?key={GEMINI_KEY}",
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (requests.RequestException, KeyError, json.JSONDecodeError, IndexError) as exc:
        log.warning("Gemini call failed for post %s: %s", post.get("shortCode"), exc)
        return None


# --------------------------------------------------------------------------- #
# Schema assembly
# --------------------------------------------------------------------------- #

def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_price(raw: str) -> str:
    if not raw:
        return ""
    text = raw.strip()
    if text.lower() in ("0", "0.0", "free"):
        return "Free"
    if text.startswith("$"):
        return text
    import re
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return f"${text}"
    return text


_DOW_MAP = {
    "mon": "mon", "monday": "mon",
    "tue": "tue", "tuesday": "tue",
    "wed": "wed", "wednesday": "wed",
    "thu": "thu", "thursday": "thu",
    "fri": "fri", "friday": "fri",
    "sat": "sat", "saturday": "sat",
    "sun": "sun", "sunday": "sun",
}
_ALL_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
_WEEKDAYS  = ["mon", "tue", "wed", "thu", "fri"]


def _parse_recurring_days(raw: str) -> list[str]:
    """Convert Gemini's recurring_days string to a list of DOW keys."""
    if not raw:
        return []
    low = raw.strip().lower()
    if low in ("everyday", "every day", "daily", "7 days", "all week"):
        return _ALL_DAYS
    if low in ("weekdays", "mon-fri", "monday-friday"):
        return _WEEKDAYS
    if low in ("weekends", "sat-sun", "saturday-sunday"):
        return ["sat", "sun"]
    # comma/slash-separated list
    days = []
    for token in low.replace("/", ",").split(","):
        key = _DOW_MAP.get(token.strip()[:3])
        if key:
            days.append(key)
    return days or []


def make_ig_event(post: dict, extracted: dict) -> dict | None:
    """Assemble one event in the app's exact internal schema, or None if unusable."""
    title    = (extracted.get("title") or "").strip()
    date_str = (extracted.get("date")  or "").strip()

    if not title or not date_str:
        return None

    # Drop events whose date has already passed (skip this check for happy hours
    # since they recur and their date is just "today as anchor")
    is_hh = bool(extracted.get("is_happy_hour"))
    if not is_hh:
        try:
            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if event_date < datetime.now(timezone.utc).date():
                return None
        except ValueError:
            return None

    short_code = post.get("shortCode") or post.get("id") or date_str
    post_url   = post.get("url") or f"https://www.instagram.com/p/{short_code}/"
    owner      = post.get("ownerUsername") or "instagram"

    category = (extracted.get("category") or "other").strip().lower()
    if category not in VALID_CATEGORIES:
        category = "other"

    recurring_days = _parse_recurring_days(extracted.get("recurring_days") or "")
    is_recurring   = bool(recurring_days)

    # For happy hours, merge deal lines into the price field so the app
    # auto-promotion logic picks them up via e.price → deal in hhF
    deal  = (extracted.get("deal") or "").strip()
    price = _normalize_price(extracted.get("price") or "") if not deal else deal

    return {
        "id":           f"ig-{short_code}",
        "title":        title,
        "date":         date_str,
        "time":         (extracted.get("time")        or "").strip(),
        "endTime":      (extracted.get("endTime")     or "").strip(),
        "location":     (extracted.get("location")    or "").strip(),
        "price":        price,
        "description":  (extracted.get("description") or "").strip()[:600],
        "category":     category,
        "link":         post_url,
        "notes":        f"Discovered via @{owner} on Instagram",
        "status":       "want",
        "isRecurring":  is_recurring,
        "recurringType": ",".join(recurring_days) if recurring_days else None,
        "happyHour":    is_hh,
        "thumbnail":    post.get("displayUrl") or "",
        "addedAt":      _now_ms(),
    }


# --------------------------------------------------------------------------- #
# Merge & persist
# --------------------------------------------------------------------------- #

def load_existing() -> list[dict]:
    if not OUTPUT.exists():
        return []
    try:
        data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s: %s", OUTPUT, exc)
        return []


def merge_and_prune(existing: list[dict], new_events: list[dict]) -> list[dict]:
    """Merge new events into existing, dedup by id+link, prune past dates."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seen_ids:   set[str] = set()
    seen_links: set[str] = set()
    result: list[dict] = []

    # Pass 1 — keep existing future events, record seen ids/links
    for ev in existing:
        ev_date = (ev.get("date") or "")
        if ev_date and ev_date < today:
            continue                    # prune past
        eid   = ev.get("id")   or ""
        elink = ev.get("link") or ""
        if eid:   seen_ids.add(eid)
        if elink: seen_links.add(elink)
        result.append(ev)

    # Pass 2 — add new events that aren't duplicates
    added = 0
    for ev in new_events:
        eid   = ev.get("id")   or ""
        elink = ev.get("link") or ""
        if eid in seen_ids or (elink and elink in seen_links):
            continue
        if eid:   seen_ids.add(eid)
        if elink: seen_links.add(elink)
        result.append(ev)
        added += 1

    log.info("Added %d new events (kept %d existing → total %d)", added, len(result) - added, len(result))
    result.sort(key=lambda e: e.get("date") or "9999-12-31")
    return result


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    # Fail loudly but cleanly if secrets are missing
    missing = [v for v in ("APIFY_API_TOKEN", "APIFY_ACTOR_TASK_ID", "GEMINI_API_KEY")
               if not os.environ.get(v)]
    if missing:
        log.error(
            "Missing required environment variables: %s\n"
            "Add them as GitHub Secrets and re-run. Exiting without changes.",
            ", ".join(missing),
        )
        sys.exit(0)   # exit 0 → workflow passes; first-time setup expected

    # 1. Pull posts from Apify
    posts  = fetch_apify_posts()
    recent = filter_recent(posts)

    if not recent:
        log.info("No recent posts to process — done.")
        return

    # 2. Classify each post with Gemini
    new_events: list[dict] = []
    for i, post in enumerate(recent):
        short_code = post.get("shortCode") or str(i)
        owner      = post.get("ownerUsername") or "?"
        log.info("Analyzing post %s (@%s) [%d/%d]", short_code, owner, i + 1, len(recent))

        extracted = gemini_classify(post)
        if not extracted or not extracted.get("is_event"):
            log.debug("  → not an event, skipping")
        else:
            ev = make_ig_event(post, extracted)
            if ev:
                new_events.append(ev)
                log.info("  → event: '%s' on %s", ev["title"], ev["date"])
            else:
                log.debug("  → extracted but unusable (missing title or date)")

        # Respect Gemini free-tier rate limit (10 RPM)
        if i < len(recent) - 1:
            time.sleep(GEMINI_DELAY_S)

    log.info("%d events extracted from %d posts", len(new_events), len(recent))

    # 3. Merge into discovered.json
    existing = load_existing()
    merged   = merge_and_prune(existing, new_events)

    # Safety guard: never overwrite a good file with nothing
    if not merged and existing:
        log.warning(
            "Merge produced 0 events but existing file had %d — aborting write.",
            len(existing),
        )
        return

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info("Wrote %d events to %s", len(merged), OUTPUT)


if __name__ == "__main__":
    main()
