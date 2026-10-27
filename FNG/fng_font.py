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
        s8 advance Y (0), s8 x offset, s8 y offset, u8 kerning count, u16 first kerning entry, s16 advance
        (field types as in the Most Wanted decompilation, RealFontOld::Glyph)
  kerning table (at +kerning offset): u32 entry count, 3 bytes ?, then entries of 4 bytes:
        first character, second character, u8 ?, s8 kerning. Second character 0 = no kerning.
Text: a $NAME$ in a string is an icon texture (the game draws it at the pen, centered on the object's y 0).
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
LANGUAGES = ("ENGLISH", "FRENCH", "GERMAN", "ITALIAN", "SPANISH", "DUTCH", "SWEDISH", "DANISH", "KOREAN", "CHINESE",
             "JAPANESE", "THAI", "POLISH", "FINNISH")


def strip_language(name):
    """'FONT_X_FRENCH' -> 'FONT_X'. The game registers a font without its language suffix."""
    upper = name.upper()
    for language in LANGUAGES:
        if upper.endswith("_" + language) and len(upper) > len(language) + 1:
            return name[:-(len(language) + 1)]
    return name


class FontError(Exception):
    pass


class Glyph:
    __slots__ = ("code", "width", "height", "x", "y", "x_offset", "y_offset", "advance", "kern_count", "kern_start", "unknown")

    def __init__(self, record):
        self.code, self.width, self.height, self.x, self.y = struct.unpack_from("<HBBHH", record, 0)
        self.x_offset, self.y_offset = struct.unpack_from("<2b", record, 9)
        self.kern_count = record[11]
        self.kern_start = struct.unpack_from("<H", record, 12)[0]
        self.advance = struct.unpack_from("<h", record, 14)[0]
        self.unknown = bytes([record[8]])                               # advance Y, always 0 in the block seen


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
        self.plain = {}                 # upper case name without the language suffix -> Font (only where no font has that name)
        self.problems = []

    def add(self, font):
        self.fonts.setdefault(font.name.upper(), font)
        plain = strip_language(font.name).upper()
        if plain != font.name.upper():
            self.plain.setdefault(plain, font)

    def find(self, resource_name):
        """Font for a resource name, by the name without folders and extension."""
        stem = resource_name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0].upper()
        return self.fonts.get(stem) or self.plain.get(stem)


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
            index.add(font)
    return index


def _items(line, icons):
    """Parts of one line: ("c", code) for a character, ("i", image) for a $NAME$ icon, "$$" is a literal "$"."""
    out = []
    i = 0
    while i < len(line):
        if line[i] == "$" and icons is not None:
            if line[i + 1:i + 2] == "$":
                out.append(("c", 36))
                i += 2
                continue
            end = line.find("$", i + 1)
            image = icons(line[i + 1:end]) if end > i + 1 else None
            if image is not None:
                out.append(("i", image))
                i = end + 1
                continue
        out.append(("c", ord(line[i])))
        i += 1
    return out


def _layout(font, text, icons=None):
    """Positions per line: [(kind, Glyph or image, x), ...] per line, widest line in pixels (with overhang),
    width of each line, advance width of each line (pen movement only, what the game wraps and shrinks by)."""
    rows = []
    widths = []
    advances = []
    for line in text.split("\n"):
        x = 0
        prev = None
        row = []
        for kind, value in _items(line, icons):
            if kind == "i":
                row.append(("i", value, x))
                x += value.width
                prev = None
                continue
            code = value
            if prev is not None:
                x += font.kerning.get((prev, code), 0)
            glyph = font.glyphs.get(code)
            if glyph is None:                           # caps-only fonts: use the other case
                other = chr(code).upper() if chr(code).upper() != chr(code) else chr(code).lower()
                glyph = font.glyphs.get(ord(other)) if len(other) == 1 else None
            if glyph is None:
                x += font.space_width
            else:
                row.append(("g", glyph, x + glyph.x_offset))
                x += glyph.advance
            prev = code
        rows.append(row)
        widths.append(max([x] + [px + g.width for _kind, g, px in row]))
        advances.append(x)
    return rows, max(widths), widths, advances


def measure_text(font, text, leading=0, icons=None):
    """(width, height) of text in pixels, without padding."""
    rows, width, _widths, _advances = _layout(font, text, icons)
    return width, font.line_height * len(rows) + leading * (len(rows) - 1)


def first_line_advance(font, text, icons=None):
    """Pen width of the first line (up to the first newline): the game shrinks a string by this when it is wider than the max width."""
    return _layout(font, text.split("\n")[0], icons)[3][0]


def wrap_text(font, text, max_width, icons=None):
    """Word wrap like the game: a line breaks at a space when the next word (with the space) does not fit by pen width.
    A space at the start of a line is dropped, a single longer word stays whole."""
    out = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            trial = word if not line else line + " " + word
            if line and _layout(font, trial, icons)[3][0] > max_width:
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


def render_text_ex(font, sheet, text, color=None, pad=4, align=0, leading=0, icons=None, origin_y=0):
    """
    Like render_text, but returns (image, left, top): where the top left of the first line box is in the image.
    Icons ($NAME$ in the text, icons(name) gives an RGBA image or None) are centered on origin_y, which is the
    object's y 0 measured from the top of the text box, and may reach outside the box.
    """
    if not HAVE_PIL:
        raise FontError("Pillow is not installed (pip install pillow)")
    rows, width, widths, _advances = _layout(font, text, icons)
    pitch = font.line_height + leading
    box_height = pitch * len(rows)
    icon_boxes = [(origin_y - value.height / 2, value.height) for row in rows for kind, value, _x in row if kind == "i"]
    extra_top = max([0] + [int(-top + 0.999) for top, _h in icon_boxes])
    extra_bottom = max([0] + [int(top + h - box_height + 0.999) for top, h in icon_boxes])
    left, top = pad, pad + extra_top
    out = Image.new("RGBA", (max(1, width) + 2 * pad, box_height + 2 * pad + 8 + extra_top + extra_bottom), (0, 0, 0, 0))
    for n, row in enumerate(rows):
        shift = (width - widths[n]) // 2 if align == 1 else (width - widths[n] if align == 2 else 0)
        for kind, value, x in row:
            if kind == "i":
                paste_clipped(out, value, x + shift + left, top + int(round(origin_y - value.height / 2)))
            elif value.width and value.height:
                paste_clipped(out, _glyph_image(font, sheet, value), x + shift + left, n * pitch + value.y_offset + top)
    if color is not None and tuple(color) != (255, 255, 255):
        r, g, b, a = out.split()
        out = Image.merge("RGBA", (r.point(lambda v: v * color[0] // 255), g.point(lambda v: v * color[1] // 255),
                                   b.point(lambda v: v * color[2] // 255), a))
    return out, left, top


def render_text(font, sheet, text, color=None, pad=4, align=0, leading=0, icons=None):
    """
    RGBA image of text drawn with the glyph sheet (a Pillow image). Lines split at newlines.
    The top left of the first line box is at (pad, pad) in the image. color = (r, g, b) tint.
    align: 0 left, 1 center, 2 right (each line inside the widest line). leading: extra pixels between lines.
    """
    return render_text_ex(font, sheet, text, color, pad, align, leading, icons)[0]
