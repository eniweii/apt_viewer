#!/usr/bin/env python3
"""The natives the screens call that have no provider in apt_engine.py (so the screen gets undefined), with the screens that call them and sample arguments.
Usage: python native_gaps.py <UI folder> [ticks=120] [screen name part ...]"""
import collections
import os
import sys

import _paths

ui = _paths.ui_folder()
ticks = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 120
only = [a.lower() for a in sys.argv[2:] if not a.isdigit()]
eng = _paths.engine()
import apt_movie
import apt_runtime

root = apt_movie.load(os.path.join(ui, "root.big"))
calls = collections.defaultdict(lambda: {"n": 0, "screens": set(), "args": []})
for sid, name in sorted(eng.screens.items()):
    if only and not any(o in name.lower() for o in only):
        continue
    rt = apt_runtime.Runtime(root, scenario=[{"at": 6, "op": "message", "message": "PUSH", "screenId": sid, "level": 1}])
    try:
        rt.step(ticks)
    except Exception:
        continue
    for key, v in rt.vm.externals.items():
        if key.startswith("_global.") and key.endswith("()") and v["kind"] == "call":
            c = calls[key]
            c["n"] += v["count"]
            c["screens"].add(name)
            if v["args"] and len(c["args"]) < 2:
                c["args"].append(v["args"])
fed = {"_global.%s.%s()" % pair for pair in eng.providers}
rows = sorted(((k, c) for k, c in calls.items() if k not in fed), key=lambda kc: kc[0])
for k, c in rows:
    print("%-48s x%-4d %-40s %s" % (k[8:], c["n"], ",".join(sorted(c["screens"])[:3]), str(c["args"])[:50]))
print("%d natives called without a provider, %d with" % (len(rows), len(calls) - len(rows)))
