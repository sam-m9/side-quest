#!/usr/bin/env python3
"""Trigger an Apify Instagram Posts task, wait for completion, extract events
via Gemini, and merge them into public/discovered.json.

Required env vars (GitHub Secrets / Variables):
  APIFY_API_TOKEN       – Apify REST API token
  APIFY_POSTS_TASK_ID   – Apify task ID for the Instagram Posts scraper
                          (e.g. "fried_mahogany/instatask")
  GEMINI_API_KEY        – Google Gemini API key
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
APIFY_TASK_ID = os.environ.get("APIFY_POSTS_TASK_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

APIFY_BASE = "https://api.apify.com/v2"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent?key={key}"
)

REQUEST_TIMEOUT = 30
RUN_POLL_INTERVAL = 20   # seconds between status checks
RUN_TIMEOUT = 600        # 10 minutes max wait for the Apify run

SOURCE_NAME = "Instagram Posts (Apify)"
ID_PREFIX = "ig-post"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("fetch_instagram_posts")


# --------------------------------------------------------------------------- #
# Helpers
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


def _parse_date(value: Any) -> date | None:
    if not value or not str(value).strip():
        return None
    try:
        from dateutil import parser as dateparser
        return dateparser.parse(str(value), fuzzy=True).date()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Step 1: Trigger Apify run and wait for completion
# --------------------------------------------------------------------------- #

def trigger_run() -> str | None:
    """Start a new Apify task run and return the run ID."""
    url = f"{APIFY_BASE}/actor-tasks/{APIFY_TASK_ID}/runs?token={APIFY_TOKEN}"
    try:
        resp = requests.post(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        run_id = resp.json()["data"]["id"]
        log.info("Started Apify run: %s", run_id)
        return run_id
    except (requests.RequestException, KeyError) as exc:
        log.error("Failed to trigger Apify run: %s", exc)
        return None


def wait_for_run(run_id: str) -> bool:
    """Poll until the run succeeds or fails. Returns True on SUCCEEDED."""
    deadline = time.time() + RUN_TIMEOUT
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{APIFY_BASE}/runs/{run_id}?token={APIFY_TOKEN}",
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            status = resp.json()["data"]["status"]
        except (requests.RequestException, KeyError) as exc:
            log.warning("Status check failed: %s — retrying", exc)
            time.sleep(RUN_POLL_INTERVAL)
            continue

        log.info("Run %s status: %s", run_id, status)
        if status == "SUCCEEDED":
            return True
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            log.error("Run %s ended with status: %s", run_id, status)
            return False
        time.sleep(RUN_POLL_INTERVAL)

    log.error("Run %s did not complete within %ds", run_id, RUN_TIMEOUT)
    return False


def fetch_dataset(run_id: str) -> list[dict]:
    """Fetch dataset items from a completed run."""
    url = f"{APIFY_BASE}/runs/{run_id}/dataset/items?token={APIFY_TOKEN}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        items = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as exc:
        log.error("Dataset fetch failed: %s", exc)
        return []

    if not isinstance(items, list):
        log.warning("Unexpected dataset shape: %s", type(items))
        return []

    log.info("Fetched %d post items from dataset", len(items))
    return items


# --------------------------------------------------------------------------- #
# Step 2: Extract text from post items
# --------------------------------------------------------------------------- #

def _text_from_post(item: dict) -> str:
    parts: list[str] = []

    # Caption is the main signal for posts
    for field in ("caption", "text", "description", "alt", "altText"):
        v = item.get(field)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
            break  # caption is usually the canonical field; don't duplicate

    # Location tag
    for field in ("locationName", "location_name", "locationId"):
        v = item.get(field)
        if isinstance(v, str) and v.strip():
            parts.append(f"Location: {v.strip()}")
            break

    # Username attribution
    username = (
        item.get("ownerUsername")
        or item.get("username")
        or item.get("author")
        or item.get("ownerId")
        or ""
    )
    if username:
        parts.append(f"@{username}")

    return "\n".join(parts)


def _thumbnail_from_post(item: dict) -> str:
    for field in ("displayUrl", "display_url", "thumbnailUrl", "thumbnail_url",
                  "imageUrl", "image_url"):
        v = item.get(field)
        if isinstance(v, str) and v.startswith("http"):
            return v
    # Some scrapers nest images in a list
    images = item.get("images") or item.get("childPosts") or []
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            v = first.get("displayUrl") or first.get("url") or ""
            if v:
                return v
        elif isinstance(first, str) and first.startswith("http"):
            return first
    return ""


def build_post_texts(items: list[dict]) -> list[dict]:
    bundles = []
    for item in items:
        text = _text_from_post(item)
        if not text.strip():
            continue
        username = (
            item.get("ownerUsername")
            or item.get("username")
            or item.get("author")
            or ""
        )
        bundles.append({
            "text": text,
            "timestamp": str(item.get("timestamp") or item.get("takenAt") or ""),
            "thumbnail": _thumbnail_from_post(item),
            "username": username,
            "post_url": (
                item.get("url")
                or (f"https://www.instagram.com/p/{item['shortCode']}/"
                    if item.get("shortCode") else "")
            ),
        })
    log.info("Built %d non-empty post bundles", len(bundles))
    return bundles


# --------------------------------------------------------------------------- #
# Step 3: Gemini extraction
# --------------------------------------------------------------------------- #

GEMINI_SYSTEM_PROMPT = """You are an Austin, TX event extraction assistant.
You will receive a batch of Instagram post captions from Austin-area accounts.

For each post that describes a real upcoming event, extract a JSON object.
Skip posts that are not about events (general lifestyle content, ads without
a specific date/time, past recaps, etc.).

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
  "link": "URL if present in the caption, or empty string",
  "thumbnail": "thumbnail URL from the post metadata or empty string",
  "username": "Instagram @username this post came from",
  "source_timestamp": "ISO timestamp of the post, or empty string"
}

Rules:
- Only include events in or near Austin, TX.
- If a date mentions only a day-of-week (e.g. "this Saturday"), resolve it
  relative to today: """ + today().isoformat() + """.
- If no date is discernible, use an empty string.
- Never fabricate details not present in the caption.
- Skip events whose date is in the past (before today).
- Return [] if no events are found.
"""


def call_gemini(bundles: list[dict]) -> list[dict]:
    if not GEMINI_KEY:
        log.error("GEMINI_API_KEY not set — skipping Gemini extraction")
        return []
    if not bundles:
        log.info("No post bundles to send to Gemini")
        return []

    lines = []
    for i, b in enumerate(bundles, 1):
        lines.append(f"--- Post {i} (@{b['username']}, {b['timestamp']}) ---")
        lines.append(b["text"])
        if b["thumbnail"]:
            lines.append(f"[thumbnail: {b['thumbnail']}]")
        if b["post_url"]:
            lines.append(f"[url: {b['post_url']}]")
        lines.append("")

    payload = {
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": "\n".join(lines)}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    url = GEMINI_URL.format(key=GEMINI_KEY)
    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as exc:
        log.error("Gemini API call failed: %s", exc)
        return []

    try:
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        log.error("Unexpected Gemini response shape: %s — %s", exc, data)
        return []

    try:
        extracted = json.loads(raw_text)
    except json.JSONDecodeError:
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
# Step 4: Convert to app schema
# --------------------------------------------------------------------------- #

def gemini_to_app_event(raw: dict, idx: int) -> dict | None:
    title = (raw.get("title") or "").strip()
    if not title:
        return None

    start = _parse_date(raw.get("date"))
    if start is None or start < today():
        return None

    username = (raw.get("username") or "").lstrip("@")
    source_suffix = f"{slugify(username)}-{idx}" if username else str(idx)
    event_id = f"{ID_PREFIX}-{source_suffix}-{slugify(title)[:30]}"

    notes = (
        f"Discovered via @{username} (Instagram)"
        if username
        else f"Discovered via {SOURCE_NAME}"
    )

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
# Step 5: Deduplicate and merge
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
    title_slug = slugify(event.get("title") or "")
    d = event.get("date") or ""
    notes = event.get("notes") or ""
    m = re.search(r"@([\w.]+)", notes)
    account = m.group(1).lower() if m else ""
    return f"{title_slug}|{d}|{account}"


def merge_events(existing: list[dict], new_events: list[dict]) -> tuple[list[dict], int]:
    cutoff = today()
    # Keep undated events and future events; drop only confirmed-past dates
    merged = [
        e for e in existing
        if (lambda d: d is None or d >= cutoff)(_parse_date(e.get("date")))
    ]

    seen_ids: set[str] = {e["id"] for e in merged}
    seen_sigs: set[str] = {_signature(e) for e in merged}

    added = 0
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
    if not APIFY_TOKEN or not APIFY_TASK_ID:
        log.error("APIFY_API_TOKEN or APIFY_POSTS_TASK_ID not set — aborting")
        return 1

    # 1. Trigger run
    run_id = trigger_run()
    if not run_id:
        return 1

    # 2. Wait for completion
    if not wait_for_run(run_id):
        return 1

    # 3. Fetch dataset
    raw_items = fetch_dataset(run_id)
    if not raw_items:
        log.warning("Dataset empty — nothing to process")
        return 0

    # 4. Extract text bundles
    bundles = build_post_texts(raw_items)
    if not bundles:
        log.warning("All post items had empty text — nothing to send to Gemini")
        return 0

    # 5. Gemini extraction
    gemini_raw = call_gemini(bundles)

    # 6. Convert to app schema
    new_events: list[dict] = []
    for i, raw in enumerate(gemini_raw):
        event = gemini_to_app_event(raw, i)
        if event:
            new_events.append(event)
    log.info("%d valid new events after schema conversion", len(new_events))

    if not new_events:
        log.info("No new events extracted — leaving %s untouched", OUTPUT_PATH)
        return 0

    # 7. Merge into discovered.json
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
