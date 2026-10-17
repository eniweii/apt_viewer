#!/usr/bin/env python3
"""
fng_tpk.py

Finds the textures in Carbon texture packs (TPK) inside .bun files and decodes them in memory.
Port of the reading side of Darius/Nikki (Support.Carbon TPKBlock + Texture, Utils/DDS/Generator,
Utils/Palette). Not tested on a real file yet, only on packs built here from the same layout.

Layout, all little-endian, positions are absolute in the file:
  0xB3300000  TPK block (top level). Inside it, blocks (id u32, size u32, payload):
    0xB3310000 info block, with parts:
        0x33310001  header: version u32, name[0x1C], file name[0x40], ...
        0x33310002  (key u32, u32) per texture
        0x33310003  offset slots: present only in a TPK whose textures are compressed (not read here)
        0x33310004  texture headers, one after the other: 0x58 bytes, name length u8, name
    0xB3320000 data block, with parts 0x33320001 and 0x33320002.
        The texture data base is the start of part 0x33320002 plus 0x80.
  Texture header (0x58 bytes): +0x0C key, +0x14 data offset, +0x18 palette offset (-1 = none),
    +0x1C data size, +0x20 palette size, +0x28 width u16, +0x2A height u16, +0x2E compression u8,
    +0x32 mip count u8. Data = pixels of the biggest mip first (palette is stored apart).
  Compression: 4 / 8 palette, 24, 32 = B,G,R,A bytes, 34 = DXT1, 36 = DXT3, 38 = DXT5, 40 = L8.
  Palette entries are B,G,R,A bytes.
"""
import io
import mmap
import os
import struct

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

BLOCK_TPK = 0xB3300000
BLOCK_INFO = 0xB3310000
BLOCK_DATA = 0xB3320000
PART_HEADER = 0x33310001
PART_KEYS = 0x33310002
PART_OFFSLOTS = 0x33310003
PART_TEXTURES = 0x33310004
PART_PIXELS = 0x33320002
DATA_BASE = 0x80                # from the start of the pixel part

COMPRESSION_NAMES = {
    0: "default", 4: "4bit", 8: "8bit", 16: "16bit", 17: "16bit 1555", 18: "16bit 565", 19: "16bit 3555",
    24: "24bit", 32: "32bit", 33: "DXT", 34: "DXT1", 36: "DXT3", 38: "DXT5", 39: "DXTN", 40: "L8",
    41: "DXT1 AIR", 42: "DXT1 AIG", 43: "DXT1 AIB", 128: "8bit 16", 129: "8bit 64", 130: "8bit IA8",
    131: "4bit IA8", 140: "4bit RGB24 A8", 141: "8bit RGB24 A8", 142: "4bit RGB16 A8", 143: "8bit RGB16 A8",
}
_DXT = {33: b"DXT1", 34: b"DXT1", 36: b"DXT3", 38: b"DXT5", 41: b"DXT1", 42: b"DXT1", 43: b"DXT1"}
_PAL4 = (4, 131, 140, 142)
_PAL8 = (8, 128, 129, 130, 141, 143)


class TpkError(Exception):
    pass


class TpkSkip(TpkError):
    """A pack that is not used here (compressed, no textures, data outside the file): skipped, not a problem."""


class TpkTexture:
    """One texture of a pack. Only the place of its data is kept."""
    __slots__ = ("path", "tpk", "name", "key", "width", "height", "compression", "mips",
                 "pixel_pos", "pixel_size", "palette_pos", "palette_size")

    @property
    def format_name(self):
        return COMPRESSION_NAMES.get(self.compression, f"type {self.compression}")


class TextureIndex:
    def __init__(self):
        self.textures = []
        self.by_name = {}           # upper case name -> TpkTexture (first one found)
        self.by_key = {}            # name hash -> TpkTexture
        self.packs = 0
        self.problems = []              # unexpected errors
        self.skipped = []               # packs (or textures) that can not be used here, with the reason

    def add(self, tex):
        self.textures.append(tex)
        self.by_name.setdefault(tex.name.upper(), tex)
        self.by_key.setdefault(tex.key, tex)

    def find(self, resource_name, bin_hash):
        """Texture for a resource name like 'BasePoly.tga', by name first, then by hash."""
        stem = os.path.splitext(os.path.basename(resource_name.replace("\\", "/")))[0]
        tex = self.by_name.get(stem.upper())
        if tex is not None:
            return tex
        for text in (stem.upper(), stem, resource_name):
            tex = self.by_key.get(bin_hash(text))
            if tex is not None:
                return tex
        return None


def _u32(mm, pos):
    return struct.unpack_from("<I", mm, pos)[0]


def _blocks(mm, start, end):
    """(id, payload start, payload end) of the blocks in a range. Stops at one that does not fit."""
    pos = start
    while pos + 8 <= end:
        cid, size = struct.unpack_from("<II", mm, pos)
        if pos + 8 + size > end:
            return
        yield cid, pos, pos + 8, pos + 8 + size
        pos += 8 + size


def read_tpk(mm, path, start, end, index):
    """Adds the textures of one TPK block (payload range start..end) to the index."""
    info = None
    pixel_part = None
    for cid, bstart, pstart, pend in _blocks(mm, start, end):
        if cid == BLOCK_INFO:
            info = (pstart, pend)
        elif cid == BLOCK_DATA:
            for pid, pbstart, _ps, _pe in _blocks(mm, pstart, pend):
                if pid == PART_PIXELS:
                    pixel_part = pbstart
    if info is None:
        raise TpkSkip("no info block")

    tpk_name = ""
    first = None
    count = None
    compressed = False
    for pid, _bs, ps, pe in _blocks(mm, *info):
        if pid == PART_HEADER and pe - ps >= 0x24:
            tpk_name = bytes(mm[ps + 4:ps + 4 + 0x1C]).split(b"\0")[0].decode("latin-1")
        elif pid == PART_KEYS:
            count = (pe - ps) // 8
        elif pid == PART_OFFSLOTS:
            compressed = True
        elif pid == PART_TEXTURES:
            first = (ps, pe)
    if compressed:
        raise TpkSkip(f"{tpk_name or 'TPK'}: compressed texture pack, not supported")
    if first is None or pixel_part is None:
        raise TpkSkip(f"{tpk_name or 'TPK'}: has no texture headers or no pixel data")

    base = pixel_part + DATA_BASE
    found = []
    outside = 0
    pos, tend = first
    while pos + 0x59 <= tend and (count is None or len(found) < count):
        tex = TpkTexture()
        tex.path = path
        tex.tpk = tpk_name
        tex.key = _u32(mm, pos + 0x0C)
        pixel_off, pal_off, tex.pixel_size, tex.palette_size = struct.unpack_from("<iiii", mm, pos + 0x14)
        tex.width, tex.height = struct.unpack_from("<HH", mm, pos + 0x28)
        tex.compression = mm[pos + 0x2E]
        tex.mips = mm[pos + 0x32]
        name_len = mm[pos + 0x58]
        tex.name = bytes(mm[pos + 0x59:pos + 0x59 + name_len]).split(b"\0")[0].decode("latin-1")
        tex.pixel_pos = base + pixel_off
        tex.palette_pos = base + pal_off if pal_off >= 0 and tex.palette_size else -1
        pos += 0x59 + name_len
        found.append(tex)
        if tex.pixel_pos + tex.pixel_size > len(mm) or tex.pixel_pos < 0:
            outside += 1                                # keep the other textures of the pack
            continue
        index.add(tex)
    if outside:
        index.skipped.append(f"{os.path.basename(path)}: {tpk_name}: {outside} texture(s) with data outside the file")


def scan_file(path, index):
    """Reads every top-level TPK block of a file into the index."""
    if os.path.getsize(path) < 8:
        return
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            pos = 0
            end = len(mm)
            while pos + 8 <= end:
                cid, size = struct.unpack_from("<II", mm, pos)
                if pos + 8 + size > end:
                    break
                if cid == BLOCK_TPK:
                    try:
                        before = len(index.textures)
                        read_tpk(mm, path, pos + 8, pos + 8 + size, index)
                        index.packs += 1
                    except TpkSkip as e:
                        index.skipped.append(f"{os.path.basename(path)} @0x{pos:X}: {e}")
                    except (TpkError, struct.error, IndexError) as e:
                        index.problems.append(f"{os.path.basename(path)} @0x{pos:X}: {e}")
                pos += 8 + size
        finally:
            mm.close()


def scan_files(paths, progress=None):
    index = TextureIndex()
    for i, path in enumerate(paths):
        if progress:
            progress(i, len(paths), path)
        try:
            scan_file(path, index)
        except OSError:
            continue
    return index


def _dds(tex, pixels):
    """Wraps DXT data in a DDS header so Pillow can decode it."""
    linear = max(1, (tex.width + 3) // 4) * max(1, (tex.height + 3) // 4) * (8 if _DXT[tex.compression] == b"DXT1" else 16)
    header = b"DDS " + struct.pack("<7I", 124, 0x1007 | 0x80000, tex.height, tex.width, linear, 1, 1)
    header += b"\0" * 44 + struct.pack("<II4s5I", 32, 4, _DXT[tex.compression], 0, 0, 0, 0, 0)
    header += struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return header + pixels


def decode(tex):
    """Returns the base mip of a texture as an RGBA Pillow image. Raises TpkError if it can not."""
    if not HAVE_PIL:
        raise TpkError("Pillow is not installed (pip install pillow)")
    w, h, comp = tex.width, tex.height, tex.compression
    if w <= 0 or h <= 0:
        raise TpkError("texture has no size")
    with open(tex.path, "rb") as f:
        f.seek(tex.pixel_pos)
        pixels = f.read(tex.pixel_size)
        palette = b""
        if tex.palette_pos >= 0:
            f.seek(tex.palette_pos)
            palette = f.read(tex.palette_size)

    try:
        if comp in _DXT:
            return Image.open(io.BytesIO(_dds(tex, pixels))).convert("RGBA")
        if comp == 32:
            return Image.frombytes("RGBA", (w, h), pixels[:w * h * 4], "raw", "BGRA")
        if comp == 24:
            return Image.frombytes("RGB", (w, h), pixels[:w * h * 3], "raw", "BGR").convert("RGBA")
        if comp == 40:
            return Image.frombytes("L", (w, h), pixels[:w * h]).convert("RGBA")
        if comp in _PAL8 or comp in _PAL4:
            if comp in _PAL4:
                index = bytearray(w * h)
                packed = pixels[:(w * h + 1) // 2]
                index[0::2] = bytes(b >> 4 for b in packed)[:len(index[0::2])]
                index[1::2] = bytes(b & 15 for b in packed)[:len(index[1::2])]
                count = 16
            else:
                index = pixels[:w * h]
                count = 256
            if len(palette) < count * 4:
                raise TpkError("palette is too short")
            idx = Image.frombytes("L", (w, h), bytes(index))
            lut = [[palette[i * 4 + c] if i < count else 0 for i in range(256)] for c in range(4)]   # B, G, R, A
            return Image.merge("RGBA", (idx.point(lut[2]), idx.point(lut[1]), idx.point(lut[0]), idx.point(lut[3])))
    except (ValueError, OSError) as e:
        raise TpkError(f"{tex.name}: {e}")
    raise TpkError(f"{tex.name}: texture format {tex.format_name} is not supported yet")
