#!/usr/bin/env python3
"""
fng_bun.py

Finds the FNG blocks in Carbon .bun files without extracting anything to disk.

Layout (from Darius/Nikki, Support.Carbon FNGroup + DatabaseLoader + SAT.Decompress):
  Block: id u32, size u32, payload. Both ids below are FNG blocks:
  0x00030203  FEngFiles. Payload is one of:
      - the package itself (starts with 'FEn\\xE7')
      - a nested 0x00030210 block (id u32, size u32, then the payload of that block)
  0x00030210  FNGCompress. Payload is 4 bytes that are skipped, then a compressed stream (JDLZ or HUFF)
  The unpacked package has the collection name (zero ended) at +0x28.
Darius reads only top-level blocks. This scanner also goes into container blocks (id bit 31)
and reports files where the walk stopped before the end of the file.
A loose .fng is the same thing: a block, or the bare package.
"""
import mmap
import os
import re
import struct

import fng_huff
import fng_jdlz

BLOCK_FNG = 0x00030203
BLOCK_FNG_PACKED = 0x00030210
FNG_MAGIC = b"FEn\xe7"
NAME_OFFSET = 0x28
HEAD_BYTES = 0x100          # enough for the name, so the list does not unpack whole files
SCAN_EXTENSIONS = (".bun", ".fng")


class FngError(Exception):
    pass


class FngEntry:
    """One FNG found in a file. Only the place is kept, the data is read again when needed."""
    __slots__ = ("path", "block", "offset", "size", "packed", "codec", "unpacked", "name", "error")

    def __init__(self, path, block, offset, size):
        self.path = path
        self.block = block          # block id the payload came from
        self.offset = offset        # offset of the payload in the file
        self.size = size            # payload size in the file
        self.packed = False
        self.codec = ""             # tag of the compressed stream: JDLZ, HUFF
        self.unpacked = size
        self.name = ""
        self.error = ""


def unpack_fng(payload, limit=None, block=BLOCK_FNG):
    """Returns the package bytes (they start with 'FEn\\xE7'). limit reads only the head."""
    if payload[:4] == FNG_MAGIC:
        return payload

    if block == BLOCK_FNG_PACKED:
        body = payload[4:]
    elif len(payload) >= 12 and struct.unpack_from("<I", payload, 0)[0] == BLOCK_FNG_PACKED:
        body = payload[12:]
    else:
        raise FngError(f"unknown payload start {bytes(payload[:4]).hex()}")

    if body[:4] == fng_jdlz.MAGIC:
        codec = fng_jdlz
    elif body[:4] == fng_huff.MAGIC:
        codec = fng_huff
    else:
        raise FngError(f"unsupported compression {bytes(body[:4])!r}")
    try:
        data = codec.decompress(body, limit)
    except (fng_jdlz.JdlzError, fng_huff.HuffError) as e:
        raise FngError(str(e))
    if data[:4] != FNG_MAGIC:
        raise FngError("unpacked data is not an FEng package")
    return data


def read_name(data):
    if len(data) <= NAME_OFFSET:
        raise FngError("package too short for a name")
    end = data.find(b"\x00", NAME_OFFSET)
    if end < 0:
        end = len(data)
    return data[NAME_OFFSET:end].decode("latin-1")


def _describe(mm, entry):
    """Fills name, packed and unpacked of an entry from its payload."""
    payload = mm[entry.offset:entry.offset + entry.size]
    entry.packed = payload[:4] != FNG_MAGIC
    if entry.packed:
        skip = 4 if entry.block == BLOCK_FNG_PACKED else 12
        entry.codec = bytes(payload[skip:skip + 4]).decode("latin-1")
        size = fng_jdlz.unpacked_size(payload[skip:]) or fng_huff.unpacked_size(payload[skip:])
        if size is not None:
            entry.unpacked = size
    try:
        entry.name = read_name(unpack_fng(payload, HEAD_BYTES, entry.block))
    except FngError as e:
        entry.error = str(e)


def _walk(mm, start, end, path, entries, warnings):
    """Walks the blocks of one level, goes into containers, collects the FNG blocks."""
    pos = start
    while pos + 8 <= end:
        cid, size = struct.unpack_from("<II", mm, pos)
        pstart = pos + 8
        pend = pstart + size
        if pend > end:
            warnings.append(f"{os.path.basename(path)}: walk stopped at 0x{pos:X} of 0x{end:X}"
                            f" (block 0x{cid:08X} is longer than the data left)")
            return
        if cid in (BLOCK_FNG, BLOCK_FNG_PACKED):
            entry = FngEntry(path, cid, pstart, size)
            _describe(mm, entry)
            entries.append(entry)
        elif cid & 0x80000000:
            _walk(mm, pstart, pend, path, entries, warnings)
        pos = pend
    if pos != end and end - pos >= 8:
        warnings.append(f"{os.path.basename(path)}: walk ended at 0x{pos:X} of 0x{end:X}")


def scan_file(path, warnings=None):
    """Returns the FngEntry list of one file. Problems of the walk are added to warnings."""
    entries = []
    warnings = [] if warnings is None else warnings
    if os.path.getsize(path) < 8:
        return entries

    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            if mm[:4] == FNG_MAGIC:         # bare package
                entry = FngEntry(path, BLOCK_FNG, 0, len(mm))
                _describe(mm, entry)
                return [entry]
            _walk(mm, 0, len(mm), path, entries, warnings)
        finally:
            mm.close()
    return entries


FONT_BLOCKS = {0x00030201: "FEngFont", 0x00039001: "LangFont"}
MAX_HOPS = 200000


class BlockRef:
    """A block found by id: where it is, nothing else."""
    __slots__ = ("path", "id", "offset", "size")

    def __init__(self, path, id_, offset, size):
        self.path = path
        self.id = id_
        self.offset = offset        # offset of the payload in the file
        self.size = size


def scan_blocks(path, ids):
    """BlockRef list of every block of the given ids in a file, also inside containers."""
    found = []
    if os.path.getsize(path) < 8:
        return found

    hops = [0]

    def walk(mm, pos, end):
        while pos + 8 <= end:
            hops[0] += 1
            if hops[0] > MAX_HOPS:
                return                  # not a block file, stop instead of walking noise
            cid, size = struct.unpack_from("<II", mm, pos)
            if pos + 8 + size > end:
                return
            if cid in ids:
                found.append(BlockRef(path, cid, pos + 8, size))
            elif cid & 0x80000000:
                walk(mm, pos + 8, pos + 8 + size)
            pos += 8 + size

    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            walk(mm, 0, len(mm))
        finally:
            mm.close()
    return found


def read_block(ref, limit=None):
    """Payload bytes of a BlockRef."""
    with open(ref.path, "rb") as f:
        f.seek(ref.offset)
        return f.read(ref.size if limit is None else min(ref.size, limit))


SMALL_BIN = 4 * 1024 * 1024        # .bin files bigger than this are not read (language files are small)


def block_path(mm, offset):
    """Blocks that contain a file offset, from the outside in: [(block id, payload offset, payload size), ...]."""
    path = []
    pos, end, hops = 0, len(mm), 0
    while pos + 8 <= end and hops < MAX_HOPS:
        hops += 1
        cid, size = struct.unpack_from("<II", mm, pos)
        if pos + 8 + size > end or pos > offset:
            break
        if pos + 8 <= offset < pos + 8 + size:
            path.append((cid, pos + 8, size))
            if cid & 0x80000000:
                pos, end = pos + 8, pos + 8 + size
                continue
            break
        pos += 8 + size
    return path


def find_bytes(path, needle, ignore_case=True, limit=100):
    """
    Hits of a text in a file (as plain bytes). Returns [(offset, block_path), ...], at most limit.
    Used to find where a name is stored when a reader does not find it.
    """
    hits = []
    if os.path.getsize(path) < len(needle):
        return hits
    pattern = re.compile(re.escape(needle.encode("latin-1", "replace")), re.I if ignore_case else 0)
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for m in pattern.finditer(mm):
                hits.append((m.start(), block_path(mm, m.start())))
                if len(hits) >= limit:
                    break
        finally:
            mm.close()
    return hits


def find_files(folder, recursive=False, exts=SCAN_EXTENSIONS):
    found = []
    if recursive:
        for root, _dirs, names in os.walk(folder):
            found += [os.path.join(root, n) for n in names]
    else:
        found = [os.path.join(folder, n) for n in os.listdir(folder)]
    found = [p for p in found if p.lower().endswith(exts) and os.path.isfile(p)]
    return sorted(found, key=str.lower)


def scan_folder(folder, recursive=False, progress=None):
    """
    Returns (entries, warnings).
    progress(file_index, file_count, path) is called before each file.
    """
    files = find_files(folder, recursive)
    entries = []
    warnings = []
    for i, path in enumerate(files):
        if progress:
            progress(i, len(files), path)
        try:
            entries += scan_file(path, warnings)
        except OSError:
            continue
    return entries, warnings


def load_package(entry):
    """Full package bytes of an entry (reads the file again)."""
    with open(entry.path, "rb") as f:
        f.seek(entry.offset)
        payload = f.read(entry.size)
    return unpack_fng(payload, None, entry.block)
