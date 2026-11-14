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

sys.exit(0 if ok else 1)
