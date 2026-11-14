#!/usr/bin/env python3
"""
apt_vm.py

A small ActionScript 1/2 stack machine that runs the bytecode decoded by apt_actions.py. It runs what is in the
file: nothing about a particular movie is built in. Whatever it cannot do is recorded, not guessed:
  - an opcode it does not implement raises Unsupported; the handler stops and the opcode is listed in vm.unsupported
  - a call on something that does not exist in the movie (a native command object, for example) does nothing,
    returns undefined and is listed in vm.unsupported as "call ..."

Evidence levels used in the viewer:
  file       opcodes, constants, branch targets and names as decoded from the apt/const entries
  inferred   the meaning given to them here (scope rules, the order registers are preloaded, event order)
  placeholder nothing here; art is drawn from the texture packs

Stubs (evidence level "stub"): the game gives a screen data and natives that are not in the file. A stub is a value or a
return value that the person writes in apt_stubs.json for a name the movie reads or calls and does not define. Nothing
is invented: with no stub the read gives undefined, and every such read, call and new is listed in vm.externals.

Math.random() comes from a seeded generator (mulberry32). It is a repeatable stand-in, not the game's generator.
"""
import math
import sys

import apt_actions as AA


class Undefined:
    def __repr__(self):
        return "undefined"


UNDEF = Undefined()
PROPS = ["_x", "_y", "_xscale", "_yscale", "_currentframe", "_totalframes", "_alpha", "_visible", "_width", "_height",
         "_rotation", "_target", "_framesloaded", "_name", "_droptarget", "_url", "_highquality", "_focusrect",
         "_soundbuftime", "_quality", "_xmouse", "_ymouse"]            # the 22 getProperty / setProperty indexes
MISSING = object()                      # "no stub for this name"


def from_json(v, name=""):
    """A stub value (JSON) as a script value: object -> Obj, list -> array Obj, number -> float."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        return Arr([from_json(x, "%s.%d" % (name, i)) for i, x in enumerate(v)])
    if isinstance(v, dict):
        o = Obj(name=name)
        for k, x in v.items():
            o.props[k] = from_json(x, "%s.%s" % (name, k))
        return o
    return UNDEF


class Unsupported(Exception):
    pass


class Rng:
    """mulberry32, 32 bit state. random() is in [0, 1)."""

    def __init__(self, seed=1):
        self.state = seed & 0xFFFFFFFF
        self.draws = 0

    def random(self):
        self.state = (self.state + 0x6D2B79F5) & 0xFFFFFFFF
        t = self.state
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        self.draws += 1
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0


class CIProps(dict):
    """A property table whose names ignore case, as in the game's script engine (Flash 6 and older): `screen.InitGui()` finds `InitGUI`, `mc.Screen`
    finds `screen`. The first spelling stored is the one kept and enumerated. Evidence: postrace.big sets `var screen` and the root reads `mc.Screen`."""
    __slots__ = ("_lc",)

    def __init__(self, items=None):
        dict.__init__(self)
        self._lc = {}
        for k, v in dict(items or {}).items():
            self[k] = v

    def _real(self, k):
        return self._lc.get(k.lower(), k) if isinstance(k, str) else k

    def __getitem__(self, k):
        return dict.__getitem__(self, self._real(k))

    def __setitem__(self, k, v):
        dict.__setitem__(self, self._lc.setdefault(k.lower(), k) if isinstance(k, str) else k, v)

    def __delitem__(self, k):
        real = self._real(k)
        dict.__delitem__(self, real)
        if isinstance(real, str):
            self._lc.pop(real.lower(), None)

    def __contains__(self, k):
        return dict.__contains__(self, self._real(k))

    def get(self, k, default=None):
        return dict.get(self, self._real(k), default)

    def pop(self, k, *default):
        real = self._real(k)
        if isinstance(real, str) and dict.__contains__(self, real):
            self._lc.pop(real.lower(), None)
        return dict.pop(self, real, *default)

    def setdefault(self, k, default=None):
        if k not in self:
            self[k] = default
        return self[k]

    def clear(self):
        dict.clear(self)
        self._lc.clear()


class Obj:
    """A plain script object (with a prototype chain)."""

    def __init__(self, proto=None, name=""):
        self.props, self.proto, self.name = CIProps(), proto, name

    def vm_get(self, name):
        o = self
        while o is not None:
            if name in o.props:
                return o.props[name]
            o = o.proto
        return UNDEF

    def vm_set(self, name, value, vm=None, offset=None):
        self.props[name] = value

    def vm_has(self, name):
        return self.vm_get(name) is not UNDEF

    def vm_call(self, name, args, vm):
        f = self.vm_get(name)
        if isinstance(f, (Func, Native)):
            return vm.call(f, self, args)
        return vm.missing_call(self.name or "object", name, args, "call %s.%s (not defined in the movie)" % (self.name or "object", name))

    def __repr__(self):
        return "[object %s]" % (self.name or "Object")


ARRAY_PROTO = Obj(name="Array")        # the methods are put in by apt_builtins


class Arr(Obj):
    """A script Array: the elements are in `items`, the methods come from ARRAY_PROTO (apt_builtins)."""

    def __init__(self, items=None):
        Obj.__init__(self, proto=ARRAY_PROTO, name="Array")
        self.items = list(items) if items else []

    def vm_get(self, name):
        if name.lower() == "length":
            return float(len(self.items))
        if name.isdigit():
            i = int(name)
            return self.items[i] if i < len(self.items) else UNDEF
        return Obj.vm_get(self, name)

    def vm_set(self, name, value, vm=None, offset=None):
        if name.lower() == "length":
            n = to_number(value)
            n = int(n) if n == n and 0 <= n < 1e7 else 0
            del self.items[n:]
            self.items.extend([UNDEF] * (n - len(self.items)))
        elif name.isdigit() and int(name) < 10000000:
            i = int(name)
            self.items.extend([UNDEF] * (i + 1 - len(self.items)))
            self.items[i] = value
        else:
            self.props[name] = value

    def __repr__(self):
        return ",".join("" if x is UNDEF or x is None else to_str(x) for x in self.items)


def call_apply(f, name, args, vm):
    """f.call(thisObj, a, b) and f.apply(thisObj, [a, b])."""
    this = args[0] if args else UNDEF
    if name == "call":
        rest = list(args[1:])
    else:
        rest = list(args[1].items) if len(args) > 1 and isinstance(args[1], Arr) else []
    return vm.call(f, this, rest)


class SuperProxy:
    """`super` inside a class function: calling it runs the superclass constructor, super.name(...) the superclass method, both on the same this."""

    def __init__(self, this, proto, ctor):
        self.this, self.proto, self.ctor = this, proto, ctor

    def vm_get(self, name):
        return self.proto.vm_get(name) if self.proto is not None else UNDEF

    def vm_call(self, name, args, vm):
        f = self.vm_get(name)
        if isinstance(f, (Func, Native)):
            return vm.call(f, self.this, args)
        return vm.missing_call("super", name, args, "call super.%s (not defined in the movie)" % name)


class Func:
    def __init__(self, insns, params, flags, nregs, dictionary, scope, offset, name=""):
        self.insns, self.params, self.flags, self.nregs = insns, params, flags, nregs
        self.dictionary, self.scope, self.offset, self.name = dictionary, scope, offset, name
        self.props = CIProps({"prototype": Obj(name="prototype")})
        self.closure = None                    # Env of the call that defined this function

    # functions are objects too (LoadingScreen.prototype)
    def vm_get(self, name):
        return self.props.get(name, UNDEF)

    def vm_set(self, name, value, vm=None, offset=None):
        self.props[name] = value

    def vm_has(self, name):
        return name in self.props

    def vm_call(self, name, args, vm):
        if name.lower() in ("call", "apply"):
            return call_apply(self, name.lower(), args, vm)
        return vm.missing_call("function", name, args, "call function.%s" % name)

    def __repr__(self):
        return "[function %s @0x%X]" % (self.name, self.offset)


class Native:
    """A function done in Python: fn(vm, this, args). With `construct(vm, args)` it is also a class (`new Array()`); `props` are its static members."""

    def __init__(self, fn, name="", construct=None, props=None):
        self.fn, self.name, self.construct = fn, name, construct
        self.props = CIProps(props)

    def vm_get(self, name):
        return self.props.get(name, UNDEF)

    def vm_set(self, name, value, vm=None, offset=None):
        self.props[name] = value

    def vm_has(self, name):
        return name in self.props

    def vm_call(self, name, args, vm):
        f = self.props.get(name, UNDEF)
        if isinstance(f, (Func, Native)):
            return vm.call(f, self, args)
        if name.lower() in ("call", "apply"):
            return call_apply(self, name.lower(), args, vm)
        return vm.missing_call(self.name or "native", name, args, "call %s.%s (not defined)" % (self.name or "native", name))


def to_number(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return 0.0
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return float("nan")
    return float("nan")


def to_str(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v != v:
            return "NaN"
        if v in (float("inf"), float("-inf")):
            return "Infinity" if v > 0 else "-Infinity"
        return "%d" % v if v == int(v) and abs(v) < 1e15 else "%g" % v
    if v is UNDEF:
        return "undefined"
    if v is None:
        return "null"
    return str(v)


def truthy(v):
    if isinstance(v, str):
        n = to_number(v)
        return n == n and n != 0
    if v is UNDEF or v is None:
        return False
    if isinstance(v, (int, float)):
        return v == v and v != 0
    return bool(v)


def _math_guard(fn, rounding):
    def run(vm, this, a):
        try:
            return fn(vm, this, a)
        except OverflowError:
            return to_number(a[0]) if rounding and a else float("inf")
        except ValueError:
            return to_number(a[0]) if rounding and a else float("nan")
    return run


class Env:
    """The local variables of an enclosing function call, kept by a function defined inside it (`var callbackThis = this; setInterval(function () { callbackThis.f(); })`)."""
    __slots__ = ("locals", "outer")

    def __init__(self, locals_, outer):
        self.locals, self.outer = locals_, outer


class Frame:
    def __init__(self, this, scope, dictionary, locals_=None):
        self.this, self.scope, self.dictionary = this, scope, dictionary
        self.locals = locals_
        self.outer = None                       # Env chain of the function this call belongs to
        self.regs = [UNDEF] * 256
        self.stack = []


class VM:
    def __init__(self, seed=1, max_steps=200000):
        self.rng = Rng(seed)
        self.max_steps = max_steps
        self.stubs = {}                # name or "name()" or "owner.name()" -> JSON value (see from_json)
        self.stub_hits = {}            # key -> how often a stub answered
        self._stub_objs = {}
        self.externals = {}            # key -> {"kind": read|call|new, "count", "args"} for names the movie does not define
        self.call_counts = {}          # function offset -> calls so far
        self.unsupported = {}          # description -> count
        self.inferred = {}             # rules applied that are not in the file: description -> count
        self.hook = None               # hook(kind, data): 'branch', 'random', 'set', 'call'
        self.root = None
        self.globals = Obj(name="_global")
        math_obj = Obj(name="Math")
        math_obj.props.update({
            "random": Native(lambda vm, this, a: vm.rng.random(), "Math.random"),
            "round": Native(lambda vm, this, a: float(math.floor(to_number(a[0]) + 0.5)) if a else float("nan"), "Math.round"),
            "floor": Native(lambda vm, this, a: float(math.floor(to_number(a[0]))) if a else float("nan"), "Math.floor"),
            "ceil": Native(lambda vm, this, a: float(math.ceil(to_number(a[0]))) if a else float("nan"), "Math.ceil"),
            "abs": Native(lambda vm, this, a: abs(to_number(a[0])) if a else float("nan"), "Math.abs"),
            "min": Native(lambda vm, this, a: min(to_number(a[0]), to_number(a[1])), "Math.min"),
            "max": Native(lambda vm, this, a: max(to_number(a[0]), to_number(a[1])), "Math.max"),
            "sin": Native(lambda vm, this, a: math.sin(to_number(a[0])), "Math.sin"),
            "cos": Native(lambda vm, this, a: math.cos(to_number(a[0])), "Math.cos"),
            "sqrt": Native(lambda vm, this, a: math.sqrt(to_number(a[0])), "Math.sqrt"),
            "pow": Native(lambda vm, this, a: math.pow(to_number(a[0]), to_number(a[1])), "Math.pow"),
            "atan2": Native(lambda vm, this, a: math.atan2(to_number(a[0]), to_number(a[1])), "Math.atan2"),
            "PI": math.pi})
        for _n, _nat in math_obj.props.items():          # Math on NaN, infinity or out of range answers NaN/infinity like the player, it does not raise
            if isinstance(_nat, Native) and _n != "random":
                _nat.fn = _math_guard(_nat.fn, _n in ("round", "floor", "ceil"))
        self.globals.props["Math"] = math_obj
        self.globals.props["_global"] = self.globals
        self.globals.props["ASSetPropFlags"] = Native(lambda vm, this, a: UNDEF, "ASSetPropFlags")
        apt_builtins.install(self)
        self.steps = 0

    def note(self, text):
        self.unsupported[text] = self.unsupported.get(text, 0) + 1

    def external(self, kind, key, args=None):
        e = self.externals.get(key)
        if e is None:
            e = self.externals[key] = {"kind": kind, "count": 0, "args": None}
        e["count"] += 1
        if args is not None:
            e["args"] = [to_str(a) for a in args]

    def stub(self, key):
        """The stub value for key (the same object every time), or MISSING."""
        if key not in self.stubs:
            return MISSING
        self.stub_hits[key] = self.stub_hits.get(key, 0) + 1
        if key not in self._stub_objs:
            self._stub_objs[key] = from_json(self.stubs[key], key.rstrip("()"))
        return self._stub_objs[key]

    def missing_call(self, owner, name, args, note):
        """A call on something the movie does not define: listed, answered by a stub when there is one."""
        self.note(note)
        key = "%s.%s()" % (owner, name)
        self.external("call", key, args)
        s = self.stub(key)
        return UNDEF if s is MISSING else s

    def infer(self, text):
        self.inferred[text] = self.inferred.get(text, 0) + 1

    # ---- calls
    def call(self, f, this, args):
        if isinstance(f, Native):
            return f.fn(self, this, args)
        if not isinstance(f, Func):
            return UNDEF
        self.call_counts[f.offset] = self.call_counts.get(f.offset, 0) + 1
        fr = Frame(this, f.scope, f.dictionary, CIProps())
        fr.outer = f.closure
        reg, fl = 1, f.flags
        for bit, val in ((0x01, this), (0x04, args), (0x10, self._super_for(f, this)), (0x40, self.root), (0x80, getattr(this, "parent", UNDEF)),
                         (0x100, self.globals)):
            if fl & bit:
                if reg < 256:
                    fr.regs[reg] = val
                reg += 1
        for i, (r, name) in enumerate(f.params):
            v = args[i] if i < len(args) else UNDEF
            if r:
                fr.regs[r] = v
            else:
                fr.locals[name] = v
        return self.run(f.insns, fr)

    # ---- variables
    def get_var(self, fr, name):
        if "." in name and not name.startswith("."):                  # eval("_root.OVERLAY_1"): a path, not a variable name
            head, *rest = name.split(".")
            v = self.get_var(fr, head)
            for part in rest:
                if v is UNDEF or v is None:
                    return UNDEF
                v = self.get_member(v, part)
            return v
        low = name.lower()
        if low == "this":
            return fr.this
        if low == "_root":
            return self.root
        if low == "_global":
            return self.globals
        if low == "_parent":
            return getattr(fr.this, "parent", UNDEF)
        if fr.locals is not None and name in fr.locals:
            return fr.locals[name]
        env = fr.outer
        while env is not None:
            if name in env.locals:
                return env.locals[name]
            env = env.outer
        sc = fr.scope
        if sc is not None and hasattr(sc, "vm_get_var"):
            v = sc.vm_get_var(name)
            if v is not UNDEF:
                return v
        elif sc is not None:
            v = sc.vm_get(name)
            if v is not UNDEF:
                return v
        v = self.globals.props.get(name, UNDEF)
        if v is UNDEF:
            s = self.stub(name)
            if s is not MISSING:
                return s
            self.external("read", name)
        return v

    def set_var(self, fr, name, value, offset=None):
        if fr.locals is not None and name in fr.locals:
            fr.locals[name] = value
            return
        env = fr.outer
        while env is not None:
            if name in env.locals:
                env.locals[name] = value
                return
            env = env.outer
        sc = fr.scope
        if sc is None:
            self.globals.props[name] = value
        elif hasattr(sc, "vm_set_var"):
            sc.vm_set_var(name, value, self, offset)
        else:
            sc.vm_set(name, value, self, offset)

    def get_member(self, obj, name):
        if hasattr(obj, "vm_get"):
            return obj.vm_get(name)
        if isinstance(obj, str) and name.lower() == "length":
            return float(len(obj))
        return UNDEF

    def set_member(self, obj, name, value, offset):
        if hasattr(obj, "vm_set"):
            obj.vm_set(name, value, self, offset)
        else:
            self.note("set %s on %s" % (name, to_str(obj)))

    # ---- the machine
    def _val(self, fr, v):
        k = v[0]
        if k == "str":
            return v[1]
        if k == "float":
            return float(v[1])
        if k == "int":
            return float(v[1])
        if k == "bool":
            return v[1]
        if k == "undef":
            return UNDEF
        if k == "reg":
            return fr.regs[v[1]]
        if k == "lookup":
            d = fr.dictionary
            if d is None or v[1] >= len(d):
                raise Unsupported("dictionary index %d with no dictionary" % v[1])
            return self._val(fr, d[v[1]])
        raise Unsupported("value kind " + k)

    def run(self, insns, fr):
        self.depth = getattr(self, "depth", 0) + 1
        if self.depth == 1:
            self.steps = 0                      # the step limit counts one top-level run, not the whole movie
        try:
            return self._run(insns, fr)
        except IndexError:                      # a pop from an empty stack: the stream is stopped and listed, the viewer goes on
            tb = sys.exc_info()[2]
            while tb.tb_next:
                tb = tb.tb_next
            pc = tb.tb_frame.f_locals.get("pc")
            where = "0x%X" % insns[pc].offset if isinstance(pc, int) and 0 <= pc < len(insns) and hasattr(insns[pc], "offset") else "?"
            raise Unsupported("stack underflow at %s (the stream starts with an operation that needs a value)" % where)
        finally:
            self.depth -= 1

    def _run(self, insns, fr):
        st = fr.stack
        pc = 0
        n = len(insns)
        while pc < n:
            self.steps += 1
            if self.steps > self.max_steps:
                raise Unsupported("step limit (loop?)")
            ins = insns[pc]
            op = ins.op
            pc += 1
            if op is None:
                raise Unsupported(ins.note or "undecodable instruction at 0x%X" % ins.offset)
            if op == 0x00:
                break
            elif op in (0x96, 0xA1, 0xB4, 0xB5, 0xB6, 0xB7, 0xB9, 0xA2, 0xA3):
                for v in ins.args:
                    st.append(self._val(fr, v))
            elif op == 0x88:
                fr.dictionary = list(ins.args)
            elif op == 0x17:
                if st:
                    st.pop()
                else:
                    self.note("pop on an empty stack at 0x%X (the compiler emits one after `if (!x) { x = function ... }`; the game ignores it)" % ins.offset)
            elif op == 0x4C:
                st.append(st[-1])
            elif op == 0x4D:
                st[-1], st[-2] = st[-2], st[-1]
            elif op in (0x0A, 0x47):
                b, a = st.pop(), st.pop()
                if isinstance(a, str) or isinstance(b, str):
                    st.append(to_str(a) + to_str(b))
                else:
                    st.append(to_number(a) + to_number(b))
            elif op == 0x0B:
                b, a = st.pop(), st.pop()
                st.append(to_number(a) - to_number(b))
            elif op == 0x0C:
                b, a = st.pop(), st.pop()
                st.append(to_number(a) * to_number(b))
            elif op == 0x0D:
                b, a = st.pop(), st.pop()
                x, y = to_number(a), to_number(b)
                st.append(x / y if y else (float("nan") if x == 0 or x != x else math.copysign(float("inf"), x)))
            elif op == 0x3F:
                b, a = st.pop(), st.pop()
                y = to_number(b)
                st.append(math.fmod(to_number(a), y) if y else float("nan"))
            elif op in (0x0F, 0x48):
                b, a = st.pop(), st.pop()
                st.append(to_number(a) < to_number(b) if not (isinstance(a, str) and isinstance(b, str)) else a < b)
            elif op == 0x67:
                b, a = st.pop(), st.pop()
                st.append(to_number(a) > to_number(b) if not (isinstance(a, str) and isinstance(b, str)) else a > b)
            elif op in (0x0E, 0x49, 0x66):
                b, a = st.pop(), st.pop()
                if isinstance(a, str) and isinstance(b, str):
                    st.append(a == b)
                elif a is UNDEF or b is UNDEF or a is None or b is None:
                    st.append((a is UNDEF or a is None) and (b is UNDEF or b is None))
                elif hasattr(a, "vm_get") or hasattr(b, "vm_get"):
                    st.append(a is b)
                else:
                    st.append(to_number(a) == to_number(b))
            elif op == 0x12:
                st.append(not truthy(st.pop()))
            elif op in (0x10, 0x11):
                b, a = st.pop(), st.pop()
                st.append((truthy(a) and truthy(b)) if op == 0x10 else (truthy(a) or truthy(b)))
            elif op in (0x50, 0x51):
                st.append(to_number(st.pop()) + (1 if op == 0x50 else -1))
            elif op == 0x4A:
                st.append(to_number(st.pop()))
            elif op == 0x4B:
                st.append(to_str(st.pop()))
            elif op == 0x18:
                v = to_number(st.pop())
                st.append(float(int(v)) if v == v and abs(v) != float("inf") else v)
            elif op in (0x60, 0x61, 0x62, 0x63, 0x64, 0x65):
                b, a = int(to_number(st.pop())), int(to_number(st.pop()))
                st.append(float({0x60: a & b, 0x61: a | b, 0x62: a ^ b, 0x63: a << (b & 31), 0x64: a >> (b & 31),
                                 0x65: (a & 0xFFFFFFFF) >> (b & 31)}[op]))
            elif op == 0x44:
                v = st.pop()
                st.append("number" if isinstance(v, float) else "string" if isinstance(v, str) else
                          "boolean" if isinstance(v, bool) else "undefined" if v is UNDEF else "object")
            elif op == 0x56:
                st.append(fr.this)
            elif op == 0x58:
                st.append(self.globals)
            elif op == 0x59:
                st.append(0.0)
            elif op == 0x5A:
                st.append(1.0)
            elif op == 0x73:
                st.append(True)
            elif op == 0x74:
                st.append(False)
            elif op == 0x75:
                st.append(None)
            elif op == 0x76:
                st.append(UNDEF)
            elif op == 0x1C:
                st.append(self.get_var(fr, to_str(st.pop())))
            elif op == 0x1D:
                v, name = st.pop(), st.pop()
                self.set_var(fr, to_str(name), v, ins.offset)
                self._event("set", ("var", to_str(name), v, ins.offset))
            elif op == 0xA4:
                st.append(self.get_var(fr, ins.args[0][1]))
            elif op == 0xA6:
                self.set_var(fr, ins.args[0][1], st.pop(), ins.offset)
            elif op == 0xAE:
                st.append(self.get_var(fr, to_str(self._val(fr, ins.args[0]))))
            elif op == 0x4E:
                name, obj = st.pop(), st.pop()
                st.append(self.get_member(obj, to_str(name)))
            elif op == 0xA5:
                st.append(self.get_member(st.pop(), ins.args[0][1]))
            elif op == 0xAF:
                st.append(self.get_member(st.pop(), to_str(self._val(fr, ins.args[0]))))
            elif op == 0x4F:
                v, name, obj = st.pop(), st.pop(), st.pop()
                self.set_member(obj, to_str(name), v, ins.offset)
                self._event("set", ("member", to_str(name), v, ins.offset, obj))
            elif op == 0xA7:
                v, obj = st.pop(), st.pop()
                self.set_member(obj, ins.args[0][1], v, ins.offset)
                self._event("set", ("member", ins.args[0][1], v, ins.offset, obj))
            elif op == 0x87:
                fr.regs[ins.args[0][1]] = st[-1]
            elif op == 0x41:
                name = to_str(st.pop())                         # defineLocal2: `var name;` pops the name only, no value
                if fr.locals is not None:
                    fr.locals.setdefault(name, UNDEF)
            elif op == 0x3C:
                v, name = st.pop(), st.pop()
                if fr.locals is not None:
                    fr.locals[to_str(name)] = v
                else:
                    self.set_var(fr, to_str(name), v, ins.offset)
            elif op == 0x3D:
                name, argc = to_str(st.pop()), int(to_number(st.pop()))
                args = [st.pop() for _ in range(argc)]
                f = self.get_var(fr, name)
                if isinstance(f, (Func, Native)):
                    # a plain call runs with the timeline as this (the clip whose script calls it), not with nothing
                    st.append(self.call(f, fr.this if hasattr(fr.this, "vm_get_var") else self.root, args))
                else:
                    self.note("call %s() (not defined in the movie)" % name)
                    self.external("call", name + "()", args)
                    s = self.stub(name + "()")
                    st.append(UNDEF if s is MISSING else s)
            elif op == 0x52:
                name, obj, argc = st.pop(), st.pop(), int(to_number(st.pop()))
                args = [st.pop() for _ in range(argc)]
                st.append(self._call_method(obj, name, args))
            elif op == 0x40:
                cname, argc = to_str(st.pop()), int(to_number(st.pop()))
                args = [st.pop() for _ in range(argc)]
                ctor = self.get_var(fr, cname)
                if isinstance(ctor, Func):
                    inst = Obj(proto=ctor.props.get("prototype"), name=cname)
                    self.call(ctor, inst, args)
                    st.append(inst)
                elif isinstance(ctor, Native) and ctor.construct is not None:
                    st.append(ctor.construct(self, args))
                else:
                    self.note("new %s (not defined in the movie)" % cname)
                    self.external("new", cname, args)
                    st.append(Obj(name=cname))
            elif op == 0x43:
                cnt = int(to_number(st.pop()))
                o = Obj()
                for _ in range(cnt):
                    v, k = st.pop(), st.pop()
                    o.props[to_str(k)] = v
                st.append(o)
            elif op == 0x42:
                cnt = int(to_number(st.pop()))
                st.append(Arr([st.pop() for _ in range(cnt)]))
            elif op in (0x8E, 0x9B):
                name = ins.args[0][1]
                params = []
                for p in ins.args[1:]:
                    if op == 0x8E and p[1].startswith("r") and "=" in p[1]:
                        r, nm = p[1][1:].split("=", 1)
                        params.append((int(r), nm))
                    else:
                        params.append((0, p[1]))
                flags, nregs = 0, 0
                if op == 0x8E and ins.note.startswith("registers"):
                    parts = ins.note.replace(",", "").split()
                    nregs, flags = int(parts[1]), int(parts[3], 16)
                f = Func(ins.body, params, flags, nregs, fr.dictionary, fr.scope, ins.offset, name)
                f.closure = Env(fr.locals, fr.outer) if fr.locals is not None else fr.outer
                if name:
                    self.set_var(fr, name, f, ins.offset)
                else:
                    st.append(f)
            elif op == 0x3E:
                return st.pop() if st else UNDEF
            elif op in (0x99, 0x9D, 0xB8):
                if ins.target is None:
                    raise Unsupported("branch with no target at 0x%X" % ins.offset)
                if op == 0x99:
                    pc = ins.target
                else:
                    c = truthy(st.pop())
                    taken = c if op == 0x9D else not c
                    self._event("branch", (ins.offset, taken))
                    if taken:
                        pc = ins.target
            elif op == 0x30:
                st.append(float(int(self.rng.random() * to_number(st.pop()))))
            elif op in (0x3A, 0x3B):
                st.pop()
                if op == 0x3A:
                    st.pop()
                st.append(True)
            elif op in (0x06, 0x07, 0x04, 0x05):
                self._clip_call(fr.this, {0x06: "play", 0x07: "stop", 0x04: "nextFrame", 0x05: "prevFrame"}[op], [])
            elif op in (0x81, 0x8C):
                self._clip_call(fr.this, "gotoAndStop", [ins.args[0][1]])
            elif op == 0x9F:
                f = st.pop()
                self._clip_call(fr.this, "gotoAndPlay" if ins.args[0][1] else "gotoAndStop", [f])
            elif op == 0x26:
                self.note("trace: %s" % to_str(st.pop()))
            elif op == 0x69:
                sup, sub = st.pop(), st.pop()
                if isinstance(sub, Func) and (isinstance(sup, Func) or (isinstance(sup, Native) and "prototype" in sup.props)):
                    proto = Obj(proto=sup.props.get("prototype"), name=sub.name)
                    proto.props["constructor"] = sup            # what super() calls from the subclass
                    sub.props["prototype"] = proto
                else:
                    self.note("extends: %s extends %s (the superclass is not defined in the movie)" % (to_str(sub), to_str(sup)))
                    self.external("extends", to_str(sup), None)
            elif op == 0x2C:
                st.pop()                                        # implements: the interface list is not kept
                for _ in range(int(to_number(st.pop()))):
                    st.pop()
            elif op == 0x2B:
                obj, ctor = st.pop(), st.pop()                  # cast: the object, unchecked
                st.append(obj)
            elif op == 0x34:
                st.append(float(getattr(self, "now_ms", 0.0)))
            elif op in (0x22, 0x23):
                value = st.pop() if op == 0x23 else None
                idx, tgt = int(to_number(st.pop())), st.pop()
                pname = PROPS[idx] if 0 <= idx < len(PROPS) else "prop%d" % idx
                tgt = fr.this if tgt in (UNDEF, "") else tgt
                if op == 0x22:
                    st.append(self.get_member(tgt, pname))
                else:
                    self.set_member(tgt, pname, value, ins.offset)
            elif op == 0x55:
                obj = st.pop()
                st.append(UNDEF)                                # the end mark the loop looks for
                for k in reversed(self._enum_keys(obj)):
                    st.append(k)
            elif op == 0x09:
                pass                                            # stopAllSounds: nothing is played
            elif op == 0x25:
                target = st.pop()
                self._clip_call(fr.this if target in (UNDEF, "") else target, "removeMovieClip", [])
            elif op == 0x53:
                name, obj, argc = st.pop(), st.pop(), int(to_number(st.pop()))
                args = [st.pop() for _ in range(argc)]
                ctor = obj if name in (UNDEF, "") else self.get_member(obj, to_str(name))
                if isinstance(ctor, Func):
                    inst = Obj(proto=ctor.props.get("prototype"), name=ctor.name)
                    self.call(ctor, inst, args)
                    st.append(inst)
                elif isinstance(ctor, Native) and ctor.construct is not None:
                    st.append(ctor.construct(self, args))
                else:
                    self.note("new %s.%s (not defined in the movie)" % (to_str(obj), to_str(name)))
                    self.external("new", "%s.%s" % (to_str(obj), to_str(name)), args)
                    st.append(Obj(name=to_str(name)))
            elif op == 0x77:
                pass
            else:
                raise Unsupported("opcode 0x%02X %s" % (op, AA.OPS.get(op, "")))
        return st.pop() if st else UNDEF

    def _super_for(self, f, this):
        """The `super` of function f running on this: found through the prototype `extends` linked (its `constructor` is the superclass)."""
        o = this.proto if isinstance(this, Obj) else None
        while o is not None:
            if o is f.props.get("prototype") or any(v is f for v in o.props.values()):
                ctor = o.props.get("constructor")
                return SuperProxy(this, o.proto, ctor) if isinstance(ctor, (Func, Native)) else UNDEF
            o = o.proto
        return UNDEF

    def _enum_keys(self, obj):
        """The names for (x in obj): own and inherited properties, no `length` of an array and no internal names."""
        keys, o = [], obj
        while isinstance(o, Obj) and o is not ARRAY_PROTO:          # the built-in methods are not enumerable
            if isinstance(o, Arr):
                keys.extend(str(i) for i in range(len(o.items)) if str(i) not in keys)
            for k in o.props:
                if k not in keys and k not in ("length", "constructor") and not k.startswith("__"):
                    keys.append(k)
            o = o.proto
        return keys

    def _call_method(self, obj, name, args):
        if isinstance(obj, SuperProxy) and (name is UNDEF or name == "" or name is None):
            return self.call(obj.ctor, obj.this, args)
        if name is UNDEF or name == "" or name is None:
            if isinstance(obj, (Func, Native)):
                return self.call(obj, None, args)
            self.note("call of a value that is not a function")
            return UNDEF
        name = to_str(name)
        if isinstance(obj, (str, float)):
            handled, value = apt_builtins.primitive_call(self, obj, name, args)
            if handled:
                return value
        if hasattr(obj, "vm_call"):
            self._event("call", (name, obj))
            return obj.vm_call(name, args, self)
        self.note("call %s on %s" % (name, to_str(obj)))
        return UNDEF

    def _clip_call(self, clip, name, args):
        if hasattr(clip, "vm_call"):
            clip.vm_call(name, args, self)

    def _event(self, kind, data):
        if self.hook is not None:
            self.hook(kind, data)


import apt_builtins  # noqa: E402  (after everything above: apt_builtins imports this module)
