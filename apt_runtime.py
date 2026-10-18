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

import apt_actions as AA
import apt_movie as M
import apt_vm as VM

PROPS = {"_x", "_y", "_xscale", "_yscale", "_rotation", "_alpha", "_visible"}


class MC:
    """A clip instance (sprite or other placed character)."""

    def __init__(self, rt, char, depth, name, matrix, scale, parent, born_tick):
        self.rt, self.char, self.depth, self.name, self.parent = rt, char, depth, name, parent
        self.placed, self.placed_scale = tuple(matrix), scale
        self.children = {}
        self.vars = {}
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
        self.frames = char.get("frames", []) if char and char["type"] == "Sprite" else []
        self.labels = {}
        for fi, ctls in enumerate(self.frames):
            for c in ctls:
                if c["op"] == "label":
                    self.labels.setdefault(c["label"], fi)

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

    def set_placed(self, matrix=None, scale=None):
        if matrix is not None:
            self.placed = tuple(matrix)
        if scale is not None:
            self.placed_scale = scale

    # ---- script access (the protocol of apt_vm)
    def vm_get(self, name):
        if name in PROPS:
            if name == "_visible":
                return self.visible
            t = self.tp if self.tp is not None else self._decomposed()     # a read does not take the transform from the timeline
            return {"_x": lambda: t["x"], "_y": lambda: t["y"], "_xscale": lambda: t["xs"], "_yscale": lambda: t["ys"],
                    "_rotation": lambda: t["rot"], "_alpha": self.alpha_percent}[name]()
        if name == "_name":
            return self.name
        if name == "_parent":
            return self.parent if self.parent is not None else VM.UNDEF
        if name == "_currentframe":
            return float(self.frame + 1)
        if name == "_totalframes":
            return float(len(self.frames))
        for ch in self.children.values():
            if ch.name == name:
                return ch
        if name in self.vars:
            return self.vars[name]
        return VM.UNDEF

    def vm_set(self, name, value, vm=None, offset=None):
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
        if name in PROPS:
            return self.vm_get(name)
        for ch in self.children.values():
            if ch.name == name:
                return ch
        return VM.UNDEF

    def vm_set_var(self, name, value, vm=None, offset=None):
        self.vars[name] = value

    def vm_call(self, name, args, vm):
        if name in ("gotoAndStop", "gotoAndPlay"):
            self.goto(args[0] if args else 1.0, play=(name == "gotoAndPlay"))
            return VM.UNDEF
        if name == "stop":
            self.playing = False
            return VM.UNDEF
        if name == "play":
            self.playing = True
            return VM.UNDEF
        if name in ("nextFrame", "prevFrame"):
            self.goto(self.frame + 1 + (1 if name == "nextFrame" else -1), play=False)
            return VM.UNDEF
        f = self.vars.get(name)
        if isinstance(f, (VM.Func, VM.Native)):
            return vm.call(f, self, args)
        return vm.missing_call(self.path().replace("/", "."), name, args, "call %s on a clip (not defined in the movie)" % name)

    # ---- timeline
    def goto(self, target, play):
        n = len(self.frames)
        if isinstance(target, str) and not VM.to_number(target) == VM.to_number(target):
            fi = self.labels.get(target)
            if fi is None:
                self.rt.vm.note("goto unknown label %s" % target)
                return
        else:
            fi = int(VM.to_number(target)) - 1
        if n == 0:
            return
        fi = min(max(fi, 0), n - 1)
        self.playing = play
        self.jump(fi)

    def jump(self, fi):
        """Move to frame fi: rebuild the display list, keep instances that stay."""
        objs, _bg, _lb = self.rt.movie.display_list(self.frames, fi)
        keep, fresh = {}, []
        for o in objs:
            old = self.children.get(o["depth"])
            if old is not None and old.char_id == o["char"]:
                old.set_placed(None if old.tp is not None else o["matrix"], o["scale"])
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
        self.rt.run_frame_actions(self, fi)

    def _place(self, c):
        d, flags = c["depth"], c["flags"]
        old = self.children.get(d)
        if old is not None and flags & M.F_MOVE and not (flags & M.F_CHAR and c["char"] != old.char_id):
            if flags & M.F_MATRIX and old.tp is None:
                old.set_placed(matrix=c["matrix"])
            if flags & M.F_CXFORM and old.alpha is None:
                old.set_placed(scale=c["scale"])
            if flags & M.F_NAME:
                old.name = c["name"]
            return None
        if not flags & M.F_CHAR:
            return None
        o = {"depth": d, "char": c["char"], "matrix": c["matrix"] if flags & M.F_MATRIX else M.IDENTITY,
             "scale": c["scale"] if flags & M.F_CXFORM else 0xFFFFFFFF, "name": c["name"] if flags & M.F_NAME else "",
             "clip": c["clip"], "events": c["events"], "born": self.frame}
        mc = self.rt.make_child(self, o)
        self.children[d] = mc
        return mc

    def start(self):
        """A clip that was just placed: enter frame 0, then its initialize events."""
        if self.frames:
            self.enter(0)
        for flags, _key, ptr in self.events:
            if flags & (1 << 0 | 1 << 9 | 1 << 18):          # load, initialize, construct
                self.rt.exec_stream(ptr, self, "clip event")

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
            if not ch.visible:
                continue
            out.append({"depth": d, "char": ch.char_id, "matrix": ch.matrix(), "scale": ch.cx_scale(), "name": ch.name,
                        "clip": ch.clip, "born": 0, "events": [], "mc": ch})
        return out


class Runtime:
    def __init__(self, movie, seed=1, stubs=None, scenario=None):
        self.movie = movie
        self.seed = seed
        self.stubs = dict(stubs or {})            # name -> JSON value, see apt_vm.from_json
        self.scenario = list(scenario or [])      # what the game does: [{"at": tick, "op": set|call|goto, ...}]
        self.stub_log = []                        # (tick, text) for every stub operation
        M.A.ENDIAN = getattr(movie, "endian", M.A.ENDIAN)
        _main, self.consts = AA.read_consts(movie.const)
        self.streams = {}
        self.reset(seed)

    # ---- control
    def reset(self, seed=None):
        if seed is not None:
            self.seed = seed
        self.tick = 0
        self.vm = VM.VM(self.seed)
        self.vm.stubs = dict(self.stubs)
        self.vm.hook = self._hook
        self.stub_log = []
        self.errors = {}
        self.current = None
        root_char = {"id": -1, "type": "Sprite", "frames": self.movie.frames}
        self.root = MC(self, root_char, 0, "_root", M.IDENTITY, 0xFFFFFFFF, None, 0)
        self.root.name = "_root"
        self.vm.root = self.root
        self.bg = None
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
                        self.exec_stream(ptr, mc, "clip enterFrame")
            self.current = None
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

    # ---- streams
    def exec_stream(self, ptr, mc, what):
        if not ptr:
            return
        insns = self.streams.get(ptr)
        if insns is None:
            try:
                insns = AA.decode_stream(self.movie.apt, ptr, self.consts)
            except (AA.DecodeError, Exception) as e:
                self._error(ptr, "decode: %s" % e)
                insns = []
            self.streams[ptr] = insns
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
        char = self.movie.chars.get(o["char"])
        mc = MC(self, char, o["depth"], o["name"], o["matrix"], o["scale"], parent, self.tick)
        mc.clip = o.get("clip", M.NO_CLIP)
        mc.events = [(f, k, p) for (f, k, p) in (o.get("events") or [])]
        return mc

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
