#!/usr/bin/env python3
"""
apt_inspect.py

Builds the text of the viewer's inspection views. No Tk here, so it can be tested alone.

Evidence tags used in the text
  [file]        decoded from the apt/const/BIG data: values, operations, names, offsets
  [inferred]    meaning given by this program (scope rules, event order, NaN rule); see apt_runtime.py / apt_vm.py
  [art]         pixels recovered from the texture packs (not a recreation)
  [unsupported] could not be decoded or run; listed, never guessed
"""
import re

import apt_actions as AA
import apt_decompile as D
import apt_movie as M
import apt_render
import apt_vm as VM


def streams_of(movie):
    """collect_streams plus decompiled text and the assignment map {offset: statement}."""
    out = AA.collect_streams(movie)
    assign = {}
    for s in out:
        s["code"], a = D.decompile(s["insns"])
        assign.update(a)
        s["unknown"] = _count_unknown(s["insns"])
        s["n"] = _count(s["insns"])
    return out, assign


def _count(insns):
    return sum(1 + (_count(i.body) if i.body else 0) for i in insns)


def _count_unknown(insns):
    return sum((1 if i.op is None else 0) + (_count_unknown(i.body) if i.body else 0) for i in insns)


# ---- archive
def archive_text(movie):
    users = {}
    for cid, c in movie.chars.items():
        if c["type"] == "Bitmap":
            users.setdefault(cid, []).append("bitmap char %d" % cid)
        elif c["type"] == "Shape" and c.get("blocks"):
            for b in c["blocks"]:
                if b["image"]:
                    users.setdefault(b["image"], []).append("shape %d" % cid)
    geom = {c["geometry_id"] + 1: cid for cid, c in movie.chars.items() if c["type"] == "Shape"}
    lines = ["[file] %s  container %s" % (movie.name, movie.container), ""]
    lines.append("%-6s %-10s %-8s %s" % ("entry", "offset", "size", "what"))
    for name, off, size in M.big_directory(movie.path):
        what = ""
        if name == "0":
            what = "APT movie data (%d bytes), stage %dx%d" % (size, movie.width, movie.height)
        elif name == "1":
            what = "constant table (%d constants)" % len(AA.read_consts(movie.const)[1])
        elif name.isdigit() and int(name) - 1 in users or name.isdigit() and movie.png_name(int(name) - 1):
            png = movie.png_name(int(name) - 1)
            who = ", ".join(sorted(set(users.get(int(name) - 1, []))))
            what = "image id %d: %s   used by %s" % (int(name) - 1, png, who or "(no character found)")
        elif name.isdigit() and int(name) in geom:
            what = "geometry of shape %d" % geom[int(name)]
        elif name.isdigit():
            what = "[unsupported] not claimed by any character"
        lines.append("%-6s 0x%08X %-8d %s" % (name, off, size, what))
    return "\n".join(lines)


# ---- timeline
def timeline_text(movie, streams):
    fps = 1000.0 / movie.ms if movie.ms else 0
    lines = ["[file] stage %dx%d, %d root frames, %d ms per frame (%.1f fps)" % (
        movie.width, movie.height, len(movie.frames), movie.ms, fps), ""]
    where = {}
    for s in streams:
        where[s["offset"]] = s["where"]
    for fi, ctls in enumerate(movie.frames):
        if not ctls:
            continue
        parts = []
        for c in ctls:
            op = c["op"]
            if op == "label":
                parts.append('label "%s"' % c["label"])
            elif op == "place":
                f = c["flags"]
                kind = "add" if f & M.F_CHAR and not f & M.F_MOVE else "move" if f & M.F_MOVE else "place"
                ch = movie.chars.get(c["char"])
                what = "" if not f & M.F_CHAR else " char %d%s" % (c["char"], " (%s)" % ch["type"] if ch else " (import)")
                parts.append("%s depth %d%s%s%s" % (kind, c["depth"], what, " matrix" if f & M.F_MATRIX else "",
                                                   ' name "%s"' % c["name"] if c["name"] else ""))
            elif op == "remove":
                parts.append("remove depth %d" % c["depth"])
            elif op == "action":
                parts.append("DoAction @0x%X" % c["ptr"])
            elif op == "initaction":
                parts.append("InitAction sprite %d @0x%X" % (c["sprite"], c["ptr"]))
            elif op == "background":
                parts.append("background 0x%08X" % c["color"])
        lines.append("frame %2d: %s" % (fi, "; ".join(parts)))
    lines += ["", "sprites (character, frames, scripts):"]
    for cid, c in sorted(movie.chars.items()):
        if c["type"] == "Sprite":
            sc = [s["where"] for s in streams if s["where"].startswith("sprite %d " % cid) or s["where"] == "init sprite %d" % cid]
            lines.append("  %3d  %3d frame(s)  %s" % (cid, len(c["frames"]), "; ".join(sc)))
    return "\n".join(lines)


# ---- scripts and behavior
def script_listing(streams):
    return ["%s  @0x%X  %d insn%s" % (s["where"], s["offset"], s["n"], "  [unsupported %d]" % s["unknown"] if s["unknown"] else "")
            for s in streams]


def disassembly_text(stream):
    return "[file] %s @0x%X\n\n%s" % (stream["where"], stream["offset"], "\n".join(AA.disassemble(stream["insns"])))


def outline(stream):
    """Short outline of a handler, read from its pseudo-code: config values, gates, property writes, calls."""
    cfg, gates, writes, calls = [], [], [], []
    for l in stream["code"]:
        t = l.strip()
        m = re.match(r"^(\w+) = ([-\d.]+);$", t)
        if m:
            cfg.append("%s = %s" % (m.group(1), m.group(2)))
        elif t.startswith("if ("):
            gates.append(t[:-2] if t.endswith(" {") else t)
        m = re.match(r"^(r\d+|[\w.]+)\.(_x|_y|_alpha|_xscale|_yscale|_rotation|_visible)\b", t)
        if m and "=" in t:
            writes.append(m.group(2))
        for c in re.findall(r"(\b[\w.]+\.(?:gotoAndStop|gotoAndPlay|stop|play)|\bMath\.\w+|\b\w+\.On\w+|\b\w+\.\w*State\w*)\(", t):
            calls.append(c)
    out = []
    if cfg:
        out.append("  config assigned: " + ", ".join(cfg))
    if gates:
        out.append("  branches: " + " | ".join(dict.fromkeys(gates)))
    if writes:
        out.append("  property writes: " + ", ".join(dict.fromkeys(writes)))
    if calls:
        out.append("  calls: " + ", ".join(dict.fromkeys(calls)))
    return out


def behavior_text(movie, streams, rt=None):
    lines = ["Behavior (from the decoded action streams)",
             "  [file] operations, names and constants    [inferred] how they run (see Runtime evidence below)    "
             "[unsupported] marked in the code", ""]
    for s in streams:
        has_fn = any(i.body is not None for i in s["insns"])
        sets = any(i.op in (0x1D, 0x4F, 0xA6, 0xA7) for i in s["insns"])
        if not (has_fn or sets or s["unknown"]):
            continue
        lines.append("== %s  @0x%X  [file]" % (s["where"], s["offset"]))
        lines += outline(s)
        lines.append("")
        lines += ["    " + l for l in s["code"]]
        lines.append("")
    if rt is not None:
        lines += runtime_evidence(rt)
    return "\n".join(lines)


def runtime_evidence(rt):
    lines = ["Runtime evidence (seed %d, tick %d)" % (rt.seed, rt.tick)]
    lines.append("  [inferred] rules applied: " + ("; ".join("%s x%d" % kv for kv in rt.vm.inferred.items()) or "none yet"))
    lines.append("  [unsupported] calls with no definition in the movie: " +
                 ("; ".join("%s x%d" % kv for kv in rt.vm.unsupported.items()) or "none"))
    ext = [k for k in rt.vm.externals]
    lines.append("  [stub] answers from apt_stubs.json: " + ("; ".join("%s x%d" % kv for kv in rt.vm.stub_hits.items()) or "none"))
    lines.append("  [stub] scenario actions run: %d    names read or called with no definition and no stub: %d" %
                 (len(rt.stub_log), len([k for k in ext if k not in rt.vm.stubs])))
    if rt.errors:
        lines.append("  [unsupported] stopped handlers: " + "; ".join("%s x%d" % kv for kv in rt.errors.items()))
    lines.append("  Math.random() is a seeded generator (mulberry32), a repeatable stand-in, not the game's generator.")
    return lines


# ---- runtime inspector
def _compose(chain):
    m = apt_render.mmul
    out = (1, 0, 0, 1, 0, 0)
    for mc in chain:
        out = m(out, mc.matrix())
    return out


def _fmt_m(m):
    return "a %.4f  b %.4f  c %.4f  d %.4f  tx %.2f  ty %.2f" % m


def _fmt_v(v):
    if isinstance(v, VM.Func):
        return "function @0x%X" % v.offset
    if isinstance(v, VM.Obj):
        return "[object %s]" % (v.name or "")
    if hasattr(v, "path"):
        return "clip " + v.path()
    return VM.to_str(v)


def shape_lines(char, world):
    """Where the geometry of a shape sits against its own origin (a shape turns around the origin of its clip)."""
    l, t, r, b = char["bounds"]
    lines = ["", "shape geometry [file]  (shape space, the clip origin is 0,0)",
             "  declared bounds   x %.1f .. %.1f   y %.1f .. %.1f" % (l, r, t, b)]
    pts = [p for blk in (char.get("blocks") or []) for tri in blk["triangles"] for p in tri]
    if not pts:
        return lines + ["  fill blocks not decoded: bounds only"]
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    lines.append("  triangle extent   x %.1f .. %.1f   y %.1f .. %.1f   (%d blocks)" % (x0, x1, y0, y1, len(char["blocks"])))
    lines.append("  origin is %s the triangles   centre of triangles (%.1f, %.1f)" % (
        "inside" if x0 <= 0 <= x1 and y0 <= 0 <= y1 else "OUTSIDE", (x0 + x1) / 2, (y0 + y1) / 2))
    dx, dy = x0 - l, y0 - t
    if abs(dx) > 1 or abs(dy) > 1:
        lines.append("  NOTE: the triangles start (%.1f, %.1f) away from the declared bounds corner" % (dx, dy))
    ox, oy = apt_render.mapply(world, 0, 0)
    cx, cy = apt_render.mapply(world, (x0 + x1) / 2, (y0 + y1) / 2)
    lines.append("  on the stage: origin (%.1f, %.1f)   centre of triangles (%.1f, %.1f)" % (ox, oy, cx, cy))
    for i, blk in enumerate(char["blocks"][:3]):
        lines.append("  block %d image %d  texture matrix: %s" % (i, blk["image"], _fmt_m(blk["matrix"])))
    return lines


def texture_text(char, image_id, im):
    """Decoded image size against the size the shape's texture matrix asks for (a mismatch moves and shrinks the picture)."""
    if im.get("image") is None:
        return ""
    w, h = im["image"].size
    out = "\n  decoded image %d x %d" % (w, h)
    for blk in (char.get("blocks") or []) if char else []:
        if blk["image"] != image_id:
            continue
        x0, y0, x1, y1 = apt_render.mapped_extent(blk)
        out += "\n  the texture matrix maps the shape to image pixels x %.1f .. %.1f  y %.1f .. %.1f  (%.0f x %.0f)" % (
            x0, x1, y0, y1, x1 - x0, y1 - y0)
        if min(abs(x0), abs(y0)) <= 1.5 and (abs((x1 - x0) - w) > 1 or abs((y1 - y0) - h) > 1):
            out += "\n  NOTE: the matrix expects a %.0f x %.0f image from its corner, the decoded one is %d x %d. Try Fit textures." % (
                x1 - x0, y1 - y0, w, h)
        break
    return out


def history_lines(rt, c, limit=12):
    """Where the timeline of the parent places this clip, frame by frame: runs of equal placements."""
    par = c.parent
    if par is None or not par.frames:
        return []
    runs = []
    for f in range(len(par.frames)):
        try:
            objs, _bg, _lb = rt.movie.display_list(par.frames, f)
        except Exception:
            return []
        m = next((o["matrix"] for o in objs if o["depth"] == c.depth and o["char"] == c.char_id), None)
        key = None if m is None else tuple(round(v, 3) for v in m)
        if runs and runs[-1][2] == key:
            runs[-1][1] = f + 1
        else:
            runs.append([f + 1, f + 1, key])
    name = c.path().split("/")[-1]
    out = ["", "placement of %s by the frames of %s [file]  (frame numbers start at 1):" % (name, par.path().split("/")[-1])]
    for a, b, key in runs[:limit]:
        out.append("  frames %3d..%-3d %s" % (a, b, "not placed" if key is None else _fmt_m(key)))
    if len(runs) > limit:
        out.append("  ... %d more runs" % (len(runs) - limit))
    return out


def contents_lines(rt, chain, limit=10):
    """What each parent clip holds at its current frame (the box and the parts that turn with it), local matrices."""
    out = []
    for c in chain[1:-1]:
        try:
            objs = c.render_list()
        except Exception:                                   # a diagnostic must never break the inspector
            continue
        out += ["", "contents of %s at frame %d/%d [file]:" % (c.path().split("/")[-1], c.frame + 1, max(1, len(c.frames)))]
        for o in objs[:limit]:
            ch = rt.movie.chars.get(o["char"])
            out.append("  depth %-3d char %-4d %-7s %s" % (o["depth"], o["char"], ch["type"] if ch else "import", _fmt_m(o["matrix"])))
        if len(objs) > limit:
            out.append("  ... %d more" % (len(objs) - limit))
    return out


def inspector_text(rt, info, assign):
    mc = info.get("mc")
    lines = []
    lines.append("%s  char %d   %s" % (info["kind"], info["char"], info["path"]))
    for key in ("detail", "text", "variable", "font"):
        if info.get(key):
            lines.append("  %s: %s" % (key, info[key]))
    if mc is None:
        return "\n".join(lines)
    chain = []
    n = mc
    while n is not None:
        chain.append(n)
        n = n.parent
    chain.reverse()
    lines.append("")
    lines.append("parent chain (root first)   [file] placement, [inferred] script-owned values")
    for c in chain:
        lines.append("  %-28s char %-4d depth %-3d frame %d/%d %s" % (
            c.path().split("/")[-1], c.char_id, c.depth, c.frame + 1, max(1, len(c.frames)), "playing" if c.playing else "stopped"))
    lines.append("")
    lines.append("placement of each clip in its parent [file]:")
    for c in chain[1:]:
        lines.append("  %-8s %s" % (c.path().split("/")[-1], _fmt_m(c.placed)))
    if len(chain) > 1:
        lines += history_lines(rt, chain[1])                  # the clip right under _root
    lines += contents_lines(rt, chain)
    lines.append("")
    lines.append("world matrix (parent x local, worldX = a*x + c*y + tx):")
    world = _compose(chain)
    lines.append("  " + _fmt_m(world))
    ch = rt.movie.chars.get(info["char"])
    if ch is not None and ch["type"] == "Shape":
        lines += shape_lines(ch, world)
    shown = 0
    for c in reversed(chain):
        if not (c.vars or c.writes or c.tp is not None):
            continue
        shown += 1
        if shown > 3:
            break
        lines += ["", "-- %s  (char %d) --" % (c.path(), c.char_id),
                  "local matrix, placement [file]: " + _fmt_m(c.placed)]
        if c.tp is not None:
            t = c.tp
            lines.append("script-owned [inferred]: x %.2f  y %.2f  xscale %.2f  yscale %.2f  rotation %.2f" % (
                t["x"], t["y"], t["xs"], t["ys"], t["rot"]))
        lines.append("alpha %.2f%%   %s" % (c.alpha_percent(), "script-owned [inferred]" if c.alpha is not None else "placement [file]"))
        if "played" in c.vars or c.vars.get("playonce") is not None:
            lines.append("one-shot state: played=%s" % _fmt_v(c.vars.get("played", VM.UNDEF)))
        wx = c.writes.get("_x")
        if wx:
            lines.append("spawn count (writes of _x): %d" % wx[0])
        if c.vars:
            lines.append("variables [file values, set by decoded scripts]:")
            for k, v in c.vars.items():
                lines.append("  %-16s %s" % (k, _fmt_v(v)))
        if c.writes:
            lines.append("last writes by script (formula from the code [file]):")
            for prop, (count, tick, value, off) in sorted(c.writes.items()):
                lines.append("  %-10s = %-12s tick %-4d x%-4d %s" % (prop, VM.to_str(value), tick, count, assign.get(off, "") if off else ""))
        if c.log:
            lines.append("recent trace (tick, event):")
            for t, kind, text in list(c.log)[-12:]:
                lines.append("  %4d %-7s %s" % (t, kind, text))
    lines += [""] + runtime_evidence(rt)
    return "\n".join(lines)
