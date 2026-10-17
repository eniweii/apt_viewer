#!/usr/bin/env python3
"""
apt_actions.py

Decoder for the ActionScript bytecode in APT files, and a collector for every action stream of a movie.
The layout follows LibreSAGE/AptTools (crates/apt/src/actions.rs, constfile.rs, parse.rs); this is a Python port of
the structure, checked on Undercover files.

A stream is [opcode u8][payload]* up to End (0x00). APT keeps the SWF opcode numbers, but payloads are C structs
aligned to the pointer size (4), counted from the start of the apt entry; 1, 2 and 4 byte immediates are not aligned.
EA adds shorthand opcodes (0x56-0x77) and constant-dictionary opcodes (0xA1-0xB9).

Value kinds from the const file (type, value) pairs: 1 string, 3 none, 4 register, 5 bool, 6 float, 7 int,
8 lookup (index into the dictionary set by DefineDictionary).

Where streams live (all collected by collect_streams):
  frame control type 1         DoAction in a movie or sprite frame
  frame control type 8         DoInitAction (sprite id, stream)
  PlaceObject (flag 0x80)      clip actions: event flags, key code, stream
  DefineFunction / Function2   function bodies inside a stream (decoded as nested blocks)
Anything that does not decode is reported (Insn.op = None, with a note), never guessed.
"""
import struct

OPS = {
    0x00: "end", 0x04: "nextFrame", 0x05: "prevFrame", 0x06: "play", 0x07: "stop", 0x08: "toggleQuality",
    0x09: "stopSounds", 0x0A: "add", 0x0B: "subtract", 0x0C: "multiply", 0x0D: "divide", 0x0E: "equals",
    0x0F: "lessThan", 0x10: "and", 0x11: "or", 0x12: "not", 0x13: "stringEquals", 0x14: "stringLength",
    0x15: "substring", 0x17: "pop", 0x18: "toInteger", 0x1C: "getVariable", 0x1D: "setVariable",
    0x20: "setTarget2", 0x21: "stringAdd", 0x22: "getProperty", 0x23: "setProperty", 0x24: "cloneSprite",
    0x25: "removeSprite", 0x26: "trace", 0x27: "startDrag", 0x28: "stopDrag", 0x29: "stringLessThan",
    0x2A: "throw", 0x2B: "castOp", 0x2C: "implementsOp", 0x30: "random", 0x31: "mbLength", 0x32: "charToAscii",
    0x33: "asciiToChar", 0x34: "getTimer", 0x35: "mbSubstring", 0x36: "mbCharToAscii", 0x37: "mbAsciiToChar",
    0x3A: "delete", 0x3B: "delete2", 0x3C: "defineLocal", 0x3D: "callFunction", 0x3E: "return", 0x3F: "modulo",
    0x40: "newObject", 0x41: "defineLocal2", 0x42: "initArray", 0x43: "initObject", 0x44: "typeOf",
    0x45: "targetPath", 0x46: "enumerate", 0x47: "add2", 0x48: "lessThan2", 0x49: "equals2", 0x4A: "toNumber",
    0x4B: "toString", 0x4C: "pushDuplicate", 0x4D: "stackSwap", 0x4E: "getMember", 0x4F: "setMember",
    0x50: "increment", 0x51: "decrement", 0x52: "callMethod", 0x53: "newMethod", 0x54: "instanceOf",
    0x55: "enumerate2", 0x56: "pushThis", 0x58: "pushGlobal", 0x59: "pushZero", 0x5A: "pushOne",
    0x5B: "callFuncPop", 0x5C: "callFuncSetVar", 0x5D: "callMethodPop", 0x5E: "callMethodSetVar",
    0x60: "bitAnd", 0x61: "bitOr", 0x62: "bitXor", 0x63: "bitLShift", 0x64: "bitRShift", 0x65: "bitURShift",
    0x66: "strictEquals", 0x67: "greater", 0x69: "extends", 0x70: "pushThisVar", 0x71: "pushGlobalVar",
    0x72: "pushZeroSetVar", 0x73: "pushTrue", 0x74: "pushFalse", 0x75: "pushNull", 0x76: "pushUndefined",
    0x77: "traceStart", 0x81: "gotoFrame", 0x83: "getUrl", 0x87: "storeRegister", 0x88: "defineDictionary",
    0x8A: "waitForFrame", 0x8B: "setTarget", 0x8C: "gotoLabel", 0x8E: "defineFunction2", 0x8F: "try",
    0x94: "with", 0x96: "push", 0x99: "branchAlways", 0x9A: "getUrl2", 0x9B: "defineFunction",
    0x9D: "branchIfTrue", 0x9E: "callFrame", 0x9F: "gotoFrame2", 0xA1: "pushString", 0xA2: "dictByte",
    0xA3: "dictWord", 0xA4: "getVarStr", 0xA5: "getMemberStr", 0xA6: "setVarStr", 0xA7: "setMemberStr",
    0xAE: "getVarDict", 0xAF: "getMemberDict", 0xB0: "callFuncPopDict", 0xB1: "callFuncSetVarDict",
    0xB2: "callMethodPopDict", 0xB3: "callMethodSetVarDict", 0xB4: "pushFloat", 0xB5: "pushByte",
    0xB6: "pushWord", 0xB7: "pushDWord", 0xB8: "branchIfFalse", 0xB9: "pushRegister", 0xBA: "breakpoint",
}
SIMPLE = {0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x14,
          0x15, 0x17, 0x18, 0x1C, 0x1D, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B,
          0x2C, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41,
          0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F, 0x50, 0x51, 0x52,
          0x53, 0x54, 0x55, 0x56, 0x58, 0x59, 0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x60, 0x61, 0x62, 0x63, 0x64, 0x65,
          0x66, 0x67, 0x69, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76}

CLIP_EVENTS = ["onLoad", "enterFrame", "unload", "mouseMove", "mouseDown", "mouseUp", "keyDown", "keyUp", "data",
               "initialize", "press", "release", "releaseOutside", "rollOver", "rollOut", "dragOver", "dragOut",
               "keyPress", "construct"]


class DecodeError(Exception):
    pass


class Insn:
    """One decoded instruction. op None = could not decode (note says why)."""
    __slots__ = ("offset", "op", "name", "args", "body", "target", "note")

    def __init__(self, offset, op, args=None, body=None, target=None, note=""):
        self.offset, self.op, self.args, self.body, self.target, self.note = offset, op, args or [], body, target, note
        self.name = OPS.get(op, "unknown_%02X" % op if op is not None else "undecodable")


class Cursor:
    def __init__(self, data, pos=0):
        self.data, self.pos = data, pos

    def take(self, n):
        if self.pos + n > len(self.data):
            raise DecodeError("read past the end at 0x%X" % self.pos)
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def align(self, n=4):
        self.pos = (self.pos + n - 1) & ~(n - 1)

    def u8(self):
        return self.take(1)[0]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def i16(self):
        return struct.unpack("<h", self.take(2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def f32(self):
        return struct.unpack("<f", self.take(4))[0]

    def ptr(self):
        self.align(4)
        return self.u32()

    def string_at(self, off):
        if off >= len(self.data):
            raise DecodeError("string offset 0x%X outside the data" % off)
        end = self.data.index(b"\0", off)
        return self.data[off:end].decode("latin-1")

    def ptr_string(self):
        off = self.ptr()
        return "" if off == 0 else self.string_at(off)


# ---- values: ('str', s) ('float', f) ('int', i) ('bool', b) ('reg', n) ('undef',) ('lookup', i)
def read_consts(const):
    """Constant table of the const file (main character offset, values)."""
    if not const.startswith(b"Apt constant file"):
        raise DecodeError("not a const file")
    c = Cursor(const, 20)
    main = c.ptr()
    n = c.i32()
    table = c.ptr()
    t = Cursor(const, table)
    out = []
    for _ in range(n):
        t.align(4)
        kind = t.i32()
        if kind == 1:
            out.append(("str", t.string_at(t.ptr())))
        elif kind == 3:
            t.ptr()
            out.append(("undef",))
        elif kind in (4, 5, 6, 7, 8):
            t.align(4)
            if kind == 6:
                v = t.f32()
            elif kind == 8:
                v = t.u32()
            else:
                v = t.i32()
            t.take(4) if False else None          # entries are 8 bytes: type + value
            out.append({4: ("reg", v), 5: ("bool", v != 0), 6: ("float", v), 7: ("int", v), 8: ("lookup", v)}[kind])
        else:
            raise DecodeError("constant type %d" % kind)
    return main, out


def fmt(v, dictionary=None, quote=True):
    k = v[0]
    if k == "str":
        return '"%s"' % v[1] if quote else v[1]
    if k == "lookup":
        if dictionary is not None and v[1] < len(dictionary):
            return fmt(dictionary[v[1]], None, quote)
        return "lookup#%d" % v[1]
    if k == "float":
        return "%g" % v[1]
    if k == "undef":
        return "undefined"
    if k == "bool":
        return "true" if v[1] else "false"
    if k == "reg":
        return "r%d" % v[1]
    return str(v[1])


# ---- stream decoding
def decode_stream(apt, offset, consts):
    """List of Insn for the stream at offset (stops at End)."""
    return _block(Cursor(apt, offset), None, consts)


def _pool(c, consts):
    c.align(4)
    n = c.i32()
    items_off = c.ptr()
    out = []
    ic = Cursor(c.data, items_off)
    for _ in range(max(0, n)):
        idx = ic.ptr()
        if idx >= len(consts):
            raise DecodeError("constant index %d of %d" % (idx, len(consts)))
        out.append(consts[idx])
    return out


def _skip_func_pool(c):
    c.align(4)
    c.i32()
    c.ptr()


def _block(c, end, consts):
    insns, boundaries, fixes = [], [], []
    start = c.pos
    while True:
        if end is not None and c.pos >= end:
            break
        boundaries.append(c.pos)
        off = c.pos
        op = c.u8()
        ins = Insn(off, op)
        fix = None
        try:
            if op == 0x00:
                pass
            elif op in (0x81, 0x87, 0x9F):
                c.align(4)
                v = c.i32()
                ins.args = [("bool", v != 0)] if op == 0x9F else [("int", v)]
            elif op == 0x83:
                ins.args = [("str", c.ptr_string()), ("str", c.ptr_string())]
            elif op in (0x8B, 0x8C):
                ins.args = [("str", c.ptr_string())]
            elif op in (0x96, 0x88):
                ins.args = _pool(c, consts)
            elif op in (0x9B, 0x8E):
                name = c.ptr_string()
                c.align(4)
                n_params = c.i32()
                if op == 0x8E:
                    regs, flags = c.i16(), c.i16()
                params_off = c.ptr()
                params = []
                if n_params > 0:
                    pc = Cursor(c.data, params_off)
                    for _i in range(n_params):
                        if op == 0x8E:
                            pc.align(4)
                            reg = pc.u32()
                            params.append("r%d=%s" % (reg, pc.ptr_string()))
                        else:
                            params.append(pc.ptr_string())
                c.align(4)
                size = c.i32()
                _skip_func_pool(c)
                body_end = c.pos + max(0, size)
                ins.args = [("str", name)] + [("str", p) for p in params]
                if op == 0x8E:
                    ins.note = "registers %d, flags 0x%04X" % (regs, flags & 0xFFFF)
                ins.body = _block(c, body_end, consts)
                c.pos = body_end
            elif op == 0x8F:                                    # try
                c.align(4)
                ts, cs, fs = c.u32(), c.u32(), c.u32()
                flags = c.u8()
                c.u8()
                c.u8()
                reg = c.u8()
                if flags & 4:
                    c.ptr()
                    ins.args = [("reg", reg)]
                else:
                    s = c.ptr_string()
                    ins.args = [("str", s)] if flags & 1 else []
                after = c.pos
                fix = ("try", after + ts, after + ts + cs, after + ts + cs + fs)
            elif op == 0x94:                                    # with
                c.align(4)
                delta = struct.unpack("<i", c.take(4))[0]
                fix = ("jump", c.pos + delta)
            elif op in (0x99, 0x9D, 0xB8):
                c.align(4)
                delta = c.i32()
                fix = ("jump", c.pos + delta)
            elif op == 0x77:
                ins.args = [("int", c.u32())]
            elif op in (0xA1, 0xA4, 0xA5, 0xA6, 0xA7):
                c.align(4)
                ins.args = [("str", c.ptr_string())]
            elif op in (0xA2, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3):
                ins.args = [("lookup", c.u8())]
            elif op == 0xA3:
                ins.args = [("lookup", c.u16())]
            elif op == 0xB4:
                ins.args = [("float", c.f32())]
            elif op == 0xB5:
                ins.args = [("int", c.u8())]
            elif op == 0xB6:
                ins.args = [("int", c.u16())]
            elif op == 0xB7:
                ins.args = [("int", c.u32())]
            elif op == 0xB9:
                ins.args = [("reg", c.u8())]
            elif op in SIMPLE:
                pass
            else:
                ins.op = None
                ins.name = "unknown_%02X" % op
                ins.note = "opcode 0x%02X is not known; the rest of this stream is not decoded" % op
                insns.append(ins)
                fixes.append(None)
                return insns
        except (DecodeError, struct.error, ValueError) as e:
            ins.note = "decode error: %s; the rest of this stream is not decoded" % e
            insns.append(ins)
            fixes.append(None)
            return insns
        insns.append(ins)
        fixes.append(fix)
        if op == 0x00 and end is None:
            break
    boundaries.append(c.pos)
    # byte targets -> instruction indices
    index = {b: i for i, b in enumerate(boundaries)}
    for ins, fix in zip(insns, fixes):
        if fix is None:
            continue
        if fix[0] == "jump":
            ins.target = index.get(fix[1])
            if ins.target is None:
                ins.note = "branch target 0x%X is not an instruction start" % fix[1]
        else:
            ins.args = ins.args + [("int", index.get(x, -1)) for x in fix[1:]]
    return insns


# ---- disassembly
def disassemble(insns, dictionary=None, indent=0):
    """Readable lines. A DefineDictionary updates the dictionary used by lookup values after it."""
    lines = []
    pad = "  " * indent
    for ins in insns:
        a = ", ".join(fmt(v, dictionary) for v in ins.args)
        if ins.op == 0x88:
            dictionary = list(ins.args)
            a = "[%s]" % ", ".join(fmt(v, None) for v in ins.args)
        tgt = "  -> #%d" % ins.target if ins.target is not None else ""
        note = "   ; " + ins.note if ins.note else ""
        lines.append("%s0x%04X  %-18s %s%s%s" % (pad, ins.offset, ins.name, a, tgt, note))
        if ins.body is not None:
            lines.append(pad + "  {")
            sub, dictionary = _body_lines(ins.body, dictionary, indent + 2)
            lines += sub
            lines.append(pad + "  }")
    return lines


def _body_lines(body, dictionary, indent):
    return disassemble(body, dictionary, indent), dictionary


# ---- collecting every stream of a movie
def collect_streams(movie):
    """All action streams of an apt_movie.Movie: [{'where', 'offset', 'insns', 'error'}].
    where: 'root frame N', 'sprite ID frame N', 'init sprite ID', and '... clip <event>' for PlaceObject handlers."""
    import apt_movie as M
    apt, const = movie.apt, movie.const
    _main, consts = read_consts(const)
    found, seen = [], set()

    def add(where, off):
        if not off or off in seen:
            return
        seen.add(off)
        try:
            found.append({"where": where, "offset": off, "insns": decode_stream(apt, off, consts), "error": ""})
        except (DecodeError, struct.error, ValueError) as e:
            found.append({"where": where, "offset": off, "insns": [], "error": str(e)})

    def walk(frames_ptr_count, label):
        n, arr = frames_ptr_count
        for fi in range(n):
            count, ctl = struct.unpack_from("<II", apt, arr + 8 * fi)
            for j in range(count):
                c = struct.unpack_from("<I", apt, ctl + 4 * j)[0]
                t = struct.unpack_from("<I", apt, c)[0]
                if t == 1:
                    add("%s frame %d" % (label, fi), struct.unpack_from("<I", apt, c + 4)[0])
                elif t == 8:
                    sid, off = struct.unpack_from("<II", apt, c + 4)
                    add("init sprite %d" % sid, off)
                elif t in (3, 9):
                    flags = struct.unpack_from("<I", apt, c + 4)[0]
                    aptr = struct.unpack_from("<I", apt, c + 0x3C)[0]
                    if flags & 0x80 and aptr:
                        nb, boff = struct.unpack_from("<II", apt, aptr)
                        bp = boff
                        for _ in range(nb):
                            bp = (bp + 3) & ~3
                            trig, key, soff = struct.unpack_from("<iiI", apt, bp)
                            bp += 12
                            ev = [CLIP_EVENTS[i] for i in range(len(CLIP_EVENTS)) if trig & (1 << i)] or [hex(trig)]
                            add("%s frame %d place depth %d char %d clip %s" % (
                                label, fi, struct.unpack_from("<I", apt, c + 8)[0],
                                struct.unpack_from("<I", apt, c + 12)[0], "+".join(ev)), soff)

    main = struct.unpack_from("<I", const, 20)[0]
    walk((struct.unpack_from("<I", apt, main + 16)[0], struct.unpack_from("<I", apt, main + 20)[0]), "root")
    for cid, ch in sorted(movie.chars.items()):
        if ch["type"] == "Sprite":
            p = ch["ptr"]
            walk((struct.unpack_from("<I", apt, p + 16)[0], struct.unpack_from("<I", apt, p + 20)[0]), "sprite %d" % cid)
    return found
