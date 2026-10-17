#!/usr/bin/env python3
"""
apt_decompile.py

Turns decoded APT bytecode (apt_actions.Insn lists) into readable pseudo-code for the Behavior view. It is a
best-effort reader of the patterns the compiler produces, not a complete decompiler:
  - expressions are rebuilt from the stack (push, getVariable, getMember, calls, arithmetic, comparisons)
  - "X; not; branchIfTrue L" becomes if (X) { ... }, a forward branchAlways at the end of that block becomes else
  - "X; pushDuplicate; not; branchIfTrue L; pop; Y" becomes (X && Y); without the not it becomes (X || Y)
  - anything else is printed as a comment with the raw instruction, never guessed
Output: lines of text, and a dict {instruction offset: statement text} for assignments (used by the inspector to show
which formula wrote a value).
"""
import apt_actions as AA

BIN = {0x0A: "+", 0x47: "+", 0x0B: "-", 0x0C: "*", 0x0D: "/", 0x3F: "%", 0x0F: "<", 0x48: "<", 0x67: ">",
       0x0E: "==", 0x49: "==", 0x66: "===", 0x10: "&&", 0x11: "||", 0x60: "&", 0x61: "|", 0x62: "^"}


def _lit(v, dictionary):
    k = v[0]
    if k == "lookup":
        if dictionary is not None and v[1] < len(dictionary):
            return _lit(dictionary[v[1]], None)
        return "dict[%d]" % v[1]
    return AA.fmt(v, None)


def _name(expr):
    return expr[1:-1] if len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"' else "[%s]" % expr


class Decompiler:
    def __init__(self):
        self.lines = []
        self.assign = {}
        self.fns = []

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

    def _expand(self, text, off=None):
        """An assignment whose value is a function prints the function body after it."""
        if "\x00FN" in text:
            k = int(text.split("\x00FN")[1].rstrip(";"))
            head, body = self.fns[k]
            target = text.split("\x00FN")[0]
            return [target + head] + ["  " + l for l in body] + ["};"]
        return [text]

    def run(self, insns, dictionary=None, indent=0):
        st, closers, sc = [], [], {}
        pad = lambda: "  " * (indent + len(closers))
        i, n = 0, len(insns)
        while i < n:
            while closers and closers[-1][0] <= i:
                if closers[-1][1] == "ifelse" and closers[-1][0] == i and insns[i].op == 0x99:
                    break                                  # handled below as "} else {"
                closers.pop()
                self.lines.append(pad() + "}")
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
                    st.append(_lit(v, dictionary))
            elif op == 0x88:
                dictionary = list(ins.args)
            elif op in (0xA1, 0xB4, 0xB5, 0xB6, 0xB7, 0xB9, 0xA2, 0xA3):
                for v in ins.args:
                    st.append(_lit(v, dictionary))
            elif op == 0x1C:
                st.append(_name(pop()))
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
                emit("%s = %s;" % (_name(nm), v), ins.offset)
            elif op == 0x4F:
                v, nm, obj = pop(), pop(), pop()
                emit("%s.%s = %s;" % (obj, _name(nm), v) if nm.startswith('"') else "%s[%s] = %s;" % (obj, nm, v), ins.offset)
            elif op in (0x3C, 0x41):
                v, nm = pop(), pop()
                emit("var %s = %s;" % (_name(nm), v), ins.offset)
            elif op == 0x87:
                v = pop()
                emit("r%d = %s;" % (ins.args[0][1], v), ins.offset)
                st.append("r%d" % ins.args[0][1])
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
                if "(" in x and x.endswith(")") and not x.startswith("("):
                    emit(x + ";")
            elif op == 0x52:
                nm, obj, argc = pop(), pop(), pop()
                try:
                    k = int(float(argc))
                except ValueError:
                    k = 0
                args = [pop() for _ in range(k)]
                st.append("%s(%s)" % (_name(nm) if obj in ('?', '""') else "%s.%s" % (obj, _name(nm)), ", ".join(args)))
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
                sub.run(ins.body, dictionary, 0)
                self.assign.update(sub.assign)
                params = ", ".join(a[1] for a in ins.args[1:])
                head = "function %s(%s) {" % (ins.args[0][1], params)
                if ins.note:
                    head += "   // " + ins.note + self._preload(ins.note)
                self.fns.append((head, sub.lines))
                st.append("\x00FN%d" % (len(self.fns) - 1))
            elif op == 0x3E:
                emit("return %s;" % pop())
            elif op in (0x06, 0x07, 0x04, 0x05):
                emit(AA.OPS[op] + "();")
            elif op in (0x81, 0x8C):
                emit("gotoAndStop(%s);" % _lit(ins.args[0], dictionary))
            elif op == 0x9D and ins.target is not None and ins.target > i:
                cond = pop()
                pos = cond[2:-1] if cond.startswith("!(") and cond.endswith(")") else "!(%s)" % cond
                emit("if (%s) {" % pos)
                closers.append((ins.target, "if"))
                # a forward branchAlways just before the end of the block makes it if/else
                last = insns[ins.target - 1] if ins.target - 1 > i else None
                if last is not None and last.op == 0x99 and last.target is not None and last.target > ins.target:
                    closers[-1] = (ins.target - 1, "ifelse", last.target)
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
