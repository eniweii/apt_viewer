#!/usr/bin/env python3
"""
apt_font.py

Reads the APT font blocks (chunk 0x00030202, e.g. in GLOBAL_APT_FONT_HD.BUN) and draws text with them.
This is not the FNG font format (fng_font.py, 'FntF' blocks 0x00030201). Layout worked out from the six
fonts of GLOBAL_APT_FONT_HD.BUN. Checked: every block ends exactly at its last glyph record, each glyph
width equals x1 - x0, and text drawn with the glyphs reads correctly.

Block, little-endian (positions from the start of the block payload):
  0x00 'FONT', u32 0x6A (?), name (zero ended, in 0x20 bytes after 0x08)
  0x28 f32 nominal size (32 or 92 or 90), u32 ascent, u32 descent, u32 ?, u32 ascent again (?), u32 line height
  0x40 u16 first character code, u16 last character code, u32 glyph count
  0x48 'FONR', then one u32 per code from first to last: index of the glyph (0 for codes without a glyph)
  next 'FONG', then 20 byte glyph records:
       u16 page (always 0), u16 x0, y0, x1, y1 (inclusive rectangle on the sheet), u16 width, u16 height,
       u16 advance, s16 x offset, s16 y offset (drawn at pen + offset, y offset from the line top)
The sheet is the texture with the same name as the font (FONT_APT_...). Kerning is not stored here.
"""
import os
import struct

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

BLOCK_APT_FONT = 0x00030202


class AptFontError(Exception):
    pass


class AptFont:
    def __init__(self):
        self.name = ""
        self.nominal = 32.0
        self.ascent = 0
        self.descent = 0
        self.line_height = 0
        self.first = 0
        self.last = 0
        self.table = []             # glyph index per code (first..last)
        self.glyphs = []            # (page, x0, y0, x1, y1, w, h, advance, xoff, yoff)
        self.source = ""
        self._crops = {}

    def glyph(self, code):
        if self.first <= code <= self.last:
            index = self.table[code - self.first]
            if code != 0x20 and index == 0:
                return None                     # code without a glyph (index 0 is the space)
            return self.glyphs[index]
        return None


def parse_font(b):
    if len(b) < 0x50 or b[:4] != b"FONT":
        raise AptFontError("no FONT header")
    f = AptFont()
    f.name = b[8:b.index(b"\0", 8)].decode("latin-1")
    size, f.ascent, f.descent, _x, _y, f.line_height = struct.unpack_from("<fIIIII", b, 0x28)
    f.nominal = size if size > 0 else 32.0
    f.first, f.last, count = struct.unpack_from("<HHI", b, 0x40)
    if b[0x48:0x4C] != b"FONR":
        raise AptFontError("no FONR table")
    n = f.last - f.first + 1
    f.table = list(struct.unpack_from("<%dI" % n, b, 0x4C))
    pos = 0x4C + 4 * n
    if b[pos:pos + 4] != b"FONG":
        raise AptFontError("no FONG table")
    if pos + 4 + 20 * count != len(b):
        raise AptFontError("glyph records do not end at the block end")
    for i in range(count):
        f.glyphs.append(struct.unpack_from("<5H3H2h", b, pos + 4 + 20 * i))
    return f


def scan_file(path, fonts):
    """Adds the APT fonts found at the top level of a chunk file to the dict (upper case name -> AptFont)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return
    pos = 0
    while pos + 8 <= len(data):
        cid, size = struct.unpack_from("<II", data, pos)
        if pos + 8 + size > len(data):
            break
        if cid == BLOCK_APT_FONT:
            try:
                font = parse_font(data[pos + 8:pos + 8 + size])
                font.source = os.path.basename(path)
                fonts.setdefault(font.name.upper(), font)
            except (AptFontError, struct.error, ValueError):
                pass
        pos += 8 + size


def _layout(font, text, wrap_width=None):
    """Lines of glyph positions: [(width, [(glyph, x)])], x in font pixels."""
    lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ") if wrap_width else [paragraph]
        row, x, widest = [], 0, 0
        pieces = []
        for wi, word in enumerate(words):
            chunk = (" " if wi and wrap_width else "") + word
            w = sum((font.glyph(ord(ch)) or (0,) * 7 + (font.glyphs[0][7],))[7] for ch in chunk)
            if wrap_width and row and x + w > wrap_width:
                pieces.append((x, row))
                row, x = [], 0
                chunk = word
            for ch in chunk:
                g = font.glyph(ord(ch))
                if g is None:
                    g = font.glyph(0x20)
                    x += g[7] if g else 8
                    continue
                row.append((g, x))
                x += g[7]
        pieces.append((x, row))
        lines.extend(pieces)
    return lines


def render_text(font, sheet, text, align=0, box_width=None, wrap=False):
    """RGBA image (white glyphs) of text at the nominal font size. box_width (font pixels) is used for alignment and wrapping."""
    if not HAVE_PIL:
        raise AptFontError("Pillow is not installed")
    lines = _layout(font, text, box_width if wrap else None)
    width = max([w for w, _r in lines] + [box_width or 0, 1])
    out = Image.new("RGBA", (int(width) + 4, font.line_height * len(lines) + 4), (0, 0, 0, 0))
    for n, (w, row) in enumerate(lines):
        shift = 0 if align == 0 else (width - w) // 2 if align == 1 else width - w
        for g, x in row:
            _page, x0, y0, x1, y1, gw, gh, _adv, xo, yo = g
            if gw <= 0 or gh <= 0:
                continue
            crop = font._crops.get((id(sheet), x0, y0, x1, y1))
            if crop is None:
                crop = sheet.crop((x0, y0, x1 + 1, y1 + 1))
                font._crops[(id(sheet), x0, y0, x1, y1)] = crop
            px, py = int(x + shift + xo) + 2, n * font.line_height + yo + 2
            if px >= 0 and py >= 0:
                out.alpha_composite(crop, (px, py)) if px + crop.width <= out.width and py + crop.height <= out.height else \
                    out.paste(crop.crop((0, 0, min(crop.width, out.width - px), min(crop.height, out.height - py))), (px, py))
    return out
