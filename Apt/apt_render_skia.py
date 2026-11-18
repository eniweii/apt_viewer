#!/usr/bin/env python3
"""
apt_render_skia.py

Skia (skia-python) drawing backend with the same interface as apt_render.Renderer, so apt_view.py can use either.
Skia draws in compiled code, so a busy screen costs much less than with Pillow. The rules are the same:
  Shape       fill triangles are a clip path, the image is drawn with the block matrix; solid blocks use the block colour
  Text        the Pillow glyph image from apt_font, drawn with the text matrix
  clipDepth   objects above the mask object, up to the clip depth, are drawn in a layer, then cut with the mask (DstIn)
  Colour transform: scale and bias (ARGB), as in apt_render.py: out = in * scale + bias, a Skia colour matrix with offsets
  Hits and stats are recorded the same way (hits come from the same bounding boxes)
  Speed: a normal draw whose final alpha rounds to zero (below half a step of 8 bits) is not drawn; it changes no pixel.
         Hits and stats are still recorded. Mask objects (record=False) are always drawn: a zero-alpha mask still cuts.
  Fast images (renderer.fast_images, off by default, "Fast images" in the viewer): not pixel exact, up to a few steps of 255.
         Textures are kept premultiplied in the surface format (Skia then blits instead of converting every pixel), an alpha-only
         tint is the paint alpha instead of a colour matrix, and a large stretched draw with a colour tint uses a copy of the
         texture tinted once at its own size (these two only when the draw is axis-aligned; rotated draws keep the colour matrix). With it off the output is the same as without these three changes.
Install: pip install skia-python
"""
import numpy as np
import skia
from PIL import Image

import apt_font
import apt_movie as M
import apt_render
from apt_render import argb, mapply, minv, mmul

SAMPLING = skia.SamplingOptions(skia.FilterMode.kLinear)
SKIP_ALPHA = 0.5 / 255.0       # below this a normal draw adds nothing to an 8-bit surface
RGBA = skia.ColorType.kRGBA_8888_ColorType
UNPREMUL = skia.AlphaType.kUnpremul_AlphaType


def _matrix(m):
    """(a, b, c, d, tx, ty) -> skia.Matrix, same meaning: x' = a x + c y + tx, y' = b x + d y + ty."""
    a, b, c, d, tx, ty = m
    return skia.Matrix.MakeAll(a, c, tx, b, d, ty, 0, 0, 1)


TINT_COPY_MIN_AREA = 200000     # fast images: a stretched draw below this many device pixels keeps the colour matrix
TINT_COPY_RATIO = 8             # ... and so does one that covers fewer than this many times the texture pixels
TINT_COPY_BYTES = 64 * 1024 * 1024


def _tint_filter(rgba, add=apt_render.NO_ADD):
    if min(rgba) >= 0.999 and not any(add):
        return None
    return skia.ColorFilters.Matrix([rgba[0], 0, 0, 0, add[0], 0, rgba[1], 0, 0, add[1], 0, 0, rgba[2], 0, add[2], 0, 0, 0, rgba[3], add[3]])


class Layer:
    """A Skia canvas with its size (the base renderer passed a Pillow image here)."""
    def __init__(self, canvas, w, h):
        self.canvas, self.width, self.height = canvas, w, h
        self.size = (w, h)


class SkiaRenderer(apt_render.Renderer):
    backend = "skia"

    def __init__(self, resources, boxes=False):
        super().__init__(resources, boxes)
        self.fast_images = False        # see the module note: premultiplied textures, paint alpha, tinted copies (not pixel exact)
        self._sk_images = {}            # id(Pillow image) -> (Pillow image, skia image); the Pillow image is kept so the id stays valid
        self._sk_fast = {}              # the same for fast images: premultiplied, in the surface format
        self._tint_cache = {}           # (id(Pillow image), r, g, b) -> (Pillow image, tinted skia image)
        self._tint_bytes = 0
        self._font = skia.Font(skia.Typeface("Arial"), 10)

    def _sk(self, pil):
        if self.fast_images:
            return self._sk_premul(pil)
        hit = self._sk_images.get(id(pil))
        if hit is None:
            if len(self._sk_images) > 2000:
                self._sk_images.clear()
            rgba = pil if pil.mode == "RGBA" else pil.convert("RGBA")
            hit = self._sk_images[id(pil)] = (pil, skia.Image.fromarray(np.asarray(rgba), RGBA, UNPREMUL))
        return hit[1]

    def _sk_premul(self, pil):
        """Fast images: the texture premultiplied in the surface format, made once by Skia itself."""
        hit = self._sk_fast.get(id(pil))
        if hit is None:
            if len(self._sk_fast) > 2000:
                self._sk_fast.clear()
                self._tint_cache.clear()
                self._tint_bytes = 0
            rgba = pil if pil.mode == "RGBA" else pil.convert("RGBA")
            raw = skia.Image.fromarray(np.asarray(rgba), RGBA, UNPREMUL)
            surf = skia.Surface(raw.width(), raw.height())
            surf.getCanvas().drawImage(raw, 0, 0)
            hit = self._sk_fast[id(pil)] = (pil, surf.makeImageSnapshot())
        return hit[1]

    @staticmethod
    def _axis_aligned(c):
        """Scale and translate only. Skia blits those fast without a colour matrix; with rotation or skew the matrix is the faster path."""
        m = c.getTotalMatrix()
        return m.getSkewX() == 0 and m.getSkewY() == 0

    def _paint(self, tint, c, add=apt_render.NO_ADD):
        """The paint for a tint (r, g, b, a scale, plus the additive offsets). Fast images: an alpha-only tint on an axis-aligned draw is the paint alpha, no colour matrix."""
        if self.fast_images and not any(add) and min(tint) < 0.999 and min(tint[0], tint[1], tint[2]) >= 0.999 and self._axis_aligned(c):
            p = skia.Paint()
            p.setAlphaf(tint[3])
            return p
        return skia.Paint(ColorFilter=_tint_filter(tint, add))

    def _shape_image(self, c, pil, tint, cover, add=apt_render.NO_ADD):
        """Draw a shape block's image. Fast images: a large stretched draw with a colour tint uses a copy of the texture tinted
        once (cached by the exact tint), then only the alpha is a paint; everything else is _paint."""
        sk = self._sk(pil)
        if self.fast_images and not any(add) and min(tint[0], tint[1], tint[2]) < 0.999 and max(tint[0], tint[1], tint[2]) <= 1.0 and self._axis_aligned(c):
            area = c.getDeviceClipBounds()
            area = min(area.width() * area.height(), cover)             # what the block really covers, not just its bounding box
            px = sk.width() * sk.height()
            if area >= TINT_COPY_MIN_AREA and area >= TINT_COPY_RATIO * px:
                key = (id(pil), tint[0], tint[1], tint[2])
                hit = self._tint_cache.get(key)
                if hit is None:
                    if self._tint_bytes + 4 * px > TINT_COPY_BYTES:
                        self._tint_cache.clear()
                        self._tint_bytes = 0
                    surf = skia.Surface(sk.width(), sk.height())
                    surf.getCanvas().drawImage(sk, 0, 0, skia.SamplingOptions(), skia.Paint(ColorFilter=_tint_filter((tint[0], tint[1], tint[2], 1.0))))
                    hit = self._tint_cache[key] = (pil, surf.makeImageSnapshot())
                    self._tint_bytes += 4 * px
                p = skia.Paint()
                p.setAlphaf(tint[3])
                c.drawImage(hit[1], 0, 0, SAMPLING, p)
                return
        c.drawImage(sk, 0, 0, SAMPLING, self._paint(tint, c, add))

    # ---- entry
    def _surface(self, movie, scale, bg, size=None):
        w, h, sx, sy = apt_render.output_geometry(movie, scale, size)
        r, g, b, _a = argb(bg) if bg is not None else (0.2, 0.2, 0.2, 1.0)
        surface = skia.Surface(w, h)
        surface.getCanvas().clear(skia.ColorSetARGB(255, int(r * 255), int(g * 255), int(b * 255)))
        return surface, Layer(surface.getCanvas(), w, h), sx, sy

    @staticmethod
    def _to_pil(surface):
        arr = surface.makeImageSnapshot().toarray(colorType=RGBA, alphaType=UNPREMUL)
        return Image.fromarray(arr, "RGBA")

    def render(self, movie, frame=0, scale=1.0, size=None):
        objs, bg, labels = movie.display_list(movie.frames, frame)
        surface, layer, sx, sy = self._surface(movie, scale, bg, size)
        self.hits, self.stats = [], {}
        self._list(layer, movie, objs, (sx, 0, 0, sy, 0, 0), apt_render.CX_IDENTITY, [], 0, frame)
        if self.grid:
            self._grid(layer, sx, sy)
        return self._to_pil(surface), self.hits, labels

    def render_runtime(self, rt, scale=1.0, size=None):
        surface, layer, sx, sy = self._surface(rt.stage, scale, rt.bg, size)
        self.hits, self.stats = [], {}
        self._list(layer, rt.movie, rt.render_list(), (sx, 0, 0, sy, 0, 0), apt_render.CX_IDENTITY, [], 0, rt.root.frame)
        if self.grid:
            self._grid(layer, sx, sy)
        return self._to_pil(surface), self.hits

    def _grid(self, layer, sx, sy):
        c = layer.canvas
        line, text = skia.Paint(Color=skia.ColorSetARGB(40, 255, 255, 255)), skia.Paint(Color=skia.ColorSetARGB(120, 255, 255, 255))
        for x in range(0, layer.width, max(8, int(100 * sx))):
            c.drawLine(x, 0, x, layer.height, line)
            c.drawString(str(int(x / sx)), x + 2, 12, self._font, text)
        for y in range(0, layer.height, max(8, int(100 * sy))):
            c.drawLine(0, y, layer.width, y, line)
            c.drawString(str(int(y / sy)), 2, y + 12, self._font, text)

    # ---- display lists
    def _list(self, layer, movie, objs, m, cx, path, depth, clock):
        if depth > 12:
            return
        c = layer.canvas
        i = 0
        while i < len(objs):
            o = objs[i]
            clip = o["clip"]
            if clip not in (M.NO_CLIP, 0) and clip > o["depth"]:
                j, inner = i + 1, []
                while j < len(objs) and objs[j]["depth"] <= clip:
                    inner.append(objs[j])
                    j += 1
                c.saveLayer(None, None)                                         # the masked objects
                self._list(layer, movie, inner, m, cx, path, depth, clock)
                c.saveLayer(None, skia.Paint(BlendMode=skia.BlendMode.kDstIn))  # the mask: keeps what is under its alpha
                self._object(layer, movie, o, m, cx, path, depth, clock, record=False)
                c.restore()
                c.restore()
                i = j
                continue
            self._object(layer, movie, o, m, cx, path, depth, clock)
            i += 1

    def _object(self, layer, movie, o, m, cx, path, depth, clock, record=True):
        """Draw one placed object. A clip the game gave a texture (`TextureHash`, see apt_runtime) lends it to the unnamed images below it."""
        mc = o.get("mc")
        th = mc.vars.get("TextureHash") if mc is not None else None
        prev = self.bound_hash
        if isinstance(th, float) and th == th:
            self.bound_hash = int(th)
        try:
            self._object_draw(layer, movie, o, m, cx, path, depth, clock, record)
        finally:
            self.bound_hash = prev

    def _object_draw(self, layer, movie, o, m, cx, path, depth, clock, record=True):
        mc = o.get("mc")                                    # a clip of the runtime: its own character (the root of a movie that loadMovie brought in has none in `movie`)
        if mc is not None:
            movie = mc.ctx.movie                            # an imported character belongs to its library movie
        char = mc.char if mc is not None and mc.char is not None else movie.chars.get(o["char"])
        mat = mmul(m, o["matrix"])
        ccx = apt_render.cx_compose(cx, o["scale"], o.get("bias", 0))
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
            self._text(layer, movie, char, mat, ccx, info, record, mc)
        elif kind == "Sprite":
            if self.anchors or self.labels:
                ax, ay = mapply(mat, 0, 0)
                cyan = skia.Paint(Color=skia.ColorSetARGB(255, 0, 255, 255))
                if self.anchors:
                    layer.canvas.drawLine(ax - 5, ay, ax + 5, ay, cyan)
                    layer.canvas.drawLine(ax, ay - 5, ax, ay + 5, cyan)
                if self.labels:
                    layer.canvas.drawString(o["name"] or "#%d" % o["char"], ax + 6, ay + 12, self._font, cyan)
            if mc is not None:
                self._list(layer, mc.ctx.movie, mc.render_list(), mat, ccx, here, depth + 1, mc.frame)
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
    @staticmethod
    def _outline(canvas, pts, color):
        p = skia.Path()
        p.moveTo(*pts[0])
        for x, y in pts[1:]:
            p.lineTo(x, y)
        p.close()
        canvas.drawPath(p, skia.Paint(Color=color, Style=skia.Paint.kStroke_Style, AntiAlias=True))

    def _marker(self, layer, mat, info, record):
        x, y = mapply(mat, 0, 0)
        yellow = skia.ColorSetARGB(255, 255, 255, 0)
        p = skia.Paint(Color=yellow, Style=skia.Paint.kStroke_Style)
        layer.canvas.drawRect(skia.Rect(x - 5, y - 5, x + 5, y + 5), p)
        layer.canvas.drawLine(x - 5, y - 5, x + 5, y + 5, p)
        if record:
            self.hits.append(((x - 6, y - 6, x + 6, y + 6), info))

    def _bitmap(self, layer, movie, char, mat, cx, info, record):
        im = self._bound(self.res.image(movie, char["id"]))
        self.stats[im["status"]] = self.stats.get(im["status"], 0) + 1
        info["images"] = [char["id"]]
        w, h = im["image"].size if im["image"] is not None else (32, 32)
        pts = [mapply(mat, x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h))]
        c = layer.canvas
        c.save()
        c.concat(_matrix(mat))
        if im["image"] is not None:
            if not (record and cx[3] + apt_render.cx_add(cx)[3] < SKIP_ALPHA):
                c.drawImage(self._sk(im["image"]), 0, 0, SAMPLING, self._paint(cx[:4], c, apt_render.cx_add(cx)))
        else:
            col = apt_render.STATUS_COLORS.get(im["status"], (255, 0, 255, 150))
            c.drawRect(skia.Rect(0, 0, w, h), skia.Paint(Color=skia.ColorSetARGB(int(col[3] * cx[3]), col[0], col[1], col[2])))
        c.restore()
        if self.boxes:
            self._outline(c, pts, skia.ColorSetARGB(255, 120, 255, 120))
        if record:
            self.hits.append((self._bbox(pts), info))

    def _shape(self, layer, movie, char, mat, cx, info, record):
        c = layer.canvas
        blocks = char.get("blocks")
        images = []
        if not blocks:                                  # geometry layout not understood: bounds only
            l, t, r, b = char["bounds"]
            pts = [mapply(mat, l, t), mapply(mat, r, t), mapply(mat, r, b), mapply(mat, l, b)]
            self._outline(c, pts, skia.ColorSetARGB(255, 120, 255, 120))
            info["detail"] = "geometry not decoded, bounds only"
            if record:
                self.hits.append((self._bbox(pts), info))
            return
        allpts = []
        c.save()
        c.concat(_matrix(mat))                          # blocks are in shape space
        for blk in blocks:
            local_area = 0.0
            p = skia.Path()
            for tri in blk["triangles"]:
                (x0, y0), (x1, y1), (x2, y2) = tri
                if (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0) < 0:     # one winding for all, so overlaps do not cancel
                    x1, y1, x2, y2 = x2, y2, x1, y1
                p.moveTo(x0, y0)
                p.lineTo(x1, y1)
                p.lineTo(x2, y2)
                p.close()
                allpts += [mapply(mat, x, y) for x, y in tri]
                local_area += abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) * 0.5
            col = blk["color"]
            tint = (((col >> 24) & 255) / 255.0, ((col >> 16) & 255) / 255.0, ((col >> 8) & 255) / 255.0, (col & 255) / 255.0)
            tint = tuple(tint[k] * cx[k] for k in range(4))
            add = apt_render.cx_add(cx)
            skip = record and tint[3] + add[3] < SKIP_ALPHA               # invisible: bookkeeping only, no drawing
            c.save()
            c.clipPath(p, doAntiAlias=True)
            if blk["image"]:
                images.append(blk["image"])
                im = self._bound(self.res.image(movie, blk["image"]))
                self.stats[im["status"]] = self.stats.get(im["status"], 0) + 1
                tinv = minv(apt_render.fit_matrix(blk, im["image"].size) if self.fit_textures else blk["matrix"]) if im["image"] is not None else None
                if tinv is not None:
                    c.concat(_matrix(tinv))                                 # image pixels -> shape space
                    if not skip:
                        self._shape_image(c, im["image"], tint, abs(mat[0] * mat[3] - mat[1] * mat[2]) * local_area, add)
                else:
                    sc = apt_render.STATUS_COLORS.get(im["status"], (255, 0, 255, 150))
                    c.drawPaint(skia.Paint(Color=skia.ColorSetARGB(int(sc[3] * cx[3]), sc[0], sc[1], sc[2])))
            elif not skip:
                c.drawPaint(skia.Paint(Color4f=skia.Color4f(tint[0] + add[0], tint[1] + add[1], tint[2] + add[2], tint[3] + add[3])))
            c.restore()
        c.restore()
        info["images"] = images
        if self.boxes and allpts:
            x0, y0, x1, y1 = self._bbox(allpts)
            self._outline(c, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], skia.ColorSetARGB(255, 120, 255, 120))
        if record and allpts:
            self.hits.append((self._bbox(allpts), info))

    def _text(self, layer, movie, char, mat, cx, info, record, mc=None):
        c = layer.canvas
        font_char = movie.chars.get(char["font_id"])
        font, sheet = self.res.font_for(font_char["name"] if font_char else None)
        shown = mc.text_value() if mc is not None else None          # what the scripts put in the field, else the initial text
        text = shown if shown is not None else (char["text"] or ("{%s}" % char["variable"] if char["variable"] else ""))
        if apt_render.LOCALIZE is not None:
            text = apt_render.LOCALIZE(text)
        text = text.replace("^", "\n").replace("\\n", "\n")
        l, t, r, b = char["bounds"]
        corners = [mapply(mat, l, t), mapply(mat, r, t), mapply(mat, r, b), mapply(mat, l, b)]
        info.update(text=text, variable=char["variable"], font=font_char["name"] if font_char else None, height=char["height"])
        if font is None or not text:
            info["detail"] = "font not found" if text else "empty text"
            if self.boxes or font is None:
                self._outline(c, corners, skia.ColorSetARGB(255, 255, 120, 120))
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
        col = argb(char["color"])
        tint = (col[0] * cx[0], col[1] * cx[1], col[2] * cx[2], col[3] * cx[3])
        c.save()
        c.concat(_matrix(mimg))
        if not (record and tint[3] < SKIP_ALPHA):
            ca = apt_render.cx_add(cx)                                  # no alpha offset on text: the glyph box would show
            c.drawImage(self._sk(img), 0, 0, SAMPLING, self._paint(tint, c, (ca[0], ca[1], ca[2], 0.0)))
        c.restore()
        if self.boxes:
            self._outline(c, corners, skia.ColorSetARGB(255, 120, 255, 120))
        if record:
            self.hits.append((self._bbox(corners), info))


def available():
    return True
