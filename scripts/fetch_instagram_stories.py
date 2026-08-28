#!/usr/bin/env python3
"""Fetch Instagram story datasets from Apify, extract events via Gemini,
and merge new events into public/discovered.json.

Required env vars (set in GitHub Secrets / local .env):
  APIFY_API_TOKEN      – Apify REST API token
  APIFY_STORY_TASK_ID  – Apify actor-task ID for the stories scraper
  GEMINI_API_KEY       – Google Gemini API key

The output schema is identical to scrape_austin.py so the app can consume
both files interchangeably.
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
from typing import Any

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "public" / "discovered.json"

APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN", "").strip()
APIFY_TASK_ID = os.environ.get("APIFY_STORY_TASK_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

APIFY_RUN_URL     = "https://api.apify.com/v2/actor-tasks/{task_id}/runs?token={token}"
APIFY_STATUS_URL  = "https://api.apify.com/v2/actor-runs/{run_id}?token={token}"
APIFY_DATASET_URL = (
    "https://api.apify.com/v2/actor-tasks/{task_id}/runs/last/dataset/items"
    "?token={token}&status=SUCCEEDED"
)
APIFY_RUN_DATASET_URL = (
    "https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?token={token}"
)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.6-flash:generateContent"
)

REQUEST_TIMEOUT   = 30
GEMINI_429_WAITS  = [30, 60, 120]  # backoff delays on rate-limit, per retry

SOURCE_NAME = "Instagram Stories (Apify)"
ID_PREFIX = "ig-story"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("fetch_instagram_stories")


# --------------------------------------------------------------------------- #
# Helpers (mirrored from scrape_austin.py)
# --------------------------------------------------------------------------- #

def today() -> date:
    return datetime.utcnow().date()


def now_ms() -> int:
    return int(time.time() * 1000)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "item"


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


def categorize(*texts: str) -> str:
    blob = " " + " ".join(t for t in texts if t).lower() + " "
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in blob for kw in keywords):
            return category
    return "other"


# --------------------------------------------------------------------------- #
# Step 1: Fetch Apify dataset
# --------------------------------------------------------------------------- #

def _trigger_and_wait(task_id: str, token: str,
                      timeout_s: int = 600, poll_s: int = 15) -> str | None:
    """Start one Apify task run, poll until done, return run_id or None."""
    start_url = APIFY_RUN_URL.format(task_id=task_id, token=token)
    try:
        resp = requests.post(start_url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        run_id = (resp.json().get("data") or {}).get("id")
        log.info("Started Apify run %s", run_id)
    except requests.RequestException as exc:
        log.error("Could not start Apify run: %s", exc)
        return None

    if not run_id:
        log.error("No run ID returned by Apify")
        return None

    status_url = APIFY_STATUS_URL.format(run_id=run_id, token=token)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_s)
        try:
            status = requests.get(status_url, timeout=REQUEST_TIMEOUT).json().get("data", {}).get("status", "")
            log.info("Apify run %s → %s", run_id, status)
            if status == "SUCCEEDED":
                return run_id
            if status in ("FAILED", "TIMED-OUT", "ABORTING", "ABORTED"):
                log.error("Apify run ended with status %s — falling back to last run", status)
                return None
        except requests.RequestException as exc:
            log.warning("Poll error: %s", exc)

    log.error("Apify run timed out after %ds", timeout_s)
    return None


def fetch_apify_stories() -> list[dict]:
    """Trigger a fresh Apify run, wait for it, then return its story items."""
    if not APIFY_TOKEN or not APIFY_TASK_ID:
        log.error("APIFY_API_TOKEN or APIFY_STORY_TASK_ID not set — aborting")
        return []

    run_id = _trigger_and_wait(APIFY_TASK_ID, APIFY_TOKEN)
    if not run_id:
        log.warning("Apify run did not succeed — skipping to avoid reprocessing old stories.")
        return []
    url = APIFY_RUN_DATASET_URL.format(run_id=run_id, token=APIFY_TOKEN)

    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        items = resp.json()
    except requests.RequestException as exc:
        log.error("Apify fetch failed: %s", exc)
        return []
    except json.JSONDecodeError as exc:
        log.error("Apify response not JSON: %s", exc)
        return []

    if not isinstance(items, list):
        log.warning("Unexpected Apify response shape: %s", type(items))
        return []

    log.info("Fetched %d story items from Apify", len(items))
    return items


# --------------------------------------------------------------------------- #
# Step 2: Extract text content from story items
# --------------------------------------------------------------------------- #

def _text_from_story(item: dict) -> str:
    """Pull every text signal out of a single story item."""
    parts: list[str] = []

    # Direct caption / text fields the scraper may surface
    for field in ("caption", "text", "title", "altText", "alt_text"):
        v = item.get(field)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())

    # Sticker text (link sticker label, poll text, question sticker, etc.)
    for sticker in item.get("stickers", []) or []:
        if isinstance(sticker, dict):
            for sf in ("text", "label", "question", "value"):
                v = sticker.get(sf)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())

    # Overlay / drawn text from OCR or scraper
    for overlay in item.get("overlays", []) or item.get("textOverlays", []) or []:
        if isinstance(overlay, dict):
            v = overlay.get("text") or overlay.get("value")
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())

    # Account username for attribution
    username = (
        item.get("ownerUsername")
        or item.get("username")
        or item.get("author")
        or ""
    )
    if username:
        parts.append(f"@{username}")

    return "\n".join(parts)


def _timestamp_from_story(item: dict) -> str:
    """Return ISO timestamp string from a story item, or empty string."""
    for field in ("timestamp", "takenAt", "taken_at", "createdAt", "created_at"):
        v = item.get(field)
        if v:
            return str(v)
    return ""


def _thumbnail_from_story(item: dict) -> str:
    for field in ("thumbnailUrl", "thumbnail_url", "displayUrl", "display_url",
                  "imageUrl", "image_url", "url"):
        v = item.get(field)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def build_story_texts(items: list[dict]) -> list[dict]:
    """Collect text + metadata from each story item into a compact bundle."""
    bundles = []
    for item in items:
        text = _text_from_story(item)
        if not text.strip():
            continue
        bundles.append({
            "text": text,
            "timestamp": _timestamp_from_story(item),
            "thumbnail": _thumbnail_from_story(item),
            "username": (
                item.get("ownerUsername")
                or item.get("username")
                or item.get("author")
                or ""
            ),
            "story_url": item.get("url") or item.get("storyUrl") or "",
        })
    log.info("Built %d non-empty story text bundles", len(bundles))
    return bundles


# --------------------------------------------------------------------------- #
# Step 3: Gemini extraction
# --------------------------------------------------------------------------- #

GEMINI_SYSTEM_PROMPT = """You are an Austin, TX event extraction assistant.
You will receive a batch of Instagram story text snippets (captions, sticker
text, overlay text) from Austin-area Instagram accounts.

For each snippet that describes a real upcoming event, extract a JSON object.
If a snippet has no event information, skip it entirely.

Return ONLY a valid JSON array of event objects (no markdown, no explanation).
Each object must have exactly these fields:

{
  "title": "Event name (required)",
  "date": "YYYY-MM-DD or empty string if unknown",
  "time": "e.g. 7:00 PM or empty string",
  "endTime": "e.g. 10:00 PM or empty string",
  "location": "Venue name and/or Austin neighborhood, or empty string",
  "price": "Free, $15, $10-$20, or empty string",
  "description": "1-2 sentence summary, or empty string",
  "category": "one of: music, food, art, sports, social, film, comedy, outdoors, other",
  "link": "URL if present in the story text, or empty string",
  "thumbnail": "thumbnail URL from the story metadata or empty string",
  "username": "Instagram @username this story came from",
  "source_timestamp": "ISO timestamp of the story, or empty string"
}

Rules:
- Only include events that appear to be happening in or near Austin, TX.
- If the date mentions only a day-of-week (e.g. "this Saturday"), resolve it
  relative to today: """ + today().isoformat() + """.
- If no date is discernible, use an empty string.
- Never fabricate details not present in the story text.
- Dates in the past (before today) should be omitted.
- Return [] if no events are found.
"""


def call_gemini(bundles: list[dict]) -> list[dict]:
    """Send story bundles to Gemini and return extracted event dicts."""
    if not GEMINI_KEY:
        log.error("GEMINI_API_KEY not set — skipping Gemini extraction")
        return []
    if not bundles:
        log.info("No story bundles to send to Gemini")
        return []

    # Format bundles into a numbered list for the prompt
    lines = []
    for i, b in enumerate(bundles, 1):
        lines.append(f"--- Story {i} (@{b['username']}, {b['timestamp']}) ---")
        lines.append(b["text"])
        if b["thumbnail"]:
            lines.append(f"[thumbnail: {b['thumbnail']}]")
        if b["story_url"]:
            lines.append(f"[url: {b['story_url']}]")
        lines.append("")

    user_content = "\n".join(lines)

    payload = {
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": user_content}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    data = None
    for attempt, wait in enumerate([0] + GEMINI_429_WAITS):
        if wait:
            log.info("Gemini rate-limited — waiting %ds before retry %d/%d",
                     wait, attempt, len(GEMINI_429_WAITS))
            time.sleep(wait)
        try:
            resp = requests.post(
                GEMINI_URL,
                headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
            if resp.status_code == 429:
                log.warning("Gemini 429 (attempt %d/%d)",
                            attempt + 1, len(GEMINI_429_WAITS) + 1)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as exc:
            log.error("Gemini API call failed: %s", exc)
            return []
        except json.JSONDecodeError as exc:
            log.error("Gemini response not JSON: %s", exc)
            return []
    if data is None:
        log.error("Gemini gave up after %d retries", len(GEMINI_429_WAITS))
        return []

    # Extract text from the response
    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        log.error("Unexpected Gemini response shape: %s — %s", exc, data)
        return []

    try:
        extracted = json.loads(raw_text)
    except json.JSONDecodeError:
        # Sometimes the model wraps in ```json ... ```
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw_text)
        if m:
            try:
                extracted = json.loads(m.group(1))
            except json.JSONDecodeError:
                log.error("Could not parse Gemini output as JSON")
                return []
        else:
            log.error("Could not parse Gemini output as JSON")
            return []

    if not isinstance(extracted, list):
        log.warning("Gemini returned non-list JSON: %s", type(extracted))
        return []

    log.info("Gemini extracted %d candidate events", len(extracted))
    return extracted


# --------------------------------------------------------------------------- #
# Step 4: Convert Gemini output → app schema
# --------------------------------------------------------------------------- #

def _parse_date(value: Any) -> date | None:
    if not value or not str(value).strip():
        return None
    try:
        from dateutil import parser as dateparser
        return dateparser.parse(str(value), fuzzy=True).date()
    except Exception:
        return None


def gemini_to_app_event(raw: dict, idx: int) -> dict | None:
    """Convert a Gemini-extracted dict into the app's internal event schema."""
    title = (raw.get("title") or "").strip()
    if not title:
        return None

    start = _parse_date(raw.get("date"))
    if start is None or start < today():
        # Events with no date or past dates are skipped
        return None

    username = (raw.get("username") or "").lstrip("@")
    source_suffix = f"{slugify(username)}-{idx}" if username else str(idx)
    event_id = f"{ID_PREFIX}-{source_suffix}-{slugify(title)[:30]}"

    notes = f"Discovered via {SOURCE_NAME}"
    if username:
        notes = f"Discovered via @{username} (Instagram Story)"

    return {
        "id": event_id,
        "title": title,
        "date": start.isoformat(),
        "time": (raw.get("time") or "").strip(),
        "endTime": (raw.get("endTime") or "").strip(),
        "location": (raw.get("location") or "").strip(),
        "price": normalize_price(raw.get("price")),
        "description": (raw.get("description") or "").strip(),
        "category": raw.get("category") or categorize(title, raw.get("description") or ""),
        "link": (raw.get("link") or "").strip(),
        "notes": notes,
        "status": "want",
        "isRecurring": False,
        "recurringType": None,
        "thumbnail": (raw.get("thumbnail") or "").strip(),
        "addedAt": now_ms(),
    }


# --------------------------------------------------------------------------- #
# Step 5: Deduplicate and merge into discovered.json
# --------------------------------------------------------------------------- #

def load_existing() -> list[dict]:
    if not OUTPUT_PATH.exists():
        return []
    try:
        return json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read existing %s: %s", OUTPUT_PATH, exc)
        return []


def _signature(event: dict) -> str:
    """Stable dedup key: normalized title + date + source account."""
    title_slug = slugify(event.get("title") or "")
    d = event.get("date") or ""
    # Extract @account from notes if present
    notes = event.get("notes") or ""
    m = re.search(r"@([\w.]+)", notes)
    account = m.group(1).lower() if m else ""
    return f"{title_slug}|{d}|{account}"


def merge_events(existing: list[dict], new_events: list[dict]) -> tuple[list[dict], int]:
    """Merge new events into existing list; return (merged_list, added_count)."""
    seen_ids: set[str] = {e["id"] for e in existing}
    seen_sigs: set[str] = {_signature(e) for e in existing}

    added = 0
    cutoff = today()
    # Keep events with no parseable date (TBD/recurring) and future-dated events;
    # only drop events whose date is confirmed to be in the past.
    merged = [e for e in existing if (lambda d: d is None or d >= cutoff)(_parse_date(e.get("date")))]

    for event in new_events:
        if event["id"] in seen_ids:
            continue
        sig = _signature(event)
        if sig in seen_sigs:
            continue
        merged.append(event)
        seen_ids.add(event["id"])
        seen_sigs.add(sig)
        added += 1

    merged.sort(key=lambda e: (e.get("date") or "9999", e.get("title") or ""))
    return merged, added


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    # 1. Fetch raw stories from Apify
    raw_items = fetch_apify_stories()
    if not raw_items:
        log.warning("No story items from Apify — nothing to do")
        return 0

    # 2. Extract text bundles
    bundles = build_story_texts(raw_items)
    if not bundles:
        log.warning("All story items had empty text — nothing to send to Gemini")
        return 0

    # 3. Extract events via Gemini
    gemini_raw = call_gemini(bundles)

    # 4. Convert to app schema
    new_events: list[dict] = []
    for i, raw in enumerate(gemini_raw):
        event = gemini_to_app_event(raw, i)
        if event:
            new_events.append(event)
    log.info("%d valid new events after schema conversion", len(new_events))

    if not new_events:
        log.info("No new events extracted — leaving %s untouched", OUTPUT_PATH)
        return 0

    # 5. Load existing events and merge
    existing = load_existing()
    merged, added = merge_events(existing, new_events)

    if added == 0:
        log.info("All extracted events were duplicates — no changes written")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info("Added %d new events → %s (%d total)", added, OUTPUT_PATH, len(merged))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
