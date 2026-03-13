#!/usr/bin/env python3
"""
USP Newsletter Curator — two-pass architecture
Fetches saved (starred) articles from the "USP newsletter" InoReader label,
evaluates them with Claude against the USP editorial criteria,
and writes selected articles to the "USP Newsletter" Airtable table.

Pass 1: Editorial selection via usp_criteria.md → returns selected indices
Pass 2: Airtable classification via usp_airtable_prompt.md → returns Airtable-ready JSON
"""

import os
import sys
import json
import re
import time
import logging
from pathlib import Path
from datetime import datetime, timezone
import requests
import anthropic

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Credentials ───────────────────────────────────────────────────────────────
INOREADER_TOKEN   = os.environ["INOREADER_TOKEN"]
INOREADER_APP_ID  = os.environ["INOREADER_APP_ID"]
INOREADER_APP_KEY = os.environ["INOREADER_APP_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
AIRTABLE_TOKEN    = os.environ["AIRTABLE_TOKEN"]

# ── InoReader config ──────────────────────────────────────────────────────────
INOREADER_BASE    = "https://www.inoreader.com/reader/api/0"
SOURCE_TAG        = "user/-/label/USP newsletter"
STARRED_STATE     = "user/-/state/com.google/starred"
NEWSLETTER_TAG    = "user/-/label/usp-newsletter-pick"
LOOKBACK_DAYS     = 7
PAGE_SIZE         = 250
TARGET_PICKS      = 15   # USP uses a tighter selection bar

# ── Model ─────────────────────────────────────────────────────────────────────
CLAUDE_MODEL = "claude-opus-4-5"

# ── Airtable config ───────────────────────────────────────────────────────────
AIRTABLE_BASE_ID  = "appU1awr0rFP2mSYK"
AIRTABLE_TABLE_ID = "tblCwiC9atWRewAyz"
AIRTABLE_API_URL  = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"

FIELD_ARTICLE_NAME      = "fldOB63Rl75F2X7O5"
FIELD_SOURCE            = "fldEAxuCtgQUiIq4q"
FIELD_LINK              = "fldccDMJRBZrJQ7YK"
FIELD_PUB_DATE          = "fldXIdFd3oWDTFeeu"
FIELD_TOPIC             = "fldFwRipLiz0zY03Y"
FIELD_SELECTION_REASON  = "fldehAlmSE6NCMFxp"

# ── Prompt files ──────────────────────────────────────────────────────────────
CRITERIA_FILE   = Path(__file__).parent / "usp_criteria.md"
AIRTABLE_PROMPT = Path(__file__).parent / "usp_airtable_prompt.md"
OUTPUT_DIR      = Path(__file__).parent / "outputs"


# ── InoReader helpers ─────────────────────────────────────────────────────────
def inoreader_headers() -> dict:
    return {
        "Authorization": f"Bearer {INOREADER_TOKEN}",
        "AppId":         INOREADER_APP_ID,
        "AppKey":        INOREADER_APP_KEY,
        "Content-Type":  "application/json",
    }


def fetch_articles() -> list[dict]:
    """
    Fetch saved (starred) articles from the USP newsletter label
    published in the last LOOKBACK_DAYS days.
    """
    cutoff_ts = int(time.time()) - (LOOKBACK_DAYS * 24 * 60 * 60)
    stream_id = requests.utils.quote(SOURCE_TAG, safe="")
    url = f"{INOREADER_BASE}/stream/contents/{stream_id}"

    all_items: list[dict] = []
    continuation = None
    page = 1

    log.info("Fetching articles from '%s' (last %d days)…", SOURCE_TAG, LOOKBACK_DAYS)

    while True:
        params: dict = {"n": PAGE_SIZE, "output": "json", "ot": cutoff_ts}
        if continuation:
            params["c"] = continuation

        resp = requests.get(url, headers=inoreader_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        all_items.extend(items)
        log.info("  Page %d: %d articles (total so far: %d)", page, len(items), len(all_items))

        continuation = data.get("continuation")
        if not continuation or not items:
            break
        page += 1
        time.sleep(0.5)

    # Filter to only saved/starred articles
    saved = [
        a for a in all_items
        if STARRED_STATE in a.get("categories", [])
    ]
    log.info(
        "→ %d total articles retrieved; %d are starred/saved",
        len(all_items), len(saved)
    )

    # If no starred articles found, fall back to all articles
    # (in case the label itself IS the saved collection)
    if not saved:
        log.warning(
            "No starred articles found — using all %d articles from label "
            "(the label may itself represent the saved collection)",
            len(all_items)
        )
        return all_items

    return saved


def tag_articles(articles: list[dict]) -> None:
    log.info("Tagging %d articles with 'usp-newsletter-pick'…", len(articles))
    url = f"{INOREADER_BASE}/edit-tag"
    for i, a in enumerate(articles, 1):
        title = a.get("title", "(no title)")
        log.info("  [%d/%d] %s", i, len(articles), title[:80])
        try:
            resp = requests.post(
                url, headers=inoreader_headers(),
                data={"a": NEWSLETTER_TAG, "i": a["id"]},
                timeout=15
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            log.warning("    Failed to tag '%s': %s", title[:60], exc)
        time.sleep(0.3)


# ── Article formatting ─────────────────────────────────────────────────────────
def format_articles(articles: list[dict]) -> str:
    lines = []
    for i, a in enumerate(articles, 1):
        title         = a.get("title", "(no title)")
        summary_raw   = a.get("summary", {}).get("content", "") or ""
        source        = a.get("origin", {}).get("title", "Unknown source")
        url           = (a.get("canonical") or [{}])[0].get("href", "")
        pub_ts        = a.get("published", 0)
        pub_date      = (datetime.fromtimestamp(pub_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                         if pub_ts else "unknown")
        summary_clean = re.sub(r"<[^>]+>", " ", summary_raw).strip()[:400]
        lines.append(
            f"[{i}] {title}\n"
            f"    Source: {source}\n"
            f"    URL: {url}\n"
            f"    Published: {pub_date}\n"
            f"    Summary: {summary_clean}"
        )
    return "\n\n".join(lines)


# ── Pass 1: Editorial selection ────────────────────────────────────────────────
def pass1_select(articles: list[dict], criteria: str) -> tuple[list[int], str]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_prompt = (
        f"Here are {len(articles)} candidate articles from the last {LOOKBACK_DAYS} days "
        f"in the USP newsletter feed. Select the top {TARGET_PICKS} highest-signal articles.\n\n"
        f"{format_articles(articles)}"
    )

    log.info("PASS 1 — Sending %d articles to Claude for editorial selection…", len(articles))
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=criteria,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = message.content[0].text.strip()
    log.info("Pass 1 response: %d chars (stop_reason=%s)", len(raw), message.stop_reason)

    if message.stop_reason == "max_tokens":
        log.error("Pass 1 response TRUNCATED — SELECTED_INDICES may be missing")

    match = re.search(r"SELECTED_INDICES:\s*\[([^\]]*)\]", raw)
    if not match:
        log.error("SELECTED_INDICES not found in Pass 1 response")
        return [], raw

    indices_1based = [int(x.strip()) for x in match.group(1).split(",")
                      if x.strip().isdigit()]
    indices_0based = [i - 1 for i in indices_1based if 1 <= i <= len(articles)]
    log.info("Pass 1 selected %d articles: indices %s",
             len(indices_0based), [i + 1 for i in indices_0based])
    return indices_0based, raw


# ── Pass 2: Airtable classification ───────────────────────────────────────────
def pass2_airtable(selected_articles: list[dict], original_indices: list[int],
                   airtable_prompt: str) -> list[dict]:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    article_lines = []
    for orig_idx, art in zip(original_indices, selected_articles):
        title         = art.get("title", "(no title)")
        summary_raw   = art.get("summary", {}).get("content", "") or ""
        source        = art.get("origin", {}).get("title", "Unknown source")
        url           = (art.get("canonical") or [{}])[0].get("href", "")
        pub_ts        = art.get("published", 0)
        pub_date      = (datetime.fromtimestamp(pub_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                         if pub_ts else "")
        summary_clean = re.sub(r"<[^>]+>", " ", summary_raw).strip()[:500]
        article_lines.append(
            f"[{orig_idx + 1}] {title}\n"
            f"    Source: {source}\n"
            f"    URL: {url}\n"
            f"    Published: {pub_date}\n"
            f"    Summary: {summary_clean}"
        )

    user_prompt = (
        f"Here are the {len(selected_articles)} pre-selected articles. "
        f"Generate one Airtable row for each.\n\n"
        + "\n\n".join(article_lines)
    )

    log.info("PASS 2 — Classifying %d selected articles for Airtable…",
             len(selected_articles))
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=airtable_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = message.content[0].text.strip()
    log.info("Pass 2 response: %d chars (stop_reason=%s)", len(raw), message.stop_reason)

    if message.stop_reason == "max_tokens":
        log.error("Pass 2 response TRUNCATED")

    # Parse JSON — three fallback strategies
    json_text = None

    m = re.search(r"AIRTABLE_ROWS_START\s*(.*?)\s*AIRTABLE_ROWS_END", raw, re.DOTALL)
    if m:
        json_text = m.group(1).strip()

    if not json_text:
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
        if m:
            json_text = m.group(1).strip()
            log.warning("Used fallback: markdown code fence")

    if not json_text:
        m = re.search(r"(\[\s*\{.*?\}\s*\])", raw, re.DOTALL)
        if m:
            json_text = m.group(1).strip()
            log.warning("Used fallback: bare JSON array")

    if not json_text:
        log.error("AIRTABLE_ROWS block not found. Raw output:\n%s", raw[:2000])
        return []

    try:
        rows = json.loads(json_text)
        log.info("Pass 2 parsed %d Airtable rows", len(rows))
        return rows
    except json.JSONDecodeError as e:
        log.error("JSON parse error: %s\nText: %s", e, json_text[:500])
        return []


# ── Airtable writing ───────────────────────────────────────────────────────────
def airtable_headers() -> dict:
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


def write_to_airtable(rows: list[dict]) -> int:
    if not rows:
        log.warning("No Airtable rows to write")
        return 0

    records = []
    for row in rows:
        fields = {
            FIELD_ARTICLE_NAME:     row.get("article_name", ""),
            FIELD_SOURCE:           row.get("source", ""),
            FIELD_SELECTION_REASON: row.get("selection_rationale", ""),
        }
        if row.get("url"):
            fields[FIELD_LINK] = row["url"]
        pub = row.get("publication_date", "")
        if pub and re.match(r"\d{4}-\d{2}-\d{2}", pub):
            fields[FIELD_PUB_DATE] = pub
        topic = row.get("topic", "").strip()
        if topic:
            fields[FIELD_TOPIC] = topic
        records.append({"fields": fields})

    written = 0
    for start in range(0, len(records), 10):
        batch = records[start:start + 10]
        resp = requests.post(
            AIRTABLE_API_URL,
            headers=airtable_headers(),
            json={"records": batch, "typecast": True},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            n = len(resp.json().get("records", []))
            written += n
            log.info("  Airtable batch %d-%d: %d rows written",
                     start + 1, start + len(batch), n)
        else:
            log.error("  Airtable batch %d-%d failed (%d): %s",
                      start + 1, start + len(batch), resp.status_code, resp.text[:300])
        time.sleep(0.25)

    return written


# ── Output saving ──────────────────────────────────────────────────────────────
def save_output(pass1_raw: str, airtable_rows: list[dict],
                selected_articles: list[dict]) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    out_path = OUTPUT_DIR / f"usp_editorial_{ts}.txt"
    content = (
        f"USP Newsletter Curator — Run {ts} UTC\n"
        f"Articles selected: {len(selected_articles)}\n\n"
        + "\n".join(f"  {i+1}. {a.get('title','?')}"
                    for i, a in enumerate(selected_articles))
        + "\n\n" + "=" * 80 + "\n\n"
        + "PASS 1 — EDITORIAL SELECTION\n\n" + pass1_raw
        + "\n\n" + "=" * 80 + "\n\n"
        + "PASS 2 — AIRTABLE ROWS\n\n"
        + json.dumps(airtable_rows, indent=2)
    )
    out_path.write_text(content, encoding="utf-8")
    log.info("Output saved → %s", out_path)


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    for f in [CRITERIA_FILE, AIRTABLE_PROMPT]:
        if not f.exists():
            log.error("Required file not found: %s", f)
            sys.exit(1)

    criteria       = CRITERIA_FILE.read_text(encoding="utf-8")
    airtable_instr = AIRTABLE_PROMPT.read_text(encoding="utf-8")
    log.info("Loaded criteria (%d chars) and airtable prompt (%d chars)",
             len(criteria), len(airtable_instr))

    # 1. Fetch saved articles from USP newsletter label
    articles = fetch_articles()
    if not articles:
        log.warning("No articles found. Exiting.")
        sys.exit(0)

    # 2. Pass 1 — editorial selection
    selected_indices, pass1_raw = pass1_select(articles, criteria)
    if not selected_indices:
        log.warning("No articles selected in Pass 1. Exiting.")
        sys.exit(0)

    selected_articles = [articles[i] for i in selected_indices]

    # 3. Pass 2 — Airtable classification
    airtable_rows = pass2_airtable(selected_articles, selected_indices, airtable_instr)

    # 4. Save output log
    save_output(pass1_raw, airtable_rows, selected_articles)

    # 5. Tag selected articles in InoReader
    tag_articles(selected_articles)

    # 6. Write to Airtable
    written = write_to_airtable(airtable_rows)
    log.info(
        "Done. %d articles tagged in InoReader, %d rows written to Airtable 'USP Newsletter'.",
        len(selected_articles), written
    )


if __name__ == "__main__":
    main()
