#!/usr/bin/env python3
"""
export_movie.py

Exports APT movies for the browser / BeamNG UI (HTML + JS). Everything BeamNG's UI needs is a plain file:
no bin, tpk or big reads at run time.

  python export_movie.py loadingscreen [more movies]      uses the paths saved by launch.py
  python export_movie.py --all
  python export_movie.py loadingscreen --ui UI_DIR --res FRONTEND_DIR --res GLOBAL_DIR
  python export_movie.py                                  opens a window with the movie list

Output: outputs/beamng/<movie>/
  index.html, apt_player.js   the player (open index.html in a browser, no server needed)
  movie.js                    the movie as  window.APT_MOVIE = {...}  (for index.html, works from file://)
  movie.json                  the same data as plain JSON
  streams.json                every decoded action stream, keyed by file offset (for the runtime, not used by the player yet)
  textures/<imageId>.png      decoded images; no file for images without pixels (the player draws the status colour)
  fonts/<FONT_NAME>.png       APT font sheets; glyph tables are inside movie.json

movie.json
  meta     name, width, height, ms (per frame), endian, counts
  chars    id -> {type, ...}   Shape {bounds, blocks [{color, image, matrix, tris}]}; the matrix is the "Fit textures" one
           Text {bounds, font, align, color, height, multiline, wordwrap, text, variable}; Sprite {frames}; Bitmap {}
  frames   root timeline; a frame is a list of controls: place, remove, action {stream}, initaction {sprite, stream}, background, label
  images   id -> {status, png, texture, pack, note, file, w, h}
  fonts    FONT NAME (upper) -> {status, file, nominal, ascent, descent, lineHeight, first, last, table, glyphs}
  imports, exports
"""
import argparse
import importlib
import json
import math
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APT = os.path.dirname(HERE)
sys.path.insert(0, APT)
sys.path.insert(0, os.path.join(os.path.dirname(APT), "common"))
import outpaths

FORMAT = 1
PLAYER_FILES = ("index.html", "apt_player.js")


def clean(o):
    """JSON safe copy: tuples become lists, non finite numbers 0, floats rounded to 5 decimals."""
    if isinstance(o, float):
        return round(o, 5) if math.isfinite(o) else 0
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def safe_name(name):
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)


def _control(c):
    c = {k: v for k, v in c.items() if k != "ptr"} if c["op"] not in ("action", "initaction") else dict(c)
    if c["op"] in ("action", "initaction"):
        c["stream"] = c.pop("ptr")
    return c


def _insn(i):
    return [i.offset, i.name, [list(a) for a in i.args], [_insn(b) for b in i.body] if i.body is not None else None,
            i.target, i.note]


def export_movie(mv, res, out, streams=None, log=print):
    """Writes one movie to the folder 'out'. mv = apt_movie.Movie, res = apt_resources.Resources.
    streams = collected action streams (default: apt_actions.collect_streams(mv))."""
    import apt_render
    os.makedirs(os.path.join(out, "textures"), exist_ok=True)

    # ---- images
    images, counts = {}, {}
    for iid in sorted(mv.image_ids()):
        info = res.image(mv, iid)
        entry = {"status": info["status"], "png": info["png"], "texture": info["texture"], "pack": info["pack"],
                 "note": info["note"], "file": None}
        im = info["image"]
        if im is not None:
            entry["file"] = "textures/%d.png" % iid
            entry["w"], entry["h"] = im.width, im.height
            im.convert("RGBA").save(os.path.join(out, entry["file"]))
        counts[info["status"]] = counts.get(info["status"], 0) + 1
        images[str(iid)] = entry

    # ---- fonts
    fonts = {}

    def font_entry(name):
        if not name or name.upper() in fonts:
            return
        font, sheet = res.font_for(name)
        if font is None:
            fonts[name.upper()] = {"status": "missing"}
            return
        os.makedirs(os.path.join(out, "fonts"), exist_ok=True)
        fn = "fonts/%s.png" % safe_name(font.name)
        sheet.convert("RGBA").save(os.path.join(out, fn))
        fonts[name.upper()] = {"status": "found", "name": font.name, "file": fn, "nominal": font.nominal,
                               "ascent": font.ascent, "descent": font.descent, "lineHeight": font.line_height,
                               "first": font.first, "last": font.last, "table": list(font.table),
                               "glyphs": [list(g) for g in font.glyphs]}

    # ---- characters
    chars = {}
    for cid, c in sorted(mv.chars.items()):
        t = c["type"]
        o = {"type": t}
        if t == "Shape":
            o["bounds"] = c["bounds"]
            blocks = c.get("blocks")
            if blocks is None:
                o["blocks"] = None
            else:
                o["blocks"] = []
                for b in blocks:
                    m = b["matrix"]
                    img = images.get(str(b["image"])) if b["image"] else None
                    if img and img.get("w"):
                        m = apt_render.fit_matrix(b, (img["w"], img["h"]))
                    flat = [v for tri in b["triangles"] for p in tri for v in p]
                    o["blocks"].append({"color": b["color"], "image": b["image"], "matrix": m, "tris": flat})
        elif t == "Text":
            fc = mv.chars.get(c["font_id"])
            o.update({k: c[k] for k in ("bounds", "align", "color", "height", "readonly", "multiline", "wordwrap",
                                        "text", "variable")})
            o["font"] = fc["name"] if fc else None
            font_entry(o["font"])
        elif t == "Sprite":
            o["frames"] = [[_control(x) for x in fr] for fr in c["frames"]]
        elif t == "Font":
            o["name"] = c.get("name")
        chars[str(cid)] = o

    # ---- streams
    if streams is None:
        try:
            import apt_actions
            streams = apt_actions.collect_streams(mv)
        except Exception as e:                              # the picture does not need the scripts
            log("  streams: %s" % e)
            streams = []
    stream_out = {str(s["offset"]): {"where": s["where"], "error": s["error"], "insns": [_insn(i) for i in s["insns"]]}
                  for s in streams}

    meta = {"format": FORMAT, "name": mv.name, "width": mv.width, "height": mv.height, "ms": mv.ms,
            "endian": getattr(mv, "endian", "<"), "images": counts, "streams": len(stream_out),
            "stream_errors": sum(1 for s in streams if s["error"] or any(i.op is None for i in s["insns"]))}
    movie = clean({"meta": meta, "chars": chars, "frames": [[_control(x) for x in fr] for fr in mv.frames],
                   "images": images, "fonts": fonts, "imports": mv.imports, "exports": mv.exports})
    text = json.dumps(movie, separators=(",", ":"))
    with open(os.path.join(out, "movie.json"), "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(out, "movie.js"), "w", encoding="utf-8") as fh:
        fh.write("window.APT_MOVIE=" + text + ";\n")
    with open(os.path.join(out, "streams.json"), "w", encoding="utf-8") as fh:
        json.dump(clean(stream_out), fh, separators=(",", ":"))
    for f in PLAYER_FILES:
        shutil.copyfile(os.path.join(HERE, "player", f), os.path.join(out, f))
    log("  %s: %dx%d, %d characters, %d root frames, images %s, fonts %d (%d missing), streams %d"
        % (mv.name, mv.width, mv.height, len(chars), len(mv.frames),
           ", ".join("%s %d" % kv for kv in sorted(counts.items())) or "none", len(fonts),
           sum(1 for f in fonts.values() if f["status"] != "found"), len(stream_out)))
    return movie


def write_index(root):
    """outputs/beamng/index.html: links to every exported movie."""
    names = sorted(d for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "movie.js")))
    rows = "".join('<li><a href="%s/index.html">%s</a></li>' % (n, n) for n in names)
    with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><meta charset=utf-8><title>APT movies</title>"
                 "<body style='font:15px sans-serif;background:#222;color:#ddd'><h3>Exported movies</h3><ul>%s</ul>" % rows)


def find_movies(ui):
    paths = {}
    if os.path.isfile(ui):
        paths[os.path.basename(ui).lower()] = ui
    else:
        for r, _, fs in os.walk(ui):
            for f in fs:
                if f.lower().endswith(".big"):
                    paths.setdefault(f.lower(), os.path.join(r, f))
    return paths


def load_settings():
    path = os.path.join(outpaths.out_dir("apt"), "launch_settings.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def make_resources(res_dirs, decomp):
    import apt_resources
    fn = None
    try:
        mod, name = (decomp or "fng_jdlz:decompress").split(":")
        fn = getattr(importlib.import_module(mod), name)
    except (ImportError, AttributeError, ValueError):
        if decomp:
            sys.exit("cannot load --decomp %s" % decomp)
    return apt_resources.Resources(res_dirs, cache_dir=outpaths.out_path("apt", "apt_cache"),
                                   override_file=outpaths.out_path("apt", "apt_overrides.json"), decomp=fn)


def export_many(ui, res_dirs, names, decomp=None, log=print):
    import apt_movie
    paths = find_movies(ui)
    root = outpaths.out_dir("beamng")
    res = make_resources(res_dirs, decomp)
    done = []
    for n in names:
        key = (n if n.lower().endswith(".big") else n + ".big").lower()
        if key not in paths:
            log("not found: %s" % n)
            continue
        try:
            mv = apt_movie.load(paths[key])
            export_movie(mv, res, os.path.join(root, os.path.splitext(key)[0]), log=log)
            done.append(key)
        except Exception as e:                          # one bad movie must not stop the rest
            log("  %s failed: %s: %s" % (n, type(e).__name__, e))
    write_index(root)
    log("done: %d movie(s) in %s" % (len(done), root))
    return done


def window(ui, res_dirs, decomp):
    import threading
    import tkinter as tk
    from tkinter import ttk
    paths = find_movies(ui) if ui else {}
    root = tk.Tk()
    root.title("APT movie export (browser / BeamNG)")
    root.geometry("640x560")
    box = tk.Listbox(root, selectmode=tk.EXTENDED, exportselection=False)
    box.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
    for n in sorted(paths):
        box.insert(tk.END, os.path.splitext(n)[0])
    logbox = tk.Text(root, height=10)
    logbox.pack(fill=tk.X, padx=6)

    def log(s):
        root.after(0, lambda: (logbox.insert(tk.END, s + "\n"), logbox.see(tk.END)))

    def go(everything=False):
        names = [box.get(i) for i in (range(box.size()) if everything else box.curselection())]
        if names:
            btn.state(["disabled"])
            def work():
                try:
                    export_many(ui, res_dirs, names, decomp, log)
                finally:
                    root.after(0, lambda: btn.state(["!disabled"]))
            threading.Thread(target=work, daemon=True).start()

    row = ttk.Frame(root)
    row.pack(fill=tk.X, padx=6, pady=6)
    btn = ttk.Button(row, text="Export selected", command=go)
    btn.pack(side=tk.LEFT)
    ttk.Button(row, text="Export all", command=lambda: go(True)).pack(side=tk.LEFT, padx=6)
    if not paths:
        log("No UI folder saved. Run launch.py once or start with --ui and --res.")
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("movies", nargs="*", help="movie names, like loadingscreen")
    ap.add_argument("--all", action="store_true", help="export every movie in the UI folder")
    ap.add_argument("--ui", default=None, help="folder with the UI .big files (default: the one saved by launch.py)")
    ap.add_argument("--res", action="append", default=None, help="folder with .bun/.bin/.lzc files (repeat)")
    ap.add_argument("--decomp", default=None, help="module:function for JDLZ files (default fng_jdlz:decompress)")
    a = ap.parse_args()
    s = load_settings()
    ui = a.ui or s.get("ui", "")
    res_dirs = a.res if a.res is not None else s.get("res", [])
    decomp = a.decomp or s.get("decomp") or None
    if not a.movies and not a.all:
        window(ui, res_dirs, decomp)
        return
    if not ui:
        sys.exit("no UI folder: use --ui, or run launch.py once to save the paths")
    names = sorted(os.path.splitext(n)[0] for n in find_movies(ui)) if a.all else a.movies
    export_many(ui, res_dirs, names, decomp)


if __name__ == "__main__":
    main()
