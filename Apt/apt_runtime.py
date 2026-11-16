#!/usr/bin/env python3
"""
apt_runtime.py

Runs an APT movie tick by tick: timelines, frame actions, clip events and onEnterFrame handlers, using the stack
machine of apt_vm.py. Everything is repeatable: the state after N ticks is a function of the file and the seed
(scrubbing back re-runs from tick 0).

Evidence levels (see apt_vm.py):  file = decoded values/operations;  inferred = the event order and scope rules below.
Inferred rules, taken from how Flash works and not checked against the game:
  - a tick advances every playing timeline one frame (parents before children, new clips wait one tick), runs the
    frame actions of the frames entered, then calls every onEnterFrame handler (parents before children)
  - a clip that is placed enters its frame 0 at once and runs that frame's actions and its initialize/construct events
  - a variable assigned without var inside a function goes to the timeline where the function was defined
  - when a script sets _x, _y, _xscale, _yscale, _rotation or _alpha, the clip keeps those values and ignores the
    placement matrix and colour alpha of later placements
  - a timeline that reaches its last frame starts again, unless a script stopped it
Not run: natives of the game (cmdLoadingScreen and so on): calls on them do nothing and are listed in vm.unsupported.
"""
import collections
import math
import os
import re

import apt_actions as AA
import apt_movie as M
import apt_vm as VM
import apt_builtins as vm_builtins

PROPS = {"_x", "_y", "_xscale", "_yscale", "_rotation", "_alpha", "_visible"}
SPECIAL = {"_width", "_height", "_name", "_parent", "_currentframe", "_totalframes", "_xmouse", "_ymouse"}
MOUSE_EVENTS = {"press": ("onPress", 10), "release": ("onRelease", 11), "releaseOutside": ("onReleaseOutside", 12),
                "rollOver": ("onRollOver", 13), "rollOut": ("onRollOut", 14)}      # handler name, clip event flag bit (as in SWF clip events)


def mmul(p, c):
    """parent * child of two (a, b, c, d, tx, ty) matrices (x' = a x + c y + tx, y' = b x + d y + ty)."""
    return (p[0] * c[0] + p[2] * c[1], p[1] * c[0] + p[3] * c[1], p[0] * c[2] + p[2] * c[3], p[1] * c[2] + p[3] * c[3],
            p[0] * c[4] + p[2] * c[5] + p[4], p[1] * c[4] + p[3] * c[5] + p[5])


class MC:
    """A clip instance (sprite or other placed character)."""

    def __init__(self, rt, char, depth, name, matrix, scale, parent, born_tick, bias=0):
        self.rt, self.char, self.depth, self.name, self.parent = rt, char, depth, name, parent
        self.placed, self.placed_scale, self.placed_bias = tuple(matrix), scale, bias
        self.children = {}
        self.vars = VM.CIProps()
        self.frame = 0
        self.playing = True
        self.tp = None                         # script-owned transform {x, y, xs, ys, rot}
        self.alpha = None                      # script-owned alpha in percent
        self.visible = True
        self.clip = M.NO_CLIP
        self.born_tick = born_tick
        self.log = collections.deque(maxlen=80)
        self.writes = {}                       # prop -> [count, tick, value, offset]
        self.events = []                       # clip actions of the placement: (flags, key, stream ptr)
        self.ctx = parent.ctx if parent is not None else rt.ctx       # the movie this clip's character and scripts belong to
        self.removed = False
        self.placed_id = char["id"] if char else -1      # the id the parent's movie placed (an imported character has another id in its own movie)
        self.events_ctx = None                 # the movie whose scripts the clip events belong to: the movie that placed the clip (None: its own)
        self.text_format = None                # setTextFormat/setNewTextFormat of a text field (kept, not used for layout)
        self.proto = None                      # prototype of the class this clip's symbol is registered to (Object.registerClass), or None
        self.ctor = None
        self.ctor_pending = False
        self.set_char(char)

    def set_char(self, char):
        self.char = char
        self.frames = char.get("frames", []) if char and char["type"] == "Sprite" else []
        self.labels = {}
        for fi, ctls in enumerate(self.frames):
            for c in ctls:
                if c["op"] == "label":
                    self.labels.setdefault(c["label"].lower(), fi)       # labels ignore case like every other name

    # ---- identity
    @property
    def char_id(self):
        return self.char["id"] if self.char else -1

    def path(self):
        parts, n = [], self
        while n is not None:
            parts.append(n.name or ("#%d" % n.char_id))
            n = n.parent
        return "/".join(reversed(parts))

    # ---- transform
    def _decomposed(self):
        a, b, c, d, tx, ty = self.placed
        xs, ys = math.hypot(a, b) * 100.0, math.hypot(c, d) * 100.0
        if a * d - b * c < 0:
            ys = -ys
        return {"x": tx, "y": ty, "xs": xs, "ys": ys, "rot": math.degrees(math.atan2(b, a))}

    def _decompose(self):
        """The script now owns the transform (a write). Reading a property must not call this."""
        self.tp = self._decomposed()

    def matrix(self):
        if self.tp is None:
            return self.placed
        t = self.tp
        r = math.radians(t["rot"])
        return (math.cos(r) * t["xs"] / 100.0, math.sin(r) * t["xs"] / 100.0,
                -math.sin(r) * t["ys"] / 100.0, math.cos(r) * t["ys"] / 100.0, t["x"], t["y"])

    def alpha_percent(self):
        if self.alpha is not None:
            return self.alpha
        return ((self.placed_scale >> 24) & 255) / 2.55

    def cx_scale(self):
        if self.alpha is None:
            return self.placed_scale
        a = int(round(min(max(self.alpha, 0.0), 100.0) * 2.55))
        return (a << 24) | (self.placed_scale & 0xFFFFFF)

    def set_placed(self, matrix=None, scale=None, bias=None):
        if matrix is not None:
            self.placed = tuple(matrix)
        if scale is not None:
            self.placed_scale = scale
        if bias is not None:
            self.placed_bias = bias

    # ---- script access (the protocol of apt_vm)
    def vm_get(self, name):
        name = name.lower() if name.lower() in PROPS or name.lower() in SPECIAL else name
        if name in PROPS:
            if name == "_visible":
                return self.visible
            t = self.tp if self.tp is not None else self._decomposed()     # a read does not take the transform from the timeline
            return {"_x": lambda: t["x"], "_y": lambda: t["y"], "_xscale": lambda: t["xs"], "_yscale": lambda: t["ys"],
                    "_rotation": lambda: t["rot"], "_alpha": self.alpha_percent}[name]()
        if name == "_width" or name == "_height":
            return self.extent(name == "_width")
        if name == "_xmouse" or name == "_ymouse":
            return self.to_local(self.rt.mouse_x, self.rt.mouse_y)[0 if name == "_xmouse" else 1]
        if name == "_name":
            return self.name
        if name == "_parent":
            return self.parent if self.parent is not None else VM.UNDEF
        if name == "_currentframe":
            return float(self.frame + 1)
        if name == "_totalframes":
            return float(len(self.frames))
        low = name.lower()
        for ch in self.children.values():
            if ch.name.lower() == low:
                return ch
        if name in self.vars:
            return self.vars[name]
        if low in ("text", "htmltext") and self.char is not None and self.char["type"] == "Text":
            return self.char["text"]                  # a text field that was not set shows its initial text
        return self.proto.vm_get(name) if self.proto is not None else VM.UNDEF

    def text_value(self):
        """What a text field shows because the scripts set it: `text` (or `htmlText` without its tags), else the variable the field is bound to in its
        parent; None = it shows the initial text of the character."""
        if self.char is None or self.char["type"] != "Text":
            return None
        for key in ("text", "htmlText"):
            v = self.vars.get(key)
            if v is not None and v is not VM.UNDEF:
                t = VM.to_str(v)
                return re.sub(r"<[^>]*>", "", t) if key == "htmlText" else t
        var = self.char.get("variable")
        if var and "." not in var and self.parent is not None:
            v = self.parent.vars.get(var)
            if v is not None and v is not VM.UNDEF:
                return VM.to_str(v)
        return None

    def vm_set(self, name, value, vm=None, offset=None):
        if name.lower() in ("_width", "_height"):
            self.set_extent(name.lower() == "_width", VM.to_number(value))
            return
        if name.lower() in PROPS:
            name = name.lower()
        if name in PROPS:
            if name != "_visible" and isinstance(value, float) and value != value:
                # NaN written to a clip property is ignored (inferred; the game's own scripts rely on it:
                # a dot multiplies _alpha by a variable that is undefined until its first spawn)
                self.rt.vm.infer("NaN written to %s is ignored" % name)
                return
            if name == "_visible":
                self.visible = VM.truthy(value)
            elif name == "_alpha":
                self.alpha = VM.to_number(value)
            else:
                if self.tp is None:
                    self._decompose()
                self.tp[{"_x": "x", "_y": "y", "_xscale": "xs", "_yscale": "ys", "_rotation": "rot"}[name]] = VM.to_number(value)
            w = self.writes.get(name)
            self.writes[name] = [(w[0] + 1) if w else 1, self.rt.tick, value, offset]
            return
        self.vars[name] = value

    def vm_has(self, name):
        return self.vm_get(name) is not VM.UNDEF

    def vm_get_var(self, name):
        if name in self.vars:
            return self.vars[name]
        if name.lower() in PROPS or name.lower() in ("_xmouse", "_ymouse"):
            return self.vm_get(name)
        low = name.lower()
        for ch in self.children.values():
            if ch.name.lower() == low:
                return ch
        return self.proto.vm_get(name) if self.proto is not None else VM.UNDEF

    def vm_set_var(self, name, value, vm=None, offset=None):
        self.vars[name] = value

    def vm_call(self, name, args, vm):
        low = name.lower()
        f = self.vars.get(name)
        if low in ("gotoandstop", "gotoandplay"):
            self.goto(args[0] if args else 1.0, play=(low == "gotoandplay"))
            return VM.UNDEF
        if low == "createemptymovieclip":
            number = VM.to_number(args[1]) if len(args) > 1 else 0.0
            depth = int(number) if number == number else 0
            mc = MC(self.rt, None, depth, VM.to_str(args[0]) if args else "", M.IDENTITY, 0xFFFFFFFF, self, self.rt.tick)
            self.children[depth] = mc                 # an empty clip: it draws nothing, but scripts hang handlers (onEnterFrame) on it
            return mc
        if low == "loadmovie":                    # the movie arrives with the next tick, as a load does (Runtime.load_into)
            self.rt.pending_loads.append((self, VM.to_str(args[0]) if args else ""))
            return VM.UNDEF
        if low == "unloadmovie":
            self.unload()
            return VM.UNDEF
        if low == "removemovieclip":
            self.remove()
            return VM.UNDEF
        if low in ("settextformat", "setnewtextformat"):
            self.text_format = args[-1] if args else None
            return VM.UNDEF
        if low in ("gettextformat", "getnewtextformat"):
            if isinstance(self.text_format, VM.Obj):
                return self.text_format
            return self.rt.vm.globals.props["TextFormat"].construct(self.rt.vm, [])
        if low == "getdepth":
            return float(self.depth)
        if low == "swapdepths":
            return self.swap_depths(args)
        if low == "hittest":
            return self.hit_test(args)
        if low == "attachmovie":
            return self.attach(args)
        if low == "duplicatemovieclip":
            return self.duplicate(args)
        if low == "stop":
            self.playing = False
            return VM.UNDEF
        if low == "play":
            self.playing = True
            return VM.UNDEF
        if low in ("nextframe", "prevframe"):
            self.goto(self.frame + 1 + (1 if low == "nextframe" else -1), play=False)
            return VM.UNDEF
        if f is None and self.proto is not None:
            f = self.proto.vm_get(name)                 # a method of the registered class
        if isinstance(f, (VM.Func, VM.Native)):
            return vm.call(f, self, args)
        return vm.missing_call(self.path().replace("/", "."), name, args, "call %s on a clip (not defined in the movie)" % name)

    # ---- load, unload, size
    def unload(self):
        """Drop what the clip shows and its scripts' variables (the clip itself, its depth, name and placement stay)."""
        self.children, self.vars, self.events = {}, VM.CIProps(), []
        self.frame, self.playing = 0, True
        self.set_char(None)

    def _symbol_id(self, name):
        """The character id a linkage name stands for in this clip's movie: an export, or an import entry of that name (ASSUMED: names ignore case)."""
        eid = self.ctx.export_id(name)
        if eid is not None:
            return eid
        low = name.lower()
        for iid, imp in self.ctx.imports.items():
            if imp["name"].lower() == low:
                return iid
        return None

    def _new_child(self, char_id, name, depth, matrix, scale, init, bias=0):
        o = {"depth": depth, "char": char_id, "matrix": matrix, "scale": scale, "bias": bias, "name": name, "clip": M.NO_CLIP, "events": [], "born": self.frame}
        mc = self.rt.make_child(self, o)
        self.children[depth] = mc
        if isinstance(init, VM.Obj):
            for k, v in init.props.items():
                mc.vm_set(k, v)
        mc.start()
        return mc

    def attach(self, args):
        """attachMovie(linkage, newName, depth, initObject): a new clip of the symbol with that linkage name (class binding included)."""
        if len(args) < 3:
            return VM.UNDEF
        cid = self._symbol_id(VM.to_str(args[0]))
        number = VM.to_number(args[2])
        if cid is None or number != number:
            self.rt.vm.note("attachMovie %s: no such symbol in %s" % (VM.to_str(args[0]), self.ctx.movie.name))
            return VM.UNDEF
        return self._new_child(cid, VM.to_str(args[1]), int(number), M.IDENTITY, 0xFFFFFFFF, args[3] if len(args) > 3 else None)

    def duplicate(self, args):
        """duplicateMovieClip(newName, depth, initObject): a sibling of the same symbol at the same placement."""
        if self.parent is None or len(args) < 2:
            return VM.UNDEF
        number = VM.to_number(args[1])
        if number != number:
            return VM.UNDEF
        return self.parent._new_child(self.placed_id, VM.to_str(args[0]), int(number), self.matrix(), self.cx_scale(), args[2] if len(args) > 2 else None, self.placed_bias)

    def swap_depths(self, args):
        """swapDepths(depth or clip): the clip takes that depth, a clip that is there goes to the depth this one had."""
        parent = self.parent
        if parent is None or not args:
            return VM.UNDEF
        a = args[0]
        if isinstance(a, MC):
            other = a if a.parent is parent else None
            new = a.depth if other is not None else None
        else:
            n = VM.to_number(a)
            new = int(n) if n == n else None
            other = parent.children.get(new) if new is not None else None
        if new is None or new == self.depth:
            return VM.UNDEF
        old = self.depth
        parent.children.pop(old, None)
        if other is not None:
            parent.children.pop(new, None)
            other.depth = old
            parent.children[old] = other
        self.depth = new
        parent.children[new] = self
        return VM.UNDEF

    # ---- mouse: position, hit test, button events
    def global_matrix(self):
        m = self.matrix()
        node = self.parent
        while node is not None:
            m = mmul(node.matrix(), m)
            node = node.parent
        return m

    def to_local(self, x, y):
        """A point of the stage in this clip's own coordinates."""
        a, b, c, d, tx, ty = self.global_matrix()
        det = a * d - b * c
        if abs(det) < 1e-12:
            return (0.0, 0.0)
        return ((d * (x - tx) - c * (y - ty)) / det, (-b * (x - tx) + a * (y - ty)) / det)

    def global_box(self):
        """(l, t, r, b) of what the clip draws, on the stage; None if it draws nothing."""
        box = self.bounds()
        if box is None:
            return None
        g = self.global_matrix()
        pts = [(g[0] * x + g[2] * y + g[4], g[1] * x + g[3] * y + g[5]) for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def contains(self, x, y):
        box = self.bounds()
        if box is None:
            return False
        lx, ly = self.to_local(x, y)
        return box[0] <= lx <= box[2] and box[1] <= ly <= box[3]

    def hit_test(self, args):
        """hitTest(x, y, shapeFlag) on a stage point, or hitTest(clip) for overlapping bounds. ASSUMED: the bounds, also when shapeFlag is true."""
        if len(args) >= 2:
            x, y = VM.to_number(args[0]), VM.to_number(args[1])
            return bool(x == x and y == y and self.contains(x, y))
        other = args[0] if args and isinstance(args[0], MC) else None
        a, b = self.global_box(), other.global_box() if other is not None else None
        return bool(a and b and a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3])

    def mouse_handler(self, kind):
        """("fn", function) for `mc.onPress = function...` (or a method of the registered class), ("event", ptr) for an onClipEvent, or None."""
        name, bit = MOUSE_EVENTS[kind]
        f = self.vars.get(name)
        if f is None and self.proto is not None:
            f = self.proto.vm_get(name)
        if isinstance(f, (VM.Func, VM.Native)):
            return ("fn", f)
        for flags, _key, ptr in self.events:
            if flags & (1 << bit):
                return ("event", ptr)
        return None

    def reacts(self):
        return any(self.mouse_handler(k) is not None for k in ("press", "release", "rollOver", "rollOut"))

    def pick(self, x, y):
        """The topmost clip under the stage point that reacts to the mouse (has a handler for press, release, rollOver or rollOut)."""
        if not self.visible:
            return None
        for d in sorted(self.children, reverse=True):
            hit = self.children[d].pick(x, y)
            if hit is not None:
                return hit
        return self if self.reacts() and self.contains(x, y) else None

    def fire(self, kind):
        h = self.mouse_handler(kind)
        if h is None:
            return
        try:
            if h[0] == "fn":
                self.rt.vm.call(h[1], self, [])
            else:
                self.rt.exec_stream(h[1], self, "clip event %s" % kind, self.events_ctx)
        except VM.Unsupported as e:
            self.rt._error(0, "%s %s: %s" % (self.path(), kind, e))

    def remove(self):
        parent = self.parent
        if parent is not None:
            for d, ch in list(parent.children.items()):
                if ch is self:
                    del parent.children[d]
        self.removed = True

    def bounds(self, depth=0):
        """(l, t, r, b) of everything the clip draws, in its own coordinates, from the declared bounds of shapes and texts; None if nothing."""
        if depth > 12:
            return None
        if self.char is not None and self.char["type"] in ("Shape", "Text"):
            l, t, r, b = self.char["bounds"]
            return (l, t, r, b) if r > l and b > t else None
        pts = []
        for ch in self.children.values():
            cb = ch.bounds(depth + 1)
            if cb is not None:
                a, b, c, d, tx, ty = ch.matrix()
                pts += [(a * x + c * y + tx, b * x + d * y + ty) for x, y in ((cb[0], cb[1]), (cb[2], cb[1]), (cb[2], cb[3]), (cb[0], cb[3]))]
        if not pts:
            return None
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def set_extent(self, width, value):
        """_width = v / _height = v: scale the clip so the bounds of what it draws are v wide/high in the parent's coordinates (nothing to scale: ignored)."""
        box = self.bounds()
        size = 0.0 if box is None else (box[2] - box[0] if width else box[3] - box[1])
        if size <= 0 or value != value:
            return
        if self.tp is None:
            self._decompose()
        self.tp["xs" if width else "ys"] = value / size * 100.0

    def extent(self, width):
        """_width / _height: the size of the drawn bounds in the parent's coordinates (0 for a clip that draws nothing)."""
        box = self.bounds()
        if box is None:
            return 0.0
        t = self.tp if self.tp is not None else self._decomposed()
        return (box[2] - box[0]) * abs(t["xs"]) / 100.0 if width else (box[3] - box[1]) * abs(t["ys"]) / 100.0

    # ---- timeline
    def goto(self, target, play):
        n = len(self.frames)
        if isinstance(target, str) and not VM.to_number(target) == VM.to_number(target):
            fi = self.labels.get(target.lower())
            if fi is None:
                self.rt.vm.note("goto unknown label %s" % target)
                return
        else:
            number = VM.to_number(target)
            if number != number:                        # gotoAndPlay(undefined) and the like: Flash stays where it is
                self.rt.vm.note("goto %s (not a frame number or label): ignored" % VM.to_str(target))
                return
            fi = int(number) - 1
        if n == 0:
            return
        fi = min(max(fi, 0), n - 1)
        self.playing = play
        self.jump(fi)

    def jump(self, fi):
        """Move to frame fi: rebuild the display list, keep instances that stay."""
        objs, _bg, _lb = self.ctx.movie.display_list(self.frames, fi)
        keep, fresh = {}, []
        for o in objs:
            old = self.children.get(o["depth"])
            if old is not None and old.placed_id == o["char"]:
                old.set_placed(None if old.tp is not None else o["matrix"], o["scale"], o.get("bias", 0))
                old.name = o["name"] or old.name
                keep[o["depth"]] = old
            else:
                mc = self.rt.make_child(self, o)
                keep[o["depth"]] = mc
                fresh.append(mc)
        self.children = keep
        self.frame = fi
        for mc in fresh:
            mc.start()
        self.rt.run_frame_actions(self, fi)

    def enter(self, fi):
        """Apply the controls of frame fi on top of the current state (a normal advance)."""
        self.frame = fi
        fresh = []
        for c in self.frames[fi]:
            if c["op"] == "place":
                mc = self._place(c)
                if mc is not None:
                    fresh.append(mc)
            elif c["op"] == "remove":
                self.children.pop(c["depth"], None)
        for mc in fresh:
            mc.start()
        self.construct()
        self.rt.run_frame_actions(self, fi)

    def construct(self):
        """The constructor of the registered class runs once, after the first frame has placed the clip's children and before that frame's actions
        (ASSUMED order: the game's constructors read their children, e.g. NFS_ColourWheel sets colour_selected._visible)."""
        if self.ctor_pending:
            self.ctor_pending = False
            self.rt.call_ctor(self)

    def _place(self, c):
        d, flags = c["depth"], c["flags"]
        old = self.children.get(d)
        if old is not None and flags & M.F_MOVE and not (flags & M.F_CHAR and c["char"] != old.placed_id):
            if flags & M.F_MATRIX and old.tp is None:
                old.set_placed(matrix=c["matrix"])
            if flags & M.F_CXFORM:
                old.set_placed(scale=c["scale"] if old.alpha is None else None, bias=c["bias"])
            if flags & M.F_NAME:
                old.name = c["name"]
            return None
        if not flags & M.F_CHAR:
            return None
        o = {"depth": d, "char": c["char"], "matrix": c["matrix"] if flags & M.F_MATRIX else M.IDENTITY,
             "scale": c["scale"] if flags & M.F_CXFORM else 0xFFFFFFFF, "bias": c["bias"] if flags & M.F_CXFORM else 0,
             "name": c["name"] if flags & M.F_NAME else "",
             "clip": c["clip"], "events": c["events"], "born": self.frame}
        mc = self.rt.make_child(self, o)
        self.children[d] = mc
        return mc

    def start(self):
        """A clip that was just placed: enter frame 0, then its initialize events."""
        if self.frames:
            self.enter(0)
        self.construct()
        for flags, _key, ptr in self.events:
            if flags & (1 << 0 | 1 << 9 | 1 << 18):          # load, initialize, construct
                self.rt.exec_stream(ptr, self, "clip event", self.events_ctx)

    def advance(self):
        if self.playing and len(self.frames) > 1:
            if self.frame + 1 < len(self.frames):
                self.enter(self.frame + 1)
            else:
                self.jump(0)
        for ch in sorted(self.children.items()):
            if ch[1].born_tick < self.rt.tick:
                ch[1].advance()

    def walk(self):
        yield self
        for _d, ch in sorted(self.children.items()):
            yield from ch.walk()

    # ---- for the renderer
    def render_list(self):
        out = []
        for d, ch in sorted(self.children.items()):
            if not ch.visible or ch.char is None:      # an empty clip has no shape to draw
                continue
            out.append({"depth": d, "char": ch.placed_id, "matrix": ch.matrix(), "scale": ch.cx_scale(), "bias": ch.placed_bias, "name": ch.name,
                        "clip": ch.clip, "born": 0, "events": [], "mc": ch})
        return out


class Ctx:
    """One movie of a runtime: the movie, its constant pool and its decoded streams (a script pointer means something only inside its own movie)."""

    def __init__(self, movie):
        self.movie = movie
        _main, self.consts = AA.read_consts(movie.const)
        self.streams = {}
        self.endian = getattr(movie, "endian", M.A.ENDIAN)
        self._exports = None
        self._export_ids = None
        self.imports = {i["id"]: i for i in movie.imports}

    def export_id(self, name):
        """The id this movie exports `name` under (names ignore case), or None."""
        if self._export_ids is None:
            self._export_ids = {e["name"].lower(): e["id"] for e in self.movie.exports}
        return self._export_ids.get(name.lower())

    def export_names(self, char_id):
        """The names the movie exports character char_id under (the linkage names Object.registerClass uses)."""
        if self._exports is None:
            self._exports = {}
            for e in self.movie.exports:
                self._exports.setdefault(e["id"], []).append(e["name"])
        return self._exports.get(char_id, [])


ENGINE = None                # an apt_engine.Engine (set by the viewer), or None
TICKS_PER_SECOND = 30.0      # not read from the file: the viewer ticks once per frame and assumes 30 frames per second for getTimer()


class Runtime:
    def __init__(self, movie, seed=1, stubs=None, scenario=None, ui_dir=None):
        self.movie = movie
        self.ui_dir = ui_dir or (os.path.dirname(os.path.abspath(movie.path)) if getattr(movie, "path", None) else None)   # where loadMovie looks
        self.seed = seed
        self.stubs = dict(stubs or {})            # name -> JSON value, see apt_vm.from_json
        self.scenario = list(scenario or [])      # what the game does: [{"at": tick, "op": set|call|goto, ...}]
        self.stub_log = []                        # (tick, text) for every stub operation
        M.A.ENDIAN = getattr(movie, "endian", M.A.ENDIAN)
        self.ctx = Ctx(movie)                     # the root movie; loadMovie adds more (self.loaded)
        self.consts, self.streams = self.ctx.consts, self.ctx.streams
        self.loaded = {}                          # movie file path -> Ctx of a movie that loadMovie brought in
        self.reset(seed)

    # ---- control
    def reset(self, seed=None):
        if seed is not None:
            self.seed = seed
        self.tick = 0
        self.vm = VM.VM(self.seed)
        self.vm.stubs = dict(self.stubs)
        if ENGINE is not None:
            ENGINE.install(self.vm)               # the natives of nfs.exe, fed with game data (apt_engine.py)
        self.vm.hook = self._hook
        self.stub_log = []
        self.errors = {}
        self.current = None
        self.libs_ready = set()                   # library movies whose init actions have run in this VM
        self.mouse_x = self.mouse_y = 0.0         # the pointer on the stage (see mouse())
        self.hover = self.pressed = None          # the clip the pointer is over / that was pressed
        self.pending_loads = []                   # [(clip, name)] asked for by loadMovie, done at the start of the next tick
        self.load_log = []                        # (tick, name, file or None)
        root_char = {"id": -1, "type": "Sprite", "frames": self.movie.frames}
        self.root = MC(self, root_char, 0, "_root", M.IDENTITY, 0xFFFFFFFF, None, 0)
        self.root.name = "_root"
        self.vm.root = self.root
        self.bg = None
        self.run_imports(self.ctx)
        # init actions (class definitions) run before the first frame
        for fi, ctls in enumerate(self.movie.frames):
            for c in ctls:
                if c["op"] == "initaction" and fi == 0:
                    self.exec_stream(c["ptr"], self.root, "init sprite %d" % c["sprite"])
        self.root.start()
        self.run_scenario(0)

    def step(self, n=1):
        for _ in range(n):
            self.tick += 1
            self.vm.now_ms = self.tick * 1000.0 / TICKS_PER_SECOND     # getTimer(); the rate is an assumption (see TICKS_PER_SECOND)
            while self.pending_loads:
                clip, name = self.pending_loads.pop(0)
                if not clip.removed:
                    self.load_into(clip, name)
            self.root.advance()
            for mc in list(self.root.walk()):
                f = mc.vars.get("onEnterFrame")
                if isinstance(f, VM.Func):
                    self.current = mc
                    try:
                        self.vm.call(f, mc, [])
                    except VM.Unsupported as e:
                        self._error(f.offset, str(e))
                for flags, _k, ptr in mc.events:
                    if flags & (1 << 1):
                        self.exec_stream(ptr, mc, "clip enterFrame", mc.events_ctx)
            self.current = None
            try:
                vm_builtins.fire_intervals(self.vm)
            except VM.Unsupported as e:
                self._error(0, "setInterval callback: %s" % e)
            self.run_scenario(self.tick)

    def seek(self, tick):
        if tick < self.tick:
            self.reset()
        if tick > self.tick:
            self.step(tick - self.tick)

    # ---- stubs: what the game would give or do (the scenario is replayed from tick 0 after every reset)
    def resolve(self, path):
        """_root.a.b / _global.X.prototype / Name (a variable of _root or a global) -> value."""
        parts = path.split(".")
        if parts[0] == "_root":
            o = self.root
        elif parts[0] == "_global":
            o = self.vm.globals
        else:
            o = self.root.vm_get_var(parts[0])
            if o is VM.UNDEF:
                o = self.vm.globals.props.get(parts[0], VM.UNDEF)
        for p in parts[1:]:
            o = self.vm.get_member(o, p)
        return o

    def run_scenario(self, tick):
        for op in self.scenario:
            if int(op.get("at", 0)) == tick:
                self.apply_op(op)

    def apply_op(self, op):
        """One game side action. Returns a text for the log. Errors are listed, not raised."""
        kind, path = op.get("op"), op.get("path", "")
        prev, self.current = self.current, self.root
        try:
            if kind == "set":
                parent, _dot, name = path.rpartition(".")
                self.vm.set_member(self.resolve(parent), name, VM.from_json(op.get("value"), path), None)
                text = "set %s = %s" % (path, op.get("value"))
            elif kind == "call":
                f = self.resolve(path)
                this = self.resolve(op["this"]) if op.get("this") else self.resolve(path.rpartition(".")[0])
                args = [VM.from_json(a, "arg") for a in op.get("args", [])]
                if not isinstance(f, (VM.Func, VM.Native)):
                    text = "call %s: not a function (%s)" % (path, VM.to_str(f))
                else:
                    r = self.vm.call(f, this, args)
                    text = "call %s(%s) -> %s" % (path, ", ".join(VM.to_str(a) for a in args), VM.to_str(r))
            elif kind == "message":
                fields = {k: v for k, v in op.items() if k not in ("at", "op")}
                if ENGINE is None:
                    text = "message: no engine installed (--engine-data / --engine-map)"
                else:
                    ENGINE.post_message(fields)
                    text = "message %s" % op.get("message")
            elif kind == "goto":
                mc = self.resolve(path)
                if hasattr(mc, "goto"):
                    mc.goto(op.get("frame", 1.0), play=bool(op.get("play", False)))
                    text = "goto %s %s" % (path, op.get("frame", 1.0))
                else:
                    text = "goto %s: not a clip" % path
            else:
                text = "unknown operation %r" % kind
        except VM.Unsupported as e:
            self._error(0, "stub op %s: %s" % (path, e))
            text = "%s %s: stopped (%s)" % (kind, path, e)
        finally:
            self.current = prev
        self.stub_log.append((self.tick, text))
        return text

    def functions(self, limit=4000):
        """[(path, owner path, Func)] for every function reachable from _global and _root, first path wins."""
        out, seen, found = [], set(), set()
        queue = collections.deque([("_global", self.vm.globals, 0), ("_root", self.root, 0)])
        while queue and len(seen) < limit:
            path, o, d = queue.popleft()
            if id(o) in seen or d > 6:
                continue
            seen.add(id(o))
            items = list(getattr(o, "props", {}).items())
            if isinstance(o, MC):
                items = list(o.vars.items()) + [(ch.name, ch) for ch in o.children.values() if ch.name]
            owner = path
            for k, v in items:
                if isinstance(v, VM.Func):
                    if id(v) not in found:
                        found.add(id(v))
                        out.append(("%s.%s" % (path, k), owner, v))
                    queue.append(("%s.%s" % (path, k), v, d + 1))
                elif isinstance(v, (VM.Obj, MC)):
                    queue.append(("%s.%s" % (path, k), v, d + 1))
        return out

    # ---- input: the way the player hands it to the movie
    def _broadcast(self, listeners, name, args=()):
        """Call name() on every listener that has it (Key.addListener / Mouse.addListener); a script that fails is listed, the rest go on."""
        for target in list(listeners):
            f = target.vm_get(name) if hasattr(target, "vm_get") else VM.UNDEF
            if isinstance(f, (VM.Func, VM.Native)):
                prev, self.current = self.current, target if isinstance(target, MC) else self.current
                try:
                    self.vm.call(f, target, list(args))
                except VM.Unsupported as e:
                    self._error(0, "%s: %s" % (name, e))
                finally:
                    self.current = prev

    def key(self, code, down=True, ascii=0, controller=0):
        """A key or pad button: Key.getCode()/getAscii()/getController() answer for the call, then onKeyDown / onKeyUp go to the Key listeners (the root's
        NFSEventHandler adds 1000 to a key-up code and calls HandleInput of the current screen). The codes are the game's (INPUTCODE: arrows 37-40, pad 300-311)."""
        vm = self.vm
        vm.key_code, vm.key_ascii, vm.key_controller = int(code), int(ascii), int(controller)
        (vm.keys_down.add if down else vm.keys_down.discard)(int(code))
        self._broadcast(vm.key_listeners, "onKeyDown" if down else "onKeyUp")

    def mouse(self, kind, x, y, delta=0.0):
        """The pointer at stage point (x, y): kind is "move", "down", "up" or "wheel". Mouse listeners get onMouseMove/Down/Up/Wheel; clips get Flash button
        events (onRollOver/onRollOut on move, onPress on down, onRelease or onReleaseOutside on up). Returns the clip under the pointer that reacts, or None."""
        self.mouse_x, self.mouse_y = float(x), float(y)
        vm = self.vm
        over = self.root.pick(self.mouse_x, self.mouse_y)
        if kind == "move":
            self._broadcast(vm.mouse_listeners, "onMouseMove")
            if over is not self.hover:
                old, self.hover = self.hover, over
                if old is not None and not old.removed:
                    old.fire("rollOut")
                if over is not None:
                    over.fire("rollOver")
        elif kind == "down":
            self._broadcast(vm.mouse_listeners, "onMouseDown")
            if over is not None:
                self.pressed = over
                over.fire("press")
        elif kind == "up":
            self._broadcast(vm.mouse_listeners, "onMouseUp")
            pressed, self.pressed = self.pressed, None
            if pressed is not None and not pressed.removed:
                pressed.fire("release" if pressed is over else "releaseOutside")
        elif kind == "wheel":
            self._broadcast(vm.mouse_listeners, "onMouseWheel", [float(delta)])
        return over

    # ---- loadMovie
    def find_movie(self, name):
        """The file in the UI folder for a loadMovie name: "PostRace.swf" -> postrace.big (ASSUMED: the game's .swf names are the .big names)."""
        base = os.path.basename(name.replace("\\", "/"))
        want = ((base[:-4] if base.lower().endswith(".swf") else base) + ".big").lower()
        if self.ui_dir and os.path.isdir(self.ui_dir):
            for f in os.listdir(self.ui_dir):
                if f.lower() == want:
                    return os.path.join(self.ui_dir, f)
        return None

    def load_into(self, clip, name):
        """Replace what `clip` shows with the movie `name`: the movie's root timeline becomes the clip's, its init actions and first frame run now."""
        path = self.find_movie(name)
        if path is None:
            self.vm.note("loadMovie %s: no such movie in the UI folder (%s)" % (name, self.ui_dir))
            self.load_log.append((self.tick, name, None))
            return
        ctx = self.loaded.get(path)
        if ctx is None:
            try:
                ctx = self.loaded[path] = Ctx(M.load(path))
            except Exception as e:
                self.vm.note("loadMovie %s: %s" % (name, e))
                self.load_log.append((self.tick, name, None))
                return
        clip.unload()
        clip.ctx = ctx
        clip.set_char({"id": -1, "type": "Sprite", "frames": ctx.movie.frames})
        self.run_imports(ctx)
        for c in ctx.movie.frames[0] if ctx.movie.frames else []:
            if c["op"] == "initaction":
                self.exec_stream(c["ptr"], clip, "init sprite %d" % c["sprite"])
        self.load_log.append((self.tick, name, path))
        clip.start()

    # ---- imports: characters (and classes) that a movie takes from library movies
    def library(self, name):
        """The Ctx of a library movie (nfs_slider.big for the import "NFS_Slider"), loaded once, its init actions run once per VM; None if there is no such file."""
        path = self.find_movie(name)
        if path is None:
            self.vm.note("import from %s: no such movie in the UI folder" % name)
            return None
        ctx = self.loaded.get(path)
        if ctx is None:
            try:
                ctx = self.loaded[path] = Ctx(M.load(path))
            except Exception as e:
                self.vm.note("import from %s: %s" % (name, e))
                return None
        if path not in self.libs_ready:
            self.libs_ready.add(path)
            self.run_imports(ctx)
            holder = MC(self, {"id": -1, "type": "Sprite", "frames": ctx.movie.frames}, 0, "", M.IDENTITY, 0xFFFFFFFF, None, 0)
            holder.ctx = ctx
            for c in ctx.movie.frames[0] if ctx.movie.frames else []:
                if c["op"] == "initaction":
                    self.exec_stream(c["ptr"], holder, "init sprite %d of %s" % (c["sprite"], ctx.movie.name))
        return ctx

    def run_imports(self, ctx):
        """Bring in every library the movie imports from, so their classes exist before the movie's own init actions and clips."""
        for lib in sorted({i["movie"] for i in ctx.imports.values()}):
            self.library(lib)

    def import_char(self, ctx, char_id):
        """(Ctx, character) a movie's imported character id stands for, or None."""
        imp = ctx.imports.get(char_id)
        if imp is None:
            return None
        lib = self.library(imp["movie"])
        cid = lib.export_id(imp["name"]) if lib is not None else None
        char = lib.movie.chars.get(cid) if cid is not None else None
        return (lib, char) if char is not None else None

    # ---- streams
    def exec_stream(self, ptr, mc, what, ctx=None):
        if not ptr:
            return
        ctx = ctx or mc.ctx
        insns = ctx.streams.get(ptr)
        if insns is None:
            M.A.ENDIAN = ctx.endian
            try:
                insns = AA.decode_stream(ctx.movie.apt, ptr, ctx.consts)
            except (AA.DecodeError, Exception) as e:
                self._error(ptr, "decode: %s" % e)
                insns = []
            ctx.streams[ptr] = insns
        prev, self.current = self.current, mc
        try:
            self.vm.run(insns, VM.Frame(mc, mc, None, None))
        except VM.Unsupported as e:
            self._error(ptr, str(e))
        finally:
            self.current = prev

    def run_frame_actions(self, mc, fi):
        for c in mc.frames[fi]:
            if c["op"] == "action":
                self.exec_stream(c["ptr"], mc, "frame %d" % fi)
            elif c["op"] == "background" and mc is self.root:
                self.bg = c["color"]

    def make_child(self, parent, o):
        char = parent.ctx.movie.chars.get(o["char"])
        ctx = parent.ctx
        if char is None:
            got = self.import_char(parent.ctx, o["char"])
            if got is not None:
                ctx, char = got
        mc = MC(self, char, o["depth"], o["name"], o["matrix"], o["scale"], parent, self.tick, o.get("bias", 0))
        mc.ctx, mc.placed_id, mc.events_ctx = ctx, o["char"], parent.ctx
        mc.clip = o.get("clip", M.NO_CLIP)
        mc.events = [(f, k, p) for (f, k, p) in (o.get("events") or [])]
        self.bind_class(mc)
        return mc

    def bind_class(self, mc):
        """A clip of a symbol that Object.registerClass gave a class becomes an instance of it: prototype now, constructor with its first frame."""
        reg = self.vm.registered
        if not reg or mc.char is None or mc.char["type"] != "Sprite":
            return
        for n in mc.ctx.export_names(mc.char_id):
            cls = reg.get(n)
            if cls is not None:
                proto = cls.vm_get("prototype")
                if isinstance(proto, VM.Obj):
                    mc.proto, mc.ctor, mc.ctor_pending = proto, cls, True
                return

    def call_ctor(self, mc):
        prev, self.current = self.current, mc
        try:
            self.vm.call(mc.ctor, mc, [])
        except VM.Unsupported as e:
            self._error(getattr(mc.ctor, "offset", 0) or 0, "constructor of %s: %s" % (mc.path(), e))
        finally:
            self.current = prev

    def _error(self, where, text):
        key = "0x%X: %s" % (where, text)
        self.errors[key] = self.errors.get(key, 0) + 1

    # ---- trace
    def _hook(self, kind, data):
        mc = self.current
        if mc is None:
            return
        if kind == "branch":
            mc.log.append((self.tick, "branch", "0x%X %s" % (data[0], "taken" if data[1] else "not taken")))
        elif kind == "set":
            if data[0] == "member" and hasattr(data[4], "log") and data[1] in PROPS:
                data[4].log.append((self.tick, "set", "%s = %s  (0x%X)" % (data[1], VM.to_str(data[2]), data[3] or 0)))
            elif data[0] == "var":
                mc.log.append((self.tick, "var", "%s = %s  (0x%X)" % (data[1], VM.to_str(data[2]), data[3] or 0)))

    # ---- views
    def render_list(self):
        return self.root.render_list()

    def all_clips(self):
        return list(self.root.walk())
