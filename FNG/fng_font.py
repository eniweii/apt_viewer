#!/usr/bin/env python3
"""
fng_font.py

Reads the Carbon font blocks (FEngFont 0x00030201) and draws text with them.
The layout below was worked out from one real block (FONT_NFS_BODY, 4720 bytes) and checked
against its glyph sheet. No reference code exists for it. Fields marked "?" are not understood.

Block, little-endian:
  0x000  name block, 0x100 bytes: font name, 'bin', '\\font_nfs_body.bin' (zero ended strings)
  0x100  name block, 0x100 bytes: font name, 'bmp', 'nfs_body.bmp'  (the glyph sheet file)
  0x200  'FntF' header ('FNTF' in some fonts), offsets below are from here
    +0x04 u32 ?          +0x08 u16 ?          +0x0A u16 glyph count + 1 (?)
    +0x0C u16 9 (space width?)  +0x0E u16 13 (?)
    +0x18 u32 offset of the kerning table      +0x1C u32 size of the font data
    +0x24 u32 line height                      +0x34 u32 127 (?)
    +0x40 f32 1 / sheet width                  +0x44 f32 1 / sheet height
    +0x90 glyph records, 16 bytes each, up to the kerning table:
        u16 code (unicode), u8 width, u8 height, u16 x, u16 y (top left on the sheet),
        u8 0, u8 x offset, s8 y offset, u8 kerning count, u8 first kerning entry, u8 0, u8 advance, u8 0
  kerning table (at +kerning offset): u32 entry count, 3 bytes ?, then entries of 4 bytes:
        first character, second character, u8 ?, s8 kerning. Second character 0 = no kerning.
Checked: glyph rectangles against the real glyph sheet (every white pixel is inside one), the kerning
count / first entry of every glyph against the table. Not checked against the game: the x offset
(glyph is drawn at pen + x offset) and the space width.
"""
import struct

import fng_bun

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

HEADER_MAGIC = b"FntF"
GLYPHS_AT = 0x90


class FontError(Exception):
    pass


class Glyph:
    __slots__ = ("code", "width", "height", "x", "y", "x_offset", "y_offset", "advance", "kern_count", "kern_start", "unknown")

    def __init__(self, record):
        self.code, self.width, self.height, self.x, self.y = struct.unpack_from("<HBBHH", record, 0)
        self.x_offset = record[9]
        self.y_offset = struct.unpack_from("<b", record, 10)[0]
        self.kern_count = record[11]
        self.kern_start = record[12]
        self.advance = record[14]
        self.unknown = bytes([record[8], record[13], record[15]])       # always 0 in the block seen


class Font:
    def __init__(self):
        self.name = ""
        self.sheet_name = ""            # file name of the glyph sheet, like 'nfs_body.bmp'
        self.glyphs = {}                # code -> Glyph
        self.kerning = {}               # (first, second) -> pixels
        self.line_height = 0
        self.space_width = 0
        self.sheet_size = (0, 0)
        self.header = {}                # the fields not understood yet, for the info view
        self.warnings = []

    @property
    def sheet_stem(self):
        return self.sheet_name.rsplit(".", 1)[0]

    def sheet_candidates(self):
        """Texture names to try for the glyph sheet, best first."""
        names = [self.name, "FONT_" + self.sheet_stem, self.sheet_stem]
        return [n for n in names if n]


def _strings(block):
    return [s.decode("latin-1") for s in block.split(b"\0") if s]


def parse_font(data):
    """Returns a Font. Raises FontError if the block does not look like a font."""
    if len(data) < 0x200 + GLYPHS_AT:
        raise FontError("block is too short for a font")
    names = _strings(data[0:0x100])
    sheet = _strings(data[0x100:0x200])
    start = data.find(HEADER_MAGIC, 0x200, 0x400)
    if start < 0:
        start = data.find(HEADER_MAGIC.upper(), 0x200, 0x400)        # some fonts write FNTF
    if start < 0 or not names:
        raise FontError("no FntF / FNTF header")

    font = Font()
    font.name = names[0]
    font.sheet_name = sheet[2] if len(sheet) >= 3 else ""
    kern_off, size, line_height = struct.unpack_from("<III", data, start + 0x18)[0], struct.unpack_from("<I", data, start + 0x1C)[0], struct.unpack_from("<I", data, start + 0x24)[0]
    font.line_height = line_height
    font.space_width = struct.unpack_from("<H", data, start + 0x0C)[0]
    u_scale, v_scale = struct.unpack_from("<2f", data, start + 0x40)
    if u_scale > 0 and v_scale > 0:
        font.sheet_size = (round(1 / u_scale), round(1 / v_scale))
    font.header = {"+0x04": struct.unpack_from("<I", data, start + 4)[0], "+0x08": struct.unpack_from("<H", data, start + 8)[0],
                   "glyph count field": struct.unpack_from("<H", data, start + 0x0A)[0],
                   "+0x0E": struct.unpack_from("<H", data, start + 0x0E)[0], "size": size}

    if kern_off < GLYPHS_AT or start + kern_off > len(data):
        raise FontError("kerning table offset is outside the block")
    count = (kern_off - GLYPHS_AT) // 16
    if abs(count - font.header["glyph count field"]) > 1:
        font.warnings.append(f"glyph count field is {font.header['glyph count field']}, the records give {count}")
    for i in range(count):
        record = data[start + GLYPHS_AT + i * 16:start + GLYPHS_AT + i * 16 + 16]
        glyph = Glyph(record)
        font.glyphs[glyph.code] = glyph

    table = start + kern_off
    pairs = struct.unpack_from("<I", data, table)[0]
    pos = table + 7
    for _ in range(pairs):
        if pos + 4 > len(data):
            font.warnings.append("kerning table is cut")
            break
        first, second, _flag, kern = struct.unpack_from("<BBBb", data, pos)
        if second:
            font.kerning[(first, second)] = kern
        pos += 4
    return font


class FontIndex:
    def __init__(self):
        self.fonts = {}                 # upper case name -> Font
        self.problems = []

    def find(self, resource_name):
        """Font for a resource name, by the name without folders and extension."""
        stem = resource_name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return self.fonts.get(stem.upper())


def scan_files(paths):
    index = FontIndex()
    for path in paths:
        try:
            refs = fng_bun.scan_blocks(path, fng_bun.FONT_BLOCKS)
        except OSError:
            continue
        for ref in refs:
            try:
                font = parse_font(fng_bun.read_block(ref))
            except (FontError, struct.error) as e:
                index.problems.append(f"{ref.path.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]} @0x{ref.offset:X}: {e}")
                continue
            index.fonts.setdefault(font.name.upper(), font)
    return index


def _layout(font, text):
    """Glyph positions per line: ([(Glyph, x), ...] per line, widest line in pixels, width of each line)."""
    rows = []
    widths = []
    for line in text.split("\n"):
        x = 0
        prev = None
        row = []
        for ch in line:
            code = ord(ch)
            if prev is not None:
                x += font.kerning.get((prev, code), 0)
            glyph = font.glyphs.get(code)
            if glyph is None:                           # caps-only fonts: use the other case
                other = ch.upper() if ch.upper() != ch else ch.lower()
                glyph = font.glyphs.get(ord(other)) if len(other) == 1 else None
            if glyph is None:
                x += font.space_width
            else:
                row.append((glyph, x + glyph.x_offset))
                x += glyph.advance
            prev = code
        rows.append(row)
        widths.append(max([x] + [px + g.width for g, px in row]))
    return rows, max(widths), widths


def measure_text(font, text, leading=0):
    """(width, height) of text in pixels, without padding."""
    rows, width, _widths = _layout(font, text)
    return width, font.line_height * len(rows) + leading * (len(rows) - 1)


def wrap_text(font, text, max_width):
    """Breaks text at spaces so no line is wider than max_width (a single longer word stays whole)."""
    out = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            trial = word if not line else line + " " + word
            if line and _layout(font, trial)[1] > max_width:
                out.append(line)
                line = word
            else:
                line = trial
        out.append(line)
    return "\n".join(out)


def _glyph_image(font, sheet, glyph):
    """The picture of one glyph, cropped once from the sheet and kept on the font."""
    cache = font.__dict__.setdefault("_crops", {})
    entry = cache.get((id(sheet), glyph.code))
    if entry is None or entry[0] is not sheet:
        entry = (sheet, sheet.crop((glyph.x, glyph.y, glyph.x + glyph.width, glyph.y + glyph.height)))
        cache[(id(sheet), glyph.code)] = entry
    return entry[1]


def paste_clipped(out, img, x, y):
    """Alpha composite img onto out at (x, y). Parts outside out are cut off."""
    if x >= 0 and y >= 0 and x + img.width <= out.width and y + img.height <= out.height:
        out.alpha_composite(img, (x, y))
        return
    sx, sy = max(0, -x), max(0, -y)
    box = (sx, sy, min(img.width, out.width - x), min(img.height, out.height - y))
    if box[2] <= box[0] or box[3] <= box[1]:
        return
    out.alpha_composite(img.crop(box), (x + sx, y + sy))


def render_text(font, sheet, text, color=None, pad=4, align=0, leading=0):
    """
    RGBA image of text drawn with the glyph sheet (a Pillow image). Lines split at newlines.
    The top left of the first line box is at (pad, pad) in the image. color = (r, g, b) tint.
    align: 0 left, 1 center, 2 right (each line inside the widest line). leading: extra pixels between lines.
    """
    if not HAVE_PIL:
        raise FontError("Pillow is not installed (pip install pillow)")
    rows, width, widths = _layout(font, text)
    pitch = font.line_height + leading
    out = Image.new("RGBA", (max(1, width) + 2 * pad, pitch * len(rows) + 2 * pad + 8), (0, 0, 0, 0))
    for n, row in enumerate(rows):
        shift = (width - widths[n]) // 2 if align == 1 else (width - widths[n] if align == 2 else 0)
        for glyph, x in row:
            if glyph.width and glyph.height:
                paste_clipped(out, _glyph_image(font, sheet, glyph), x + shift + pad, n * pitch + glyph.y_offset + pad)
    if color is not None and tuple(color) != (255, 255, 255):
        r, g, b, a = out.split()
        out = Image.merge("RGBA", (r.point(lambda v: v * color[0] // 255), g.point(lambda v: v * color[1] // 255),
                                   b.point(lambda v: v * color[2] // 255), a))
    return out
