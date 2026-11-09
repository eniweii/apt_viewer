#!/usr/bin/env python3
"""
fng_tpk.py

Finds the textures in texture packs (TPK) inside .bun files and decodes them in memory.
Port of the reading side of Darius/Nikki (Support.Carbon and Support.Undercover TPKBlock + Texture,
Utils/DDS/Generator, Utils/Palette) and of NI240SX's UCGT (geometryFile/textures: Undercover header layout and
Xbox 360 textures; the tiling address is RareView's, via UCGT). Not tested on a real file yet, only on packs built
here from the same layouts.

Reader profile by TPK version (first u32 of the info part): 8 = ProStreet / Carbon, 9 = Undercover (PC and
Xbox 360, detected per texture). Any other version is read as the Carbon one.

Layout, all little-endian, positions are absolute in the file:
  0xB3300000  TPK block (top level). Inside it, blocks (id u32, size u32, payload):
    0xB3310000 info block, with parts:
        0x33310001  header: version u32, name[0x1C], file name[0x40], ...
        0x33310002  (key u32, u32) per texture
        0x33310003  offset slots (0x18 bytes each), present only in a TPK whose textures are compressed
        0x33310004  texture headers, one after the other: 0x58 bytes, name length u8, name
    0xB3320000 data block, with parts 0x33320001 and 0x33320002.
        The texture data base is the start of part 0x33320002 plus 0x80.
  Texture header (0x58 bytes): +0x0C key, +0x14 data offset, +0x18 palette offset (-1 = none),
    +0x1C data size, +0x20 palette size, +0x28 width u16, +0x2A height u16, +0x2E compression u8,
    +0x32 mip count u8. Data = pixels of the biggest mip first (palette is stored apart).
  Compression: 4 / 8 palette, 17 = ARGB1555, 18 = RGB565, 24, 32 = B,G,R,A bytes, 34 = DXT1, 36 = DXT3,
    38 = DXT5, 39 = DXTN (previewed as DXT5), 40 = L8.
  Palette entries are B,G,R,A bytes.

Compressed pack (part 0x33310003 present). Slot: key u32, offset i32 (from the start of the TPK block),
  encoded size, decoded size, user flags u8, flag u8, ref count i16, u32. Each slot holds one texture, its
  0x94 byte header is at the END of the decoded data and the pixels (and palette) come before it:
    flag 0  raw bytes.   flag 1  one wrapped block (RAWW / JDLZ / HUFF / RFPK).
    flag 2  compressed in place: chunks 0x55441122, size, packed size, position, packed position, 0, then a
            wrapped block. Decoded chunks are joined in order of their position.
  COMP (native codec in Darius) is not available here and is reported as skipped.

Undercover (version 9) differs in three places:
  - slots (all flags): the texture header is at the START of the decoded slot, 0x100 bytes (part 1 as above, part 2
    at +124), and the pixels follow it: at +0x100 on PC, at +0x1000 on Xbox 360.
  - part 0x33310005: format entries, one per texture. PC: 0, 0, 0, format u32, 0, 0 (24 bytes). Anything else is
    an Xbox 360 entry (32 bytes). A PC format of 0 is read as Xbox 360, like UCGT does.
  - pixel part of a pack without slots: the images start after its 0x11111111 filler words.
Xbox 360 pixels (UC only): DXT data is swapped per 16 bit word, 32 bit data per 32 bit word, then untiled in tiles
of 32 x 32 blocks (8 bytes per DXT1 block, 16 per DXT3 / DXT5 / DXTN block, 4 per 32 bit pixel). Only the base mip
is read. DXTN is BC5 there (and on PC when the format says BC5U / ATI2): a two channel normal map.
"""
import array
import importlib
import importlib.util
import io
import mmap
import os
import re
import struct
import sys

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
PART_FORMATS = 0x33310005         # Undercover: 24 bytes per texture (32 on Xenon), format and platform
PART_PIXELS = 0x33320002
DATA_BASE = 0x80                # from the start of the pixel part
SLOT_SIZE = 0x18
COMP_HEADER = 0x94              # texture header at the end of a decoded slot
MAGIC_LZ = 0x55441122
MAX_DEPTH = 4                   # nested container chunks searched for TPKs
MAX_DIM = 16384

COMPRESSION_NAMES = {
    0: "default", 4: "4bit", 8: "8bit", 16: "16bit", 17: "16bit 1555", 18: "16bit 565", 19: "16bit 3555",
    24: "24bit", 32: "32bit", 33: "DXT", 34: "DXT1", 36: "DXT3", 38: "DXT5", 39: "DXTN", 40: "L8",
    41: "DXT1 AIR", 42: "DXT1 AIG", 43: "DXT1 AIB", 128: "8bit 16", 129: "8bit 64", 130: "8bit IA8",
    131: "4bit IA8", 140: "4bit RGB24 A8", 141: "8bit RGB24 A8", 142: "4bit RGB16 A8", 143: "8bit RGB16 A8",
}
_DXT = {33: b"DXT1", 34: b"DXT1", 36: b"DXT3", 38: b"DXT5", 39: b"DXT5", 41: b"DXT1", 42: b"DXT1", 43: b"DXT1"}
_NOTES = {39: "DXTN is previewed as DXT5, not bit exact"}
FOURCC_DXT = {0x31545844: b"DXT1", 0x33545844: b"DXT3", 0x35545844: b"DXT5"}
FOURCC_BC5 = (0x55354342, 0x32495441)          # BC5U, ATI2
_PAL4 = (4, 131, 140, 142)
_PAL8 = (8, 128, 129, 130, 141, 143)


class TpkError(Exception):
    pass


class TpkSkip(TpkError):
    """A pack that is not used here (compressed, no textures, data outside the file): skipped, not a problem."""


class Profile:
    """Reader profile, chosen by the version in the info part (first u32). Like UCGT: 8 = ProStreet / Carbon,
    9 = Undercover. Other versions are read as the Carbon one."""
    def __init__(self, name, header_at_start, image_start, xenon_image_start):
        self.name = name
        self.header_at_start = header_at_start          # texture header at the start of a slot, else at the end
        self.image_start = image_start                  # pixels after a header at the start (PC)
        self.xenon_image_start = xenon_image_start      # the same on Xbox 360 (a big gap after the header)


CARBON = Profile("carbon", False, 0, 0)
UNDERCOVER = Profile("undercover", True, 0x100, 0x1000)


def profile_for(version):
    return UNDERCOVER if version == 9 else CARBON


class TpkTexture:
    """One texture of a pack. Only the place of its data is kept, except for a compressed slot: its decoded
    pixels and palette are in data / palette (pixel_pos is then the slot position, unique per texture)."""
    __slots__ = ("path", "tpk", "name", "key", "width", "height", "compression", "mips",
                 "pixel_pos", "pixel_size", "palette_pos", "palette_size", "data", "palette",
                 "version", "xenon", "fourcc")

    def __init__(self):
        self.data = None
        self.palette = None
        self.version = 0                # TPK version of the pack (8 Carbon family, 9 Undercover)
        self.xenon = False              # Xbox 360 data: big-endian and tiled
        self.fourcc = 0                 # format code of the header part 2 (Undercover), 0 = not known

    @property
    def format_name(self):
        return COMPRESSION_NAMES.get(self.compression, f"type {self.compression}")


class TextureIndex:
    def __init__(self):
        self.textures = []
        self.by_name = {}           # upper case name -> TpkTexture (first one found)
        self.by_key = {}            # name hash -> TpkTexture (first one found)
        self.all_by_name = {}       # upper case name -> every TpkTexture with it, in scan order
        self.all_by_key = {}
        self.packs = 0
        self.problems = []              # unexpected errors
        self.skipped = []               # packs (or textures) that can not be used here, with the reason
        self.pack_kinds = {}            # (TPK version, "slots" | "plain") -> number of packs read
        self.reasons = {}               # (file, reason with numbers masked) -> [count, first full line]

    def note(self, file, reason, line):
        """Counts a skipped or failed texture or pack by file and reason (numbers and the texture name masked),
        keeping the first full line as an example, for report()."""
        key = (file, re.sub(r"0x[0-9A-Fa-f]+|\d+", "#", re.sub(r"^\S+: ", "", reason)))
        entry = self.reasons.setdefault(key, [0, line])
        entry[0] += 1

    def report(self):
        """Text for a person: what was read, in which pack versions, and why the rest was not used."""
        kinds = ", ".join(f"v{v} {k}: {n}" for (v, k), n in sorted(self.pack_kinds.items())) or "none"
        lines = [f"{len(self.textures)} textures in {self.packs} packs read (packs by version and kind: {kinds})"]
        if not self.reasons:
            lines.append("Nothing was skipped.")
        for (where, reason), (n, example) in sorted(self.reasons.items(), key=lambda kv: (-kv[1][0], kv[0])):
            lines.append(f"{n:6d} x {where}: {reason}")
            lines.append(f"           e.g. {example}")
        return "\n".join(lines)

    def add(self, tex):
        self.textures.append(tex)
        self.by_name.setdefault(tex.name.upper(), tex)
        self.by_key.setdefault(tex.key, tex)
        self.all_by_name.setdefault(tex.name.upper(), []).append(tex)
        self.all_by_key.setdefault(tex.key, []).append(tex)

    def candidates(self, resource_name, bin_hash):
        """Every texture for a resource name like 'BasePoly.tga', in scan order: those with that name, else by hash."""
        stem = os.path.splitext(os.path.basename(resource_name.replace("\\", "/")))[0]
        found = self.all_by_name.get(stem.upper())
        if found:
            return list(found)
        for text in (stem.upper(), stem, resource_name):
            found = self.all_by_key.get(bin_hash(text))
            if found:
                return list(found)
        return []

    def find(self, resource_name, bin_hash, prefer=None):
        """Texture for a resource name, by name first, then by hash. prefer: file or pack names (parts of them,
        any case), most wanted first; the first candidate that matches the earliest one wins, else the first found.
        A name starting with "=" must equal the file name without extension or the pack name."""
        found = self.candidates(resource_name, bin_hash)
        if not found:
            return None
        for token in prefer or ():
            token = token.lower()
            for tex in found:
                base = os.path.basename(tex.path).lower()
                pack = (tex.tpk or "").lower()
                if token.startswith("="):
                    if token[1:] in (os.path.splitext(base)[0], pack):
                        return tex
                elif token in base or token in pack:
                    return tex
        return found[0]


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


_modules = {}


def _sibling_module(name, *dirs):
    """Imports a module by name, or from a sibling folder of the repo (Apt/, FNG/, common/). None if not found."""
    if name in _modules:
        return _modules[name]
    mod = None
    try:
        mod = importlib.import_module(name)
    except ImportError:
        here = os.path.dirname(os.path.abspath(__file__))
        for d in dirs:
            file = os.path.join(here, d, name + ".py")
            if not os.path.isfile(file):
                continue
            added = os.path.normpath(os.path.join(here, "..", "common"))
            sys.path.insert(0, added)
            try:
                spec = importlib.util.spec_from_file_location(name, file)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[name] = mod
                spec.loader.exec_module(mod)
                break
            except Exception:
                sys.modules.pop(name, None)
                mod = None
            finally:
                sys.path.remove(added)
    _modules[name] = mod
    return mod


def unwrap(data):
    """Decodes one compressed block (RAWW, JDLZ, HUFF, RFPK). Data without a known header is returned as it is,
    like Darius does. COMP needs a native codec and raises TpkSkip."""
    if len(data) < 0x10:
        raise TpkError("compressed block is shorter than its header")
    magic = bytes(data[:4])
    try:
        if magic == b"RAWW":
            size = struct.unpack_from("<I", data, 8)[0]
            if size > len(data) - 0x10:
                raise TpkError("RAWW: block is shorter than its size")
            return bytes(data[0x10:0x10 + size])
        if magic == b"JDLZ":
            mod = _sibling_module("fng_jdlz", "", "common", "../common")
            if mod is None:
                raise TpkSkip("JDLZ: fng_jdlz.py not found")
            return mod.decompress(bytes(data))
        if magic == b"HUFF":
            mod = _sibling_module("fng_huff", "", "FNG", "../FNG")
            if mod is None:
                raise TpkSkip("HUFF: fng_huff.py not found")
            return mod.decompress(bytes(data))
        if magic == b"RFPK":
            mod = _sibling_module("apt_scan", "", "Apt", "../Apt")
            if mod is None:
                raise TpkSkip("RFPK: apt_scan.py not found or not importable")
            return mod.rfpk_decompress(bytes(data))[0]
    except TpkError:
        raise
    except Exception as e:
        raise TpkError(f"{magic.decode('latin-1')} block: {e}")
    if magic == b"COMP":
        raise TpkSkip("COMP compression (native codec) is not supported")
    if magic == b"BEST":
        raise TpkSkip("BEST is not a stored compression type")
    return bytes(data)


def _slot_chunks(mm, pos, size):
    """Decoded chunks (position, bytes) of a compressed-in-place slot (flag 2), in order of position."""
    stop = pos + size
    chunks = []
    p = pos
    while p + 0x28 <= stop:
        if _u32(mm, p) != MAGIC_LZ:
            p += 4
            continue
        dsize, esize, dpos, _epos = struct.unpack_from("<4i", mm, p + 4)
        if esize < 0x18:
            raise TpkError("chunk with a bad size")
        version = mm[p + 0x1C]
        block = _u32(mm, p + 0x24) + (0 if version == 2 else 0x10)
        a = p + 0x18
        if block < 0x10 or a + block > stop:
            raise TpkError("chunk data lies outside the slot")
        data = unwrap(bytes(mm[a:a + block]))
        if dsize > 0 and len(data) != dsize:
            raise TpkError(f"chunk decoded to {len(data)} bytes, header says {dsize}")
        chunks.append((dpos, data))
        p += esize
    if not chunks:
        raise TpkError("slot has no compressed chunks")
    chunks.sort(key=lambda c: c[0])
    return b"".join(c[1] for c in chunks)


def _part2(buf, pos):
    """Header part 2 (UCGT readHeaderPart2): (xenon, fourcc, position after it). A PC entry is 0, 0, 0, format,
    0, 0 (24 bytes); anything else is an Xbox 360 entry (32 bytes). The format is only known for PC."""
    if pos + 24 > len(buf):
        raise TpkError("format part is outside the data")
    a, b, c, fmt, d, e = struct.unpack_from("<6I", buf, pos)
    if a == 0 and b == 0 and c == 0 and fmt != 0 and d == 0 and e == 0:
        return False, fmt, pos + 24
    return True, 0, pos + 32


def _header_fields(buf, h, tex):
    """Texture header part 1 at h, the same layout in every version: the Carbon 0x58 byte header and the name."""
    if h + 0x59 > len(buf):
        raise TpkError("texture header is outside the data")
    name_len = buf[h + 0x58]
    if h + 0x59 + name_len > len(buf):
        raise TpkError("texture name does not fit the header")
    tex.key = struct.unpack_from("<I", buf, h + 0x0C)[0]
    pixel_off, pal_off, tex.pixel_size, tex.palette_size = struct.unpack_from("<iiii", buf, h + 0x14)
    tex.width, tex.height = struct.unpack_from("<HH", buf, h + 0x28)
    tex.compression = buf[h + 0x2E]
    tex.mips = buf[h + 0x32]
    tex.name = bytes(buf[h + 0x59:h + 0x59 + name_len]).split(b"\0")[0].decode("latin-1")
    if not (0 < tex.width <= MAX_DIM and 0 < tex.height <= MAX_DIM):
        raise TpkError(f"{tex.name}: bad size {tex.width}x{tex.height}")
    if tex.pixel_size <= 0 or tex.palette_size < 0 or tex.palette_size > 0x10000:
        raise TpkError(f"{tex.name}: bad data size {tex.pixel_size} / palette size {tex.palette_size}")
    return pixel_off, pal_off


def _slot_texture(mm, path, tpk_name, block_pos, end, slot_pos, slot, version=8):
    key, off, enc, dec, _uflags, flag, _refs, _unk = slot
    profile = profile_for(version)
    pos = block_pos + off
    if off < 0 or enc <= 0 or pos + enc > end:
        raise TpkError("slot lies outside the pack")
    if flag in (0, 1):
        buf = bytes(mm[pos:pos + enc])
        if flag == 1:
            buf = unwrap(buf)
            if dec > 0 and len(buf) != dec:
                raise TpkError(f"decoded to {len(buf)} bytes, slot says {dec}")
    elif flag == 2:
        buf = _slot_chunks(mm, pos, enc)
        if dec > 0 and len(buf) != dec:
            raise TpkError(f"chunks give {len(buf)} bytes, slot says {dec}")
    else:
        raise TpkSkip(f"slot flag {flag} is not supported")

    tex = TpkTexture()
    tex.path = path
    tex.tpk = tpk_name
    tex.version = version
    tex.pixel_pos = pos
    tex.palette_pos = -1
    if profile.header_at_start:
        # Undercover: header (0x100 bytes, 0x1000 on Xbox 360) first, then the pixels
        if len(buf) < profile.image_start:
            raise TpkError("decoded slot is shorter than a texture header")
        pixel_off, pal_off = _header_fields(buf, 0, tex)
        tex.xenon, tex.fourcc, _next = _part2(buf, 124)
        body_start = profile.xenon_image_start if tex.xenon else profile.image_start
        body_end = len(buf)
    else:
        # Carbon / ProStreet: pixels first, 0x94 byte header at the end
        if len(buf) < COMP_HEADER:
            raise TpkError("decoded slot is shorter than a texture header")
        body_start, body_end = 0, len(buf) - COMP_HEADER
        if buf[body_end + 0x58] + 0x59 > COMP_HEADER:
            raise TpkError("texture name does not fit the header")
        pixel_off, pal_off = _header_fields(buf, body_end, tex)
    has_pal = tex.palette_size > 0 and pal_off >= 0
    if has_pal:
        base = min(pixel_off, pal_off)
        data_at, pal_at = body_start + pixel_off - base, body_start + pal_off - base
    else:
        data_at, pal_at = body_start, body_start
    if data_at + tex.pixel_size > body_end or (has_pal and pal_at + tex.palette_size > body_end):
        raise TpkError(f"{tex.name}: pixel or palette data lies outside the slot")
    tex.data = buf[data_at:data_at + tex.pixel_size]
    tex.palette = buf[pal_at:pal_at + tex.palette_size] if has_pal else b""
    return tex


def _read_slots(mm, path, tpk_name, block_pos, end, ps, pe, index, version=8):
    """Reads the textures of a compressed pack from its offset slots. A bad slot is reported, the others stay."""
    base = os.path.basename(path)
    for i in range((pe - ps) // SLOT_SIZE):
        slot_pos = ps + i * SLOT_SIZE
        slot = struct.unpack_from("<IiiiBBhi", mm, slot_pos)
        where = f"{base} @0x{block_pos:X}: {tpk_name} (v{version}): slot {i} (key 0x{slot[0]:08X})"
        try:
            index.add(_slot_texture(mm, path, tpk_name, block_pos, end, slot_pos, slot, version))
        except TpkSkip as e:
            index.skipped.append(f"{where}: {e}")
            index.note(base, str(e), f"{where}: {e}")
        except (TpkError, struct.error, IndexError, ValueError) as e:
            index.problems.append(f"{where}: {e}")
            index.note(base, str(e), f"{where}: {e}")


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
    version = 0
    first = None
    count = None
    slots = None
    formats = None
    for pid, _bs, ps, pe in _blocks(mm, *info):
        if pid == PART_HEADER and pe - ps >= 0x24:
            version = _u32(mm, ps)
            tpk_name = bytes(mm[ps + 4:ps + 4 + 0x1C]).split(b"\0")[0].decode("latin-1")
        elif pid == PART_FORMATS:
            formats = (ps, pe)
        elif pid == PART_KEYS:
            count = (pe - ps) // 8
        elif pid == PART_OFFSLOTS:
            slots = (ps, pe)
        elif pid == PART_TEXTURES:
            first = (ps, pe)
    if slots is not None and slots[1] - slots[0] >= SLOT_SIZE:
        _read_slots(mm, path, tpk_name, start - 8, end, slots[0], slots[1], index, version)
        return version, "slots"
    if first is None or pixel_part is None:
        raise TpkSkip(f"{tpk_name or 'TPK'}: has no texture headers or no pixel data")

    profile = profile_for(version)
    base = pixel_part + DATA_BASE
    if profile is UNDERCOVER:
        # the images start after the 0x11111111 filler of the texture container (UCGT); else the Carbon rule
        p = pixel_part + 8
        while p + 4 <= len(mm) and _u32(mm, p) == 0x11111111:
            p += 4
        if p > pixel_part + 8:
            base = p
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
        tex.version = version
        pos += 0x59 + name_len
        found.append(tex)
    if profile is UNDERCOVER and formats is not None:
        fpos = formats[0]
        for tex in found:                               # one entry per texture: 24 bytes (PC) or 32 (Xbox 360)
            if fpos + 24 > formats[1]:
                break
            tex.xenon, tex.fourcc, fpos = _part2(mm, fpos)
    for tex in found:
        if tex.pixel_pos + tex.pixel_size > len(mm) or tex.pixel_pos < 0:
            outside += 1                                # keep the other textures of the pack
            continue
        index.add(tex)
    if outside:
        line = f"{os.path.basename(path)}: {tpk_name} (v{version}): {outside} texture(s) with data outside the file"
        index.skipped.append(line)
        index.note(os.path.basename(path), "texture(s) with data outside the file", line)
    return version, "plain"


def _scan_range(mm, path, start, end, index, depth):
    """Reads the TPK blocks of a range. Other container chunks (bit 31 set) are searched too, down to MAX_DEPTH."""
    pos = start
    while pos + 8 <= end:
        cid, size = struct.unpack_from("<II", mm, pos)
        if pos + 8 + size > end:
            break
        if cid == BLOCK_TPK:
            where = f"{os.path.basename(path)} @0x{pos:X}"
            try:
                kind = read_tpk(mm, path, pos + 8, pos + 8 + size, index)
                index.packs += 1
                index.pack_kinds[kind] = index.pack_kinds.get(kind, 0) + 1
            except TpkSkip as e:
                index.skipped.append(f"{where}: {e}")
                index.note(os.path.basename(path), str(e), f"{where}: {e}")
            except (TpkError, struct.error, IndexError) as e:
                index.problems.append(f"{where}: {e}")
                index.note(os.path.basename(path), str(e), f"{where}: {e}")
        elif cid & 0x80000000 and size >= 8 and depth < MAX_DEPTH:
            _scan_range(mm, path, pos + 8, pos + 8 + size, index, depth + 1)
        pos += 8 + size


def scan_file(path, index):
    """Reads every TPK block of a file into the index, also those in nested container chunks."""
    if os.path.getsize(path) < 8:
        return
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            _scan_range(mm, path, 0, len(mm), index, 0)
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


def _dxt_fourcc(tex):
    """DXT1 / DXT3 / DXT5 of a texture. A DXTN texture follows the format of its header part 2 when it has one."""
    if tex.compression == 39 and tex.fourcc in FOURCC_DXT:
        return FOURCC_DXT[tex.fourcc]
    return _DXT[tex.compression]


def _is_bc5(tex):
    """DXTN on Xbox 360 is BC5U, on PC when the format part says so (two channel normal map)."""
    return tex.compression == 39 and (tex.xenon or tex.fourcc in FOURCC_BC5)


def _dds(tex, pixels):
    """Wraps DXT data in a DDS header so Pillow can decode it."""
    linear = _dxt_size(tex)
    header = b"DDS " + struct.pack("<7I", 124, 0x1007 | 0x80000, tex.height, tex.width, linear, 1, 1)
    header += b"\0" * 44 + struct.pack("<II4s5I", 32, 4, _dxt_fourcc(tex), 0, 0, 0, 0, 0)
    header += struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return header + pixels


def _dxt_size(tex):
    return max(1, (tex.width + 3) // 4) * max(1, (tex.height + 3) // 4) * (8 if _dxt_fourcc(tex) == b"DXT1" else 16)


def _min_pixels(tex):
    """Bytes the base mip needs, or None for a format that is not known."""
    n = tex.width * tex.height
    comp = tex.compression
    if comp in _DXT:
        return _dxt_size(tex)
    if comp == 32:
        return n * 4
    if comp == 24:
        return n * 3
    if comp in (17, 18):
        return n * 2
    if comp == 40 or comp in _PAL8:
        return n
    if comp in _PAL4:
        return (n + 1) // 2
    return None


_EXPAND5 = bytes((v << 3) | (v >> 2) for v in range(32))
_EXPAND6 = bytes((v << 2) | (v >> 4) for v in range(64))


def _decode16(tex, pixels):
    """RGB565 (18) and ARGB1555 (17), little-endian 16 bit values."""
    n = tex.width * tex.height
    words = array.array("H")
    words.frombytes(pixels[:n * 2])
    if sys.byteorder == "big":
        words.byteswap()
    out = bytearray(n * 4)
    if tex.compression == 18:
        for i, v in enumerate(words):
            j = i * 4
            out[j] = _EXPAND5[v >> 11]
            out[j + 1] = _EXPAND6[(v >> 5) & 63]
            out[j + 2] = _EXPAND5[v & 31]
            out[j + 3] = 255
    else:
        for i, v in enumerate(words):
            j = i * 4
            out[j] = _EXPAND5[(v >> 10) & 31]
            out[j + 1] = _EXPAND5[(v >> 5) & 31]
            out[j + 2] = _EXPAND5[v & 31]
            out[j + 3] = 255 if v & 0x8000 else 0
    return Image.frombytes("RGBA", (tex.width, tex.height), bytes(out))


def _swap16(data):
    n = len(data) // 2 * 2
    out = bytearray(n)
    out[0::2] = data[1:n:2]
    out[1::2] = data[0:n:2]
    return bytes(out)


def _swap32(data):
    n = len(data) // 4 * 4
    out = bytearray(n)
    for k in range(4):
        out[k::4] = data[3 - k:n:4] if n else b""
    return bytes(out)


def _xg_xy(offset, width, pitch):
    """Where block number `offset` of an Xbox 360 tiled image lies: (x, y) in blocks. XGAddress2DTiledX / Y of
    RareView (via UCGT). Works on ints and on numpy integer arrays. width is in blocks, pitch in bytes per block."""
    aligned = (width + 31) & ~31
    log_bpp = (pitch >> 2) + ((pitch >> 1) >> (pitch >> 2))
    ob = offset << log_bpp
    ot = ((ob & ~4095) >> 3) + ((ob & 1792) >> 2) + (ob & 63)
    om = ot >> (7 + log_bpp)
    per_row = aligned >> 5
    macro_x = (om % per_row) << 2
    tile_x = ((((ot >> (5 + log_bpp)) & 2) + (ob >> 6)) & 3)
    x = ((macro_x + tile_x) << 3) + (((((ot >> 1) & ~15) + (ot & 15)) & ((pitch << 3) - 1)) >> log_bpp)
    macro_y = (om // per_row) << 2
    tile_y = ((ot >> (6 + log_bpp)) & 1) + ((ob & 2048) >> 10)
    micro_y = ((((ot & (((pitch << 6) - 1) & ~31)) + ((ot & 15) << 1)) >> (3 + log_bpp)) & ~1)
    y = ((macro_y + tile_y) << 3) + micro_y + ((ot & 16) >> 4)
    return x, y


def _untile(src, wb, hb, pitch):
    """Linear blocks (wb x hb) from an Xbox 360 tiled stream. The stream is laid out in tiles of 32 x 32 blocks, so
    it is walked over the size rounded up to that; blocks outside the image or past the data are dropped."""
    aligned = ((wb + 31) & ~31) * ((hb + 31) & ~31)
    count = min(aligned, len(src) // pitch)
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        idx = np.arange(count, dtype=np.int64)
        x, y = _xg_xy(idx, wb, pitch)
        ok = (x < wb) & (y < hb)
        blocks = np.frombuffer(src, dtype=np.uint8, count=count * pitch).reshape(count, pitch)
        out = np.zeros((wb * hb, pitch), dtype=np.uint8)
        out[(y * wb + x)[ok]] = blocks[ok]
        return out.tobytes()
    out = bytearray(wb * hb * pitch)
    for n in range(count):
        x, y = _xg_xy(n, wb, pitch)
        if x < wb and y < hb:
            d = (y * wb + x) * pitch
            out[d:d + pitch] = src[n * pitch:(n + 1) * pitch]
    return bytes(out)


def _xenon_pixels(tex, pixels):
    """Base mip of an Xbox 360 texture as linear little-endian data, like the PC one. As in UCGT: DXT data is
    swapped per 16 bit word, 32 bit per 32 bit word, then untiled. Other formats are not known for this platform."""
    comp = tex.compression
    if comp in _DXT:
        pitch = 8 if _dxt_fourcc(tex) == b"DXT1" and comp != 39 else 16
        wb, hb = max(1, (tex.width + 3) // 4), max(1, (tex.height + 3) // 4)
        return _untile(_swap16(pixels), wb, hb, pitch)
    if comp == 32:
        return _untile(_swap32(pixels), tex.width, tex.height, 4)
    raise TpkError(f"{tex.name}: format {tex.format_name} is not supported for Xbox 360 textures")


def _bc4(block):
    """16 values (0..255, row by row) of one BC4 block of 8 bytes."""
    e0, e1 = block[0], block[1]
    if e0 > e1:
        pal = [e0, e1] + [((7 - i) * e0 + i * e1 + 3) // 7 for i in range(1, 7)]
    else:
        pal = [e0, e1] + [((5 - i) * e0 + i * e1 + 2) // 5 for i in range(1, 5)] + [0, 255]
    bits = int.from_bytes(block[2:8], "little")
    return [pal[(bits >> (3 * i)) & 7] for i in range(16)]


def _decode_bc5(tex, pixels):
    """BC5 (two BC4 blocks: red, green) normal map as RGBA. Blue is rebuilt from red and green; alpha is opaque."""
    w, h = tex.width, tex.height
    wb, hb = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    out = bytearray(w * h * 4)
    for by in range(hb):
        for bx in range(wb):
            o = (by * wb + bx) * 16
            red, green = _bc4(pixels[o:o + 8]), _bc4(pixels[o + 8:o + 16])
            for i in range(16):
                x, y = bx * 4 + (i & 3), by * 4 + (i >> 2)
                if x >= w or y >= h:
                    continue
                r, g = red[i], green[i]
                nx, ny = r / 127.5 - 1.0, g / 127.5 - 1.0
                nz = max(0.0, 1.0 - nx * nx - ny * ny) ** 0.5
                j = (y * w + x) * 4
                out[j], out[j + 1], out[j + 2], out[j + 3] = r, g, min(255, int(nz * 127.5 + 127.5 + 0.5)), 255
    return Image.frombytes("RGBA", (w, h), bytes(out))


def _payload(tex):
    """(pixels, palette) bytes of a texture: from memory for a compressed slot, else read from the file."""
    if tex.data is not None:
        return tex.data, tex.palette or b""
    if tex.pixel_pos < 0:
        raise TpkError(f"{tex.name}: no pixel data")
    with open(tex.path, "rb") as f:
        f.seek(tex.pixel_pos)
        pixels = f.read(tex.pixel_size)
        palette = b""
        if tex.palette_pos >= 0:
            f.seek(tex.palette_pos)
            palette = f.read(tex.palette_size)
    return pixels, palette


def texture_note(tex):
    """Short remark when a texture is only previewed (not decoded exactly), else an empty string."""
    if tex.compression == 39:
        if _is_bc5(tex):
            return "BC5 normal map, blue rebuilt from red and green"
        if tex.fourcc in FOURCC_DXT:
            return ""
    return _NOTES.get(tex.compression, "")


def decode(tex):
    """Returns the base mip of a texture as an RGBA Pillow image. Raises TpkError if it can not."""
    if not HAVE_PIL:
        raise TpkError("Pillow is not installed (pip install pillow)")
    w, h, comp = tex.width, tex.height, tex.compression
    if w <= 0 or h <= 0:
        raise TpkError(f"{tex.name}: texture has no size")
    pixels, palette = _payload(tex)
    if tex.xenon:
        pixels = _xenon_pixels(tex, pixels)
    need = _min_pixels(tex)
    if need is None:
        if comp in (16, 19):
            raise TpkError(f"{tex.name}: 16 bit format {tex.format_name} has no verified channel layout")
        raise TpkError(f"{tex.name}: texture format {tex.format_name} is not supported yet")
    if len(pixels) < need:
        raise TpkError(f"{tex.name}: {tex.format_name} needs {need} bytes of pixels, pack has {len(pixels)}")

    try:
        if comp in _DXT:
            if _is_bc5(tex):
                img = _decode_bc5(tex, pixels)
            else:
                img = Image.open(io.BytesIO(_dds(tex, pixels))).convert("RGBA")
            note = texture_note(tex)
            if note:
                img.info["tpk_note"] = note
            return img
        if comp == 32:
            return Image.frombytes("RGBA", (w, h), pixels[:w * h * 4], "raw", "BGRA")
        if comp == 24:
            return Image.frombytes("RGB", (w, h), pixels[:w * h * 3], "raw", "BGR").convert("RGBA")
        if comp in (17, 18):
            return _decode16(tex, pixels)
        if comp == 40:
            return Image.frombytes("L", (w, h), pixels[:w * h]).convert("RGBA")
        if comp in _PAL4:
            index = bytearray(w * h)
            packed = pixels[:(w * h + 1) // 2]
            index[0::2] = bytes(b >> 4 for b in packed)[:len(index[0::2])]
            index[1::2] = bytes(b & 15 for b in packed)[:len(index[1::2])]
            count = 16
        else:
            index = pixels[:w * h]
            count = 256
        if not palette:
            raise TpkError(f"{tex.name}: {tex.format_name} texture has no palette")
        if len(palette) < count * 4:
            raise TpkError(f"{tex.name}: palette has {len(palette) // 4} colors, {count} are needed")
        idx = Image.frombytes("L", (w, h), bytes(index))
        lut = [[palette[i * 4 + c] if i < count else 0 for i in range(256)] for c in range(4)]   # B, G, R, A
        return Image.merge("RGBA", (idx.point(lut[2]), idx.point(lut[1]), idx.point(lut[0]), idx.point(lut[3])))
    except (ValueError, OSError) as e:
        raise TpkError(f"{tex.name}: {e}")
