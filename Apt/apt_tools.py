#!/usr/bin/env python3
"""
The tools around the apt viewer in one place.

  python apt_tools.py check                          what the engine feed needs and what is missing (paths, counts)
  python apt_tools.py census --ui <UI folder>        run every movie headless and list the natives the screens call (and which are fed)
  python apt_tools.py engine-map --ui <UI folder>    remake beamng/apt_engine_map.json from nfs.exe (needs pefile + capstone, and nfs.exe in the data folder)
  python apt_tools.py runtime-images --ui <UI folder>   runtime-bound images (placeholders and their TextureHash bindings)
  python apt_tools.py decompile <movie.big>          the decompiled scripts of one movie
  python apt_tools.py exe-addresses                  remake beamng/EXE_ADDRESSES.md and apt_exe_addresses.json from nfs.exe (pefile + capstone)

The engine data folder is Apt/engine_data (--data to use another): see engine_data/README.txt.
"""
import argparse
import collections
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "beamng"), os.path.join(HERE, "..", "common")):
    if p not in sys.path:
        sys.path.insert(0, p)


def cmd_check(a):
    import apt_engine
    paths = apt_engine.find_data(a.data)
    print("engine data folder: %s" % paths["dir"])
    for role, found, text in apt_engine.status(paths):
        print("  %-6s %s %s" % (role, "ok     " if found else "MISSING", text))
    for mod in ("pefile", "capstone"):
        try:
            __import__(mod)
            print("  %-8s ok       (only for the engine-map tool)" % mod)
        except ImportError:
            print("  %-8s MISSING  pip install %s   (only for the engine-map tool)" % (mod, mod))
    print("The viewer runs without any of this; with the map, language tables and attrib dump the natives of the game answer with real data.")


def cmd_census(a):
    import apt_data, apt_engine, apt_movie, apt_runtime
    eng = apt_engine.build_default(a.data)
    if eng is None:
        sys.exit("no apt_engine_map.json found (see: apt_tools.py check)")
    apt_runtime.ENGINE = eng
    calls, args, failed = collections.Counter(), {}, 0
    for p in sorted(glob.glob(os.path.join(a.ui, "*.big"))):
        try:
            rt = apt_runtime.Runtime(apt_movie.load(p))
            rt.step(a.ticks)
        except Exception:
            failed += 1
            continue
        for k, v in rt.vm.externals.items():
            if k.startswith("_global.") and k.endswith("()"):
                calls[k[8:-2]] += 1
                args.setdefault(k[8:-2], v.get("args"))
    print("%d movies run for %d ticks (%d failed). Natives the screens call on their own:" % (len(calls) and len(glob.glob(os.path.join(a.ui, '*.big'))) - failed, a.ticks, failed))
    for k, n in calls.most_common():
        obj, _d, member = k.rpartition(".")
        print("%4d  %-50s %s  args %s" % (n, k, "fed " if (obj, member) in eng.providers else "    ", args[k]))


def cmd_engine_map(a):
    import engine_map
    import apt_engine
    paths = apt_engine.find_data(a.data)
    exe = a.exe or paths["exe"]
    if not exe:
        sys.exit("nfs.exe not found: put it in %s or use --exe" % paths["dir"])
    out = a.out or os.path.join(HERE, "beamng", "apt_engine_map.json")
    argv = ["engine_map.py", exe, a.ui, out]
    if paths["attrib"] and paths["names"]:
        argv += [paths["attrib"], paths["names"]]
    sys.argv = argv
    engine_map.main()


def cmd_runtime_images(a):
    import apt_engine
    import runtime_images
    paths = apt_engine.find_data(a.data)
    out = a.out or os.path.join(HERE, "beamng")
    argv = ["runtime_images.py", a.ui, os.path.join(HERE, "apt_report.json"), paths["map"] or os.path.join(HERE, "beamng", "apt_engine_map.json")]
    if paths["attrib"] and paths["names"]:
        argv += [paths["attrib"], paths["names"]]
    else:
        argv += ["", ""]
    argv.append(out)
    sys.argv = argv
    runtime_images.main()


def cmd_exe_addresses(a):
    import apt_engine
    import exe_addresses
    paths = apt_engine.find_data(a.data)
    exe = a.exe or paths["exe"]
    if not exe:
        sys.exit("nfs.exe not found: put it in %s or use --exe" % paths["dir"])
    sys.argv = ["exe_addresses.py", exe, paths["map"] or os.path.join(HERE, "beamng", "apt_engine_map.json"), os.path.join(HERE, "beamng")]
    exe_addresses.main()


def cmd_decompile(a):
    import apt_movie
    import inventory
    print(inventory.movie_text(inventory.inventory_movie(apt_movie.load(a.movie), log=lambda *x: None)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=None, help="engine data folder (default Apt/engine_data)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    c = sub.add_parser("census"); c.add_argument("--ui", required=True); c.add_argument("--ticks", type=int, default=250); c.set_defaults(fn=cmd_census)
    m = sub.add_parser("engine-map"); m.add_argument("--ui", required=True); m.add_argument("--exe"); m.add_argument("--out"); m.set_defaults(fn=cmd_engine_map)
    r = sub.add_parser("runtime-images"); r.add_argument("--ui", required=True); r.add_argument("--out"); r.set_defaults(fn=cmd_runtime_images)
    x = sub.add_parser("exe-addresses"); x.add_argument("--exe"); x.set_defaults(fn=cmd_exe_addresses)
    d = sub.add_parser("decompile"); d.add_argument("movie"); d.set_defaults(fn=cmd_decompile)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
