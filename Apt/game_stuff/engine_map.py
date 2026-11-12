#!/usr/bin/env python3
"""
Map the native functions nfs.exe registers into the apt scripts (the _global.* objects the movies call but never define).

  python engine_map.py <nfs.exe> [<movies folder with the UI .big files>] [<out.json>] [<attrib dump.json> <hashes_main.txt>]

What it does (needs: pip install pefile capstone):
  1. Finds every member registration in nfs.exe. The exe registers a native as
         push handler; call NewNative; add esp,4; push eax; push "Name"; mov ecx,object; call SetMember
     NewNative and SetMember are located as the two functions that are called the same number of times and always in that pairing.
  2. Reads each handler (a small wrapper) and records its signature from the helper calls it makes:
         arguments   GetArgInt(i) / GetArg(i)
         result      NewObject(type) with the property names it sets, or none (undefined)
         other       strings it references, plus how many game functions it calls
  3. If a movies folder is given, groups the registrations into the objects the scripts use (by member name overlap with the
     decompiled scripts) and reports which script-used members were not found.
  4. With an attrib dump (UndercoverVLT_Dumper output) and a hash name list, follows the direct calls from each handler (3 levels) and
     records which attrib classes, fields and collections it can reach, by looking for their name hashes (VLT lookup2, seed 0xABCDEF00)
     as immediates in the code. This shows where each member gets its data from the attrib database.
Addresses of the helpers are those of the Undercover PC nfs.exe; they are looked up by role below, not hard coded as the object model.
"""
import collections, json, os, re, struct, sys

import capstone
import pefile
from capstone import x86

NEW_NATIVE, SET_MEMBER = 0x9ADAB0, 0x9ADBC0                    # create a native function value / set a member on an object
ARG_INT, ARG_RAW = 0x805B90, 0x9AD9D0                           # read argument i of the script call (as int / as it is)
RETURN_STRING = 0x805F50                                        # return a string (GetScreenName passes the table's name pointer)
NEW_OBJECT, RETURN_UNDEF = 0x9ADA70, 0x9ADA20                   # result object of a given type / "return undefined"
RETURN_VALUE = {0x805FB0: "bool", 0x805F50: "string", 0x805FC0: "int?", 0x805FD0: "unknown value"}   # return a plain value; the bool one is confirmed (IsWidescreen)
SET_PROP = {0x805DD0: "set(int?)", 0x805C80: "set(?)", 0x805E50: "set(?)", 0x805CF0: "set(string)"}   # property setters, name pushed before the call


class Exe:
    def __init__(self, path):
        self.pe = pefile.PE(path)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.data = open(path, "rb").read()
        sec = self.pe.sections[0]
        self.t0, self.tv, self.tn = sec.PointerToRawData, self.base + sec.VirtualAddress, sec.Misc_VirtualSize
        self.text = self.data[self.t0:self.t0 + self.tn]
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        self.md.detail = True

    def cstr(self, va, limit=80):
        for s in self.pe.sections:
            a = self.base + s.VirtualAddress
            if a <= va < a + s.SizeOfRawData:
                o = s.PointerToRawData + va - a
                e = self.data.find(b"\0", o, o + limit)
                b = self.data[o:e] if e >= 0 else b""
                if len(b) >= 2 and all(32 <= c < 127 for c in b):
                    return b.decode()
        return None

    def calls_to(self, target):
        out = []
        for m in re.finditer(b"\xe8", self.text):
            p = m.start()
            if p + 5 <= len(self.text) and self.tv + p + 5 + struct.unpack("<i", self.text[p + 1:p + 5])[0] == target:
                out.append(self.tv + p)
        return out

    def insns(self, va, size):
        o = self.t0 + va - self.tv
        return list(self.md.disasm(self.data[o:o + size], va))

    def func_end(self, va):
        o = self.t0 + va - self.tv
        e = self.data.find(b"\xcc\xcc", o)
        return min(e if e > 0 else o + 4000, o + 6000) - o


def registrations(exe):
    news = exe.calls_to(NEW_NATIVE)
    rows = []
    for c in exe.calls_to(SET_MEMBER):
        before = [i for i in exe.insns(c - 60, 60) if i.address < c][-12:]
        name = None
        for i in reversed(before):
            if i.mnemonic == "push" and i.op_str.startswith("0x") and name is None:
                s = exe.cstr(int(i.op_str, 16))
                if s and s.isidentifier():
                    name = s
        handler = None
        near = [n for n in news if c - 80 < n < c]
        if near:
            for i in reversed([i for i in exe.insns(near[-1] - 12, 12) if i.address < near[-1]]):
                if i.mnemonic == "push" and i.op_str.startswith("0x") and exe.tv <= int(i.op_str, 16) < exe.tv + exe.tn:
                    handler = int(i.op_str, 16)
                    break
        fs = exe.data.rfind(b"\xcc\xcc", exe.t0, exe.t0 + c - exe.tv) + 2
        rows.append({"member": name, "handler": handler, "registrar": exe.tv + fs - exe.t0})
    return rows


def signature(exe, handler):
    ins = exe.insns(handler, exe.func_end(handler))
    args, props, strings, result, ncalls = [], [], [], None, 0
    pushes = []
    for i in ins:
        if i.mnemonic == "push" and i.op_str.startswith(("0x", "-")) or (i.mnemonic == "push" and i.op_str.isdigit()):
            try:
                pushes.append(int(i.op_str, 0))
            except ValueError:
                pass
        if i.mnemonic == "call" and i.operands and i.operands[0].type == x86.X86_OP_IMM:
            t = i.operands[0].imm & 0xFFFFFFFF
            ncalls += 1
            if t in (ARG_INT, ARG_RAW) and pushes:
                args.append({ARG_INT: "int", ARG_RAW: "any"}[t] + "@%d" % pushes[-1])
            elif t == NEW_OBJECT and pushes:
                result = "object(type %d)" % pushes[-1]
            elif t in SET_PROP:
                for v in reversed(pushes[-5:]):
                    s = exe.cstr(v) if v >= 0x400000 else None
                    if s:
                        props.append({"name": s, "setter": SET_PROP[t]})
                        break
            elif t == RETURN_UNDEF:
                result = result or "undefined"
            elif t in RETURN_VALUE:
                result = RETURN_VALUE[t]
        elif i.mnemonic in ("push", "mov"):
            for op in i.operands:
                if op.type == x86.X86_OP_IMM and (op.imm & 0xFFFFFFFF) >= 0x400000:
                    s = exe.cstr(op.imm & 0xFFFFFFFF)
                    if s and s not in strings and not any(p["name"] == s for p in props):
                        strings.append(s)
    if props:
        result = "object"                                  # it fills a result object with named properties
    return {"args": args, "result": result, "properties": props, "strings": strings[:10], "game_calls": ncalls}


def screen_table(exe, rows):
    """The screen table GetScreenName searches: [{id, name, handler}]. The handler walks a table of 12-byte entries
    (id, name pointer, code pointer); its start, end and step are read from the handler's own instructions
    (mov ecx, start / cmp ecx, end / add ecx, step)."""
    reg = next((r for r in rows if r["member"] == "GetScreenName" and r["handler"]), None)
    if reg is None:
        return []
    start = end = step = None
    for i in exe.insns(reg["handler"], exe.func_end(reg["handler"])):
        a, _, b = i.op_str.partition(", ")
        if a == "ecx" and b.startswith("0x"):
            if i.mnemonic == "mov" and start is None:
                start = int(b, 16)
            elif i.mnemonic == "cmp" and end is None:
                end = int(b, 16)
            elif i.mnemonic == "add" and step is None:
                step = int(b, 16)
    if None in (start, end, step):
        return []

    def read(va, n):
        for sec in exe.pe.sections:
            base = exe.base + sec.VirtualAddress
            if base <= va < base + sec.SizeOfRawData:
                o = sec.PointerToRawData + va - base
                return exe.data[o:o + n]
        return b"\0" * n
    out = []
    for va in range(start, end, step):
        ident, name_ptr, code_ptr = struct.unpack("<III", read(va, 12))
        out.append({"id": ident, "name": exe.cstr(name_ptr) if name_ptr else None, "handler": "0x%X" % code_ptr})
    return out


def vlt_hash(text, init=0xABCDEF00):
    """VaultLib Vlt32Hasher: Bob Jenkins lookup2 over the low byte of each character."""
    M = 0xFFFFFFFF
    k = bytes(ord(c) & 0xFF for c in text)

    def mix(a, b, c):
        a = (a - b - c) & M; a ^= c >> 13; b = (b - c - a) & M; b ^= (a << 8) & M; c = (c - a - b) & M; c ^= b >> 13
        a = (a - b - c) & M; a ^= c >> 12; b = (b - c - a) & M; b ^= (a << 16) & M; c = (c - a - b) & M; c ^= b >> 5
        a = (a - b - c) & M; a ^= c >> 3; b = (b - c - a) & M; b ^= (a << 10) & M; c = (c - a - b) & M; c ^= b >> 15
        return a, b, c
    a = b = 0x9E3779B9
    c, i = init, 0
    while len(k) - i >= 12:
        a = (a + struct.unpack("<I", k[i:i + 4])[0]) & M; b = (b + struct.unpack("<I", k[i + 4:i + 8])[0]) & M
        c = (c + struct.unpack("<I", k[i + 8:i + 12])[0]) & M
        a, b, c = mix(a, b, c)
        i += 12
    c = (c + len(k)) & M
    t = k[i:]
    for n, shift, reg in ((11, 24, "c"), (10, 16, "c"), (9, 8, "c"), (8, 24, "b"), (7, 16, "b"), (6, 8, "b"), (5, 0, "b"),
                          (4, 24, "a"), (3, 16, "a"), (2, 8, "a"), (1, 0, "a")):
        if len(t) >= n:
            v = t[n - 1] << shift
            if reg == "a": a = (a + v) & M
            elif reg == "b": b = (b + v) & M
            else: c = (c + v) & M
    return mix(a, b, c)[2]


def load_names(path):
    """{vlt hash: name} from a list of names (one per line, # comments); lower case variants are tried too."""
    out = {}
    for line in open(path, encoding="latin1"):
        line = line.rstrip("\n")
        if line and not line.startswith("#"):
            for t in {line, line.lower()}:
                out.setdefault(vlt_hash(t), line)
    return out


def attrib_reach(exe, rows, dump_path, names_path, depth=3, limit=250):
    """{handler address: [attrib names it can reach]}: classes, fields and collections whose hash appears in the code behind the handler."""
    names = load_names(names_path)
    dump = json.load(open(dump_path))
    nm = lambda h: names.get(h, "0x%08X" % h)
    kind = {}
    for c in dump["classes"]:
        ch = int(c["hash"], 16)
        kind[ch] = ("class", nm(ch), None)
        for f in c["fields"]:
            kind.setdefault(int(f["hash"], 16), ("field", nm(int(f["hash"], 16)), nm(ch)))
        for col in c["collections"]:
            kind.setdefault(int(col["hash"], 16), ("collection", nm(int(col["hash"], 16)), nm(ch)))
    cache = {}

    def scan(fva):
        if fva not in cache:
            hits, calls = set(), set()
            for i in exe.insns(fva, exe.func_end(fva)):
                for op in i.operands:
                    if op.type == x86.X86_OP_IMM:
                        v = op.imm & 0xFFFFFFFF
                        if i.mnemonic == "call":
                            if exe.tv <= v < exe.tv + exe.tn:
                                calls.add(v)
                        elif v > 0xFFFF and v in kind:
                            hits.add(v)
            cache[fva] = (hits, calls)
        return cache[fva]
    out = {}
    for r in rows:
        h = r["handler"]
        if not h or h in out:
            continue
        seen, frontier, hits = {h}, [h], set()
        for _ in range(depth):
            nxt = []
            for f in frontier:
                fh, calls = scan(f)
                hits |= fh
                for c in calls - seen:
                    if len(seen) < limit:
                        seen.add(c)
                        nxt.append(c)
            frontier = nxt
        grouped = collections.defaultdict(set)
        for x in hits:
            k, n, cls = kind[x]
            grouped["classes" if k == "class" else "fields" if k == "field" else "collections"].add(n if k == "class" else "%s.%s" % (cls, n))
        out[h] = {k: sorted(v) for k, v in grouped.items()}
    return out


def script_members(folder):
    """Members of every engine global the scripts call: {object: set(members)}; names no script assigns."""
    sys.path[:0] = ["../", "./", "../../common"]
    import apt_movie, glob, inventory
    texts = [inventory.movie_text(inventory.inventory_movie(apt_movie.load(p), log=lambda *a: None))
             for p in sorted(glob.glob(folder.rstrip("/\\") + "/*.big"))]
    t = "\n".join(texts)
    assigned = set(re.findall(r"^\s*_global\.(\w+) = ", t, re.M))
    names = sorted({n for n in re.findall(r"_global\.(\w+)", t) if n not in assigned})
    return {n: set(re.findall(r"_global\.%s\.(\w+)" % n, t)) for n in names}


def main():
    exe = Exe(sys.argv[1])
    rows = registrations(exe)
    for r in rows:
        r["signature"] = signature(exe, r["handler"]) if r["handler"] else None
    if len(sys.argv) > 5:
        reach = attrib_reach(exe, rows, sys.argv[4], sys.argv[5])
        for r in rows:
            if r["signature"] is not None and r["handler"] in reach and reach[r["handler"]]:
                r["signature"]["attrib"] = reach[r["handler"]]
    by_reg = collections.defaultdict(list)
    for r in rows:
        by_reg[r["registrar"]].append(r)
    out = {"registrations": len(rows), "registrars": {hex(k): [r["member"] for r in v] for k, v in by_reg.items()}}
    objects = {}
    if len(sys.argv) > 2:
        used = script_members(sys.argv[2])
        sets = {k: {r["member"] for r in v if r["member"]} for k, v in by_reg.items()}
        by_name = collections.defaultdict(list)
        for r in rows:
            if r["member"]:
                by_name[r["member"]].append(r)
        for obj, members in used.items():
            if not members:
                objects[obj] = {"registrar": None, "members": {}, "not_in_exe": []}
                continue
            best = max(sets, key=lambda k: len(members & sets[k]))
            found, ambiguous = {}, []
            for m in sorted(members):
                here = [r for r in by_name.get(m, []) if r["registrar"] == best]
                cand = here or by_name.get(m, [])                  # not in the best registrar: use it only when the name is unique
                if len(cand) == 1:
                    found[m] = cand[0]
                elif cand:
                    ambiguous.append(m)
            objects[obj] = {"registrar": hex(best),
                            "members": {m: {"handler": hex(r["handler"]) if r["handler"] else None,
                                            "registrar": hex(r["registrar"]), **(r["signature"] or {})} for m, r in found.items()},
                            "ambiguous": ambiguous,
                            "not_in_exe": sorted(members - set(found) - set(ambiguous))}
        out["objects"] = objects
    out_path = sys.argv[3] if len(sys.argv) > 3 else "apt_engine_map.json"
    json.dump(out, open(out_path, "w"), indent=1)
    screens = screen_table(exe, rows)
    if screens:                                                 # screen id -> movie name, beside the map
        json.dump(screens, open(os.path.join(os.path.dirname(os.path.abspath(out_path)), "apt_screen_names.json"), "w"), indent=1)
        print("%d screens (id -> movie name) written to apt_screen_names.json" % len(screens))
    print("%d registrations, %d registrar functions" % (len(rows), len(by_reg)))
    if objects:
        print("%d objects, members not found in the exe: %d, ambiguous: %d" % (len(objects), sum(len(o["not_in_exe"]) for o in objects.values()),
                                                                           sum(len(o.get("ambiguous", [])) for o in objects.values())))


if __name__ == "__main__":
    main()
