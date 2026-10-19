"""Role changes isolate cached representations and preserve in-flight users."""
from __future__ import annotations

import io
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.media_proxy import MediaProxyServer


class Request:
    def __init__(self, proxy, entry, output=None):
        self.path = f"/play/{proxy._token_for(entry)}.{entry.proxy_ext}"
        self.headers = {}
        self.wfile = output if output is not None else io.BytesIO()
        self.status = None
        self.output_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.output_headers[name] = value

    def end_headers(self):
        pass


class HeldOutput(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, data):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("Test did not release the old player request")
        return super().write(data)


class ProxyRoleCacheTests(unittest.TestCase):
    MEDIA = "#EXTM3U\n#EXTINF:2,\nclip.png\n#EXT-X-ENDLIST\n"
    MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nclip.png\n"

    def setUp(self):
        self.proxy = MediaProxyServer(buffer_seconds=0)
        self.addCleanup(self.proxy.stop)
        self.parent = self.proxy._lookup(self.proxy.register(
            "https://fixture.test/root.m3u8", is_hls=True, hls_png_wrapped=True))

    def rewrite(self, text):
        return self.proxy._rewrite_m3u8(text, self.parent.upstream, self.parent)

    def serve(self, entry, payload):
        request = Request(self.proxy, entry)
        with patch.object(self.proxy, "_fetch_wrapped_payload", return_value=(payload, entry.upstream)) as fetch:
            self.proxy._handle(request, True, "GET")
        return request, fetch.call_count

    def test_media_manifest_media_fetches_new_body_instead_of_old_cache(self):
        _, segments, _ = self.rewrite(self.MEDIA)
        original = segments[0]
        request, _ = self.serve(original, b"OLD")
        self.assertEqual(request.wfile.getvalue(), b"OLD")
        self.rewrite(self.MASTER)
        manifest = self.parent.hls_child_by_upstream[original.upstream]
        nested = b"#EXTM3U\n#EXTINF:2,\nnested.png\n#EXT-X-ENDLIST\n"
        request, count = self.serve(manifest, nested)
        self.assertEqual((request.status, count), (200, 1))
        _, segments, _ = self.rewrite(self.MEDIA)
        replacement = segments[0]
        request, count = self.serve(replacement, b"NEW")
        self.assertEqual((request.status, count, request.wfile.getvalue()), (200, 1, b"NEW"))
        self.assertIsNot(replacement, original)
        self.assertIsNot(replacement, manifest)
        self.assertTrue(original.closed and manifest.closed)
        self.assertFalse(original.cache_path.exists() or manifest.cache_path.exists())
        self.assertEqual(self.parent.children, [replacement])
        self.assertEqual(self.proxy._cache_budget.used, 4096)

    def test_same_role_refresh_reuses_cache_without_refetch(self):
        _, segments, _ = self.rewrite(self.MEDIA)
        original = segments[0]
        self.serve(original, b"ORIGINAL")
        before = self.proxy._token_for(original)
        _, segments, _ = self.rewrite(self.MEDIA)
        self.assertEqual(segments, [original])
        self.assertEqual(self.proxy._token_for(original), before)
        request, count = self.serve(original, b"UNUSED")
        self.assertEqual((request.status, count, request.wfile.getvalue()), (200, 0, b"ORIGINAL"))
        self.assertEqual(len(self.parent.children), 1)

    def test_failed_role_change_retains_original_cache_and_lookup(self):
        _, segments, _ = self.rewrite(self.MEDIA)
        original = segments[0]
        self.serve(original, b"ORIGINAL")
        for fail_commit in (False, True):
            if fail_commit:
                with patch.object(self.proxy, "_retire_manifest_children", side_effect=RuntimeError("commit failed")):
                    with self.assertRaisesRegex(RuntimeError, "commit failed"):
                        self.rewrite(self.MASTER)
            else:
                with self.assertRaises(ValueError):
                    self.rewrite(self.MASTER + "#EXTINF:invalid,\n")
            self.assertIs(self.parent.hls_child_by_upstream[original.upstream], original)
            self.assertFalse(original.closed or original._superseded)
            self.assertEqual(self.parent.children, [original])
            request, count = self.serve(original, b"UNUSED")
            self.assertEqual((request.status, count, request.wfile.getvalue()), (200, 0, b"ORIGINAL"))
            self.assertEqual(self.proxy._cache_budget.used, 4096)

    def test_active_descendant_finishes_before_old_role_generation_is_reclaimed(self):
        self.rewrite(self.MASTER)
        old_manifest = self.parent.children[0]
        nested = "#EXTM3U\n#EXTINF:2,\nnested.png\n#EXT-X-ENDLIST\n"
        _, segments, _ = self.proxy._rewrite_m3u8(nested, old_manifest.upstream, old_manifest)
        old_segment = segments[0]
        self.serve(old_segment, b"OLD")
        output = HeldOutput()
        request = Request(self.proxy, old_segment, output)
        errors = []

        def old_request():
            try:
                self.proxy._handle(request, True, "GET")
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=old_request)
        thread.start()
        try:
            self.assertTrue(output.entered.wait(2))
            _, segments, _ = self.rewrite(self.MEDIA)
            replacement = segments[0]
            self.assertIsNot(replacement, old_manifest)
            self.assertFalse(old_manifest.closed or old_segment.closed)
            current, _ = self.serve(replacement, b"NEW")
            self.assertEqual(current.wfile.getvalue(), b"NEW")
        finally:
            output.release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((request.status, output.getvalue()), (200, b"OLD"))
        self.assertTrue(old_manifest.closed and old_segment.closed)
        self.assertFalse(old_segment.cache_path.exists())
        self.assertEqual(self.parent.children, [replacement])
        self.assertEqual(self.proxy._cache_budget.used, 4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
