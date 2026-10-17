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

Math.random() comes from a seeded generator (mulberry32). It is a repeatable stand-in, not the game's generator.
"""
import math

import apt_actions as AA


class Undefined:
    def __repr__(self):
        return "undefined"


UNDEF = Undefined()


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


class Obj:
    """A plain script object (with a prototype chain)."""

    def __init__(self, proto=None, name=""):
        self.props, self.proto, self.name = {}, proto, name

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
        vm.note("call %s.%s (not defined in the movie)" % (self.name or "object", name))
        return UNDEF

    def __repr__(self):
        return "[object %s]" % (self.name or "Object")


class Func:
    def __init__(self, insns, params, flags, nregs, dictionary, scope, offset, name=""):
        self.insns, self.params, self.flags, self.nregs = insns, params, flags, nregs
        self.dictionary, self.scope, self.offset, self.name = dictionary, scope, offset, name
        self.props = {"prototype": Obj(name="prototype")}

    # functions are objects too (LoadingScreen.prototype)
    def vm_get(self, name):
        return self.props.get(name, UNDEF)

    def vm_set(self, name, value, vm=None, offset=None):
        self.props[name] = value

    def vm_has(self, name):
        return name in self.props

    def vm_call(self, name, args, vm):
        vm.note("call function.%s" % name)
        return UNDEF

    def __repr__(self):
        return "[function %s @0x%X]" % (self.name, self.offset)


class Native:
    def __init__(self, fn, name=""):
        self.fn, self.name = fn, name

    def vm_get(self, name):
        return UNDEF


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


class Frame:
    def __init__(self, this, scope, dictionary, locals_=None):
        self.this, self.scope, self.dictionary = this, scope, dictionary
        self.locals = locals_
        self.regs = [UNDEF] * 256
        self.stack = []


class VM:
    def __init__(self, seed=1, max_steps=200000):
        self.rng = Rng(seed)
        self.max_steps = max_steps
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
        self.globals.props["Math"] = math_obj
        self.globals.props["_global"] = self.globals
        self.globals.props["ASSetPropFlags"] = Native(lambda vm, this, a: UNDEF, "ASSetPropFlags")
        self.steps = 0

    def note(self, text):
        self.unsupported[text] = self.unsupported.get(text, 0) + 1

    def infer(self, text):
        self.inferred[text] = self.inferred.get(text, 0) + 1

    # ---- calls
    def call(self, f, this, args):
        if isinstance(f, Native):
            return f.fn(self, this, args)
        if not isinstance(f, Func):
            return UNDEF
        fr = Frame(this, f.scope, f.dictionary, {})
        reg, fl = 1, f.flags
        for bit, val in ((0x01, this), (0x04, args), (0x10, UNDEF), (0x40, self.root), (0x80, getattr(this, "parent", UNDEF)),
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
        if name == "this":
            return fr.this
        if name == "_root":
            return self.root
        if name == "_global":
            return self.globals
        if name == "_parent":
            return getattr(fr.this, "parent", UNDEF)
        if fr.locals is not None and name in fr.locals:
            return fr.locals[name]
        sc = fr.scope
        if sc is not None and hasattr(sc, "vm_get_var"):
            v = sc.vm_get_var(name)
            if v is not UNDEF:
                return v
        elif sc is not None:
            v = sc.vm_get(name)
            if v is not UNDEF:
                return v
        return self.globals.props.get(name, UNDEF)

    def set_var(self, fr, name, value, offset=None):
        if fr.locals is not None and name in fr.locals:
            fr.locals[name] = value
            return
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
        if isinstance(obj, str) and name == "length":
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
                st.pop()
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
            elif op in (0x3C, 0x41):
                v, name = st.pop(), st.pop()
                if fr.locals is not None:
                    fr.locals[to_str(name)] = v
                else:
                    self.set_var(fr, to_str(name), v, ins.offset)
                if op == 0x41:
                    pass
            elif op == 0x3D:
                name, argc = to_str(st.pop()), int(to_number(st.pop()))
                args = [st.pop() for _ in range(argc)]
                f = self.get_var(fr, name)
                if isinstance(f, (Func, Native)):
                    st.append(self.call(f, None, args))
                else:
                    self.note("call %s() (not defined in the movie)" % name)
                    st.append(UNDEF)
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
                else:
                    self.note("new %s (not defined in the movie)" % cname)
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
                o = Obj(name="Array")
                for i in range(cnt):
                    o.props[str(i)] = st.pop()
                o.props["length"] = float(cnt)
                st.append(o)
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
            elif op == 0x77:
                pass
            else:
                raise Unsupported("opcode 0x%02X %s" % (op, AA.OPS.get(op, "")))
        return st.pop() if st else UNDEF

    def _call_method(self, obj, name, args):
        if name is UNDEF or name == "" or name is None:
            if isinstance(obj, (Func, Native)):
                return self.call(obj, None, args)
            self.note("call of a value that is not a function")
            return UNDEF
        name = to_str(name)
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
