#!/usr/bin/env python3
"""
apt_patch_methods.py

An x360 dev screen can be an older build than the PC one: its class lacks methods the PC game calls (found on
LONGLOADINGSCREEN: ExitComplete, FadeInText, FadeOutScreen, FadeOutComplete). This tool adds them to a PC-format
(little endian, already converted) .big.

Two ways to say what to add:
  --pc PC.BIG|DIR   compare with the PC file of the same screen (a folder is searched for the same file name) and
                    copy every class method that the PC file defines and the converted file does not.
  (no --pc)         the built-in list in SPECS (the LongLoadingScreen methods).

How: it does not touch the existing streams. A new DoAction (frame control type 1) is appended to the end of
root frame 0, after the one that creates the screen. It runs

    _global.<Class>.prototype.<Method> = function(...) {...};

for every missing method. With --pc the function is read from the PC stream and written again (constants copied by
value, branch jumps recomputed). A function it cannot write (try blocks, dictionary opcodes inside the body) is
reported and skipped. A method that the class already defines is left alone, so a file that is complete comes out
unchanged and is not written.

--pc also lists what it cannot copy: root frames the PC movie has beyond the converted one, classes the converted
movie lacks, and functions that use names of clips the converted movie does not have (they work on a missing clip;
--stub-missing-refs adds those with an empty body instead).

Everything new goes to the end of the apt data and the const file (new streams, the pools they read, a copy of the
frame control list, a copy of the const table with the new entries). The .big is rebuilt with 0x80 aligned entries.

Usage:
  python apt_patch_methods.py LONGLOADINGSCREEN.BIG --pc longloadingscreen_pc.big [-o OUT.BIG] [--dry-run]
  python apt_patch_methods.py CONVERTED_DIR --pc PC_DIR -o OUT_DIR
A folder is searched for .big files (subfolders too); the output folder gets the same layout, and files that need
nothing, or have no PC file, are copied unchanged. The input must already be in PC byte order
(apt_endian_swap.py --to pc). Without -o the result is written next to the input as <name>_patched.BIG.
"""
import argparse
import os
import struct
import sys

import apt_actions as AA
import apt_movie as M
import apt_scan as A

# What each class needs, in the order they are added: (method, parameters, body). Bodies as in the PC build.
#   None                  empty body
#   "call_global_cmd:X"   _global.cmdLoadingScreen.X();  (PC FadeOutComplete)
# FadeInText of the PC build writes the loading texts into mTextGroup clips that the older build does not have,
# so it is added with the same name and parameter but an empty body.
SPECS = {
    "LongLoadingScreen": [
        ("ExitComplete", [], None),
        ("FadeInText", ["index"], None),
        ("FadeOutComplete", [], "call_global_cmd:TextFadeOutComplete"),
        ("FadeOutScreen", [], None),
    ],
}


# ---------------------------------------------------------------- .big container

def read_big(d):
    """(header bytes up to the first entry, [(name, data)]) of a BIGF file."""
    if d[:4] != b"BIGF":
        raise ValueError("not a BIGF file")
    n = struct.unpack(">I", d[8:12])[0]
    p, ents = 16, []
    for _ in range(n):
        o, s = struct.unpack(">II", d[p:p + 8])
        p += 8
        z = d.index(b"\0", p)
        ents.append((d[p:z].decode("latin-1"), o, s))
        p = z + 1
    first = min(o for _, o, _ in ents)
    return d[:first], [(nm, d[o:o + s]) for nm, o, s in ents]


def write_big(head, ents):
    """Rebuild: same header and entry order, offsets recomputed (entries start on 0x80), total size at +4."""
    n = len(ents)
    table, pos = bytearray(), len(head)
    offs = []
    for nm, data in ents:
        pos = (pos + 0x7F) & ~0x7F
        offs.append(pos)
        pos += len(data)
    out = bytearray(head)
    p = 16
    for (nm, data), o in zip(ents, offs):
        struct.pack_into(">II", out, p, o, len(data))
        p += 8 + len(nm.encode("latin-1")) + 1
    struct.pack_into("<I", out, 4, pos)
    for (nm, data), o in zip(ents, offs):
        out.extend(b"\0" * (o - len(out)))
        out.extend(data)
    return bytes(out)


# ---------------------------------------------------------------- const file

class Consts:
    """The const table of an Apt constant file; get() returns the index of a value, adding it when it is new."""

    def __init__(self, const):
        self.data = bytearray(const)
        _main, vals = AA.read_consts(bytes(const))
        self.vals = list(vals)
        self.n0 = len(vals)
        self.own = {}

    def get(self, v):
        """Index of v. Strings and registers reuse an entry of the file. A number always gets an entry of its own
        (shared between the numbers this tool writes): in the game, a function pushing a number through an entry
        of the converted file crashed the screen, with an own entry of the same value it worked."""
        if v[0] in ("int", "float", "bool"):
            if v in self.own:
                return self.own[v]
            self.vals.append(v)
            self.own[v] = len(self.vals) - 1
            return self.own[v]
        if v in self.vals:
            return self.vals.index(v)
        self.vals.append(v)
        return len(self.vals) - 1

    def finish(self):
        """Append the new strings and a table with the new entries; point the header at it."""
        if len(self.vals) == self.n0:
            return bytes(self.data)
        d = self.data
        old_table = struct.unpack_from("<I", d, 28)[0]
        strs = {}
        for v in self.vals[self.n0:]:
            if v[0] == "str" and v[1] not in strs:
                strs[v[1]] = len(d)
                d.extend(v[1].encode("latin-1") + b"\0")
        while len(d) % 4:
            d.append(0)
        table = len(d)
        d.extend(d[old_table:old_table + 8 * self.n0])
        for v in self.vals[self.n0:]:
            k = v[0]
            if k == "str":
                d.extend(struct.pack("<iI", 1, strs[v[1]]))
            elif k == "undef":
                d.extend(struct.pack("<iI", 3, 0))
            elif k == "reg":
                d.extend(struct.pack("<ii", 4, v[1]))
            elif k == "bool":
                d.extend(struct.pack("<ii", 5, 1 if v[1] else 0))
            elif k == "float":
                d.extend(struct.pack("<if", 6, v[1]))
            elif k == "int":
                d.extend(struct.pack("<ii", 7, v[1]))
            else:
                raise Unsupported("constant kind %s" % k)
        struct.pack_into("<i", d, 24, len(self.vals))
        struct.pack_into("<I", d, 28, table)
        return bytes(d)


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------- stream writer

NO_ENCODE = {0x88: "defineDictionary", 0x8F: "try", 0xA2: "dictByte", 0xA3: "dictWord", 0xAE: "getVarDict",
             0xAF: "getMemberDict", 0xB0: "callFuncPopDict", 0xB1: "callFuncSetVarDict",
             0xB2: "callMethodPopDict", 0xB3: "callMethodSetVarDict"}


class Writer:
    """Writes the new data at the end of the apt entry; positions are absolute offsets in the apt data."""

    def __init__(self, apt, consts):
        self.apt = bytearray(apt)
        while len(self.apt) % 4:
            self.apt.append(0)
        self.consts = consts
        self.pools = {}                         # key -> positions of the pointers to patch

    def pos(self):
        return len(self.apt)

    def align(self):
        while len(self.apt) % 4:
            self.apt.append(0)

    def op(self, code):
        self.apt.append(code)

    def raw(self, fmt, v):
        self.apt.extend(struct.pack("<" + fmt, v))

    def i32(self, v):
        self.align()
        self.raw("i", v)

    def ptr_later(self, key):
        self.align()
        self.pools.setdefault(key, []).append(self.pos())
        self.apt.extend(b"\0\0\0\0")

    def push(self, *values):
        self.op(0x96)
        self.i32(len(values))
        self.ptr_later(tuple(self.consts.get(v) for v in values))

    def func(self, op, name, params, regs, flags, body):
        """DefineFunction (0x9B) or DefineFunction2 (0x8E). params: names (0x9B) or (register, name) (0x8E).
        body(writer) writes the body; its size is filled in afterwards."""
        self.op(op)
        self.ptr_later(("str", name))
        self.i32(len(params))
        if op == 0x8E:
            self.apt.extend(struct.pack("<hh", regs, flags))
        self.ptr_later(("params2" if op == 0x8E else "params", tuple(params)))
        self.i32(0)
        size_at = self.pos() - 4
        self.apt.extend(struct.pack("<II", 0x98765432, 0x12345678))
        start = self.pos()
        body(self)
        struct.pack_into("<i", self.apt, size_at, self.pos() - start)

    def string(self, s):
        self.align()
        at = self.pos()
        self.apt.extend(s.encode("latin-1") + b"\0")
        return at

    def finish_data(self):
        """Pools, parameter lists and strings after the code; patches the pointers."""
        strs = {}

        def sat(s):
            if s not in strs:
                strs[s] = self.string(s)
            return strs[s]

        for key, places in list(self.pools.items()):
            self.align()
            if key[0] == "str":
                at = sat(key[1])
            elif key[0] == "params":
                at = self.pos()
                self.apt.extend(b"\0" * 4 * max(1, len(key[1])))
                for i, p in enumerate(key[1]):
                    struct.pack_into("<I", self.apt, at + 4 * i, sat(p))
            elif key[0] == "params2":
                at = self.pos()
                self.apt.extend(b"\0" * 8 * max(1, len(key[1])))
                for i, (reg, p) in enumerate(key[1]):
                    struct.pack_into("<II", self.apt, at + 8 * i, reg, sat(p))
            else:
                at = self.pos()
                self.apt.extend(struct.pack("<%dI" % len(key), *key))
            for p in places:
                struct.pack_into("<I", self.apt, p, at)


# ---------------------------------------------------------------- writing decoded instructions again

def _val(a, dictionary):
    """A decoded value with lookups replaced through the dictionary in force; Unsupported when it cannot be."""
    if a[0] == "lookup":
        if not dictionary or a[1] >= len(dictionary) or dictionary[a[1]][0] == "lookup":
            raise Unsupported("dictionary lookup #%d is not resolved" % a[1])
        return dictionary[a[1]]
    return a


def problems(insns, dictionary):
    """Why these instructions cannot be written again ([] when they can)."""
    out = []
    for i in insns:
        if i.op is None:
            out.append("undecodable opcode at 0x%X" % i.offset)
            continue
        if i.note.startswith("decode error") or i.note.startswith("branch target"):
            out.append("0x%X: %s" % (i.offset, i.note))
        if i.op in NO_ENCODE:
            out.append("%s at 0x%X" % (NO_ENCODE[i.op], i.offset))
        if i.op == 0x96:
            for a in i.args:
                try:
                    _val(a, dictionary)
                except Unsupported as e:
                    out.append("0x%X: %s" % (i.offset, e))
        if i.body is not None:
            out += problems(i.body, dictionary)
    return out


def body_strings(insns, dictionary):
    """Strings a function uses (push values and inline strings), nested functions included."""
    out = set()
    for i in insns:
        for a in i.args:
            try:
                a = _val(a, dictionary)
            except Unsupported:
                continue
            if a[0] == "str" and a[1]:
                out.add(a[1])
        if i.body is not None:
            out |= body_strings(i.body, dictionary)
    return out


def emit_block(w, insns, dictionary):
    starts, jumps = [], []
    for i in insns:
        starts.append(w.pos())
        op = i.op
        if op == 0x00 or op in AA.SIMPLE:
            w.op(op)
        elif op in (0x81, 0x87):
            w.op(op)
            w.i32(i.args[0][1])
        elif op == 0x9F:
            w.op(op)
            w.i32(1 if i.args[0][1] else 0)
        elif op == 0x83:
            w.op(op)
            w.ptr_later(("str", i.args[0][1]))
            w.ptr_later(("str", i.args[1][1]))
        elif op in (0x8B, 0x8C, 0xA1, 0xA4, 0xA5, 0xA6, 0xA7):
            w.op(op)
            w.ptr_later(("str", i.args[0][1]))
        elif op == 0x96:
            w.op(op)
            vals = [_val(a, dictionary) for a in i.args]
            w.i32(len(vals))
            w.ptr_later(tuple(w.consts.get(v) for v in vals))
        elif op in (0x9B, 0x8E):
            name = i.args[0][1]
            if op == 0x8E:
                regs, flags = [int(x, 0) for x in (i.note.split()[1].rstrip(","), i.note.split()[3])]
                params = []
                for p in i.args[1:]:
                    r, _, nm = p[1].partition("=")
                    params.append((int(r[1:]), nm))
            else:
                regs = flags = 0
                params = [p[1] for p in i.args[1:]]
            w.func(op, name, params, regs, flags, lambda ww, b=i.body: emit_block(ww, b, dictionary))
        elif op in (0x99, 0x9D, 0xB8, 0x94):
            w.op(op)
            w.i32(0)
            jumps.append((w.pos() - 4, i.target))
        elif op == 0x77:
            w.op(op)
            w.apt.extend(struct.pack("<I", i.args[0][1]))
        elif op == 0xB4:
            w.op(op)
            w.apt.extend(struct.pack("<f", i.args[0][1]))
        elif op in (0xB5, 0xB9):
            w.op(op)
            w.apt.append(i.args[0][1])
        elif op == 0xB6:
            w.op(op)
            w.apt.extend(struct.pack("<H", i.args[0][1]))
        elif op == 0xB7:
            w.op(op)
            w.apt.extend(struct.pack("<I", i.args[0][1]))
        else:
            raise Unsupported("opcode 0x%02X" % op)
    starts.append(w.pos())
    for at, target in jumps:
        struct.pack_into("<i", w.apt, at, starts[target] - (at + 4))


# ---------------------------------------------------------------- what to add

def body_for(spec):
    if spec is None:
        return lambda w: None
    kind, _, name = spec.partition(":")
    if kind == "call_global_cmd":
        def body(w):
            w.push(("int", 0), ("str", "_global"))
            w.op(0x1C)                          # getVariable
            w.push(("str", "cmdLoadingScreen"))
            w.op(0x4E)                          # getMember
            w.push(("str", name))
            w.op(0x52)                          # callMethod
            w.op(0x17)                          # pop
        return body
    raise ValueError(spec)


def spec_members(methods):
    """Built-in list -> [(name, writer function)]."""
    return [(name, lambda w, p=params, b=body: w.func(0x9B, "", p, 0, 0, body_for(b))) for name, params, body in methods]


def build_stream(w, cls, members):
    w.push(("str", "_global"))
    w.op(0x1C)
    w.push(("str", cls))
    w.op(0x4E)
    w.push(("str", "prototype"))
    w.op(0x4E)
    w.op(0x87)                                  # storeRegister 2
    w.i32(2)
    w.op(0x17)
    for name, write in members:
        w.push(("reg", 2), ("str", name))
        write(w)
        w.op(0x4F)                              # setMember
    w.op(0x00)


def _resolved(insns, dictionary=None):
    """Yield (insn, args) with lookup values replaced through the last DefineDictionary (as the disassembler does)."""
    for i in insns:
        if i.name == "defineDictionary":
            dictionary = list(i.args)
        args = [dictionary[a[1]] if a[0] == "lookup" and dictionary and a[1] < len(dictionary) else a for a in i.args]
        yield i, args
        if i.body:
            yield from _resolved(i.body, dictionary)


def _walk(insns):
    for i in insns:
        yield i
        if i.body:
            yield from _walk(i.body)


def class_streams(mv):
    """{class: stream} for the streams with a '<Class>::template' string (the init stream of the class)."""
    out = {}
    for s in AA.collect_streams(mv):
        for _i, args in _resolved(s["insns"]):
            for a in args:
                if a[0] == "str" and a[1].endswith("::template"):
                    out.setdefault(a[1][:-len("::template")], s)
    return out


def members_of(stream):
    """[(name, function instruction, dictionary)] for 'push r?, "Name"; defineFunction; setMember' at the top level."""
    out, dictionary = [], None
    flat = stream["insns"]
    for k, ins in enumerate(flat):
        if ins.name == "defineDictionary":
            dictionary = list(ins.args)
        if (ins.name == "push" and k + 2 < len(flat) and flat[k + 1].name in ("defineFunction", "defineFunction2")
                and flat[k + 2].name == "setMember"):
            try:
                vals = [_val(a, dictionary) for a in ins.args]
            except Unsupported:
                continue
            if len(vals) == 2 and vals[0][0] == "reg" and vals[1][0] == "str":
                out.append((vals[1][1], flat[k + 1], dictionary))
    return out


def defined_methods(mv, cls):
    """Names the class gets as prototype members: every stream that names the class and its prototype (the init
    stream with <cls>::template, and the one this tool adds). None when the class is not in the movie."""
    names, seen = set(), False
    for s in AA.collect_streams(mv):
        flat = s["insns"]
        strs = {a[1] for _i, args in _resolved(flat) for a in args if a[0] == "str"}
        if cls + "::template" in strs:
            seen = True
        if cls not in strs or "prototype" not in strs:
            continue
        dictionary = None
        for k, ins in enumerate(flat[:-1]):
            if ins.name == "defineDictionary":
                dictionary = list(ins.args)
            if ins.name == "push" and flat[k + 1].name in ("defineFunction", "defineFunction2"):
                for a in ins.args:
                    if a[0] == "lookup" and dictionary and a[1] < len(dictionary):
                        a = dictionary[a[1]]
                    if a[0] == "str":
                        names.add(a[1])
    return names if seen else None


def movie_names(mv):
    """Instance names and frame labels used in the movie (root and sprites)."""
    out = set()
    lists = [mv.frames] + [ch["frames"] for ch in mv.chars.values() if ch["type"] == "Sprite"]
    for frames in lists:
        for frame in frames:
            for c in frame:
                if c["op"] == "place" and c.get("name"):
                    out.add(c["name"])
                elif c["op"] == "label":
                    out.add(c["label"])
    return out


def from_pc(mv, pc, stub_refs):
    """([(class, [(name, write function)])], report lines) from the differences to the PC movie."""
    report, todo = [], []
    mine, theirs = class_streams(mv), class_streams(pc)
    my_names, pc_clips = movie_names(mv), movie_names(pc)
    for cls, pstream in theirs.items():
        if cls not in mine:
            report.append("%s: class is not in the converted movie, not copied" % cls)
            continue
        have = defined_methods(mv, cls) or set()
        add = []
        for name, fn, dictionary in members_of(pstream):
            if name in have:
                continue
            bad = problems([fn], dictionary)
            if bad:
                report.append("%s.%s: not copied (%s)" % (cls, name, "; ".join(bad[:3])))
                continue
            refs = sorted(s for s in body_strings([fn], dictionary) if len(s) >= 3 and
                          any(n.startswith(s) for n in pc_clips) and not any(n.startswith(s) for n in my_names))
            stub = bool(refs) and stub_refs
            if refs:
                report.append("%s.%s: uses clip names the converted movie lacks: %s%s" % (
                    cls, name, ", ".join(refs), " -> added with an empty body" if stub else ""))
            if stub:
                add.append((name, lambda w, f=fn: _stub(w, f)))
            else:
                add.append((name, lambda w, f=fn, d=dictionary: emit_block(w, [f], d)))
        report.append("%s: %d method(s) in the PC movie that the converted one lacks: %s" % (
            cls, len(add), ", ".join(n for n, _ in add) or "-"))
        if add:
            todo.append((cls, add))
    # what cannot be copied
    pf, mf = len(pc.frames), len(mv.frames)
    if pf > mf:
        by = {s["where"]: s for s in AA.collect_streams(pc)}
        for fi in range(mf, pf):
            s = by.get("root frame %d" % fi)
            if s:
                used = sorted({a[1] for _i, args in _resolved(s["insns"]) for a in args if a[0] == "str" and a[1]})
                report.append("PC root frame %d (the converted movie ends at %d) has a script using: %s" % (
                    fi, mf - 1, ", ".join(used)))
    return todo, report


def _stub(w, f):
    """The function f with the same name and parameters and an empty body."""
    if f.op == 0x9B:
        w.func(0x9B, f.args[0][1], [p[1] for p in f.args[1:]], 0, 0, lambda ww: None)
        return
    params = []
    for p in f.args[1:]:
        r, _, nm = p[1].partition("=")
        params.append((int(r[1:]), nm))
    regs, flags = [int(x, 0) for x in (f.note.split()[1].rstrip(","), f.note.split()[3])]
    w.func(0x8E, f.args[0][1], params, regs, flags, lambda ww: None)


# ---------------------------------------------------------------- patching

def load_movie(path):
    d = open(path, "rb").read()
    head, ents = read_big(d)
    e = dict(ents)
    if A.use_endian(e["0"], e["1"]) != "<":
        raise ValueError("big endian data: convert it to PC first (apt_endian_swap.py --to pc)")
    return head, ents, M.Movie(os.path.basename(path), [(n, b) for n, b in ents])


def patch(path_in, path_pc=None, stub_refs=False):
    """(new bytes or None, [report lines])."""
    try:
        head, ents, mv = load_movie(path_in)
    except ValueError as e:
        return None, [str(e)]
    e = dict(ents)
    apt, const = e["0"], e["1"]
    report, todo = [], []
    if path_pc:
        try:
            _h, _e, pc = load_movie(path_pc)
        except ValueError as ex:
            return None, ["PC file: %s" % ex]
        A.use_endian(apt, const)
        todo, report = from_pc(mv, pc, stub_refs)
        A.use_endian(apt, const)
    else:
        for cls, methods in SPECS.items():
            have = defined_methods(mv, cls)
            if have is None:
                continue
            missing = [m for m in methods if m[0] not in have]
            report.append("%s: has %s; adding %s" % (cls, sorted(have & {m[0] for m in methods}) or "none of them",
                                                     [m[0] for m in missing] or "nothing"))
            if missing:
                todo.append((cls, spec_members(missing)))
    if not todo:
        return None, report + ["nothing to add"]
    consts = Consts(const)
    w = Writer(apt, consts)
    main = struct.unpack_from("<I", const, 20)[0]
    frames = struct.unpack_from("<I", w.apt, main + 20)[0]
    count, ctl = struct.unpack_from("<II", w.apt, frames)               # root frame 0
    ptrs = list(struct.unpack_from("<%dI" % count, w.apt, ctl))
    try:
        for cls, members in todo:
            w.align()
            stream = w.pos()
            build_stream(w, cls, members)
            w.align()
            w.apt.extend(struct.pack("<II", 1, stream))                  # DoAction control
            ptrs.append(w.pos() - 8)
        w.finish_data()
        w.align()
        new_ctl = w.pos()
        w.apt.extend(struct.pack("<%dI" % len(ptrs), *ptrs))
        struct.pack_into("<II", w.apt, frames, len(ptrs), new_ctl)
        new_const = consts.finish()
    except Unsupported as ex:
        return None, report + ["not written: %s" % ex]
    new_ents = [(n, bytes(w.apt) if n == "0" else new_const if n == "1" else b) for n, b in ents]
    out = write_big(head, new_ents)
    # check: reads back, the old streams are unchanged, the new ones decode and define the methods
    mv2 = M.Movie(os.path.basename(path_in), [(n, b) for n, b in read_big(out)[1]])
    old = {s["offset"]: AA.disassemble(s["insns"]) for s in AA.collect_streams(mv)}
    new = {s["offset"]: s for s in AA.collect_streams(mv2)}
    for off, lines in old.items():
        if off not in new or AA.disassemble(new[off]["insns"]) != lines:
            raise RuntimeError("stream 0x%X changed" % off)
    for off, s in new.items():
        if off not in old and (s["error"] or any(i.op is None for i in _walk(s["insns"]))):
            raise RuntimeError("new stream does not decode: %s" % s["error"])
    for cls, members in todo:
        got = defined_methods(mv2, cls) or set()
        lost = [n for n, _ in members if n not in got]
        if lost:
            raise RuntimeError("%s: not defined after the patch: %s" % (cls, lost))
        report.append("%s: stream added at root frame 0, %d bytes more apt, %d const entries added" % (
            cls, len(w.apt) - len(apt), len(consts.vals) - consts.n0))
    return out, report


def pc_for(f, pc):
    """The PC file for f: pc itself, or the file of the same name (any case) in the folder pc."""
    if not pc:
        return None
    if not os.path.isdir(pc):
        return pc
    for n in os.listdir(pc):
        if n.lower() == os.path.basename(f).lower():
            return os.path.join(pc, n)
    return ""


def expand(inputs):
    """[(file, relative path)]: files as given, folders searched recursively for .big files."""
    out = []
    for f in inputs:
        if os.path.isdir(f):
            for root, _d, names in sorted(os.walk(f)):
                for n in sorted(names):
                    if n.lower().endswith(".big"):
                        p = os.path.join(root, n)
                        out.append((p, os.path.relpath(p, f)))
        else:
            out.append((f, os.path.basename(f)))
    return out


def main():
    ap = argparse.ArgumentParser(description="Add the PC screen methods an older x360 build lacks.")
    ap.add_argument("files", nargs="+", help=".big files (already converted to PC) or folders of them")
    ap.add_argument("--pc", help="PC .big of the same screen, or a folder with the PC .big files")
    ap.add_argument("--stub-missing-refs", action="store_true",
                    help="add functions that use clips the converted movie lacks with an empty body")
    ap.add_argument("-o", "--out", help="output file (one input file) or folder")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = ap.parse_args()
    items = expand(a.files)
    folder_mode = len(items) > 1 or any(os.path.isdir(f) for f in a.files)
    if not items:
        print("no .big files found")
        return
    for f, rel in items:
        print(rel)
        p = pc_for(f, a.pc)
        if p == "":
            print("    no PC file with this name in " + a.pc)
            out, report = None, ["not patched"]
        else:
            out, report = patch(f, p, a.stub_missing_refs)
        for r in report:
            print("    " + r)
        if a.dry_run:
            continue
        if folder_mode and a.out and out is None and not any(r.startswith(("big endian", "PC file")) for r in report):
            out = open(f, "rb").read()                  # a complete output folder: unchanged files are copied
            report = ["copied unchanged"]
            print("    copied unchanged")
        if out is None:
            continue
        if a.out and folder_mode:
            dst = os.path.join(a.out, rel)
        elif a.out:
            dst = a.out
        else:
            root, ext = os.path.splitext(f)
            dst = root + "_patched" + ext
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        open(dst, "wb").write(out)
        print("    written: " + dst)


if __name__ == "__main__":
    main()
