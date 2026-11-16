#!/usr/bin/env python3
"""Checks for runtime-bound textures (clip.TextureHash) and script-set text in the renderers, with stubs (no game files).
Run: python test_apt_render.py"""
import sys

from PIL import Image

import apt_render
import apt_resources
import fng_tpk

ok = True


def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# Resources.image_for_hash: by key in the index, a dynamic pack, or missing
class Tex:
    def __init__(self, name, key):
        self.name, self.key, self.path, self.tpk = name, key, "/x/FRONTEND.BUN", "pack"


res = apt_resources.Resources.__new__(apt_resources.Resources)
res._images, res.keys, res.tex = {}, {0x22222222: [{"file": "DYN.BUN", "hash": "0x22222222"}]}, fng_tpk.TextureIndex()
res.tex.add(Tex("HINT_PIC", 0x11111111))
real_decode = fng_tpk.decode
fng_tpk.decode = lambda tex: Image.new("RGBA", (4, 4))
try:
    a = res.image_for_hash(0x11111111)
    check("a hash in the texture index gives the texture", a["status"] == "found" and a["texture"] == "HINT_PIC" and a["image"].size == (4, 4))
    check("the hash may come as a float from the VM", res.image_for_hash(float(0x11111111)) is a)
finally:
    fng_tpk.decode = real_decode
check("a hash of a compressed pack is dynamic", res.image_for_hash(0x22222222)["status"] == "dynamic")
check("an unknown hash is missing", res.image_for_hash(0x33333333)["status"] == "missing")

# the renderer: a clip's TextureHash is lent to unnamed images below it only
r = apt_render.Renderer.__new__(apt_render.Renderer)
r.bound_hash, r.boxes, r.hits = None, False, []
bound = {"status": "found", "image": "BOUND"}
r.res = type("R", (), {"image_for_hash": lambda self, h: dict(bound, hash=h)})()
check("without a binding an unnamed image stays unnamed", r._bound({"status": "unnamed"})["status"] == "unnamed")
r.bound_hash = 77
check("with a binding an unnamed image shows the bound texture", r._bound({"status": "unnamed"}).get("hash") == 77)
check("a named image is not replaced", r._bound({"status": "found", "image": "OWN"})["image"] == "OWN")
r.bound_hash = None

seen = []
mc_outer = type("MC", (), {"vars": {"TextureHash": 123.0}})()
mc_inner = type("MC", (), {"vars": {}})()
mc_nan = type("MC", (), {"vars": {"TextureHash": float("nan")}})()
r._object_draw = lambda *a: seen.append(r.bound_hash)


def nested(*a):
    seen.append(r.bound_hash)
    if a[2]["mc"] is mc_outer:
        r._object(None, None, {"mc": mc_inner}, None, None, [], 0, 0)
        r._object(None, None, {"mc": mc_nan}, None, None, [], 0, 0)


r._object_draw = nested
r._object(None, None, {"mc": mc_outer}, None, None, [], 0, 0)
check("the binding reaches children, a NaN binding is ignored, and it ends with the clip", seen == [123, 123, 123] and r.bound_hash is None)

# text the scripts set replaces the initial text; a field never set keeps it
r.res = type("R", (), {"font_for": lambda self, name: (None, None)})()
movie = type("M", (), {"chars": {}})()
char = {"font_id": 1, "text": "initial", "variable": "", "bounds": (0, 0, 50, 10), "height": 10, "align": 0, "wordwrap": 0, "multiline": 0, "type": "Text"}
layer = Image.new("RGBA", (60, 20))
mc_set = type("MC", (), {"text_value": lambda self: "set by script"})()
info = {}
r._text(layer, movie, char, (1, 0, 0, 1, 0, 0), (1, 1, 1, 1), info, False, mc_set)
check("script-set text is drawn instead of the initial text", info["text"] == "set by script")
info = {}
r._text(layer, movie, char, (1, 0, 0, 1, 0, 0), (1, 1, 1, 1), info, False, type("MC", (), {"text_value": lambda self: None})())
check("a field never set shows its initial text", info["text"] == "initial")

# Skia: a normal draw with zero final alpha is skipped (no pixel changes), a mask draw (record=False) is not
try:
    import apt_render_skia
except ImportError:
    apt_render_skia = None
if apt_render_skia is not None:
    class FakeCanvas:
        def __init__(self):
            self.draws = 0
        def save(self): pass
        def restore(self): pass
        def concat(self, m): pass
        def clipPath(self, p, doAntiAlias=True): pass
        def drawPaint(self, paint): self.draws += 1
        def drawImage(self, *a): self.draws += 1

    def skia_shape(alpha, record):
        s = apt_render_skia.SkiaRenderer.__new__(apt_render_skia.SkiaRenderer)
        s.stats, s.hits, s.boxes = {}, [], False
        cv = FakeCanvas()
        layer = type("L", (), {"canvas": cv})()
        blk = {"triangles": [((0, 0), (4, 0), (4, 4))], "color": 0xFFFFFFFF, "image": 0}
        info = {}
        s._shape(layer, None, {"blocks": [blk]}, (1, 0, 0, 1, 0, 0), (1, 1, 1, alpha), info, record)
        return cv.draws, s.hits
    check("a zero-alpha solid shape is not drawn, its hit is kept", skia_shape(0.0, True)[0] == 0 and len(skia_shape(0.0, True)[1]) == 1)
    check("a faint but visible shape is drawn", skia_shape(0.01, True)[0] == 1)
    check("a zero-alpha mask shape is still drawn", skia_shape(0.0, False)[0] == 1)

# Skia fast images: off by default; paint alpha only for axis-aligned alpha-only tints; premultiplied textures
if apt_render_skia is not None:
    import skia
    fr = apt_render_skia.SkiaRenderer.__new__(apt_render_skia.SkiaRenderer)
    fr.__init__(None)
    check("fast images are off by default", fr.fast_images is False)
    straight = skia.Surface(8, 8).getCanvas()
    turned = skia.Surface(8, 8).getCanvas()
    turned.rotate(10)
    half = (1.0, 1.0, 1.0, 0.5)
    check("off: an alpha tint is the colour matrix", fr._paint(half, straight).getColorFilter() is not None)
    fr.fast_images = True
    p = fr._paint(half, straight)
    check("on, axis-aligned: an alpha tint is the paint alpha", p.getColorFilter() is None and abs(p.getAlphaf() - 0.5) < 1e-6)
    check("on, rotated: the colour matrix stays", fr._paint(half, turned).getColorFilter() is not None)
    check("on: a colour tint keeps the colour matrix in _paint", fr._paint((0.5, 1.0, 1.0, 1.0), straight).getColorFilter() is not None)
    tex = Image.new("RGBA", (4, 4), (200, 100, 50, 128))
    fr.fast_images = False
    plain = fr._sk(tex)
    fr.fast_images = True
    fast = fr._sk(tex)
    check("on: the texture is premultiplied, off: it is not", fast.alphaType() == skia.AlphaType.kPremul_AlphaType and plain.alphaType() == skia.AlphaType.kUnpremul_AlphaType)
    check("both texture forms stay cached side by side", fr._sk(tex) is fast and fr._sk_images[id(tex)][1] is plain)

# Skia GPU renderer: the backend name, and (when a GL context can be made) fast images stays off whatever is set
try:
    import apt_render_skia_gpu
except ImportError:
    apt_render_skia_gpu = None
if apt_render_skia_gpu is not None:
    check("the GPU renderer reports its backend name", apt_render_skia_gpu.SkiaGpuRenderer.backend == "skia-gpu")
    try:
        gpu = apt_render_skia_gpu.SkiaGpuRenderer(None)
    except (ImportError, RuntimeError):
        gpu = None                                       # no glfw or no OpenGL here: only the name was checked
    if gpu is not None:
        gpu.fast_images = True
        check("the GPU renderer ignores fast images", gpu.fast_images is False)

# Colour transform: scale and bias composed down the tree (out = in * scale + bias, an object's own first, then its parents')
import apt_render as AR
root_cx = AR.CX_IDENTITY
child = AR.cx_compose(root_cx, 0xFF000000, 0x00CBCBCB)                      # multiplier 0, bias 0xCB: a flat grey
check("scale 0 + bias 0xCB gives a flat grey", child[:3] == (0.0, 0.0, 0.0) and all(abs(v - 203 / 255.0) < 1e-9 for v in child[4:7]) and child[3] == 1.0 and child[7] == 0.0)
inner = AR.cx_compose((0.5, 0.5, 0.5, 1.0, 0.1, 0.1, 0.1, 0.0), 0xFFFFFFFF, 0x00808080)   # inside a parent that halves and adds 0.1
check("a child bias goes through the parent's multiplier and offset", all(abs(v - (128 / 255.0 * 0.5 + 0.1)) < 1e-9 for v in inner[4:7]))
check("no bias leaves the composed multipliers as before", AR.cx_compose((0.5, 1, 1, 0.5, 0, 0, 0, 0), 0x80FFFFFF, 0)[3] == 0.5 * (128 / 255.0))
check("a multiplier-only cx tuple has no additive part", AR.cx_add((1, 1, 1, 1)) == AR.NO_ADD)
region = Image.new("RGBA", (2, 1), (100, 100, 100, 255))
tinted = AR.Renderer._tint(region, (0.0, 0.0, 0.0, 1.0), (203 / 255.0, 203 / 255.0, 203 / 255.0, 0.0))
check("the Pillow tint adds the bias after the multiplier", tinted.getpixel((0, 0)) == (203, 203, 203, 255))
check("the Pillow tint is unchanged without a bias", AR.Renderer._tint(region, (0.5, 1.0, 1.0, 1.0)).getpixel((0, 0)) == (50, 100, 100, 255))
if apt_render_skia is not None:
    import skia
    import numpy as np
    surf = skia.Surface(1, 1)
    surf.getCanvas().clear(skia.ColorSetARGB(0, 0, 0, 0))
    src = skia.Image.fromarray(np.array([[[200, 10, 10, 255]]], dtype=np.uint8), skia.ColorType.kRGBA_8888_ColorType, skia.AlphaType.kUnpremul_AlphaType)
    surf.getCanvas().drawImage(src, 0, 0, skia.SamplingOptions(), skia.Paint(ColorFilter=apt_render_skia._tint_filter((0.0, 0.0, 0.0, 1.0), (0.5, 0.5, 0.5, 0.0))))
    px = surf.makeImageSnapshot().toarray(colorType=skia.ColorType.kRGBA_8888_ColorType, alphaType=skia.AlphaType.kUnpremul_AlphaType)[0, 0]
    check("the Skia colour matrix adds the bias after the multiplier", all(abs(int(v) - 128) <= 1 for v in px[:3]) and px[3] == 255)
    check("the Skia filter is still None without tint and bias", apt_render_skia._tint_filter((1, 1, 1, 1)) is None)


    class DrawCount:
        def __init__(self):
            self.draws = 0
        def save(self): pass
        def restore(self): pass
        def concat(self, m): pass
        def clipPath(self, p, doAntiAlias=True): pass
        def drawPaint(self, paint): self.draws += 1

    def shape_draws(cx):
        s = apt_render_skia.SkiaRenderer.__new__(apt_render_skia.SkiaRenderer)
        s.stats, s.hits, s.boxes = {}, [], False
        cv = DrawCount()
        blk = {"triangles": [((0, 0), (4, 0), (4, 4))], "color": 0xFFFFFFFF, "image": 0}
        s._shape(type("L", (), {"canvas": cv})(), None, {"blocks": [blk]}, (1, 0, 0, 1, 0, 0), cx, {}, True)
        return cv.draws
    check("zero alpha and no bias is not drawn", shape_draws((1, 1, 1, 0.0, 0, 0, 0, 0)) == 0)
    check("zero alpha multiplier with an alpha bias is still drawn", shape_draws((1, 1, 1, 0.0, 0, 0, 0, 0.5)) == 1)

# Runtime clips keep the placement bias and hand it to the renderer
import apt_runtime
mc = apt_runtime.MC.__new__(apt_runtime.MC)
mc.placed, mc.placed_scale, mc.placed_bias = (1, 0, 0, 1, 0, 0), 0xFF000000, 0x00CBCBCB
mc.set_placed(bias=0x00FEFEFE)
check("a clip keeps and updates its placement bias", mc.placed_bias == 0x00FEFEFE and mc.placed_scale == 0xFF000000)

# Text alignment as in Flash: 0 left, 1 right, 2 centre
import apt_font
glyph = (0, 0, 0, 3, 3, 4, 4, 10, 0, 0)
apt_font._layout = lambda font, text, width: [(10, [(glyph, 0)])]
fake_font = type("F", (), {"line_height": 8, "_crops": {}})()
sheet = Image.new("RGBA", (8, 8), (255, 255, 255, 255))
left = {a: apt_font.render_text(fake_font, sheet, "x", align=a, box_width=100).getbbox()[0] for a in (0, 1, 2)}
check("text align 0 is left, 1 is right, 2 is centre", left[0] < left[2] < left[1] and left[1] > 85)

sys.exit(0 if ok else 1)
