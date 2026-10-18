#!/usr/bin/env python3
"""
apt_movie.py

Movie model for the viewer: reads one UI .big file (apt + const + resource entries) into characters,
shape fill blocks, timelines and placements. Uses the BIG and apt readers of apt_scan.py.

Layouts (little-endian, offsets in the apt entry, checked on Undercover files):
  character: u32 type, u32 0x09876543, ...
    Shape  +0x10 4 x f32 bounds, +0x20 geometry id (BIG entry = id + 1)
    Text   +0x10 4 x f32 bounds, +0x20 font id, +0x24 align, +0x28 colour (ARGB), +0x2C f32 font height,
           +0x30 read only, +0x34 multiline, +0x38 word wrap, +0x3C text ptr, +0x40 variable ptr
    Font   +0x10 name ptr (a FONT_APT_... name)
    Sprite / main movie: +0x10 frame count, +0x14 frame array (u32 control count, u32 control array ptr)
  control: u32 type, 1 DoAction, 2 FrameLabel (+4 label ptr), 3 PlaceObject2, 4 RemoveObject2 (+4 depth),
    5 BackgroundColour (+4 ARGB), 8 DoInitAction
    PlaceObject2: +4 flags (1 move, 2 character, 4 matrix, 8 colour transform, 16 ratio, 32 name), +8 depth,
      +0x0C character, +0x10 matrix a b c d tx ty (f32), +0x28 colour scale (ARGB), +0x2C colour bias,
      +0x30 ratio, +0x34 name ptr, +0x38 clip depth (0xFFFFFFFF = none), +0x3C actions ptr
  shape geometry entry: header (total size, block count, 0, offset of block 0, offsets of blocks 1.. at 0x10);
    fill block: +0x20 colour (RGBA, alpha last), +0x24 image id (0 = none), +0x30 matrix a b c d tx ty
    (shape position -> image pixel), +0x48 triangle count, +0x4C vertex offset (from block + 0x10),
    vertices of 32 bytes (x f32, y f32, rest not used here), 3 per triangle.
"""
import os
import struct

import apt_scan as A

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
NO_CLIP = 0xFFFFFFFF

F_MOVE, F_CHAR, F_MATRIX, F_CXFORM, F_RATIO, F_NAME, F_CLIP, F_EVENTS = 1, 2, 4, 8, 16, 32, 0x40, 0x80


def _f32(b, o):
    return struct.unpack_from(A.ENDIAN + "f", b, o)[0]


class Movie:
    def __init__(self, name, entries):
        self.name = name
        self.ents = dict(entries)
        self.apt = self.ents["0"]
        self.const = self.ents["1"]
        if not self.apt.startswith(b"Apt Data"):
            raise ValueError("no apt data")
        self.endian = A.use_endian(self.apt, self.const)        # "<" PC, ">" x360
        main = A.au32(self.const, 20)
        self.width, self.height, self.ms = (A.au32(self.apt, main + 36 + 4 * i) for i in range(3))
        self.chars = {}
        n_chars, ap_chars = A.au32(self.apt, main + 28), A.au32(self.apt, main + 32)
        for i in range(n_chars):
            p = A.au32(self.apt, ap_chars + 4 * i)
            if p:
                self.chars[i] = self._character(i, p)
        self.frames = self._frames(main)
        self.imports = []
        n, a = A.au32(self.apt, main + 48), A.au32(self.apt, main + 52)
        for i in range(n):
            q = a + 16 * i
            self.imports.append({"movie": A.cstr(self.apt, A.au32(self.apt, q)), "name": A.cstr(self.apt, A.au32(self.apt, q + 4)),
                                 "id": A.au32(self.apt, q + 8)})
        self.exports = []
        n, a = A.au32(self.apt, main + 56), A.au32(self.apt, main + 60)
        for i in range(n):
            q = a + 8 * i
            self.exports.append({"name": A.cstr(self.apt, A.au32(self.apt, q)), "id": A.au32(self.apt, q + 4)})

    # ---- characters
    def _character(self, cid, p):
        a = self.apt
        t = A.au32(a, p)
        c = {"id": cid, "type": A.TYPES.get(t, str(t)), "ptr": p}
        if t == 1:
            c["bounds"] = [_f32(a, p + 16 + 4 * k) for k in range(4)]
            c["geometry_id"] = A.au32(a, p + 32)
            c["blocks"] = self._blocks(self.ents.get(str(c["geometry_id"] + 1)))
        elif t == 2:
            c["bounds"] = [_f32(a, p + 16 + 4 * k) for k in range(4)]
            c["font_id"], c["align"], c["color"] = A.au32(a, p + 32), A.au32(a, p + 36), A.au32(a, p + 40)
            c["height"] = _f32(a, p + 44)
            c["readonly"], c["multiline"], c["wordwrap"] = (A.au32(a, p + 48 + 4 * k) for k in range(3))
            c["text"] = A.cstr(a, A.au32(a, p + 60)) or ""
            c["variable"] = A.cstr(a, A.au32(a, p + 64)) or ""
        elif t == 3:
            c["name"] = A.cstr(a, A.au32(a, p + 16))
        elif t == 5:
            c["frames"] = self._frames(p)
        return c

    def _blocks(self, blob):
        """Fill blocks of a shape: [{'color','image','matrix','triangles': [((x,y),(x,y),(x,y))]}]. None if the layout does not fit."""
        if not blob or len(blob) < 0x20:
            return None
        try:
            total, n, _z, first = struct.unpack_from(A.ENDIAN + "4I", blob, 0)
            if total != len(blob) or not 1 <= n <= 64:
                return None
            offs = [first] + [A.au32(blob, 0x10 + 4 * i) for i in range(n - 1)]
            out = []
            for o in offs:
                size = A.au32(blob, o)
                color, image = A.au32(blob, o + 0x20), A.au32(blob, o + 0x24)
                matrix = struct.unpack_from(A.ENDIAN + "6f", blob, o + 0x30)
                tri, voff = A.au32(blob, o + 0x48), A.au32(blob, o + 0x4C)
                start = o + 0x10 + voff
                if not 0 < tri < 4000 or start + tri * 96 > o + size or start + tri * 96 > len(blob):
                    return None
                verts = [struct.unpack_from(A.ENDIAN + "2f", blob, start + 32 * i) for i in range(tri * 3)]
                out.append({"color": color, "image": image if image < 0x10000 else 0, "matrix": matrix,
                            "triangles": [tuple(verts[3 * i:3 * i + 3]) for i in range(tri)]})
            return out
        except (struct.error, IndexError):
            return None

    # ---- timelines
    def _frames(self, p):
        a = self.apt
        n, arr = A.au32(a, p + 16), A.au32(a, p + 20)
        frames = []
        for i in range(n):
            count, ctl = A.au32(a, arr + 8 * i), A.au32(a, arr + 8 * i + 4)
            frames.append([self._control(A.au32(a, ctl + 4 * j)) for j in range(count)] if count else [])
        return frames

    def _control(self, c):
        a = self.apt
        t = A.au32(a, c)
        if t in (3, 9):
            flags = A.au32(a, c + 4)
            events = []
            aptr = A.au32(a, c + 60)
            if flags & F_EVENTS and aptr:                  # clip actions: (event flags, key code, stream)
                nb, boff = A.au32(a, aptr), A.au32(a, aptr + 4)
                bp = boff
                for _ in range(nb):
                    bp = (bp + 3) & ~3
                    events.append(struct.unpack_from(A.ENDIAN + "iiI", a, bp))
                    bp += 12
            return {"op": "place", "flags": flags, "depth": A.au32(a, c + 8), "char": A.au32(a, c + 12),
                    "matrix": struct.unpack_from(A.ENDIAN + "6f", a, c + 16), "scale": A.au32(a, c + 40), "bias": A.au32(a, c + 44),
                    "name": A.cstr(a, A.au32(a, c + 52)) or "", "clip": A.au32(a, c + 56) if flags & F_CLIP else NO_CLIP,
                    "events": events, "ptr": c}
        if t == 1:
            return {"op": "action", "ptr": A.au32(a, c + 4)}
        if t == 8:
            return {"op": "initaction", "sprite": A.au32(a, c + 4), "ptr": A.au32(a, c + 8)}
        if t == 4:
            return {"op": "remove", "depth": A.au32(a, c + 4)}
        if t == 5:
            return {"op": "background", "color": A.au32(a, c + 4)}
        if t == 2:
            return {"op": "label", "label": A.cstr(a, A.au32(a, c + 4)) or ""}
        return {"op": "type%d" % t}

    def display_list(self, frames, upto):
        """Objects on the stage after frame 'upto': ([sorted objects], background colour or None, labels)."""
        stage, bg, labels = {}, None, []
        for fi in range(min(upto + 1, len(frames))):
            for ctl in frames[fi]:
                op = ctl["op"]
                if op == "place":
                    flags, d = ctl["flags"], ctl["depth"]
                    obj = stage.get(d) if flags & F_MOVE else None
                    if obj is None:
                        if not flags & F_CHAR:
                            continue
                        obj = {"depth": d, "char": ctl["char"], "matrix": IDENTITY, "scale": 0xFFFFFFFF, "bias": 0,
                               "name": "", "clip": NO_CLIP, "born": fi, "events": []}
                        stage[d] = obj
                    if flags & F_CHAR:
                        if obj["char"] != ctl["char"]:
                            obj["born"] = fi                    # another character: its clock starts again
                        obj["char"] = ctl["char"]
                    if flags & F_MATRIX:
                        obj["matrix"] = ctl["matrix"]
                    if flags & F_CXFORM:
                        obj["scale"], obj["bias"] = ctl["scale"], ctl["bias"]
                    if flags & F_NAME:
                        obj["name"] = ctl["name"]
                    if flags & F_CLIP:
                        obj["clip"] = ctl["clip"]
                    obj["events"] = ctl["events"] or obj.get("events", [])
                elif op == "remove":
                    stage.pop(ctl["depth"], None)
                elif op == "background":
                    bg = ctl["color"]
                elif op == "label" and fi == upto:
                    labels.append(ctl["label"])
        return [stage[d] for d in sorted(stage)], bg, labels

    # ---- images
    def image_ids(self):
        """Image ids used by bitmap characters and shape fills: {id: 'bitmap' | 'shape'}."""
        ids = {}
        for cid, c in self.chars.items():
            if c["type"] == "Bitmap":
                ids.setdefault(cid, "bitmap")
            elif c["type"] == "Shape" and c.get("blocks"):
                for b in c["blocks"]:
                    if b["image"]:
                        ids.setdefault(b["image"], "shape")
        return ids

    def png_name(self, image_id):
        raw = self.ents.get(str(image_id + 1))
        if raw is not None and len(raw) < 64:
            return raw.rstrip(b"\0").decode("latin-1")
        return None


def big_directory(path):
    """[(name, offset, size)] of a BIGF/BIG4 file, as stored (header: magic, size LE, count BE, header size BE)."""
    d = open(path, "rb").read()
    count = struct.unpack_from(">I", d, 8)[0]
    p, out = 16, []
    for _ in range(count):
        off, size = struct.unpack_from(">II", d, p)
        p += 8
        e = d.index(b"\0", p)
        out.append((d[p:e].decode("latin-1"), off, size))
        p = e + 1
    return out


def load(path):
    kind, entries = A.read_big(path)
    mv = Movie(os.path.basename(path), entries)
    mv.path, mv.container = path, kind
    return mv
