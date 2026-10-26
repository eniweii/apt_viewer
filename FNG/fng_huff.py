#!/usr/bin/env python3
"""
fng_huff.py

HUFF decoder for the Carbon FNG blocks. Read from the disassembly of LZCompressLib.dll
(the Darius/Binarius library, HUFF routine at 0x10002BD0) and checked against that DLL.

Stream header, 16 bytes little-endian: 'HUFF', version u8, header size u8, u16, unpacked size u32,
packed size u32. The body after it is a bit stream, most significant bit first:

  flags     16 bits. 0x8000 = sizes are 32 bit (else 24 bit), 0x0100 = a packed size field comes first.
            The rest (flags & ~0x100) is the mode: 0x30FB plain, 0x32FB byte delta, 0x34FB delta of delta
            (add 0x8000 for the 32 bit forms)
  [packed size]  24 or 32 bits, skipped
  size      24 or 32 bits, unpacked size
  escape    8 bits, the byte value that starts an escape
  table     canonical Huffman table. For each code length 1, 2, 3...: the number of codes of that
            length (gamma code, see gamma). It ends when the code space is full.
            Then, for each code in order, the symbol as a gamma code of
            "how many unused symbols to skip after the previous symbol".
  data      symbols. A plain symbol is one output byte. The escape symbol is followed by gamma n:
              n > 0   repeat the previous output byte n times
              n == 0  next bit 0: a literal byte follows (8 bits). Next bit 1: end of stream.
  Last step: mode 0x32FB / 0x34FB runs a running sum over the output once / twice.

gamma: z zero bits, a 1 bit, then z+2 bits b. Value = b + 2^(z+2) - 4. (z >= 16 reads z+3 bits.)
"""
import struct

HEADER_SIZE = 16
MAGIC = b"HUFF"


class HuffError(Exception):
    pass


class _Bits:
    """Most significant bit first. Reading past the end gives zero bits, like the original."""

    def __init__(self, data):
        self.data = data
        self.pos = 0                # bit position

    def peek(self, n):
        """Next n bits (n <= 32) without consuming them."""
        byte = self.pos >> 3
        chunk = int.from_bytes(self.data[byte:byte + 6].ljust(6, b"\0"), "big")
        shift = 48 - (self.pos & 7) - n
        return (chunk >> shift) & ((1 << n) - 1)

    def read(self, n):
        if n == 0:
            return 0
        value = self.peek(n)
        self.pos += n
        return value

    def gamma(self):
        z = 0
        while self.read(1) == 0:
            z += 1
            if z > 32 or (self.pos >> 3) > len(self.data) + 8:
                raise HuffError("bad gamma code")
        bits = z + 2 if z < 16 else z + 3
        return self.read(bits) + (1 << bits) - 4


def unpacked_size(data):
    """Unpacked size from the 16 byte header, or None if data is not a HUFF stream."""
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        return None
    return struct.unpack_from("<I", data, 8)[0]


def decompress(data, limit=None):
    """Decodes a complete HUFF stream (header + body). limit stops after that many output bytes."""
    if unpacked_size(data) is None:
        raise HuffError("not a HUFF stream")

    br = _Bits(data[HEADER_SIZE:])

    flags = br.read(16)
    wide = 32 if flags & 0x8000 else 24
    if flags & 0x100:
        br.read(wide)
    size = br.read(wide)
    mode = flags & ~0x100
    escape = br.read(8)

    # code length table
    first = [0] * 40            # first code of each length minus codes of shorter lengths
    count = [0] * 40
    limit_ = [0] * 40           # code end of each length, left aligned to 16 bits
    code = 0
    total = 0
    length = 1
    while True:
        code <<= 1
        first[length] = code - total
        n = br.gamma()
        count[length] = n
        code += n
        total += n
        limit_[length] = ((code << (16 - length)) & 0xFFFF) if n and length <= 16 else 0
        if n != 0 and limit_[length] == 0:
            break
        length += 1
        if length >= 32:
            raise HuffError("code length table does not close")
    max_len = length
    limit_[max_len] = 0xFFFFFFFF

    # symbols in code order
    used = [False] * 256
    symbols = []
    prev = 255
    for _ in range(total):
        skip = br.gamma() + 1
        while skip:
            prev = (prev + 1) & 0xFF
            if not used[prev]:
                skip -= 1
        used[prev] = True
        symbols.append(prev)

    # data
    out = bytearray()
    want = size if limit is None else min(size, limit)
    while len(out) < want:
        top = br.peek(16)
        length = 1
        while not (count[length] and top < limit_[length]):
            length += 1
            if length > max_len:
                raise HuffError("bad code")
        value = br.read(length)
        index = value - first[length]
        if not 0 <= index < len(symbols):
            raise HuffError("bad code")
        sym = symbols[index]

        if sym != escape:
            out.append(sym)
            continue

        n = br.gamma()
        if n:
            if not out:
                raise HuffError("run without a previous byte")
            out += bytes([out[-1]]) * n
        elif br.read(1) == 0:
            out.append(br.read(8))
        else:
            break
        if (br.pos >> 3) > len(br.data) + 8:
            raise HuffError("stream ends too early")

    if len(out) < want or (limit is None and len(out) != size):
        raise HuffError(f"stream gave {len(out)} bytes, header says {size}")

    if mode in (0x32FB, 0xB2FB):
        acc = 0
        for i, b in enumerate(out):
            acc = (acc + b) & 0xFF
            out[i] = acc
    elif mode in (0x34FB, 0xB4FB):
        acc = acc2 = 0
        for i, b in enumerate(out):
            acc = (acc + b) & 0xFF
            acc2 = (acc2 + acc) & 0xFF
            out[i] = acc2
    return bytes(out[:want])
