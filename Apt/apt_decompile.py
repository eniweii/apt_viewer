#!/usr/bin/env python3
"""
apt_decompile.py

Turns decoded APT bytecode (apt_actions.Insn lists) into readable pseudo-code for the Behavior view. It is a
best-effort reader of the patterns the compiler produces, not a complete decompiler:
  - expressions are rebuilt from the stack (push, getVariable, getMember, calls, arithmetic, comparisons)
  - "X; not; branchIfTrue L" becomes if (X) { ... }, a forward branchAlways at the end of that block becomes else
  - "X; pushDuplicate; not; branchIfTrue L; pop; Y" becomes (X && Y); without the not it becomes (X || Y)
  - a backward branchAlways makes a loop: "L: C; branchIfTrue E; ...; branchAlways L; E:" becomes while (!C) { ... } (the
    exit test is normalised the same way as an if), no exit test gives while (true), a backward branchIfTrue with no
    matching back jump gives do { } while (C); inside a loop a jump to the loop exit is break, a jump back to the loop
    start is continue
  - a switch is recognised from its compiled shape: "storeRegister R; push K; strictEquals; branchIfTrue Lcase" followed by
    "push R, K; strictEquals; branchIfTrue Lcase" tests and one branchAlways to the default (or the end); the jumps to
    the end become break
  - for (x in obj) from enumerate2, delete, extends/implements, cast, typeof, getTimer, getProperty/setProperty, array and
    object literals, new obj.method(), gotoAndPlay/Stop; callMethod without a name calls the object (super(...)); dynamic
    variable names print as eval(expr); inside functions the preloaded registers and parameters print by name
  - trace pops its message and prints trace(message); the bit shifts print as << >> >>>
  - a branchAlways to the next instruction (the compiler's empty else) is dropped
  - anything else is printed as a comment with the raw instruction, never guessed
Output: lines of text, and a dict {instruction offset: statement text} for assignments (used by the inspector to show
which formula wrote a value).
"""
import re

import apt_actions as AA

BIN = {0x0A: "+", 0x47: "+", 0x0B: "-", 0x0C: "*", 0x0D: "/", 0x3F: "%", 0x0F: "<", 0x48: "<", 0x67: ">",
       0x0E: "==", 0x49: "==", 0x66: "===", 0x10: "&&", 0x11: "||", 0x60: "&", 0x61: "|", 0x62: "^", 0x63: "<<", 0x64: ">>", 0x65: ">>>"}


PROPS = ["_x", "_y", "_xscale", "_yscale", "_currentframe", "_totalframes", "_alpha", "_visible", "_width", "_height",
         "_rotation", "_target", "_framesloaded", "_name", "_droptarget", "_url", "_highquality", "_focusrect",
         "_soundbuftime", "_quality", "_xmouse", "_ymouse"]


def _lit(v, dictionary, regnames=None):
    k = v[0]
    if k == "reg" and regnames and v[1] in regnames:
        return regnames[v[1]]
    if k == "lookup":
        if dictionary is not None and v[1] < len(dictionary):
            return _lit(dictionary[v[1]], None)
        return "dict[%d]" % v[1]
    return AA.fmt(v, None)


def _name(expr):
    return expr[1:-1] if len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"' else "[%s]" % expr


def _vname(expr):
    """A variable name: bare when it is a plain string, else eval(expr) (a name computed at run time)."""
    return expr[1:-1] if len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"' else "eval(%s)" % expr


def _member(obj, nm):
    """obj.name for a plain string name, obj[expr] otherwise."""
    if len(nm) >= 2 and nm[0] == '"' and nm[-1] == '"' and nm[1:-1].replace("_", "a").isalnum():
        return "%s.%s" % (obj, nm[1:-1])
    return "%s[%s]" % (obj, nm)


def _count(text):
    try:
        return int(float(text))
    except ValueError:
        return 0


# instructions after which nothing is left on the stack (a statement ends here), used to tell c ? a : b from if/else
STATEMENT_END = {0x17, 0x4F, 0x1D, 0x3C, 0x41, 0x3E, 0x99, 0x9D, 0xB8, 0x26, 0x23, 0x3A, 0x3B, 0x2A, 0x25, 0x06, 0x07,
                 0x04, 0x05, 0x81, 0x8C, 0x8B, 0x20, 0x83, 0x9A, 0x5B, 0x5C, 0x5D, 0x5E, 0xB0, 0xB1, 0xB2, 0xB3, 0x72,
                 0x08, 0x09, 0x27, 0x28, 0x9F, 0x9E, 0x8F, 0x94, 0x00}


class Decompiler:
    def __init__(self):
        self.lines = []
        self.raw = []
        self.assign = {}
        self.fns = []

    @staticmethod
    def _regnames(ins):
        """{register: name} inside a function: the preloaded this/arguments/super/_root/_parent/_global and the named parameters."""
        out = {}
        for part in Decompiler._preload(ins.note).lstrip("; ").split(", "):
            if "=" in part:
                r, nm = part.split("=", 1)
                out[int(r[1:])] = nm
        for a in ins.args[1:]:
            m = re.match(r"^r(\d+)=(.+)$", str(a[1]))
            if m:
                out[int(m.group(1))] = m.group(2)
        return out

    @staticmethod
    def _preload(note):
        """Which registers a function gets preloaded (order: this, arguments, super, _root, _parent, _global)."""
        try:
            flags = int(note.split("flags")[1].strip(), 16)
        except (IndexError, ValueError):
            return ""
        names, reg, out = ((0x01, "this"), (0x04, "arguments"), (0x10, "super"), (0x40, "_root"), (0x80, "_parent"), (0x100, "_global")), 1, []
        for bit, nm in names:
            if flags & bit:
                out.append("r%d=%s" % (reg, nm))
                reg += 1
        return "; " + ", ".join(out) if out else ""

    @staticmethod
    def _find_loops(insns):
        """Loops from backward jumps: (start, header index or None, last index, "while" | "dowhile").
        A backward branchAlways ends a while loop; the earlier backward jumps to the same start are continues.
        The header is the first forward conditional jump from the start that leaves just past the loop."""
        out, last_back = [], {}
        for k, ins in enumerate(insns):
            if ins.op == 0x99 and ins.target is not None and ins.target <= k:
                last_back[ins.target] = k
        for t, b in sorted(last_back.items()):
            h = None
            for k in range(t, b):
                if insns[k].op in (0x9D, 0xB8) and insns[k].target == b + 1:
                    h = k
                    break
            out.append((t, h, b, "while"))
        for k, ins in enumerate(insns):
            if ins.op in (0x9D, 0xB8) and ins.target is not None and ins.target <= k and ins.target not in last_back:
                out.append((ins.target, None, k, "dowhile"))
        return sorted(out, key=lambda lp: (lp[0], -lp[2]))

    @staticmethod
    def _find_ternaries(insns):
        """"c ? a : b" compiles like an if/else but leaves a value on the stack: "branchIfTrue Lelse; <a>; branchAlways Lend;
        Lelse: <b>; Lend:". Told apart from if/else by both branches ending in a value, not in a statement.
        Returns {branchIfTrue index: (else start, end)}."""
        def statement(k):
            ins = insns[k]
            if ins.op in (0x8E, 0x9B):
                return bool(ins.args and ins.args[0][1])            # a named function is a declaration
            return ins.op in STATEMENT_END
        out, n = {}, len(insns)
        for j, ins in enumerate(insns):
            t = ins.target
            if ins.op != 0x9D or t is None or t <= j + 1 or t > n or insns[t - 1].op != 0x99:
                continue
            e = insns[t - 1].target
            if e is None or e <= t or e > n or t - 2 <= j or e - 1 <= t - 1 or statement(t - 2) or statement(e - 1):
                continue
            out[j] = (t, e)
        return out

    @staticmethod
    def _for_in(insns, i):
        """enumerate2 at i followed by the for-in loop head and a loop that jumps back to it."""
        if i + 5 >= len(insns) or [x.op for x in insns[i + 1:i + 4]] != [0x87, 0x96, 0x49] or insns[i + 4].op != 0x9D:
            return False
        t = insns[i + 4].target
        return t is not None and t > i + 5 and insns[t - 1].op == 0x99 and insns[t - 1].target == i + 1

    @staticmethod
    def _falls(insns, i):
        """True when the branchAlways at i only jumps over jumps to the next instruction, i.e. it falls through."""
        t, k = insns[i].target, i + 1
        while t is not None and k < t and insns[k].op == 0x99 and insns[k].target == k + 1:
            k += 1
        return t is not None and k == t

    @staticmethod
    def _find_or_ifs(insns):
        """"if (a || b) { ... }" compiles to "A; branchIfTrue L; B; branchIfTrue L; branchAlways S; L: ...".
        Returns {index of the last test branch: (indices of all test branches, S)}."""
        by_target, out = {}, {}
        for j, ins in enumerate(insns):
            if ins.op == 0x9D and ins.target is not None and ins.target > j:
                by_target.setdefault(ins.target, []).append(j)
        for k, ins in enumerate(insns):
            if ins.op == 0x99 and ins.target is not None and ins.target > k + 1 and k >= 1:
                members = [j for j in by_target.get(k + 1, ()) if j < k]
                if members and members[-1] == k - 1:
                    out[k - 1] = (members, ins.target, k)
        return out

    @staticmethod
    def _find_switches(insns):
        """Switch statements by their compiled shape: {first test branch index: info}.
        "storeRegister R; <case value code>; strictEquals; branchIfTrue Lcase" is the first test, every later test starts with
        "push R, ..." and ends the same way; one branchAlways after the last test jumps to the default (or the end).
        info: store (storeRegister index), tests (branch indices), default, end (where the break jumps go), skip_to."""
        out, n = {}, len(insns)
        stop = (0x99, 0x9D, 0xB8, 0x8E, 0x9B, 0x8F, 0x3E)

        def test_end(k):
            """Index of the test branch for a test whose code starts at k (strictEquals then forward branchIfTrue)."""
            while k < n and insns[k].op not in stop:
                if insns[k].op == 0x66:
                    b = insns[k + 1] if k + 1 < n else None
                    return k + 1 if b is not None and b.op == 0x9D and b.target is not None and b.target > k + 1 else None
                k += 1
            return None
        for s_, ins in enumerate(insns):
            if ins.op != 0x87 or not ins.args:
                continue
            reg = ins.args[0][1]
            j = test_end(s_ + 1)
            if j is None:
                continue
            tests, k = [j], j + 1
            while k < n and insns[k].op == 0x96 and insns[k].args and insns[k].args[0] == ("reg", reg):
                e = test_end(k + 1)
                if e is None:
                    break
                tests.append(e)
                k = e + 1
            if k >= n or insns[k].op != 0x99 or insns[k].target is None or insns[k].target <= k or len(tests) < 2:
                continue
            default = insns[k].target
            starts = sorted({insns[t].target for t in tests})
            votes = {}
            for st_ in starts[1:] + [default]:
                if 0 < st_ <= n and insns[st_ - 1].op == 0x99 and insns[st_ - 1].target is not None and insns[st_ - 1].target > st_ - 1:
                    votes[insns[st_ - 1].target] = votes.get(insns[st_ - 1].target, 0) + 1
            end = max(votes, key=lambda v: (votes[v], v)) if votes else default
            if end < max(starts + [default]):
                continue
            out[tests[0]] = {"store": s_, "tests": tests, "default": default, "end": end, "skip_to": k + 1}
        return out

    def _expand(self, text, off=None):
        """An assignment whose value is a function prints the function body after it."""
        if "\x00FN" in text:
            m = re.match(r"^(.*)\x00FN(\d+);$", text, re.S)
            if m and "\x00FN" not in m.group(1):
                head, body = self.fns[int(m.group(2))]
                return [m.group(1) + head] + ["  " + l for l in body] + ["};" if m.group(1) else "}"]
            # a function literal used as a value (callback argument ...): print it in place
            def inline(mm):
                head, body = self.fns[int(mm.group(1))]
                return "\n".join([head] + ["  " + l for l in body] + ["}"])
            return re.sub(r"\x00FN(\d+)", inline, text).split("\n")
        return [text]

    def run(self, insns, dictionary=None, indent=0, regnames=None):
        st, closers, sc = [], [], {}
        rn = lambda r: (regnames or {}).get(r, "r%d" % r)
        pad = lambda: "  " * (indent + len(closers))
        i, n = 0, len(insns)
        loops = self._find_loops(insns)                    # [(start, header or None, end, kind)]
        starts = {}
        for lp in loops:
            starts.setdefault(lp[0], []).append(lp)
        headers = {lp[1]: lp for lp in loops if lp[1] is not None and lp[3] == "while"}
        ends = {lp[2] for lp in loops if lp[3] == "while"}
        lstack = []                                        # loops and switches we are inside, innermost last
        switches = self._find_switches(insns)
        sw_store = {sw["store"]: j for j, sw in switches.items()}
        sw_test = {t: j for j, sw in switches.items() for t in sw["tests"]}
        sw_cases = {}
        sw_last = set()
        for sw in switches.values():
            loops.append((sw["tests"][0], None, sw["end"] - 1, "switch"))    # a jump to its end is a break
            if sw["end"] - 1 < n and insns[sw["end"] - 1].op == 0x99 and insns[sw["end"] - 1].target == sw["end"]:
                sw_last.add(sw["end"] - 1)                 # the last body's own jump to the end: not printed
        labels_at = {}
        sw_expr = {}
        or_last = {j: v for j, v in self._find_or_ifs(insns).items()
                   if len(v[0]) >= 2 or not any(lp[0] <= v[2] <= lp[2] and v[1] == lp[2] + 1 for lp in loops)}
        or_member = {j: last for last, (ms, _s, _k) in or_last.items() for j in ms[:-1]}
        or_acc = {}
        tern = self._find_ternaries(insns)
        tern_jump = {t - 1: e for t, e in tern.values()}
        tern_pending = {}                                  # end index -> [[condition, then value], ...]

        def open_if(pos, tgt, idx):
            for line in self._expand("if (%s) {" % pos):
                self.lines.append(pad() + line)
            closers.append((tgt, "if"))
            # a forward branchAlways just before the end of the block makes it if/else
            # (not when that jump is a break out of an enclosing loop, or only jumps over no-op jumps)
            last = insns[tgt - 1] if tgt - 1 > idx else None
            if (last is not None and last.op == 0x99 and last.target is not None and last.target > tgt
                    and not self._falls(insns, tgt - 1)
                    and not any(lp[0] <= tgt - 1 <= lp[2] and last.target == lp[2] + 1 for lp in loops)):
                closers[-1] = (tgt - 1, "ifelse", last.target)

        def jump_kind(idx, tgt):
            """break / continue when the jump at idx leaves or restarts an enclosing loop, else None."""
            for lp in reversed(lstack):
                if tgt == lp[2] + 1 and idx != lp[2]:
                    return "break"
                if tgt == lp[0] and idx != lp[2] and lp[3] == "while":
                    return "continue"
            return None
        while i < n:
            while closers and closers[-1][0] <= i:
                if closers[-1][1] == "ifelse" and closers[-1][0] == i and insns[i].op == 0x99:
                    break                                  # handled below as "} else {"
                if closers[-1][1] == "dowhile":
                    break                                  # closed by its own branch below
                if closers[-1][1] in ("loop", "switch"):
                    lstack.pop()
                closers.pop()
                self.lines.append(pad() + "}")
            for text in labels_at.pop(i, ()):
                self.lines.append(pad()[:-2] + text)
            for lp in starts.get(i, ()):
                if lp[3] == "dowhile":
                    self.lines.append(pad() + "do {")
                    closers.append((lp[2], "dowhile"))
                    lstack.append(lp)
                elif lp[1] is None:
                    self.lines.append(pad() + "while (true) {")
                    closers.append((lp[2], "loop"))
                    lstack.append(lp)
            for entry in reversed(tern_pending.pop(i, [])):
                other = st.pop() if st else "?"
                c, a = entry
                if c.startswith("!(") and c.endswith(")"):
                    st.append("(%s ? %s : %s)" % (c[2:-1], a, other))
                else:
                    st.append("(%s ? %s : %s)" % (c, other, a))
            if i in sc:
                left = sc.pop(i)
                right = st.pop() if st else "?"
                st.append("(%s %s %s)" % (left[0], left[1], right))
            ins = insns[i]
            op = ins.op

            def emit(text, off=None):
                for line in self._expand(text):
                    self.lines.append(pad() + line)
                if off is not None:
                    self.assign[off] = text if "\x00FN" not in text else text.split("\x00FN")[0] + "function"

            def pop():
                return st.pop() if st else "?"
            # short circuit
            if op == 0x4C and i + 3 < n:
                if insns[i + 1].op == 0x12 and insns[i + 2].op == 0x9D and insns[i + 3].op == 0x17 and insns[i + 2].target:
                    sc[insns[i + 2].target] = (pop(), "&&")
                    i += 4
                    continue
                if insns[i + 1].op == 0x9D and insns[i + 2].op == 0x17 and insns[i + 1].target:
                    sc[insns[i + 1].target] = (pop(), "||")
                    i += 3
                    continue
            if op in (0x96,):
                for v in ins.args:
                    st.append(_lit(v, dictionary, regnames))
            elif op == 0x88:
                dictionary = list(ins.args)
            elif op in (0xA1, 0xB4, 0xB5, 0xB6, 0xB7, 0xB9, 0xA2, 0xA3):
                for v in ins.args:
                    st.append(_lit(v, dictionary, regnames))
            elif op == 0x1C:
                st.append(_vname(pop()))
            elif op == 0xA4:
                st.append(ins.args[0][1])
            elif op == 0x4E:
                nm = pop()
                obj = pop()
                st.append("%s.%s" % (obj, _name(nm)) if nm.startswith('"') else "%s[%s]" % (obj, nm))
            elif op == 0xA5:
                st.append("%s.%s" % (pop(), ins.args[0][1]))
            elif op == 0x1D:
                v, nm = pop(), pop()
                emit("%s = %s;" % (_vname(nm), v), ins.offset)
            elif op == 0x4F:
                v, nm, obj = pop(), pop(), pop()
                emit("%s.%s = %s;" % (obj, _name(nm), v) if nm.startswith('"') else "%s[%s] = %s;" % (obj, nm, v), ins.offset)
            elif op == 0x41:
                emit("var %s;" % _vname(pop()), ins.offset)    # defineLocal2 declares with no value: it pops the name only
            elif op == 0x3C:
                v, nm = pop(), pop()
                emit("var %s = %s;" % (_vname(nm), v), ins.offset)
            elif op == 0x87 and i in sw_store:
                sw_expr[sw_store[i]] = pop()               # the switch value; printed with the switch
                st.append(rn(ins.args[0][1]))
            elif op == 0x9D and i in sw_test:
                j0 = sw_test[i]
                sw = switches[j0]
                cond = pop()
                sw_cases.setdefault(j0, []).append((ins.target, cond[cond.index(" === ") + 5:-1]))
                if i != sw["tests"][-1]:
                    i += 1
                    continue
                emit("switch (%s) {" % sw_expr.get(j0, "?"))
                closers.append((sw["end"], "switch"))
                lstack.append((j0, None, sw["end"] - 1, "switch"))
                names = {}
                for tgt, k_text in sw_cases[j0]:
                    names.setdefault(tgt, []).append("case %s:" % k_text)
                dflt = sw["default"]
                if dflt != sw["end"] and not (dflt == sw["end"] - 1 and dflt in sw_last):
                    names.setdefault(dflt, []).append("default:")
                for tgt, lst in names.items():
                    labels_at.setdefault(tgt, []).extend(lst)
                i = sw["skip_to"]
                continue
            elif op == 0x87:
                v = pop()
                emit("%s = %s;" % (rn(ins.args[0][1]), v), ins.offset)
                st.append(rn(ins.args[0][1]))
            elif op in BIN:
                b, a = pop(), pop()
                st.append("(%s %s %s)" % (a, BIN[op], b))
            elif op == 0x12:
                x = pop()
                st.append(x[2:-1] if x.startswith("!(") and x.endswith(")") else "!(%s)" % x)
            elif op == 0x4C:
                st.append(st[-1] if st else "?")
            elif op == 0x17:
                x = pop()
                if ("(" in x and x.endswith(")") and not x.startswith("(")) or x.startswith("delete "):
                    emit(x + ";")
            elif op == 0x52:
                nm, obj, argc = pop(), pop(), pop()
                try:
                    k = int(float(argc))
                except ValueError:
                    k = 0
                args = [pop() for _ in range(k)]
                if nm in ("undefined", '""'):
                    callee = obj                           # no method name: the object itself is called (super(...))
                elif obj in ('?', '""'):
                    callee = _vname(nm)
                else:
                    callee = _member(obj, nm)
                st.append("%s(%s)" % (callee, ", ".join(args)))
            elif op == 0x3D:
                nm, argc = pop(), pop()
                k = int(float(argc)) if argc.replace(".", "").isdigit() else 0
                args = [pop() for _ in range(k)]
                st.append("%s(%s)" % (_name(nm), ", ".join(args)))
            elif op == 0x40:
                nm, argc = pop(), pop()
                k = int(float(argc)) if argc.replace(".", "").isdigit() else 0
                args = [pop() for _ in range(k)]
                st.append("new %s(%s)" % (_name(nm), ", ".join(args)))
            elif op in (0x8E, 0x9B):
                sub = Decompiler()
                sub.run(ins.body, dictionary, 0, self._regnames(ins) if op == 0x8E else None)
                self.assign.update(sub.assign)
                params = ", ".join(a[1] for a in ins.args[1:])
                head = "function %s(%s) {" % (ins.args[0][1], params)
                if ins.note:
                    head += "   // " + ins.note + self._preload(ins.note)
                self.fns.append((head, sub.lines))
                if ins.args and ins.args[0][1]:
                    emit("\x00FN%d;" % (len(self.fns) - 1))      # a named function is a declaration: nothing is left on the stack
                else:
                    st.append("\x00FN%d" % (len(self.fns) - 1))
            elif op == 0x3E:
                emit("return %s;" % pop())
            elif op == 0x3A:
                nm, obj = pop(), pop()
                st.append("delete %s" % _member(obj, nm))
            elif op == 0x3B:
                st.append("delete %s" % _vname(pop()))
            elif op == 0x69:
                sup, sub_ = pop(), pop()
                emit("%s extends %s;" % (sub_, sup))
            elif op == 0x2C:
                cls = pop()
                ifs = [pop() for _ in range(_count(pop()))]
                emit("%s implements %s;" % (cls, ", ".join(ifs)))
            elif op == 0x2B:
                obj, ctor = pop(), pop()
                st.append("cast(%s, %s)" % (obj, ctor))
            elif op == 0x34:
                st.append("getTimer()")
            elif op == 0x44:
                st.append("typeof(%s)" % pop())
            elif op == 0x22:
                idx, tgt = pop(), pop()
                k = _count(idx)
                prop = PROPS[k] if 0 <= k < len(PROPS) else "prop%d" % k
                st.append(prop if tgt == '""' else "%s.%s" % (tgt, prop))
            elif op == 0x23:
                v, idx, tgt = pop(), pop(), pop()
                k = _count(idx)
                prop = PROPS[k] if 0 <= k < len(PROPS) else "prop%d" % k
                emit("%s = %s;" % (prop if tgt == '""' else "%s.%s" % (tgt, prop), v), ins.offset)
            elif op == 0x42:
                st.append("[%s]" % ", ".join([pop() for _ in range(_count(pop()))]))
            elif op == 0x43:
                pairs = []
                for _ in range(_count(pop())):
                    v = pop()
                    k_ = pop()
                    pairs.append("%s: %s" % (k_[1:-1] if k_[:1] == '"' and k_[1:-1].replace("_", "a").isalnum() else "[%s]" % k_, v))
                st.append("{%s}" % ", ".join(pairs))
            elif op == 0x53:
                nm, obj, argc = pop(), pop(), pop()
                args = [pop() for _ in range(_count(argc))]
                st.append("new %s(%s)" % (obj if nm in ("undefined", '""') else _member(obj, nm), ", ".join(args)))
            elif op == 0x25:
                emit("removeMovieClip(%s);" % pop())
            elif op == 0x09:
                emit("stopAllSounds();")
            elif op == 0x9F:
                emit("%s(%s);" % ("gotoAndPlay" if ins.args and ins.args[0][1] else "gotoAndStop", pop()))
            elif op == 0x55 and self._for_in(insns, i):
                # for (v in obj): enumerate2; L: storeRegister k; push undefined; equals2; branchIfTrue End; [push k; storeRegister v; pop]
                end_ = insns[i + 4].target - 1
                var_ = i + 7 < n and insns[i + 5].op in (0x96, 0xB9) and insns[i + 6].op == 0x87 and insns[i + 7].op == 0x17
                name = rn(insns[i + 6].args[0][1]) if var_ else rn(insns[i + 1].args[0][1])
                emit("for (%s in %s) {" % (name, pop()))
                closers.append((end_, "loop"))
                lstack.append((i + 1, i + 4, end_, "while"))
                i += 8 if var_ else 5
                continue
            elif op == 0x26:
                emit("trace(%s);" % pop())
            elif op in (0x06, 0x07, 0x04, 0x05):
                emit(AA.OPS[op] + "();")
            elif op in (0x81, 0x8C):
                emit("gotoAndStop(%s);" % _lit(ins.args[0], dictionary))
            elif op in (0x9D, 0xB8) and i in headers:
                lp = headers[i]
                cond = pop()
                if op == 0xB8:
                    pos = cond
                else:
                    pos = cond[2:-1] if cond.startswith("!(") and cond.endswith(")") else "!(%s)" % cond
                emit("while (%s) {" % pos)
                closers.append((lp[2], "loop"))
                lstack.append(lp)
            elif op in (0x9D, 0xB8) and closers and closers[-1][1] == "dowhile" and closers[-1][0] == i:
                cond = pop()
                if op == 0xB8:
                    cond = cond[2:-1] if cond.startswith("!(") and cond.endswith(")") else "!(%s)" % cond
                lstack.pop()
                closers.pop()
                self.lines.append(pad() + "} while (%s);" % cond)
            elif op in (0x9D, 0xB8) and ins.target is not None and jump_kind(i, ins.target):
                cond = pop()
                if op == 0xB8:
                    cond = cond[2:-1] if cond.startswith("!(") and cond.endswith(")") else "!(%s)" % cond
                emit("if (%s) {" % cond)
                self.lines.append(pad() + "  %s;" % jump_kind(i, ins.target))
                self.lines.append(pad() + "}")
            elif op == 0x9D and i in tern:
                tern_pending.setdefault(tern[i][1], []).append([pop(), None])
            elif op == 0x99 and i in tern_jump:
                for entry in reversed(tern_pending.get(tern_jump[i], [])):
                    if entry[1] is None:
                        entry[1] = pop()
                        break
            elif op == 0x9D and i in or_member:
                or_acc.setdefault(or_member[i], []).append(pop())
            elif op == 0x9D and i in or_last:
                _ms, tgt, k = or_last[i]
                conds = or_acc.pop(i, []) + [pop()]
                open_if(conds[0] if len(conds) == 1 else "(%s)" % " || ".join(conds), tgt, i)
                i = k + 1
                continue
            elif op == 0x9D and ins.target is not None and ins.target > i:
                cond = pop()
                pos = cond[2:-1] if cond.startswith("!(") and cond.endswith(")") else "!(%s)" % cond
                open_if(pos, ins.target, i)
            elif op == 0x99 and self._falls(insns, i):
                pass                                       # jump to the next instruction (the empty else of an if)
            elif op == 0x99 and i in sw_last:
                pass                                       # last body of a switch: its jump to the end is implied
            elif op == 0x99 and i in ends:
                pass                                       # the back jump of a while loop; its "}" was printed above
            elif op == 0x99 and ins.target is not None and jump_kind(i, ins.target):
                emit("%s;" % jump_kind(i, ins.target))
            elif op == 0x99 and closers and closers[-1][1] == "ifelse" and closers[-1][0] == i:
                _e, _k, end = closers.pop()
                self.lines.append(pad() + "} else {")
                closers.append((end, "else"))
            elif op == 0x00:
                pass
            elif op in (0x4A, 0x4B, 0x18):
                x = pop()
                st.append("%s(%s)" % ({0x4A: "Number", 0x4B: "String", 0x18: "int"}[op], x))
            elif op in (0x50, 0x51):
                x = pop()
                st.append("(%s %s 1)" % (x, "+" if op == 0x50 else "-"))
            elif op == 0x56:
                st.append("this")
            elif op == 0x59:
                st.append("0")
            elif op == 0x5A:
                st.append("1")
            elif op in (0x73, 0x74, 0x75, 0x76):
                st.append({0x73: "true", 0x74: "false", 0x75: "null", 0x76: "undefined"}[op])
            else:
                self.raw.append(i)                         # index of an instruction printed as a comment
                emit("/* %s %s */" % (ins.name, ", ".join(AA.fmt(v, dictionary) for v in ins.args)))
            i += 1
        while closers:
            closers.pop()
            self.lines.append("  " * (indent + len(closers)) + "}")
        return self.lines


def decompile(insns, dictionary=None):
    d = Decompiler()
    d.run(insns, dictionary)
    return d.lines, d.assign
