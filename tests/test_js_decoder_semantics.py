"""Audit B17: replay supported JS string transforms without executing code."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.js_decoder import decode_obfuscated_urls


def script(url, operations="", *, suffix="", assembly=False):
    prefix = f"const _parts{suffix} = {json.dumps([url])}; const _order{suffix} = [0];"
    if assembly:
        prefix += f"let url = '';for(let i=0;i<_order{suffix}.length;i++){{url += _parts{suffix}[_order{suffix}.indexOf(i)];}}"
    return prefix + operations


class JSDecoderSemanticsTests(unittest.TestCase):
    def test_string_replace_only_changes_first_match_and_returns_final_url(self):
        actual = decode_obfuscated_urls(script(
            "https://cdn.example.test/aTOKENbTOKEN.mp4",
            'url = decodeURIComponent(url).replace("TOKEN", "");'
        ))
        self.assertEqual(actual, ["https://cdn.example.test/abTOKEN.mp4"])

    def test_assembled_variable_ignores_other_variables(self):
        actual = decode_obfuscated_urls(script(
            "https%3A%2F%2Fcdn.example.test%2FaTOKEN.mp4",
            'other = other.replace("cdn", "wrong");url = decodeURIComponent(url);url = url.replace("TOKEN", "");',
            assembly=True,
        ))
        self.assertEqual(actual, ["https://cdn.example.test/a.mp4"])

    def test_operations_are_applied_in_source_order(self):
        actual = decode_obfuscated_urls(script(
            "https%3A%2F%2Fcdn.example.test%2FaTOKEN.mp4",
            'url = url.replace("TOKEN", "%54OKEN");url = decodeURIComponent(url).replace("TOKEN", "");'
        ))
        self.assertEqual(actual, ["https://cdn.example.test/a.mp4"])

    def test_literal_escapes_are_decoded_without_python_or_js_eval(self):
        actual = decode_obfuscated_urls(script(
            "https://cdn.example.test/aTOKEN.mp4",
            r'''url = url.replace('\x54\u004fKEN', '\/');'''
        ))
        self.assertEqual(actual, ["https://cdn.example.test/a/.mp4"])

    def test_javascript_dollar_substitutions(self):
        actual = decode_obfuscated_urls(script(
            "https://cdn.example.test/aTOKEN.mp4",
            '''url = url.replace("TOKEN", "$$$&");'''
        ))
        self.assertEqual(actual, ["https://cdn.example.test/a$TOKEN.mp4"])

    def test_empty_string_search_inserts_once(self):
        actual = decode_obfuscated_urls(script(
            "https://cdn.example.test/a.mp4",
            '''url = url.replace("", "");'''
        ))
        self.assertEqual(actual, ["https://cdn.example.test/a.mp4"])

    def test_plain_signed_url_is_not_decoded_without_decode_operation(self):
        url = "https://cdn.example.test/a.mp4?token=one%2Ftwo%252Fthree"
        self.assertEqual(decode_obfuscated_urls(script(url)), [url])

    def test_unsupported_regex_replace_does_not_emit_wrong_raw_candidate(self):
        self.assertEqual(decode_obfuscated_urls(script(
            "https://cdn.example.test/aTOKEN.mp4", 'url = decodeURIComponent(url).replace(/TOKEN/g, "");'
        )), [])

    def test_invalid_percent_sequence_cannot_succeed_decode_uri_component(self):
        self.assertEqual(decode_obfuscated_urls(script(
            "https://cdn.example.test/a.mp4?x=%no", 'url = decodeURIComponent(url);'
        )), [])

    def test_multiple_unattributed_transform_targets_are_not_guessed(self):
        self.assertEqual(decode_obfuscated_urls(script(
            "https://cdn.example.test/aTOKEN.mp4",
            'a = a.replace("TOKEN", "");b = b.replace("TOKEN", "wrong");'
        )), [])

    def test_comments_do_not_supply_transforms(self):
        actual = decode_obfuscated_urls(script(
            "https://cdn.example.test/aTOKEN.mp4",
            '// url = url.replace("TOKEN", "wrong");\nurl = url.replace("TOKEN", "");'
        ))
        self.assertEqual(actual, ["https://cdn.example.test/a.mp4"])

    def test_string_contents_do_not_supply_transforms(self):
        actual = decode_obfuscated_urls(script(
            "https://cdn.example.test/aTOKEN.mp4",
            '''console.log('url = url.replace("TOKEN", "wrong");');'''
            '''url = url.replace("TOKEN", "");''',
            assembly=True,
        ))
        self.assertEqual(actual, ["https://cdn.example.test/a.mp4"])

    def test_property_assignment_does_not_reassign_assembled_variable(self):
        actual = decode_obfuscated_urls(script(
            "https://cdn.example.test/aTOKEN.mp4",
            '''window . url = url.replace("TOKEN", "wrong");'''
            '''url = url.replace("TOKEN", "");''',
            assembly=True,
        ))
        self.assertEqual(actual, ["https://cdn.example.test/a.mp4"])

    def test_newline_terminated_self_assignments_follow_js_asi(self):
        actual = decode_obfuscated_urls(script(
            "https%3A%2F%2Fcdn.example.test%2FaTOKEN.mp4",
            'url = decodeURIComponent(url)\nurl = url.replace("TOKEN", "")\n'
        ))
        self.assertEqual(actual, ["https://cdn.example.test/a.mp4"])

    def test_independent_blocks_have_stable_order_and_do_not_cross_apply(self):
        page = (
            '<script>' + script("https://cdn.example.test/aTOKEN.mp4", 'url = url.replace("TOKEN", "");', suffix="A") + '</script>'
            '<script>' + script("https://cdn.example.test/bJUNK.mp4", 'url = url.replace("JUNK", "");', suffix="B") + '</script>'
        )
        for _ in range(4):
            self.assertEqual(decode_obfuscated_urls(page), [
                "https://cdn.example.test/a.mp4", "https://cdn.example.test/b.mp4"
            ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
