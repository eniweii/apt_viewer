#!/usr/bin/env python3
"""Pictures of the viewer to check a change by eye (small PNGs, readable on a phone).
Usage: python shots.py <UI folder> <movie> [options]            (movie: leaderboardscreen; with --push: a screen of root.big, like TreeMenu)
  --frame F            static view at frame F (default 0)            --runtime N   run N ticks of the runtime instead
  --push               <movie> is a screen name: root.big runs and the game pushes it (as gui_drive.py / screen_sweep.py do)
  --keys Down@120,Return@130     keys sent to the game at ticks (Tk key names; pressed at the tick, released one tick later)
  --click 300,200@150            a mouse click at the movie point (x, y) at a tick
  --sheet 0,30,60      contact sheet: one picture per tick (runtime) or frame (static), labelled; --cols 3
  --crop X,Y,W,H       crop of the picture (output pixels)           --zoom K   enlarge the crop K times (pixels kept sharp)
  --size WxH           render size                                    --renderer auto|skia|skia-gpu|pillow     --fast  (Skia fast images)
  --res DIR            resource folder (repeat) or APT_RES (folders separated like PATH)
  --compare OLD_REPO   the same picture made with the Apt/ and common/ of another checkout, stacked: old on top, new below
  --game FILE          your game screenshot next to our picture (same height, labelled game / viewer); --game-crop X,Y,W,H
  --window             a screenshot of the whole Tk viewer on a virtual display (needs python3-tk and xvfb; re-runs itself under xvfb-run)
  --out FILE           default shot.png
The engine feed comes from Apt/engine_data or APT_ENGINE_DATA (see _paths.py). Prints the file and its size; the status line of the viewer in window mode."""
import argparse
import os
import subprocess
import sys
import tempfile

import _paths

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("ui")
ap.add_argument("movie")
ap.add_argument("--frame", type=int, default=0)
ap.add_argument("--runtime", type=int, default=None)
ap.add_argument("--push", action="store_true")
ap.add_argument("--keys", default="")
ap.add_argument("--click", default="")
ap.add_argument("--sheet", default="")
ap.add_argument("--cols", type=int, default=3)
ap.add_argument("--crop", default="")
ap.add_argument("--zoom", type=int, default=1)
ap.add_argument("--size", default="")
ap.add_argument("--renderer", default="auto", choices=("auto", "skia", "skia-gpu", "pillow"))
ap.add_argument("--fast", action="store_true")
ap.add_argument("--res", action="append", default=[])
ap.add_argument("--repo", default="")                               # use this checkout's Apt/ and common/ (set by --compare for the second run)
ap.add_argument("--compare", default="")
ap.add_argument("--game", default="")
ap.add_argument("--game-crop", default="")
ap.add_argument("--window", action="store_true")
ap.add_argument("--out", default="shot.png")
a = ap.parse_args()
a.out = os.path.abspath(a.out)
if a.repo:                                                          # an older state of the repo for before/after pictures
    sys.path[:0] = [os.path.join(a.repo, "Apt"), os.path.join(a.repo, "common")]
if a.window and not os.environ.get("DISPLAY") and not os.environ.get("APT_SHOTS_XVFB"):
    os.environ["APT_SHOTS_XVFB"] = "1"                              # no display: run again on a virtual one
    os.execvp("xvfb-run", ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", sys.executable] + sys.argv)

from PIL import Image, ImageDraw

import apt_input
import apt_movie
import apt_render
import apt_resources
import apt_runtime

ui = _paths.ui_folder()
eng = _paths.engine()
apt_render.LOCALIZE = eng.localize if eng.language is not None else None
res_dirs = a.res or [d for d in os.environ.get("APT_RES", "").split(os.pathsep) if d]
tmp = os.path.join(tempfile.gettempdir(), "apt_shots")             # texture index and overrides stay out of the repo
os.makedirs(tmp, exist_ok=True)
try:
    import fng_jdlz
    decomp = fng_jdlz.decompress
except ImportError:
    decomp = None
res = apt_resources.Resources(res_dirs, cache_dir=os.path.join(tmp, "cache"), override_file=os.path.join(tmp, "overrides.json"), decomp=decomp, log=lambda *x: None)
size = tuple(int(v) for v in a.size.lower().split("x")) if a.size else None
box = lambda s: tuple(int(v) for v in s.split(",")) if s else None


def label(img, text):
    """The picture under a one-line band with the text."""
    band = Image.new("RGB", (img.width, 16), (30, 30, 30))
    ImageDraw.Draw(band).text((4, 2), text, fill=(255, 255, 255))
    out = Image.new("RGB", (img.width, img.height + 16))
    out.paste(band, (0, 0))
    out.paste(img.convert("RGB"), (0, 16))
    return out


def stack(images, vertical=True):
    w = max(i.width for i in images) if vertical else sum(i.width for i in images)
    h = sum(i.height for i in images) if vertical else max(i.height for i in images)
    out = Image.new("RGB", (w + (0 if vertical else 4 * (len(images) - 1)), h + (4 * (len(images) - 1) if vertical else 0)), (255, 0, 0))
    pos = 0
    for i in images:
        out.paste(i, (0, pos) if vertical else (pos, 0))
        pos += (i.height if vertical else i.width) + 4
    return out


def crop_zoom(img):
    if a.crop:
        x, y, w, h = box(a.crop)
        img = img.crop((x, y, x + w, y + h))
    return img.resize((img.width * a.zoom, img.height * a.zoom), Image.NEAREST) if a.zoom > 1 else img


def events():
    """[(tick, kind, args)] from --keys and --click, in tick order."""
    ev = []
    for item in filter(None, a.keys.split(",")):
        name, tick = item.split("@")
        ev.append((int(tick), "key", name))
    for item in filter(None, a.click.split(";")):
        xy, tick = item.split("@")
        ev.append((int(tick), "click", tuple(float(v) for v in xy.split(","))))
    return sorted(ev, key=lambda e: e[0])


def advance(rt, pending, to_tick):
    """Run the runtime to a tick, sending the events that fall on the way."""
    while rt.tick < to_tick:
        while pending and pending[0][0] <= rt.tick + 1:
            _t, kind, arg = pending.pop(0)
            if kind == "key":
                ev = apt_input.key_event(arg, arg if len(arg) == 1 else "")
                if ev is None:
                    sys.exit("no game input for the key %r (see apt_input.KEYSYM_CODES)" % arg)
                rt.key(ev[0], True, ev[1])
                rt.step(1)
                rt.key(ev[0], False, ev[1])
            else:
                rt.mouse("move", *arg)
                rt.mouse("down", *arg)
                rt.mouse("up", *arg)
        rt.step(1)


def make_runtime():
    if a.push:
        ids = {v.lower(): k for k, v in eng.screens.items()}
        if a.movie.lower() not in ids:
            sys.exit("no screen %r in the exe's table (%s ...)" % (a.movie, ", ".join(sorted(eng.screens.values())[:8])))
        movie = apt_movie.load(os.path.join(ui, "root.big"))
        rt = apt_runtime.Runtime(movie, scenario=[{"at": 6, "op": "message", "message": "PUSH", "screenId": ids[a.movie.lower()], "level": 1}])
    else:
        movie = apt_movie.load(os.path.join(ui, a.movie + ".big" if not a.movie.endswith(".big") else a.movie))
        rt = apt_runtime.Runtime(movie)
    rt.text_measure = apt_render.text_measure(res)
    return movie, rt


def make_renderer():
    r = None
    if a.renderer == "skia-gpu":
        import apt_render_skia_gpu
        r = apt_render_skia_gpu.SkiaGpuRenderer(res)
    elif a.renderer in ("auto", "skia"):
        try:
            import apt_render_skia
            r = apt_render_skia.SkiaRenderer(res)
        except ImportError:
            if a.renderer == "skia":
                raise
    r = r or apt_render.Renderer(res)
    r.fast_images = a.fast
    return r


def window():
    """The whole Tk viewer on a virtual display, after the same steps."""
    import tkinter as tk

    import apt_view
    from PIL import ImageGrab
    root = tk.Tk()
    root.geometry("1900x1050+0+0")
    app = apt_view.App(root, ui, res, "skia" if a.renderer == "auto" else a.renderer, os.path.join(tmp, "stubs.json"))
    root.update()
    app.movie_tree.selection_set(("root" if a.push else a.movie.replace(".big", "")) + ".big")
    app.movie_tree.event_generate("<<TreeviewSelect>>")
    root.update()
    if a.runtime is not None or a.push:
        app.rt_var.set(True)
        app.rt_reset()
        if a.push:
            ids = {v.lower(): k for k, v in eng.screens.items()}
            app.rt.scenario.append({"at": 6, "op": "message", "message": "PUSH", "screenId": ids[a.movie.lower()], "level": 1})
        advance(app.rt, events(), a.runtime or 120)
    else:
        app.frame_var.set(a.frame)
    app.redraw()
    root.update()
    root.update_idletasks()
    img = ImageGrab.grab(xdisplay=os.environ["DISPLAY"])
    print("viewer:", app.status.get())
    root.destroy()
    return img


def picture():
    """One picture of the movie, or the sheet."""
    if a.window:
        return window()
    movie, rt = make_runtime() if (a.runtime is not None or a.push) else (apt_movie.load(os.path.join(ui, a.movie + ".big")), None)
    r = make_renderer()
    pending = events()
    marks = [int(v) for v in a.sheet.split(",")] if a.sheet else [a.runtime if rt is not None else a.frame]
    if rt is None and a.keys:
        sys.exit("--keys and --click need --runtime or --push")
    shots = []
    for m in sorted(marks):
        if rt is not None:
            advance(rt, pending, m if m is not None else 120)
            img = r.render_runtime(rt, 1.0, size)[0]
        else:
            img = r.render(movie, m, 1.0, size)[0]
        shots.append(label(crop_zoom(img), ("tick %d" if rt is not None else "frame %d") % m) if a.sheet else crop_zoom(img))
    if len(shots) == 1:
        return shots[0]
    rows = [stack(shots[i:i + a.cols], vertical=False) for i in range(0, len(shots), a.cols)]
    return stack(rows)


out = picture().convert("RGB")
if a.compare:                                                       # the same picture from the other checkout
    old = os.path.join(tmp, "old.png")
    argv = [v for v in sys.argv[1:]]
    for flag in ("--compare", "--out"):
        if flag in argv:
            i = argv.index(flag)
            del argv[i:i + 2]
    subprocess.run([sys.executable, os.path.abspath(__file__)] + argv + ["--repo", a.compare, "--out", old], check=True)
    out = stack([label(Image.open(old), "old: " + a.compare), label(out, "new")])
if a.game:                                                          # the game next to us
    game = Image.open(a.game).convert("RGB")
    if a.game_crop:
        x, y, w, h = box(a.game_crop)
        game = game.crop((x, y, x + w, y + h))
    game = game.resize((max(1, round(game.width * out.height / game.height)), out.height), Image.LANCZOS)
    out = stack([label(game, "game"), label(out, "viewer")], vertical=False)
out.save(a.out)
print("saved %s %dx%d" % (a.out, out.width, out.height))
