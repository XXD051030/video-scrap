"""Read media bytes carried in a PNG ``roUd`` ancillary chunk.

This handles one explicitly identified HLS transport wrapper. It does not
decode images, execute scripts, or infer whether ordinary media is playable.
"""

from __future__ import annotations

import sys
import zlib


MAX_HLS_PAYLOAD_BYTES = 64 * 1024 * 1024

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def decode_hls_payload(
    data: bytes, *, max_output: int = MAX_HLS_PAYLOAD_BYTES
) -> bytes:
    """Return media from a validated PNG wrapper, or unchanged non-PNG bytes.

    PNG input must contain exactly one ``roUd`` chunk and a final, empty
    ``IEND`` chunk. Every chunk boundary and CRC is checked, without reading
    image properties or pixels. The first ``roUd`` byte selects plain (0) or
    standard zlib-compressed (1) data. Empty payloads are invalid.

    ``max_output`` bounds only the recovered payload, including while it is
    being decompressed. Invalid wrappers raise ``ValueError`` rather than
    returning bytes that could be mistaken for an HLS segment.
    """
    if not data.startswith(_PNG_SIGNATURE):
        return data
    if (
        not isinstance(max_output, int)
        or isinstance(max_output, bool)
        or max_output < 0
        or max_output >= sys.maxsize
    ):
        raise ValueError("HLS payload limit must be a nonnegative bounded integer")

    view = memoryview(data)
    pos = len(_PNG_SIGNATURE)
    payload: memoryview | None = None
    has_iend = False

    while pos < len(view):
        if len(view) - pos < 12:
            raise ValueError("Truncated PNG chunk header or CRC")
        length = int.from_bytes(view[pos:pos + 4], "big")
        chunk_type = bytes(view[pos + 4:pos + 8])
        chunk_end = pos + 12 + length
        if chunk_end > len(view):
            raise ValueError("Truncated PNG chunk data or CRC")
        chunk = view[pos + 8:pos + 8 + length]
        expected_crc = int.from_bytes(view[pos + 8 + length:chunk_end], "big")
        actual_crc = zlib.crc32(chunk, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError("PNG chunk CRC mismatch")

        if chunk_type == b"roUd":
            if payload is not None:
                raise ValueError("PNG wrapper contains multiple roUd chunks")
            if not chunk:
                raise ValueError("PNG roUd chunk is empty")
            payload = chunk
        elif chunk_type == b"IEND":
            if length:
                raise ValueError("PNG IEND chunk must be empty")
            if chunk_end != len(view):
                raise ValueError("PNG wrapper has data after IEND")
            has_iend = True
            break
        pos = chunk_end

    if not has_iend:
        raise ValueError("PNG wrapper is missing IEND")
    if payload is None:
        raise ValueError("PNG wrapper is missing roUd media data")

    flag = payload[0]
    encoded = payload[1:]
    if flag == 0:
        if len(encoded) > max_output:
            raise ValueError("HLS payload exceeds the configured limit")
        result = bytes(encoded)
    elif flag == 1:
        inflater = zlib.decompressobj()
        try:
            result = inflater.decompress(encoded, max_output + 1)
        except zlib.error as exc:
            raise ValueError("Invalid zlib HLS payload") from exc
        if len(result) > max_output:
            raise ValueError("HLS payload exceeds the configured limit")
        if not inflater.eof:
            raise ValueError("Truncated zlib HLS payload")
        if inflater.unused_data or inflater.unconsumed_tail:
            raise ValueError("Zlib HLS payload has trailing data")
    else:
        raise ValueError("Unknown PNG roUd payload flag")

    if not result:
        raise ValueError("PNG wrapper contains an empty HLS payload")
    return result
