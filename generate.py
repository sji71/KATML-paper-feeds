#!/usr/bin/env python3
"""Build two public RSS feeds from OpenAlex and OpenReview (stdlib only)."""

import argparse
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

OPENALEX_URL = "https://api.openalex.org/works"
OPENREVIEW_URL = "https://api2.openreview.net/notes"
PAGE_SIZE = 100
RETRIES = 4


class FeedError(Exception):
    pass


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value):
    if not isinstance(value, str):
        return ""
    parser = TextOnly()
    parser.feed(value)
    parser.close()
    text = html.unescape(" ".join(parser.parts))
    # XML 1.0 cannot represent most control characters.
    text = "".join(c for c in text if c in "\t\n\r" or
                   0x20 <= ord(c) <= 0xD7FF or
                   0xE000 <= ord(c) <= 0xFFFD or
                   0x10000 <= ord(c) <= 0x10FFFF)
    return re.sub(r"\s+", " ", text).strip()


def get_json(base_url, params, *, token=None):
    url = base_url + "?" + urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "KATML-paper-feeds/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    for attempt in range(RETRIES + 1):
        try:
            with urlopen(Request(url, headers=headers), timeout=20) as response:
                data = json.load(response)
            if not isinstance(data, dict):
                raise FeedError("API response is not a JSON object")
            return data
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == RETRIES:
                raise FeedError(f"API request failed (HTTP {exc.code})") from None
            retry_after = exc.headers.get("Retry-After", "")
            delay = min(60, int(retry_after)) if retry_after.isdecimal() else min(30, 2 ** attempt)
        except (URLError, TimeoutError, socket.timeout):
            if attempt == RETRIES:
                raise FeedError("API request failed after timeout/network retries") from None
            delay = min(30, 2 ** attempt)
        time.sleep(delay)
    raise AssertionError("unreachable")


def abstract_from_index(index):
    if index is None:
        return ""
    if not isinstance(index, dict):
        raise FeedError("OpenAlex abstract index is malformed")
    positions = {}
    for word, indexes in index.items():
        if not isinstance(word, str) or not isinstance(indexes, list):
            raise FeedError("OpenAlex abstract index is malformed")
        for position in indexes:
            if not isinstance(position, int) or position < 0 or position in positions:
                raise FeedError("OpenAlex abstract positions are malformed")
            positions[position] = word
    if positions and set(positions) != set(range(len(positions))):
        raise FeedError("OpenAlex abstract positions are incomplete")
    return plain_text(" ".join(positions[i] for i in range(len(positions))))


def parse_date(value):
    if not isinstance(value, str):
        raise FeedError("OpenAlex publication_date is missing")
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise FeedError("OpenAlex publication_date is invalid") from None


def parse_ms(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise FeedError("OpenReview public date is missing")
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OverflowError, ValueError, OSError):
        raise FeedError("OpenReview public date is invalid") from None


def required_text(value, label):
    result = plain_text(value)
    if not result:
        raise FeedError(f"{label} is missing")
    return result


def openalex_items(terms, start_date, today, key):
    seen = {}
    fetched = 0
    for term in terms:
        cursor = "*"
        cursors = set()
        expected = None
        term_fetched = 0
        while True:
            if cursor in cursors:
                raise FeedError("OpenAlex repeated a page cursor")
            cursors.add(cursor)
            data = get_json(OPENALEX_URL, {
                "search": term,
                "filter": f"from_publication_date:{start_date},to_publication_date:{today}",
                "per_page": PAGE_SIZE,
                "cursor": cursor,
                "select": "id,title,publication_date,abstract_inverted_index",
            }, token=key)
            rows, meta = data.get("results"), data.get("meta")
            if not isinstance(rows, list) or not isinstance(meta, dict):
                raise FeedError("OpenAlex page is malformed")
            count = meta.get("count")
            if not isinstance(count, int) or count < 0 or (expected is not None and count != expected):
                raise FeedError("OpenAlex page count changed or is invalid")
            expected = count
            if len(rows) > PAGE_SIZE or (not rows and term_fetched < count):
                raise FeedError("OpenAlex pagination ended early")
            term_fetched += len(rows)
            if term_fetched > count:
                raise FeedError("OpenAlex returned more records than its count")
            for row in rows:
                if not isinstance(row, dict):
                    raise FeedError("OpenAlex work is malformed")
                work_id = row.get("id")
                if not isinstance(work_id, str) or not re.fullmatch(r"https://openalex\.org/W\d+", work_id):
                    raise FeedError("OpenAlex work ID is invalid")
                seen[work_id] = {
                    "guid": work_id,
                    "title": required_text(row.get("title"), "OpenAlex title"),
                    "link": work_id,
                    "description": abstract_from_index(row.get("abstract_inverted_index")),
                    "date": parse_date(row.get("publication_date")),
                }
            if term_fetched == count:
                break
            next_cursor = meta.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise FeedError("OpenAlex pagination has no next cursor")
            cursor = next_cursor
        fetched += term_fetched
        print(f"OpenAlex query {term!r}: fetched {term_fetched}")
    print(f"OpenAlex: fetched {fetched}, unique/written {len(seen)}")
    return list(seen.values())


def field_value(content, name):
    value = content.get(name)
    if isinstance(value, dict):
        value = value.get("value")
    return value


def openreview_items(invitations, cutoff_ms):
    seen = {}
    fetched = 0
    for invitation in invitations:
        offset = 0
        expected = None
        while True:
            data = get_json(OPENREVIEW_URL, {
                "invitation": invitation,
                "mintcdate": cutoff_ms,
                "limit": PAGE_SIZE,
                "offset": offset,
                "count": "true",
                "sort": "tcdate:asc",
            })
            rows, count = data.get("notes"), data.get("count")
            if not isinstance(rows, list) or not isinstance(count, int) or count < 0:
                raise FeedError("OpenReview page is malformed")
            if expected is not None and count != expected:
                raise FeedError("OpenReview page count changed")
            expected = count
            if len(rows) > PAGE_SIZE or (not rows and offset < count):
                raise FeedError("OpenReview pagination ended early")
            offset += len(rows)
            if offset > count:
                raise FeedError("OpenReview returned more records than its count")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("content"), dict):
                    raise FeedError("OpenReview note is malformed")
                note_id = row.get("id")
                if not isinstance(note_id, str) or not re.fullmatch(r"[A-Za-z0-9]{10}", note_id):
                    raise FeedError("OpenReview note ID is invalid")
                content = row["content"]
                # A public submission's online date is preferable to its later acceptance date.
                date_ms = row.get("odate") or row.get("cdate") or row.get("tcdate")
                seen[note_id] = {
                    "guid": "https://openreview.net/forum?id=" + note_id,
                    "title": required_text(field_value(content, "title"), "OpenReview title"),
                    "link": "https://openreview.net/forum?id=" + note_id,
                    "description": plain_text(field_value(content, "abstract")),
                    "date": parse_ms(date_ms),
                }
            if offset == count:
                break
        fetched += offset
        print(f"OpenReview invitation {invitation!r}: fetched {offset}")
    print(f"OpenReview: fetched {fetched}, unique/written {len(seen)}")
    return list(seen.values())


def make_rss(name, items):
    if not items:
        raise FeedError(f"{name}: no items; refusing to publish an empty feed")
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    for tag, value in (("title", f"KATML {name} papers"),
                       ("link", "https://github.com/sji71/KATML-paper-feeds"),
                       ("description", f"Recent public papers from {name}")):
        ET.SubElement(channel, tag).text = value
    for item in sorted(items, key=lambda x: (x["date"], x["guid"]), reverse=True):
        node = ET.SubElement(channel, "item")
        for tag, value in (("guid", item["guid"]), ("title", item["title"]),
                           ("link", item["link"]), ("description", item["description"]),
                           ("pubDate", format_datetime(item["date"]))):
            ET.SubElement(node, tag).text = value
    data = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    # The Power Automate RSS connector documents a 5 MB content limit.
    if len(data) > 5_000_000:
        raise FeedError(f"{name}: RSS exceeds Power Automate's 5 MB limit")
    parsed = ET.fromstring(data)
    if len(parsed.findall("./channel/item")) != len(items):
        raise FeedError(f"{name}: RSS validation failed")
    for node in parsed.findall("./channel/item"):
        if any(node.find(tag) is None for tag in ("guid", "title", "link", "description", "pubDate")):
            raise FeedError(f"{name}: RSS item lacks a required field")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    days = config.get("lookback_days")
    terms = config.get("openalex", {}).get("search_terms")
    invitations = config.get("openreview", {}).get("invitation_ids")
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise FeedError("lookback_days must be a positive integer")
    if not isinstance(terms, list) or not terms or any(not isinstance(t, str) or not t.strip() for t in terms):
        raise FeedError("openalex.search_terms must contain search terms")
    if not isinstance(invitations, list) or not invitations or any(
        not isinstance(i, str) or not i.strip() or i.startswith("REPLACE_") for i in invitations
    ):
        raise FeedError("replace openreview.invitation_ids with public submission invitation IDs")
    key = os.environ.get("OPENALEX_API_KEY")
    if not key:
        raise FeedError("OPENALEX_API_KEY is required")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    alex = openalex_items(terms, cutoff.date().isoformat(), now.date().isoformat(), key)
    review = openreview_items(invitations, int(cutoff.timestamp() * 1000))
    outputs = {"openalex.xml": make_rss("OpenAlex", alex),
               "openreview.xml": make_rss("OpenReview", review)}
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, data in outputs.items():
        (args.output / filename).write_bytes(data)
        print(f"Wrote {args.output / filename}: {len(data)} bytes")


if __name__ == "__main__":
    try:
        main()
    except (FeedError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Feed generation failed: {exc}", file=sys.stderr)
        sys.exit(1)
