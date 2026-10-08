"""Offline checks for PNG-carried HLS payload bytes and rejection boundaries.

Run directly with ``.venv/bin/python tests/test_hls_payload.py``. The fixtures
are synthesized PNG chunks, plain text, and short MPEG-TS-shaped byte strings;
no file, image decoder, network request, or external program is involved.
"""

from __future__ import annotations

import struct
import sys
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.hls_payload import MAX_HLS_PAYLOAD_BYTES, decode_hls_payload  # noqa: E402


PNG = b"\x89PNG\r\n\x1a\n"
TS = (b"\x47\x40\x00\x10" + bytes(range(184))) * 3


def _chunk(chunk_type: bytes, body: bytes = b"") -> bytes:
    crc = zlib.crc32(body, zlib.crc32(chunk_type)) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + chunk_type + body + struct.pack(">I", crc)


def _wrapped(body: bytes, *, flag: int = 0, extra_chunks: bytes = b"") -> bytes:
    encoded = zlib.compress(body) if flag == 1 else body
    # A tiny image-shaped IHDR supplies normal surrounding chunks. No image
    # is decoded; the fixture's only media content is in the ancillary chunk.
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        PNG + _chunk(b"IHDR", ihdr) + extra_chunks
        + _chunk(b"roUd", bytes([flag]) + encoded) + _chunk(b"IEND")
    )


class HlsPayloadTests(unittest.TestCase):
    def test_exported_default_limit_is_64_mebibytes(self) -> None:
        self.assertEqual(MAX_HLS_PAYLOAD_BYTES, 64 * 1024 * 1024)

    def test_non_png_media_text_and_empty_input_are_returned_unchanged(self) -> None:
        for data in (TS, b"#EXTM3U\n#EXTINF:1,\nfixture.ts\n", b"plain response", b"", PNG[:7]):
            with self.subTest(data=data[:20]):
                self.assertIs(decode_hls_payload(data), data)
                # This is a wrapper-specific bound, not a generic media cap.
                self.assertIs(decode_hls_payload(data, max_output=0), data)

    def test_uncompressed_payload_is_exact_without_image_conversion(self) -> None:
        self.assertEqual(decode_hls_payload(_wrapped(TS)), TS)
        self.assertEqual(decode_hls_payload(_wrapped(b"media\x00bytes\xff")), b"media\x00bytes\xff")

    def test_standard_zlib_payload_is_exact(self) -> None:
        self.assertEqual(decode_hls_payload(_wrapped(TS, flag=1)), TS)
        self.assertEqual(decode_hls_payload(_wrapped(b"#EXTM3U\n", flag=1)), b"#EXTM3U\n")

    def test_other_valid_chunks_are_ignored_after_crc_validation(self) -> None:
        extras = _chunk(b"tEXt", b"fixture\x00plain metadata") + _chunk(b"IDAT", b"not decoded")
        self.assertEqual(decode_hls_payload(_wrapped(TS, extra_chunks=extras)), TS)

    def test_minimal_container_needs_no_image_decoder_or_ihdr_semantics(self) -> None:
        container = PNG + _chunk(b"roUd", b"\x00" + TS) + _chunk(b"IEND")
        self.assertEqual(decode_hls_payload(container), TS)

    def test_empty_or_missing_media_chunk_is_rejected(self) -> None:
        cases = (
            PNG + _chunk(b"IEND"),
            PNG + _chunk(b"roUd") + _chunk(b"IEND"),
            _wrapped(b""),
            _wrapped(b"", flag=1),
            PNG + _chunk(b"roUd", b"\x01") + _chunk(b"IEND"),
        )
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    decode_hls_payload(data)

    def test_unknown_payload_flags_are_rejected(self) -> None:
        for flag in (2, 127, 255):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(ValueError, "flag"):
                    decode_hls_payload(_wrapped(TS, flag=flag))

    def test_duplicate_media_chunks_are_rejected_instead_of_concatenated(self) -> None:
        data = PNG + _chunk(b"roUd", b"\x00first") + _chunk(b"roUd", b"\x00second") + _chunk(b"IEND")
        with self.assertRaisesRegex(ValueError, "multiple"):
            decode_hls_payload(data)

    def test_every_truncated_container_prefix_fails_closed(self) -> None:
        valid = _wrapped(TS, flag=1)
        # Starting at a complete PNG signature avoids confusing a non-PNG
        # prefix with a wrapper that has actually declared itself to be PNG.
        for length in range(len(PNG), len(valid)):
            with self.subTest(length=length):
                with self.assertRaises(ValueError):
                    decode_hls_payload(valid[:length])

    def test_big_endian_lengths_cannot_read_past_the_container(self) -> None:
        for claimed in (10, 0x7FFFFFFF, 0xFFFFFFFF):
            data = PNG + struct.pack(">I", claimed) + b"roUd\x00x\x00\x00\x00\x00"
            with self.subTest(claimed=claimed):
                with self.assertRaisesRegex(ValueError, "Truncated"):
                    decode_hls_payload(data)

    def test_media_and_other_chunk_crc_mismatches_are_rejected(self) -> None:
        for chunk_type, body in ((b"roUd", b"\x00" + TS), (b"tEXt", b"fixture"), (b"IEND", b"")):
            corrupted = bytearray(_chunk(chunk_type, body))
            corrupted[-1] ^= 1
            data = PNG + bytes(corrupted)
            if chunk_type != b"roUd":
                data += _chunk(b"roUd", b"\x00" + TS)
            if chunk_type != b"IEND":
                data += _chunk(b"IEND")
            with self.subTest(chunk_type=chunk_type):
                with self.assertRaisesRegex(ValueError, "CRC"):
                    decode_hls_payload(data)

    def test_iend_must_be_present_empty_and_last(self) -> None:
        media = _chunk(b"roUd", b"\x00" + TS)
        cases = (
            PNG + media,
            PNG + media + _chunk(b"IEND", b"not empty"),
            PNG + media + _chunk(b"IEND") + b"garbage",
            PNG + media + _chunk(b"IEND") + _chunk(b"tEXt", b"after end"),
            PNG + _chunk(b"IEND") + media,
        )
        for data in cases:
            with self.subTest(data=data[-20:]):
                with self.assertRaises(ValueError):
                    decode_hls_payload(data)

    def test_invalid_raw_deflate_gzip_and_truncated_zlib_are_rejected(self) -> None:
        raw_deflate = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw = raw_deflate.compress(TS) + raw_deflate.flush()
        gzip_deflate = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
        gzip = gzip_deflate.compress(TS) + gzip_deflate.flush()
        compressed = zlib.compress(TS)
        cases = (b"not zlib data", raw, gzip, compressed[:1], compressed[:-1], compressed[:-4])
        for encoded in cases:
            with self.subTest(encoded=encoded[:16]):
                data = PNG + _chunk(b"roUd", b"\x01" + encoded) + _chunk(b"IEND")
                with self.assertRaises(ValueError):
                    decode_hls_payload(data)

    def test_zlib_trailing_garbage_and_second_stream_are_rejected(self) -> None:
        for tail in (b"garbage", b"\x00", zlib.compress(b"second stream")):
            with self.subTest(tail=tail):
                data = PNG + _chunk(b"roUd", b"\x01" + zlib.compress(TS) + tail) + _chunk(b"IEND")
                with self.assertRaisesRegex(ValueError, "trailing"):
                    decode_hls_payload(data)

    def test_exact_output_limit_succeeds_and_one_byte_less_fails(self) -> None:
        for flag in (0, 1):
            with self.subTest(flag=flag):
                data = _wrapped(TS, flag=flag)
                self.assertEqual(decode_hls_payload(data, max_output=len(TS)), TS)
                with self.assertRaisesRegex(ValueError, "limit"):
                    decode_hls_payload(data, max_output=len(TS) - 1)
                with self.assertRaisesRegex(ValueError, "limit"):
                    decode_hls_payload(data, max_output=0)

    def test_compressed_bomb_is_bounded_during_decompression(self) -> None:
        data = _wrapped(b"A" * (1024 * 1024), flag=1)
        real_inflater = zlib.decompressobj()
        calls: list[int] = []

        class RecordingInflater:
            def decompress(self, encoded, max_length):
                calls.append(max_length)
                return real_inflater.decompress(encoded, max_length)

            def __getattr__(self, name):
                return getattr(real_inflater, name)

        with patch("src.hls_payload.zlib.decompressobj", return_value=RecordingInflater()):
            with self.assertRaisesRegex(ValueError, "limit"):
                decode_hls_payload(data, max_output=128)
        self.assertEqual(calls, [129])
        self.assertFalse(real_inflater.eof)
        self.assertTrue(real_inflater.unconsumed_tail)

    def test_invalid_output_limits_are_rejected_for_wrapped_media(self) -> None:
        for limit in (-1, True, False, 1.5, "64", None, sys.maxsize, sys.maxsize + 1):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    decode_hls_payload(_wrapped(TS), max_output=limit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
