"""Bound untrusted JS transforms before allocation and isolate bad declarations."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tracemalloc
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import js_decoder as decoder
from src.scraper import VideoScraper
from http_fixtures import html_response


def block(url, operations="", name="a"):
    return (f'<script>const _s{name}={json.dumps([url])};const _i{name}=[0];'
            f'let s="";s+=_s{name}[_i{name}.indexOf(0)];{operations}</script>')


class DecoderResourceTests(unittest.TestCase):
    def test_repeated_suffix_expansion_rejects_candidate_and_keeps_next_block(self):
        operations = '''s=s.replace("", "$' $'");''' * 10
        good = "https://cdn.example.test/next.m3u8"
        page = block("https://example.invalid/a.mp4", operations) + block(good, name="b")
        self.assertEqual(decoder.decode_obfuscated_urls(page), [good])

    def test_suffix_expansion_is_rejected_before_large_string_allocation(self):
        value = "a" * decoder.MAX_CANDIDATE_CHARS
        replacement = "$'" * 1000  # Would allocate about 65 MiB without the guard.
        tracemalloc.start()
        try:
            with self.assertRaises(ValueError):
                decoder._js_string_replace(value, "", replacement)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_exact_limit_is_allowed_but_one_extra_character_is_not(self):
        value = "x" * decoder.MAX_CANDIDATE_CHARS
        self.assertEqual(decoder._js_string_replace(value, "x", "y"), "y" + value[1:])
        with self.assertRaises(ValueError):
            decoder._js_string_replace(value, "", "y")

    def test_dollar_tokens_preserve_js_string_search_semantics(self):
        cases = (
            ("abc", "b", "$$", "a$c"),
            ("abc", "b", "$&", "abc"),
            ("abc", "b", "$`", "aac"),
            ("abc", "b", "$'", "acc"),
            ("abc", "b", "$1:$<x>:$x", "a$1:$<x>:$xc"),
            ("abc", "", "$`-$&-$'", "--abcabc"),
            ("abcb", "b", "$$$&$`$'", "a$bacbcb"),
        )
        for value, needle, replacement, expected in cases:
            with self.subTest(value=value, needle=needle, replacement=replacement):
                self.assertEqual(decoder._js_string_replace(value, needle, replacement), expected)

    def test_unmatched_large_template_does_not_change_a_small_input(self):
        self.assertEqual(decoder._js_string_replace("abc", "missing", "$'" * 1000), "abc")

    def test_decode_and_chained_expressions_keep_existing_order(self):
        expected = "https://cdn.example.test/a.mp4?token=one%2Ftwo"
        encoded = quote(expected.replace("a.mp4", "aTOKEN.mp4"), safe="")
        page = block(encoded, 's=decodeURIComponent(s).replace("TOKEN", "");')
        self.assertEqual(decoder.decode_obfuscated_urls(page), [expected])

    def test_oversized_intermediate_is_rejected_even_if_later_code_would_shrink(self):
        with patch.object(decoder, "MAX_CANDIDATE_CHARS", 50):
            page = block("https://fixture.test/a.mp4", '''s=s.replace("", "$'$'").replace("https://fixture.test/", "");''')
            self.assertEqual(decoder.decode_obfuscated_urls(page), [])

    def test_total_output_budget_retains_earlier_and_later_fitting_candidates(self):
        first = "https://fixture.test/" + "a" * 30 + ".mp4"
        second = "https://fixture.test/" + "b" * 30 + ".mp4"
        third = "https://f.test/c.mp4"
        with patch.object(decoder, "MAX_TOTAL_CANDIDATE_CHARS", 80):
            actual = decoder.decode_obfuscated_urls(block(first) + block(second, name="b") + block(third, name="c"))
        self.assertEqual(actual, [first, third])
        self.assertLessEqual(sum(map(len, actual)), 80)

    def test_duplicate_candidates_do_not_exhaust_output_budget(self):
        first, second = "https://fixture.test/a.mp4", "https://fixture.test/b.mp4"
        page = "".join(block(first, name=str(i)) for i in range(5)) + block(second, name="last")
        with patch.object(decoder, "MAX_TOTAL_CANDIDATE_CHARS", len(first) + len(second)):
            self.assertEqual(decoder.decode_obfuscated_urls(page), [first, second])

    def test_page_work_budget_stops_later_work_without_discarding_completed_urls(self):
        urls = [f"https://fixture.test/{i}.mp4" for i in range(20)]
        page = "".join(block(url, name=str(i)) for i, url in enumerate(urls))
        with patch.object(decoder, "MAX_TRANSFORM_WORK_CHARS", 100):
            actual = decoder.decode_obfuscated_urls(page)
        self.assertTrue(actual)
        self.assertLess(len(actual), len(urls))
        self.assertEqual(actual, urls[:len(actual)])

    def test_unrelated_huge_integer_declaration_does_not_hide_other_arrays(self):
        before, after = "https://fixture.test/before.mp4", "https://fixture.test/after.m3u8"
        limit = sys.get_int_max_str_digits()
        page = block(before) + "<script>const _noise=[" + "9" * 5000 + "];</script>" + block(after, name="b")
        self.assertEqual(decoder.decode_obfuscated_urls(page), [before, after])
        self.assertEqual(sys.get_int_max_str_digits(), limit)

    def test_real_scrape_keeps_normal_and_hidden_video_with_unrelated_huge_integer(self):
        hidden = "https://cdn.example.test/hidden.m3u8"
        page_url = "https://page.example.test/watch"
        html = '<video src="https://cdn.example.test/normal.mp4"></video>' + block(quote(hidden, safe="")) + "<script>const _noise=[" + "9" * 5000 + "];</script>"
        logs = []
        with patch("src.scraper.ScopedCookieSession.get", return_value=html_response(text=html, url=page_url)), patch.object(VideoScraper, "_scrape_with_ytdlp", return_value=[]):
            items = VideoScraper().scrape(page_url, logs.append)
        self.assertEqual({item.url for item in items}, {"https://cdn.example.test/normal.mp4", hidden})
        self.assertFalse(any("JS decoder failed" in line for line in logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
