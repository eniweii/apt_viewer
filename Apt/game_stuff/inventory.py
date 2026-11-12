#!/usr/bin/env python3
"""
inventory.py

Reads APT movies and writes what is needed to rebuild their widgets and behavior as Vue components. Nothing is run;
the scripts are only read (decoded, rebuilt as pseudo code with apt_decompile).

  python inventory.py loadingscreen customizecar [more]     uses the paths saved by launch.py
  python inventory.py --all
  python inventory.py --ui UI_DIR --all
  python inventory.py                                       opens a window with the movie list

Output: outputs/beamng/inventory/
  summary.md        all movies together: movie table, widget library use (imports), the state vocabulary (timeline labels),
                    clip events, what the game has to call (defined functions), what the UI calls out to (externals),
                    sound references, text variables, FSCommand strings
  summary.json      the same data
  <movie>.json      one movie: clips, labels, placements, text variables, streams analysis
  <movie>.txt       one movie, readable: clips with labels and children, then the pseudo code of every script

Words used in the report
  defined       functions the movie defines: the interface the game (or a parent movie) calls into the UI
  externals     calls the movie makes that it does not define itself: what the UI asks the game for (guess: a name that is
                defined in another movie of the set is listed as external here too; check the cross movie table)
  hints         name based guesses of what a clip is (button, list, keyboard ...); only a starting point
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APT = os.path.dirname(HERE)
sys.path.insert(0, APT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(APT), "common"))
import outpaths

STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
CALL_RE = re.compile(r'(?<![\w$.])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\(')
ASSIGN_RE = re.compile(r'^\s*([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*|\[[^\]]*\])*)\s*=[^=]')
HANDLER_RE = re.compile(r'\.(on[A-Z][\w$]*)\s*=')
KEYWORDS = {"if", "while", "for", "switch", "function", "return", "Number", "String", "int", "typeof", "else"}
SOUND_RE = re.compile(r"(?i)sound|sfx|snd|audio|voice|music|bank|stinger|beep|whoosh|\.wav|\.mp3|\.ogg")
BUILTIN_METHODS = {
    "gotoAndPlay", "gotoAndStop", "play", "stop", "nextFrame", "prevFrame", "attachMovie", "duplicateMovieClip",
    "removeMovieClip", "getBounds", "hitTest", "localToGlobal", "globalToLocal", "swapDepths", "getDepth",
    "createEmptyMovieClip", "createTextField", "setMask", "startDrag", "stopDrag", "unloadMovie", "loadMovie",
    "push", "pop", "shift", "unshift", "splice", "slice", "join", "concat", "reverse", "sort", "toString", "charAt",
    "charCodeAt", "indexOf", "lastIndexOf", "substr", "substring", "split", "toLowerCase", "toUpperCase", "floor",
    "ceil", "round", "abs", "min", "max", "random", "sin", "cos", "sqrt", "pow", "atan2", "parseInt", "parseFloat",
    "isNaN", "getTimer", "setInterval", "clearInterval", "hasOwnProperty", "addListener", "removeListener",
}
BUILTIN_ROOTS = {"Math", "String", "Number", "Array", "Object", "Key", "Mouse", "Date", "Boolean", "Stage"}
HINTS = [
    ("button", r"btn|button|item|option|choice"),
    ("list/scroller", r"scroll|list|carousel|slider|grid|row"),
    ("tab", r"tab|page"),
    ("keyboard", r"vkb|keyboard|key"),
    ("popup", r"popup|dialog|prompt|confirm|message|msg|alert"),
    ("progress", r"progress|load|meter|gauge|bar"),
    ("text input", r"input|edit|entry|name"),
    ("cursor/focus", r"cursor|focus|highlight|select|pointer"),
    ("tooltip/help", r"tip|help|hint|info|desc"),
]
GLOBAL_ROOTS = ("_root", "_global", "_level0", "_parent")


# ---------------------------------------------------------------- streams
def count_insns(insns):
    n = 0
    for i in insns:
        n += 1
        if i.body:
            n += count_insns(i.body)
    return n


def walk_insns(insns):
    for i in insns:
        yield i
        if i.body:
            yield from walk_insns(i.body)


def unknown_ops(insns):
    return sorted({i.name for i in walk_insns(insns) if i.op is None or i.name.startswith("unknown_")})


def functions_in(insns):
    """[(name, params, body)] of every function defined in the stream, nested ones included."""
    out = []
    for i in walk_insns(insns):
        if i.op in (0x8E, 0x9B):
            out.append((i.args[0][1] if i.args else "", [a[1] for a in i.args[1:]], i.body or []))
    return out


def matching_paren(text, start):
    depth = 0
    for k in range(start, len(text)):
        c = text[k]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return k
    return len(text)


def analyze_stream(insns):
    """Pseudo code and the facts read from it: {'text', 'functions', 'calls', 'gotos', 'handlers', 'assigned', 'strings', 'urls'}."""
    import apt_decompile as AD
    try:
        lines, _assign = AD.decompile(insns)
    except Exception as e:                              # best effort reader: keep what is known
        lines = ["/* decompile failed: %s: %s */" % (type(e).__name__, e)]
    funcs = functions_in(insns)
    text = "\n".join(lines)
    extra = []
    for name, params, body in funcs:                    # functions the decompiler printed no body for
        if name and ("function %s(" % name) not in text:
            try:
                sub, _a = AD.decompile(body)
            except Exception as e:
                sub = ["/* decompile failed: %s */" % e]
            extra.append("function %s(%s) {" % (name, ", ".join(params)))
            extra += ["  " + s for s in sub]
            extra.append("}")
    full = text + ("\n" + "\n".join(extra) if extra else "")
    calls = {}
    for m in CALL_RE.finditer(full):
        callee = m.group(1)
        if callee in KEYWORDS or full[max(0, m.start() - 9):m.start()].endswith("function "):
            continue
        end = matching_paren(full, m.end() - 1)
        args = STR_RE.findall(full[m.end():end])
        c = calls.setdefault(callee, {"count": 0, "args": []})
        c["count"] += 1
        if args and args not in c["args"] and len(c["args"]) < 6:
            c["args"].append(args)
    gotos = sorted({m.group(1) for m in re.finditer(r'goto(?:AndStop|AndPlay)\("((?:[^"\\]|\\.)*)"\)', full)})
    assigned = sorted({m.group(1) for line in full.split("\n") for m in [ASSIGN_RE.match(line)] if m
                       and (m.group(1).split(".")[0].split("[")[0] in GLOBAL_ROOTS)})
    urls = []
    for i in walk_insns(insns):
        if i.op == 0x83:
            urls.append([a[1] for a in i.args])
        elif i.op == 0x8B:
            urls.append(["setTarget"] + [a[1] for a in i.args])
    return {"text": full, "functions": [{"name": n, "params": p} for n, p, _b in funcs], "calls": calls, "gotos": gotos,
            "handlers": sorted(set(HANDLER_RE.findall(full))), "assigned": assigned,
            "strings": sorted(set(STR_RE.findall(full))), "urls": urls}


def callee_parts(name):
    parts = name.split(".")
    return parts[0], parts[-1]


# ---------------------------------------------------------------- movie
def hints_for(names):
    low = " ".join(n.lower() for n in names if n)
    return [k for k, rx in HINTS if re.search(rx, low)]


def timeline_labels(frames):
    return [[c["label"], fi] for fi, fr in enumerate(frames) for c in fr if c["op"] == "label"]


def placements(frames, owner, out):
    """Adds {char: {'names': set, 'parents': set}} for every place control of a timeline."""
    for fr in frames:
        for c in fr:
            if c["op"] == "place" and c["flags"] & 2:
                e = out.setdefault(c["char"], {"names": set(), "parents": set()})
                e["parents"].add(owner)
            if c["op"] == "place" and c.get("name"):
                out.setdefault(c["char"], {"names": set(), "parents": set()})["names"].add(c["name"])


def inventory_movie(mv, streams=None, log=print):
    import apt_actions as AA
    if streams is None:
        try:
            streams = AA.collect_streams(mv)
        except Exception as e:
            log("  streams: %s: %s" % (type(e).__name__, e))
            streams = []
    chars, types = mv.chars, {}
    for c in chars.values():
        types[c["type"]] = types.get(c["type"], 0) + 1
    place = {}
    placements(mv.frames, "root", place)
    for cid, c in chars.items():
        if c["type"] == "Sprite":
            placements(c["frames"], cid, place)
    exp = {}
    for e in mv.exports:
        exp.setdefault(e["id"], []).append(e["name"])

    # stream analysis, by origin
    analysed, clip_events, frame_actions, init_actions = [], {}, {}, {}
    for s in streams:
        a = analyze_stream(s["insns"]) if s["insns"] else {"text": "", "functions": [], "calls": {}, "gotos": [], "handlers": [],
                                                           "assigned": [], "strings": [], "urls": []}
        rec = {"where": s["where"], "offset": s["offset"], "insns": count_insns(s["insns"]), "error": s["error"],
               "unknown_ops": unknown_ops(s["insns"])}
        rec.update({k: a[k] for k in ("functions", "calls", "gotos", "handlers", "assigned", "urls")})
        analysed.append((rec, a["text"], a["strings"]))
        w = s["where"]
        m = re.match(r"(root|sprite \d+) frame (\d+) place depth (\d+) char (\d+) clip (.+)$", w)
        if m:
            clip_events.setdefault(int(m.group(4)), set()).update(m.group(5).split("+"))
        m = re.match(r"(root|sprite (\d+)) frame (\d+)$", w)
        if m:
            owner = int(m.group(2)) if m.group(2) else "root"
            frame_actions.setdefault(owner, []).append(int(m.group(3)))
        m = re.match(r"init sprite (\d+)$", w)
        if m:
            init_actions[int(m.group(1))] = True

    # clips
    clips = []
    for cid, c in sorted(chars.items()):
        if c["type"] != "Sprite":
            continue
        kids = sorted({x["char"] for fr in c["frames"] for x in fr if x["op"] == "place" and x["flags"] & 2})
        pl = place.get(cid, {"names": set(), "parents": set()})
        names = list(exp.get(cid, [])) + sorted(pl["names"])
        labels = timeline_labels(c["frames"])
        clips.append({"id": cid, "frames": len(c["frames"]), "export": exp.get(cid, []), "instance_names": sorted(pl["names"]),
                      "placed_in": sorted(pl["parents"], key=str), "children": kids, "labels": labels,
                      "frame_actions": sorted(frame_actions.get(cid, [])), "init_action": cid in init_actions,
                      "events": sorted(clip_events.get(cid, [])), "hints": hints_for(names)})
    texts = []
    for cid, c in sorted(chars.items()):
        if c["type"] == "Text":
            texts.append({"id": cid, "variable": c["variable"], "text": c["text"], "font": (chars.get(c["font_id"]) or {}).get("name"),
                          "height": c["height"], "multiline": bool(c["multiline"]), "wordwrap": bool(c["wordwrap"]),
                          "instance_names": sorted(place.get(cid, {"names": set()})["names"])})

    # whole movie script facts
    defined, externals, local_calls, gotos, handlers, assigned, urls, sounds = {}, {}, {}, {}, set(), set(), [], []
    for rec, _t, strings in analysed:
        for f in rec["functions"]:
            if f["name"]:
                defined.setdefault(f["name"], {"params": f["params"], "where": []})["where"].append(rec["where"])
        for g in rec["gotos"]:
            gotos.setdefault(g, []).append(rec["where"])
        handlers.update(rec["handlers"])
        assigned.update(rec["assigned"])
        urls += [dict(where=rec["where"], args=u) for u in rec["urls"]]
    defined_names = set(defined)
    for rec, _t, strings in analysed:
        for callee, info in rec["calls"].items():
            root, last = callee_parts(callee)
            if last in defined_names or callee in defined_names:
                bucket = local_calls
            elif last in BUILTIN_METHODS or root in BUILTIN_ROOTS:
                continue
            else:
                bucket = externals
            e = bucket.setdefault(callee, {"count": 0, "args": [], "where": []})
            e["count"] += info["count"]
            e["where"].append(rec["where"])
            for a in info["args"]:
                if a not in e["args"] and len(e["args"]) < 6:
                    e["args"].append(a)
        for s in strings:
            if SOUND_RE.search(s):
                sounds.append({"string": s, "where": rec["where"]})
        for callee in rec["calls"]:
            if SOUND_RE.search(callee):
                sounds.append({"call": callee, "args": rec["calls"][callee]["args"], "where": rec["where"]})
    for u in urls:
        if any(SOUND_RE.search(str(x)) for x in u["args"]):
            sounds.append({"url": u["args"], "where": u["where"]})
    seen_s, uniq = set(), []
    for s in sounds:
        k = json.dumps(s, sort_keys=True)
        if k not in seen_s:
            seen_s.add(k)
            uniq.append(s)

    return {
        "meta": {"name": mv.name, "width": mv.width, "height": mv.height, "ms": mv.ms, "root_frames": len(mv.frames),
                 "char_types": types, "streams": len(streams), "stream_errors": sum(1 for r, _t, _s in analysed if r["error"]),
                 "unknown_ops": sorted({o for r, _t, _s in analysed for o in r["unknown_ops"]})},
        "exports": mv.exports, "imports": mv.imports,
        "root": {"labels": timeline_labels(mv.frames), "frame_actions": sorted(frame_actions.get("root", []))},
        "clips": clips, "texts": texts,
        "script": {"defined": defined, "externals": externals, "local_calls": local_calls, "gotos": gotos,
                   "handlers": sorted(handlers), "assigned_globals": sorted(assigned), "urls": urls, "sounds": uniq},
        "streams": [r for r, _t, _s in analysed],
        "_pseudo": [(r["where"], r["offset"], t) for r, t, _s in analysed],
    }


def movie_text(inv):
    m = inv["meta"]
    out = ["%s  %dx%d  %d ms/frame  %d root frames" % (m["name"], m["width"], m["height"], m["ms"], m["root_frames"]),
           "characters: " + ", ".join("%s %d" % kv for kv in sorted(m["char_types"].items())),
           "streams: %d (errors %d)%s" % (m["streams"], m["stream_errors"],
                                           ("; undecoded ops: " + ", ".join(m["unknown_ops"])) if m["unknown_ops"] else ""),
           ""]
    if inv["imports"]:
        out.append("IMPORTS")
        out += ["  id %d  %s / %s" % (i["id"], i["movie"], i["name"]) for i in inv["imports"]]
        out.append("")
    if inv["exports"]:
        out.append("EXPORTS")
        out += ["  id %d  %s" % (e["id"], e["name"]) for e in inv["exports"]]
        out.append("")
    out.append("ROOT labels: " + (", ".join("%s@%d" % tuple(l) for l in inv["root"]["labels"]) or "none")
               + "   frame actions at: " + (", ".join(map(str, inv["root"]["frame_actions"])) or "none"))
    out.append("")
    out.append("CLIPS")
    for c in inv["clips"]:
        out.append("  sprite %d  frames %d  %s%s" % (c["id"], c["frames"], ("export " + "/".join(c["export"]) + "  ") if c["export"] else "",
                                                     ("as " + "/".join(c["instance_names"])) if c["instance_names"] else ""))
        if c["labels"]:
            out.append("      labels: " + ", ".join("%s@%d" % tuple(l) for l in c["labels"]))
        if c["events"]:
            out.append("      clip events: " + ", ".join(c["events"]))
        if c["frame_actions"] or c["init_action"]:
            out.append("      scripts: frame actions at %s%s" % (c["frame_actions"] or "none", ", init action" if c["init_action"] else ""))
        if c["children"]:
            out.append("      children: " + ", ".join(map(str, c["children"])))
        if c["hints"]:
            out.append("      hints: " + ", ".join(c["hints"]))
    out.append("")
    out.append("TEXT FIELDS")
    for t in inv["texts"]:
        out.append("  char %d  var %r  text %r  font %s  h %g%s" % (t["id"], t["variable"], t["text"], t["font"], t["height"],
                                                                   "  as " + "/".join(t["instance_names"]) if t["instance_names"] else ""))
    out.append("")
    s = inv["script"]
    out.append("DEFINED (called by the game or a parent)")
    out += ["  %s(%s)  [%s]" % (n, ", ".join(d["params"]), "; ".join(d["where"][:2])) for n, d in sorted(s["defined"].items())] or ["  none"]
    out.append("EXTERNAL CALLS (asked of the game)")
    out += ["  %s  x%d  args %s" % (n, d["count"], d["args"][:3]) for n, d in sorted(s["externals"].items())] or ["  none"]
    out.append("GOTO LABELS: " + (", ".join(sorted(s["gotos"])) or "none"))
    out.append("HANDLERS ASSIGNED: " + (", ".join(s["handlers"]) or "none"))
    out.append("GLOBALS WRITTEN: " + (", ".join(s["assigned_globals"]) or "none"))
    out.append("SOUND REFERENCES")
    out += ["  " + json.dumps({k: v for k, v in x.items() if k != "where"}) + "   (" + x["where"] + ")" for x in s["sounds"]] or ["  none"]
    out.append("FSCommand / getUrl / setTarget")
    out += ["  %s   (%s)" % (u["args"], u["where"]) for u in s["urls"]] or ["  none"]
    out.append("")
    out.append("=" * 70)
    out.append("PSEUDO CODE")
    for where, off, text in inv["_pseudo"]:
        out.append("")
        out.append("---- %s  @0x%X" % (where, off))
        out.append(text or "(empty)")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- summary of a set
def summarize(invs):
    movies, imports, labels, events, defined, externals, sounds, texts, urls = [], {}, {}, {}, {}, {}, [], {}, []
    for inv in invs:
        n, m = inv["meta"]["name"], inv["meta"]
        movies.append([n, "%dx%d" % (m["width"], m["height"]), m["root_frames"], len(inv["clips"]), len(inv["texts"]), m["streams"],
                       len(inv["script"]["defined"]), len(inv["script"]["externals"]), len(m["unknown_ops"])])
        for i in inv["imports"]:
            imports.setdefault("%s / %s" % (i["movie"], i["name"]), []).append(n)
        for lab in [l[0] for l in inv["root"]["labels"]] + [l[0] for c in inv["clips"] for l in c["labels"]]:
            labels.setdefault(lab, set()).add(n)
        for c in inv["clips"]:
            for e in c["events"]:
                events.setdefault(e, set()).add(n)
        for f, d in inv["script"]["defined"].items():
            defined.setdefault(f, {"movies": set(), "params": d["params"]})["movies"].add(n)
        for f, d in inv["script"]["externals"].items():
            e = externals.setdefault(f, {"movies": set(), "args": []})
            e["movies"].add(n)
            for a in d["args"]:
                if a not in e["args"] and len(e["args"]) < 6:
                    e["args"].append(a)
        sounds += [dict(movie=n, **s) for s in inv["script"]["sounds"]]
        urls += [dict(movie=n, **u) for u in inv["script"]["urls"]]
        for t in inv["texts"]:
            if t["variable"]:
                texts.setdefault(t["variable"], set()).add(n)
    ls = lambda s: sorted(s)
    return {"movies": movies, "imports": {k: ls(v) for k, v in sorted(imports.items())},
            "labels": {k: ls(v) for k, v in sorted(labels.items())}, "clip_events": {k: ls(v) for k, v in sorted(events.items())},
            "defined": {k: {"movies": ls(v["movies"]), "params": v["params"]} for k, v in sorted(defined.items())},
            "externals": {k: {"movies": ls(v["movies"]), "args": v["args"]} for k, v in sorted(externals.items())},
            "sounds": sounds, "text_variables": {k: ls(v) for k, v in sorted(texts.items())}, "urls": urls}


def summary_md(s):
    short = lambda names: ", ".join(names[:6]) + (" +%d" % (len(names) - 6) if len(names) > 6 else "")
    o = ["# APT inventory", "", "## Movies", "", "| movie | size | root frames | clips | texts | streams | defined | externals | undecoded op kinds |",
         "|---|---|---|---|---|---|---|---|---|"]
    o += ["| " + " | ".join(str(x) for x in r) + " |" for r in s["movies"]]
    o += ["", "## Widget library use (imports)", ""]
    o += ["- `%s`: %s" % (k, short(v)) for k, v in s["imports"].items()] or ["- none"]
    o += ["", "## State vocabulary (timeline labels, by number of movies)", ""]
    o += ["- `%s` (%d): %s" % (k, len(v), short(v)) for k, v in sorted(s["labels"].items(), key=lambda kv: (-len(kv[1]), kv[0]))] or ["- none"]
    o += ["", "## Clip events", ""]
    o += ["- `%s`: %s" % (k, short(v)) for k, v in s["clip_events"].items()] or ["- none"]
    o += ["", "## Defined functions (the interface the game calls)", ""]
    o += ["- `%s(%s)`: %s" % (k, ", ".join(v["params"]), short(v["movies"])) for k, v in s["defined"].items()] or ["- none"]
    o += ["", "## External calls (what the UI asks of the game)", ""]
    o += ["- `%s` %s: %s" % (k, v["args"][:2] if v["args"] else "", short(v["movies"])) for k, v in
          sorted(s["externals"].items(), key=lambda kv: (-len(kv[1]["movies"]), kv[0]))] or ["- none"]
    o += ["", "## Sound references", ""]
    o += ["- %s: %s" % (x["movie"], json.dumps({k: v for k, v in x.items() if k not in ("movie", "where")})) for x in s["sounds"]] or ["- none"]
    o += ["", "## Text variables", ""]
    o += ["- `%s`: %s" % (k, short(v)) for k, v in s["text_variables"].items()] or ["- none"]
    o += ["", "## FSCommand / getUrl / setTarget", ""]
    o += ["- %s: %s" % (x["movie"], x["args"]) for x in s["urls"]] or ["- none"]
    return "\n".join(o) + "\n"


# ---------------------------------------------------------------- running
def write_all(invs, out):
    os.makedirs(out, exist_ok=True)
    for inv in invs:
        base = os.path.join(out, os.path.splitext(inv["meta"]["name"])[0])
        with open(base + ".txt", "w", encoding="utf-8") as fh:
            fh.write(movie_text(inv))
        data = {k: v for k, v in inv.items() if k != "_pseudo"}
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
    s = summarize(invs)
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=1)
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(summary_md(s))
    return s


def run(ui, names, log=print):
    import apt_movie
    from export_movie import find_movies
    paths = find_movies(ui)
    out = outpaths.out_dir("beamng/inventory")
    invs = []
    for n in names:
        key = (n if n.lower().endswith(".big") else n + ".big").lower()
        if key not in paths:
            log("not found: %s" % n)
            continue
        try:
            inv = inventory_movie(apt_movie.load(paths[key]), log=log)
            invs.append(inv)
            m = inv["meta"]
            log("  %s: %d clips, %d texts, %d streams, %d defined, %d externals, %d sound refs%s" % (
                key, len(inv["clips"]), len(inv["texts"]), m["streams"], len(inv["script"]["defined"]),
                len(inv["script"]["externals"]), len(inv["script"]["sounds"]),
                (", undecoded: " + ", ".join(m["unknown_ops"])) if m["unknown_ops"] else ""))
        except Exception as e:
            log("  %s failed: %s: %s" % (n, type(e).__name__, e))
    if invs:
        write_all(invs, out)
        log("done: %d movie(s), report in %s" % (len(invs), out))
    return invs


def window(ui):
    import threading
    import tkinter as tk
    from tkinter import ttk
    from export_movie import find_movies
    paths = find_movies(ui) if ui else {}
    root = tk.Tk()
    root.title("APT inventory")
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
                    run(ui, names, log)
                finally:
                    root.after(0, lambda: btn.state(["!disabled"]))
            threading.Thread(target=work, daemon=True).start()

    row = ttk.Frame(root)
    row.pack(fill=tk.X, padx=6, pady=6)
    btn = ttk.Button(row, text="Inventory selected", command=go)
    btn.pack(side=tk.LEFT)
    ttk.Button(row, text="Inventory all", command=lambda: go(True)).pack(side=tk.LEFT, padx=6)
    if not paths:
        log("No UI folder saved. Run launch.py once or start with --ui.")
    root.mainloop()


def main():
    from export_movie import find_movies, load_settings
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("movies", nargs="*", help="movie names, like loadingscreen")
    ap.add_argument("--all", action="store_true", help="every movie in the UI folder")
    ap.add_argument("--ui", default=None, help="folder with the UI .big files (default: the one saved by launch.py)")
    a = ap.parse_args()
    ui = a.ui or load_settings().get("ui", "")
    if not a.movies and not a.all:
        window(ui)
        return
    if not ui:
        sys.exit("no UI folder: use --ui, or run launch.py once to save the paths")
    run(ui, sorted(os.path.splitext(n)[0] for n in find_movies(ui)) if a.all else a.movies)


if __name__ == "__main__":
    main()
