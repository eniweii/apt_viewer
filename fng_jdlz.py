#!/usr/bin/env python3
"""
fng_jdlz.py

JDLZ decoder for the Carbon FNG blocks. Port of jdlz::decompress from the MW SDK
(github.com/tsyvm/mwsdk, src/jdlz.cpp). Not tested on a real Carbon file yet.

Header, 16 bytes little-endian:
  'JDLZ', version u8, header size u8, reserved u16, unpacked size u32, packed size u32
Body: two 9-bit flag registers, refilled one byte at a time.
  f1 (every step)    bit set = back reference, bit clear = literal byte
  f2 (every match)   bit set = long length, short distance (0x0F). bit clear = short length, long distance
"""
import struct

HEADER_SIZE = 16
MAGIC = b"JDLZ"


class JdlzError(Exception):
    pass


def unpacked_size(data):
    """Unpacked size from the header, or None if data is not a JDLZ stream."""
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        return None
    return struct.unpack_from("<I", data, 8)[0]


def decompress(data, limit=None):
    """
    Decodes a complete JDLZ stream (header + body).
    limit: stop after this many output bytes (used to read only the head of a file).
    """
    out_len = unpacked_size(data)
    if out_len is None:
        raise JdlzError("not a JDLZ stream")

    target = out_len if limit is None else min(out_len, limit)
    out = bytearray()
    pos = HEADER_SIZE
    n = len(data)
    f1 = 1
    f2 = 1

    try:
        while pos < n and len(out) < target:
            if f1 == 1:
                f1 = data[pos] | 0x100
                pos += 1
            if f2 == 1:
                f2 = data[pos] | 0x100
                pos += 1

            if f1 & 1:
                b0 = data[pos]
                b1 = data[pos + 1]
                pos += 2
                if f2 & 1:
                    length = (((b0 & 0xF0) << 4) | b1) + 3
                    dist = (b0 & 0x0F) + 1
                else:
                    length = (b0 & 0x1F) + 3
                    dist = (((b0 & 0xE0) << 3) | b1) + 17
                if dist > len(out):
                    raise JdlzError("back reference before start of output")
                start = len(out) - dist
                if dist >= length:
                    out += out[start:start + length]
                else:
                    # overlapping copy repeats the last dist bytes, byte by byte
                    for i in range(length):
                        out.append(out[start + i])
                f2 >>= 1
            else:
                out.append(data[pos])
                pos += 1
            f1 >>= 1
    except IndexError:
        raise JdlzError("stream ends too early")

    if limit is None and len(out) < out_len:
        raise JdlzError(f"stream gave {len(out)} bytes, header says {out_len}")
    return bytes(out[:target])
