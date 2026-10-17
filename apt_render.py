#!/usr/bin/env python3
"""
apt_render.py

Draws one frame of an APT movie with Pillow (no Tk), and records what was drawn where, so a viewer can
answer "what is under this pixel". Used by apt_view.py.

What is drawn
  Shape       fill blocks as triangles; textured blocks sample the image with the block matrix
              (shape position -> image pixel); solid blocks use the block colour (RGBA).
  Text        APT font glyphs, scaled to the text height, aligned inside the text bounds.
              The text is the string in the movie (often a ~KEY localisation key) or {variable}.
  Sprite      its first frame, placed with the matrix and colour transform of the parent.
  clipDepth   objects above the mask object, up to the clip depth, are cut to the mask alpha.
  Missing     magenta (no pack has it), cyan (dynamic pack, not decoded), orange (no name entry), red (undecodable).
  Not drawn   buttons, morphs, static text, video, actions, timeline playback. Imported characters are marked.
Colour transform: scale only (ARGB), the bias is ignored.
"""
import math

from PIL import Image, ImageChops, ImageDraw

import apt_font
import apt_movie as M

STATUS_COLORS = {"missing": (255, 0, 255, 150), "dynamic": (0, 220, 255, 150), "unnamed": (255, 160, 0, 150),
                 "undecodable": (255, 60, 60, 150)}


# ---- 2x3 matrices (a, b, c, d, tx, ty): x' = a x + c y + tx, y' = b x + d y + ty
def mmul(p, l):
    pa, pb, pc, pd, px, py = p
    la, lb, lc, ld, lx, ly = l
    return (pa * la + pc * lb, pb * la + pd * lb, pa * lc + pc * ld, pb * lc + pd * ld,
            pa * lx + pc * ly + px, pb * lx + pd * ly + py)


def minv(m):
    a, b, c, d, tx, ty = m
    det = a * d - b * c
    if abs(det) < 1e-9:
        return None
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(ia * tx + ic * ty), -(ib * tx + id_ * ty))


def mapply(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def argb(v):
    return ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0, ((v >> 24) & 255) / 255.0


class Renderer:
    def __init__(self, resources, boxes=False):
        self.res = resources
        self.boxes = boxes
        self.loop_sprites = False       # False: a sprite stays on its last frame, True: it starts again
        self.grid = False               # 100 stage pixel grid
        self.anchors = False            # a cross at the origin of every sprite
        self.labels = False             # instance name or character id next to the anchor
        self.hits = []
        self.stats = {}
        self._text_cache = {}           # (font, text, align, width, wrap) -> glyph image, kept small

    # ---- entry
    def render(self, movie, frame=0, scale=1.0):
        w, h = max(1, int(movie.width * scale)), max(1, int(movie.height * scale))
        objs, bg, labels = movie.display_list(movie.frames, frame)
        r, g, b, a = argb(bg) if bg is not None else (0.2, 0.2, 0.2, 1.0)
        canvas = Image.new("RGBA", (w, h), (int(r * 255), int(g * 255), int(b * 255), 255))
        self.hits, self.stats = [], {}
        self._list(canvas, movie, objs, (scale, 0, 0, scale, 0, 0), (1, 1, 1, 1), [], 0, frame)
        if self.grid:
            self._grid(canvas, scale)
        return canvas, self.hits, labels

    def _grid(self, canvas, scale):
        d = ImageDraw.Draw(canvas)
        step = max(8, int(100 * scale))
        for x in range(0, canvas.width, step):
            d.line((x, 0, x, canvas.height), fill=(255, 255, 255, 40))
            d.text((x + 2, 2), str(int(x / scale)), fill=(255, 255, 255, 120))
        for y in range(0, canvas.height, step):
            d.line((0, y, canvas.width, y), fill=(255, 255, 255, 40))
            d.text((2, y + 2), str(int(y / scale)), fill=(255, 255, 255, 120))

    def render_runtime(self, rt, scale=1.0):
        """Draw the current state of an apt_runtime.Runtime."""
        movie = rt.movie
        w, h = max(1, int(movie.width * scale)), max(1, int(movie.height * scale))
        r, g, b, _a = argb(rt.bg) if rt.bg is not None else (0.2, 0.2, 0.2, 1.0)
        canvas = Image.new("RGBA", (w, h), (int(r * 255), int(g * 255), int(b * 255), 255))
        self.hits, self.stats = [], {}
        self._list(canvas, movie, rt.render_list(), (scale, 0, 0, scale, 0, 0), (1, 1, 1, 1), [], 0, rt.root.frame)
        if self.grid:
            self._grid(canvas, scale)
        return canvas, self.hits

    # ---- display lists
    def _list(self, layer, movie, objs, m, cx, path, depth, clock):
        if depth > 12:
            return
        i = 0
        while i < len(objs):
            o = objs[i]
            clip = o["clip"]
            if clip not in (M.NO_CLIP, 0) and clip > o["depth"]:
                mask = Image.new("RGBA", layer.size, (0, 0, 0, 0))
                self._object(mask, movie, o, m, cx, path, depth, clock, record=False)
                j, inner = i + 1, []
                while j < len(objs) and objs[j]["depth"] <= clip:
                    inner.append(objs[j])
                    j += 1
                part = Image.new("RGBA", layer.size, (0, 0, 0, 0))
                self._list(part, movie, inner, m, cx, path, depth, clock)
                part.putalpha(ImageChops.multiply(part.getchannel("A"), mask.getchannel("A")))
                layer.alpha_composite(part)
                i = j
                continue
            self._object(layer, movie, o, m, cx, path, depth, clock)
            i += 1

    def _object(self, layer, movie, o, m, cx, path, depth, clock, record=True):
        char = movie.chars.get(o["char"])
        mat = mmul(m, o["matrix"])
        oc = argb(o["scale"])
        ccx = (cx[0] * oc[0], cx[1] * oc[1], cx[2] * oc[2], cx[3] * oc[3])
        here = path + ["%s#%d" % (o["name"] or "obj", o["char"])]
        info = {"char": o["char"], "name": o["name"], "path": " > ".join(here), "movie": movie.name}
        if char is None:
            imp = next((i for i in movie.imports if i["id"] == o["char"]), None)
            info.update(kind="import", detail="%s / %s" % (imp["movie"], imp["name"]) if imp else "character not in this movie")
            self._marker(layer, mat, info, record)
            return
        kind = char["type"]
        info["kind"] = kind
        info["mc"] = o.get("mc")
        if kind == "Shape":
            self._shape(layer, movie, char, mat, ccx, info, record)
        elif kind == "Text":
            self._text(layer, movie, char, mat, ccx, info, record)
        elif kind == "Sprite":
            if self.anchors or self.labels:
                ax, ay = mapply(mat, 0, 0)
                d = ImageDraw.Draw(layer)
                if self.anchors:
                    d.line((ax - 5, ay, ax + 5, ay), fill=(0, 255, 255, 255))
                    d.line((ax, ay - 5, ax, ay + 5), fill=(0, 255, 255, 255))
                if self.labels:
                    d.text((ax + 6, ay + 2), o["name"] or "#%d" % o["char"], fill=(0, 255, 255, 255))
            mc = o.get("mc")
            if mc is not None:                              # runtime: the clip keeps its own frame and children
                self._list(layer, movie, mc.render_list(), mat, ccx, here, depth + 1, mc.frame)
            else:
                n = len(char["frames"])
                sf = max(0, clock - o.get("born", 0))
                sf = (sf % n if self.loop_sprites else min(sf, n - 1)) if n else 0
                objs, _bg, _lb = movie.display_list(char["frames"], sf)
                self._list(layer, movie, objs, mat, ccx, here, depth + 1, sf)
        elif kind == "Bitmap":
            self._bitmap(layer, movie, char, mat, ccx, info, record)
        else:
            info["detail"] = "not drawn (%s)" % kind
            self._marker(layer, mat, info, record)

    # ---- leaves
    def _marker(self, layer, mat, info, record):
        x, y = mapply(mat, 0, 0)
        d = ImageDraw.Draw(layer)
        d.rectangle((x - 5, y - 5, x + 5, y + 5), outline=(255, 255, 0, 255))
        d.line((x - 5, y - 5, x + 5, y + 5), fill=(255, 255, 0, 255))
        if record:
            self.hits.append(((x - 6, y - 6, x + 6, y + 6), info))

    def _bitmap(self, layer, movie, char, mat, cx, info, record):
        """A bitmap character placed directly: the image at its own pixel size (a marker box if it has no pixels)."""
        im = self.res.image(movie, char["id"])
        self.stats[im["status"]] = self.stats.get(im["status"], 0) + 1
        info["images"] = [char["id"]]
        w, h = im["image"].size if im["image"] is not None else (32, 32)
        pts = [mapply(mat, x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h))]
        bx, by, rw, rh = self._region(layer, pts)
        if rw > 0 and rh > 0:
            if im["image"] is not None:
                region = self._sample(im["image"], mat, bx, by, rw, rh)
            else:
                region = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
                ImageDraw.Draw(region).polygon([(x - bx, y - by) for x, y in pts], fill=STATUS_COLORS.get(im["status"], (255, 0, 255, 150)))
            if region is not None:
                layer.alpha_composite(self._tint(region, cx), (bx, by))
        if self.boxes:
            ImageDraw.Draw(layer).polygon(pts, outline=(120, 255, 120, 255))
        if record:
            self.hits.append((self._bbox(pts), info))

    def _sample(self, img, mimg, bx, by, w, h):
        inv = minv(mimg)
        if inv is None:
            return None
        data = (inv[0], inv[2], inv[0] * bx + inv[2] * by + inv[4], inv[1], inv[3], inv[1] * bx + inv[3] * by + inv[5])
        return img.transform((w, h), Image.AFFINE, data, Image.BILINEAR)

    @staticmethod
    def _tint(region, rgba):
        if min(rgba) >= 0.999:                              # identity colour transform: nothing to do
            return region
        r, g, b, a = region.split()
        f = lambda ch, k: ch if k >= 0.999 else ch.point(lambda v: int(v * k))
        return Image.merge("RGBA", (f(r, rgba[0]), f(g, rgba[1]), f(b, rgba[2]), f(a, rgba[3])))

    def _region(self, layer, pts):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        bx0, by0 = max(0, int(math.floor(min(xs)))), max(0, int(math.floor(min(ys))))
        bx1, by1 = min(layer.width, int(math.ceil(max(xs)))), min(layer.height, int(math.ceil(max(ys))))
        return bx0, by0, bx1 - bx0, by1 - by0

    def _shape(self, layer, movie, char, mat, cx, info, record):
        blocks = char.get("blocks")
        images = []
        if not blocks:                                  # geometry layout not understood: bounds only
            l, t, r, b = char["bounds"]
            pts = [mapply(mat, l, t), mapply(mat, r, t), mapply(mat, r, b), mapply(mat, l, b)]
            ImageDraw.Draw(layer).polygon(pts, outline=(120, 255, 120, 255))
            info["detail"] = "geometry not decoded, bounds only"
            if record:
                self.hits.append((self._bbox(pts), info))
            return
        allpts = []
        for blk in blocks:
            polys = [[mapply(mat, x, y) for x, y in tri] for tri in blk["triangles"]]
            pts = [p for poly in polys for p in poly]
            allpts += pts
            bx, by, w, h = self._region(layer, pts)
            if w <= 0 or h <= 0:
                continue
            mask = Image.new("L", (w, h), 0)
            dm = ImageDraw.Draw(mask)
            for poly in polys:
                dm.polygon([(x - bx, y - by) for x, y in poly], fill=255)
            c = blk["color"]
            tint = (((c >> 24) & 255) / 255.0, ((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0)
            tint = tuple(tint[k] * cx[k] for k in range(4))
            region = None
            if blk["image"]:
                images.append(blk["image"])
                im = self.res.image(movie, blk["image"])
                self.stats[im["status"]] = self.stats.get(im["status"], 0) + 1
                if im["image"] is not None:
                    tinv = minv(blk["matrix"])
                    if tinv is not None:
                        region = self._sample(im["image"], mmul(mat, tinv), bx, by, w, h)
                if region is None:
                    col = STATUS_COLORS.get(im["status"], (255, 0, 255, 150))
                    region = Image.new("RGBA", (w, h), col)
                    tint = (1, 1, 1, cx[3])
            else:
                region = Image.new("RGBA", (w, h), (255, 255, 255, 255))
            region = self._tint(region, tint)
            region.putalpha(ImageChops.multiply(region.getchannel("A"), mask))
            layer.alpha_composite(region, (bx, by))
        info["images"] = images
        if self.boxes and allpts:
            ImageDraw.Draw(layer).rectangle(self._bbox(allpts), outline=(120, 255, 120, 255))
        if record and allpts:
            self.hits.append((self._bbox(allpts), info))

    @staticmethod
    def _bbox(pts):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def _text(self, layer, movie, char, mat, cx, info, record):
        font_char = movie.chars.get(char["font_id"])
        font, sheet = self.res.font_for(font_char["name"] if font_char else None)
        text = char["text"] or ("{%s}" % char["variable"] if char["variable"] else "")
        text = text.replace("^", "\n").replace("\\n", "\n")          # ^ is a line break in the game strings
        l, t, r, b = char["bounds"]
        corners = [mapply(mat, l, t), mapply(mat, r, t), mapply(mat, r, b), mapply(mat, l, b)]
        info.update(text=text, variable=char["variable"], font=font_char["name"] if font_char else None, height=char["height"])
        if font is None or not text:
            info["detail"] = "font not found" if text else "empty text"
            if self.boxes or font is None:
                ImageDraw.Draw(layer).polygon(corners, outline=(255, 120, 120, 255))
            if record:
                self.hits.append((self._bbox(corners), info))
            return
        scale = char["height"] / font.nominal if char["height"] > 0 else 1.0
        align = char["align"] if char["align"] in (0, 1, 2) else 0
        bw, wrap = max(1, int((r - l - 4) / scale)), bool(char["wordwrap"] or char["multiline"])
        key = (id(font), text, align, bw, wrap)
        img = self._text_cache.get(key)
        if img is None:
            if len(self._text_cache) > 256:
                self._text_cache.clear()
            img = self._text_cache[key] = apt_font.render_text(font, sheet, text, align=align, box_width=bw, wrap=wrap)
        mimg = mmul(mat, (scale, 0, 0, scale, l + 2 - 2 * scale, t + 2 - 2 * scale))
        pts = [mapply(mimg, x, y) for x, y in ((0, 0), (img.width, 0), (img.width, img.height), (0, img.height))]
        bx, by, w, h = self._region(layer, pts)
        if w > 0 and h > 0:
            region = self._sample(img, mimg, bx, by, w, h)
            if region is not None:
                c = argb(char["color"])
                region = self._tint(region, (c[0] * cx[0], c[1] * cx[1], c[2] * cx[2], c[3] * cx[3]))
                layer.alpha_composite(region, (bx, by))
        if self.boxes:
            ImageDraw.Draw(layer).polygon(corners, outline=(120, 255, 120, 255))
        if record:
            self.hits.append((self._bbox(corners), info))
