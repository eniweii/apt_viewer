#!/usr/bin/env python3
"""Run the real Tk viewer on a virtual display and drive it: select root.big, push a screen, turn Input on, send keys and mouse events.
Usage: xvfb-run -a python gui_drive.py <UI folder> [screen name = CarShopMenu]
Needs python3-tk and xvfb (apt-get update; apt-get install -y python3-tk xvfb). Prints what reached the game; exits 1 if a check fails."""
import os
import sys
import tempfile
import types

import _paths

ui = _paths.ui_folder()
screen = sys.argv[2] if len(sys.argv) > 2 else "CarShopMenu"
eng = _paths.engine()
import tkinter as tk

import apt_render
import apt_resources
import apt_runtime
import apt_view
import apt_vm as VM

apt_render.LOCALIZE = eng.localize if eng.language is not None else None
ids = {v: k for k, v in eng.screens.items()}
root = tk.Tk()
tmp = tempfile.mkdtemp(prefix="apt_gui_")                      # the viewer writes its cache, overrides and stubs files here, not into the repo
res = apt_resources.Resources([], cache_dir=os.path.join(tmp, "cache"), override_file=os.path.join(tmp, "overrides.json"))
app = apt_view.App(root, ui, res, "pillow", os.path.join(tmp, "stubs.json"))
app.movie_tree.selection_set("root.big")
app.movie_tree.event_generate("<<TreeviewSelect>>")
root.update()
app.rt_reset()
app.rt.scenario.append({"at": 6, "op": "message", "message": "PUSH", "screenId": ids[screen], "level": 1})
app.rt_step(120)
root.update()
failed = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failed.append(name)


def count(key):
    return app.rt.vm.externals.get("_global." + key, {}).get("count", 0)


key = lambda sym, ch="": types.SimpleNamespace(widget=app.canvas, keysym=sym, char=ch)
check("%s is on screen" % screen, app.rt.root.vm_get("OVERLAY_1") is not VM.UNDEF)
n0 = count("EventTranslator.OnPadDown()") + count("CmdFECarLot.CarHighlighted()")
app.on_key_down(key("Down"))
check("Input off: a key does not reach the game", count("EventTranslator.OnPadDown()") + count("CmdFECarLot.CarHighlighted()") == n0)
app.input_var.set(True)
app.on_input_toggle()
app.on_key_down(key("Down"))
app.on_key_up(key("Down"))
check("Input on: Down reaches the game", count("EventTranslator.OnPadDown()") + count("CmdFECarLot.CarHighlighted()") > n0)
check("a key with no meaning is ignored", not app.keys_held and app.on_key_down(key("Shift_L")) is None and not app.keys_held)
ev = types.SimpleNamespace(x=300, y=200, delta=120)
x, y = app.stage_xy(ev)
_w, _h, sx, sy = apt_render.output_geometry(app.movie, 1.0, app.output_size())
check("canvas pixel -> movie coordinates -> canvas pixel", abs(x * sx * app.scale - app.canvas.canvasx(300)) < 1e-6)
app.canvas.event_generate("<Motion>", x=300, y=200)
app.canvas.event_generate("<ButtonPress-1>", x=300, y=200)
app.canvas.event_generate("<ButtonRelease-1>", x=300, y=200)
root.update()
check("mouse events did not raise script errors", not app.rt.errors)
root.destroy()
sys.exit(1 if failed else 0)
