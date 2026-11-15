#!/usr/bin/env python3
"""Push every screen of the exe's table through root.big (PUSH message) and run N ticks each: which load, which reach OnEnterComplete, which raise.
Usage: python screen_sweep.py <UI folder> [ticks=80]    (about 25 s for 80 ticks)"""
import os
import sys
import time

import _paths

ui = _paths.ui_folder()
ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 80
eng = _paths.engine()
import apt_movie
import apt_runtime

root = apt_movie.load(os.path.join(ui, "root.big"))
rows, start = [], time.time()
for sid, name in sorted(eng.screens.items()):
    rt = apt_runtime.Runtime(root, scenario=[{"at": 6, "op": "message", "message": "PUSH", "screenId": sid, "level": 1}])
    try:
        rt.step(ticks)
    except Exception as e:                                   # a Python exception is a bug of the VM, not of the script
        rows.append((name, "EXCEPTION " + repr(e)[:70]))
        continue
    events = [e[0] for e in eng.events]
    loaded = bool(rt.load_log and rt.load_log[-1][2])
    rows.append((name, "loaded" if loaded else "no movie file", "constructed" if "OnScreenConstructed" in events else "-",
                 "entered" if "OnEnterComplete" in events else "-", "errors=%d" % len(rt.errors)))
print("%d screens in %.0f s" % (len(rows), time.time() - start))
for r in rows:
    print("  " + " | ".join(str(x) for x in r))
