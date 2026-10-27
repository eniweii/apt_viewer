#!/usr/bin/env python3
"""
apt_resources.py

Finds what an APT movie refers to in the bun/bin/lzc files:
  - textures: decoded with common/fng_tpk.py, by name first, then by hash
  - dynamic packs (keys only, compressed in place): the key is known, the pixels are not decoded yet
  - APT fonts (apt_font.py)
  - manual choices (apt_overrides.json): "<movie>|<image id>" or "png:<PNG NAME>" -> texture name

Image status:  found | override | dynamic | undecodable | missing | unnamed
RFPK/JDLZ .lzc files are unpacked once into a cache folder, then read like any other file.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
import apt_font
import apt_scan
import fng_tpk


class Resources:
    def __init__(self, res_dirs, cache_dir="apt_cache", override_file="apt_overrides.json", decomp=None, log=print):
        self.log = log
        self.override_file = override_file
        self.overrides = {}
        if os.path.exists(override_file):
            with open(override_file, encoding="utf-8") as fh:
                self.overrides = json.load(fh)
        # key and name index of apt_scan (also knows the keys of dynamic packs)
        self.names, self.keys, self.scan_log = apt_scan.build_index(res_dirs, decomp)
        self.tex = fng_tpk.TextureIndex()
        self.fonts = {}
        os.makedirs(cache_dir, exist_ok=True)
        for d in res_dirs:
            paths = [d] if os.path.isfile(d) else [os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs]
            for path in sorted(paths):
                low = path.lower()
                if not low.endswith((".bun", ".bin", ".lzc")):
                    continue
                real = path
                if low.endswith(".lzc") or open(path, "rb").read(4) in (b"RFPK", b"JDLZ"):
                    data, err = apt_scan.load_resource_file(path, decomp)
                    if data is None:
                        continue
                    if open(path, "rb").read(4) in (b"RFPK", b"JDLZ"):
                        real = os.path.join(cache_dir, os.path.basename(path) + ".unpacked")
                        if not os.path.exists(real) or os.path.getmtime(real) < os.path.getmtime(path):
                            with open(real, "wb") as fh:
                                fh.write(data)
                try:
                    fng_tpk.scan_file(real, self.tex)
                except Exception as e:        # one bad file must not stop the scan
                    log("texture scan failed: %s: %s" % (path, e))
                apt_font.scan_file(real, self.fonts)
        self._images = {}
        self._peeked = {}
        log("textures: %d decodable, %d names, %d keys, %d fonts, %d overrides" % (
            len(self.tex.textures), len(self.names), len(self.keys), len(self.fonts), len(self.overrides)))

    # ---- overrides
    def set_override(self, movie, image_id, texture):
        key = "%s|%d" % (movie.name, image_id)
        if texture:
            self.overrides[key] = texture
        else:
            self.overrides.pop(key, None)
        self._images.pop(key, None)
        self._peeked.pop(key, None)
        with open(self.override_file, "w", encoding="utf-8") as fh:
            json.dump(self.overrides, fh, indent=1, sort_keys=True)

    # ---- images
    def texture_names(self):
        """Every texture name that can be decoded, sorted."""
        return sorted({t.name for t in self.tex.textures})

    def image(self, movie, image_id):
        """{'status','png','texture','pack','image','note'} for an image id of a movie."""
        key = "%s|%d" % (movie.name, image_id)
        info = self._images.get(key)
        if info is None:
            info = self._resolve(movie, image_id, key)
            self._images[key] = info
        return info

    def peek(self, movie, image_id):
        """Status of an image without decoding its pixels (for lists)."""
        key = "%s|%d" % (movie.name, image_id)
        if key in self._images:
            return self._images[key]["status"]
        cached = self._peeked.get(key)
        if cached is None:
            cached = self._resolve(movie, image_id, key, decode=False)["status"]
            self._peeked[key] = cached
        return cached

    def _resolve(self, movie, image_id, key, decode=True):
        png = movie.png_name(image_id)
        override = self.overrides.get(key) or (self.overrides.get("png:" + png.upper()) if png else None)
        info = {"id": image_id, "png": png, "texture": None, "pack": None, "image": None, "status": "unnamed", "note": ""}
        name = override or (os.path.splitext(png)[0] if png else None)
        if not name:
            return info
        info["texture"] = name
        tex = self.tex.find(name, apt_scan.bin_hash)
        if tex is not None:
            info["pack"] = os.path.basename(tex.path) + " / " + tex.tpk
            if not decode:
                info["status"] = "override" if override else "found"
                return info
            try:
                info["image"] = fng_tpk.decode(tex)
                info["status"] = "override" if override else "found"
            except fng_tpk.TpkError as e:
                info["status"], info["note"] = "undecodable", str(e)
            return info
        hits = self.names.get(name.upper(), []) or self.keys.get(apt_scan.bin_hash(name.upper()), []) \
            or self.keys.get(apt_scan.bin_hash(name), [])
        if hits:
            info["status"] = "dynamic"
            info["pack"] = "%s (key %s)" % (hits[0]["file"], hits[0]["hash"])
            info["note"] = "texture is in a pack that is compressed in place; pixels are not decoded yet"
        else:
            info["status"] = "missing"
            info["note"] = "no pack has this name or key"
        return info

    # ---- fonts
    def font_for(self, name):
        """(AptFont, sheet image) for an APT font name, or (None, None)."""
        font = self.fonts.get((name or "").upper())
        if font is None:
            return None, None
        key = "font|" + font.name
        sheet = self._images.get(key)
        if sheet is None:
            tex = self.tex.by_name.get(font.name.upper())
            try:
                sheet = fng_tpk.decode(tex) if tex is not None else False
            except fng_tpk.TpkError:
                sheet = False
            self._images[key] = sheet
        return (font, sheet) if sheet else (None, None)
