import json
from email.utils import parsedate_to_datetime
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

import generate


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "lookback_days": 14,
            "openalex": {"search_terms": ["sample"]},
            "openreview": {"invitation_ids": ["Venue/2026/Conference/-/Submission"]},
        }))
        self.output = self.root / "dist"

    def run_generator(self):
        with patch.dict(os.environ, {"OPENALEX_API_KEY": "test-key"}), \
             patch("generate.sys.argv", ["generate.py", "--config", str(self.config),
                                         "--output", str(self.output)]):
            generate.main()

    def test_pagination_fields_and_deduplication(self):
        def response(url, params, **kwargs):
            if url == generate.OPENALEX_URL:
                base = {"id": "https://openalex.org/W1", "title": "A & B",
                        "publication_date": "2026-10-19",
                        "abstract_inverted_index": {"A": [0], "summary": [2]}}
                other = dict(base, id="https://openalex.org/W2", title="Second")
                if params["cursor"] == "*":
                    return {"meta": {"count": 3, "next_cursor": "next"},
                            "results": [base, other]}
                return {"meta": {"count": 3, "next_cursor": None}, "results": [base]}
            note = {"id": "abcdefgh12", "content": {
                "title": {"value": "<b>Title</b>"},
                "abstract": {"value": "<p>Plain &amp; clear</p>"}},
                "odate": 1792368000000}
            self.assertIn("mintcdate", params)
            self.assertEqual(params["count"], "true")
            if params["offset"] == 0:
                return {"count": 2, "notes": [note]}
            return {"count": 2, "notes": [dict(note, id="abcdefgh13")]}

        with patch("generate.get_json", side_effect=response):
            self.run_generator()
        alex = ET.parse(self.output / "openalex.xml")
        review = ET.parse(self.output / "openreview.xml")
        self.assertEqual(len(alex.findall("./channel/item")), 2)
        self.assertEqual(len(review.findall("./channel/item")), 2)
        first = next(item for item in alex.findall("./channel/item")
                     if item.findtext("guid") == "https://openalex.org/W1")
        self.assertEqual(first.findtext("description"), "A summary")
        self.assertEqual(first.findtext("guid"), "https://openalex.org/W1")
        self.assertEqual(parsedate_to_datetime(first.findtext("pubDate")).year, 2026)
        self.assertEqual(review.find("./channel/item/description").text, "Plain & clear")
        self.assertIn("https://openreview.net/forum?id=abcdefgh13",
                      [item.findtext("link") for item in review.findall("./channel/item")])

    def test_incomplete_page_blocks_both_outputs(self):
        def response(url, params, **kwargs):
            if url == generate.OPENALEX_URL:
                return {"meta": {"count": 1, "next_cursor": None}, "results": [{
                    "id": "https://openalex.org/W1", "title": "Test",
                    "publication_date": "2026-10-19", "abstract_inverted_index": None}]}
            return {"count": 2, "notes": []}

        with patch("generate.get_json", side_effect=response):
            with self.assertRaisesRegex(generate.FeedError, "pagination ended early"):
                self.run_generator()
        self.assertFalse(self.output.exists())

    def test_untitled_openalex_work_is_skipped_without_losing_other_items(self):
        def response(url, params, **kwargs):
            if url == generate.OPENALEX_URL:
                return {"meta": {"count": 2, "next_cursor": None}, "results": [
                    {"id": "https://openalex.org/W1", "title": None,
                     "publication_date": "2026-10-19", "abstract_inverted_index": None},
                    {"id": "https://openalex.org/W2", "title": "Valid paper",
                     "publication_date": "2026-10-19", "abstract_inverted_index": None},
                ]}
            return {"count": 1, "notes": [{
                "id": "abcdefgh12", "content": {"title": {"value": "OpenReview paper"}},
                "odate": 1792368000000,
            }]}

        with patch("generate.get_json", side_effect=response):
            self.run_generator()
        items = ET.parse(self.output / "openalex.xml").findall("./channel/item")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].findtext("guid"), "https://openalex.org/W2")
        self.assertEqual(items[0].findtext("title"), "Valid paper")
        self.assertEqual(len(ET.parse(self.output / "openreview.xml").findall("./channel/item")), 1)

    def test_all_untitled_openalex_works_block_both_outputs(self):
        def response(url, params, **kwargs):
            if url == generate.OPENALEX_URL:
                return {"meta": {"count": 1, "next_cursor": None}, "results": [{
                    "id": "https://openalex.org/W1", "title": None,
                    "publication_date": "2026-10-19", "abstract_inverted_index": None,
                }]}
            return {"count": 1, "notes": [{
                "id": "abcdefgh12", "content": {"title": {"value": "OpenReview paper"}},
                "odate": 1792368000000,
            }]}

        with patch("generate.get_json", side_effect=response):
            with self.assertRaisesRegex(generate.FeedError, "refusing to publish an empty feed"):
                self.run_generator()
        self.assertFalse(self.output.exists())

    def test_invalid_xml_character_is_removed(self):
        data = generate.make_rss("Test", [{
            "guid": "https://example.org/1", "title": generate.plain_text("A\x01B"),
            "link": "https://example.org/1", "description": "Text",
            "date": generate.parse_date("2026-10-19") }])
        self.assertEqual(ET.fromstring(data).findtext("./channel/item/title"), "AB")

    def test_sparse_openalex_abstract_uses_available_words_in_position_order(self):
        abstract = generate.abstract_from_index({"last": [5], "first": [1], "middle": [3]})
        self.assertEqual(abstract, "first middle last")
        rss = generate.make_rss("OpenAlex", [{
            "guid": "https://openalex.org/W1", "title": "Valid paper",
            "link": "https://openalex.org/W1", "description": abstract,
            "date": generate.parse_date("2026-10-19"),
        }])
        self.assertEqual(ET.fromstring(rss).findtext("./channel/item/description"), abstract)
        with self.assertRaisesRegex(generate.FeedError, "positions are malformed"):
            generate.abstract_from_index({"first": [1], "another": [1]})

    def test_429_is_retried_without_key_in_url(self):
        error = HTTPError("https://api.openalex.org/works", 429, "rate limit", {}, None)
        with patch("generate.urlopen", side_effect=[error, BytesIO(b'{"results":[]}')]) as opener, \
             patch("generate.time.sleep") as sleeper:
            self.assertEqual(generate.get_json(generate.OPENALEX_URL, {"cursor": "*"},
                                               token="test-key"), {"results": []})
        self.assertEqual(opener.call_count, 2)
        request = opener.call_args.args[0]
        self.assertNotIn("test-key", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        sleeper.assert_called_once()


if __name__ == "__main__":
    unittest.main()
