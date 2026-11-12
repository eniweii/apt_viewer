#!/usr/bin/env python3
"""
One file with every address in nfs.exe that the apt UI depends on (Undercover PC build, image base 0x400000).

  python exe_addresses.py <nfs.exe> [<apt_engine_map.json>] [<out folder>]      (needs pefile + capstone)

Writes EXE_ADDRESSES.md (to read) and apt_exe_addresses.json (to use): the helper functions the natives are built from, the function that creates
the _global objects and their constructors, every registrar with its handlers, the data tables the natives walk (decoded where the meaning
is known, raw otherwise), the game functions the handlers call most, and one line per native with its signature.
Meanings are marked: confirmed (checked against game data or a handler's behaviour) or guessed.
"""
import collections, json, os, re, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv, _argv = sys.argv[:1], sys.argv
import engine_map as E
sys.argv = _argv
from capstone import x86

# role of each helper: (address, name, what, how sure)
HELPERS = [
    (E.NEW_NATIVE, "NewNative", "wraps a handler address into a native function value; push handler; call; add esp,4", "confirmed (all 272 registrations)"),
    (E.SET_MEMBER, "SetMember", "sets a member on an object by name: push eax(native); push \"Name\"; mov ecx,object; call", "confirmed"),
    (E.ARG_INT, "GetArgInt", "argument i of the script call as an int (push i; call)", "confirmed (GetScreenName)"),
    (E.ARG_RAW, "GetArg", "argument i of the script call as it is (push i; call); the value is then converted by the caller", "guessed from IsInputNecessary"),
    (E.NEW_OBJECT, "NewResultObject", "creates the result object of a native: push type; call (types seen 1, 2, 5, 9, 32)", "confirmed (GetWidescreen, GetRegions)"),
    (E.RETURN_UNDEF, "ReturnUndefined", "ends a native with no value", "confirmed (PlaySound)"),
    (0x805FB0, "ReturnBool", "ends a native with a bool", "confirmed (IsWidescreen)"),
    (0x805FC0, "ReturnInt?", "ends a native with a value (jmp 0x9DABC0)", "guessed"),
    (0x805FD0, "ReturnValue?", "ends a native with a value (jmp 0x9DACA0)", "unknown type"),
    (E.RETURN_STRING, "ReturnString", "ends a native with a string: push char*; call", "confirmed (GetScreenName)"),
    (0x805DD0, "SetPropInt?", "sets a property on the result object by name (int pointer)", "guessed from GetWidescreen"),
    (0x805C80, "SetProp?", "sets a property on the result object by name", "type not decoded"),
    (0x805E50, "SetProp?", "sets a property on the result object by name", "type not decoded"),
    (0x805CF0, "SetPropString", "sets a string property (formats into a stack buffer first)", "guessed"),
    (0x5391D0, "FindScreen", "screen object from a screen id, through the manager pointer at [0x12AA044]", "confirmed (GetIsScreenDirty, GetTreeData)"),
    (0x535B80, "PostGameEvent", "posts a command to the game (the handlers that act, such as OnEnterComplete and the Cmd setters)", "guessed"),
]
DATA = [
    (0x12AA044, "ScreenManager*", "global pointer to the screen manager (FindScreen's this)", "confirmed"),
]
# tables whose meaning is known: start -> (name, columns, how sure)
KNOWN = {
    0xCBAEB8: ("screens", ["id", "movie name", "handler"], "confirmed: 92 of 95 names are movies of the UI folder; GetScreenName walks it"),
    0xCBA7B8: ("languages", ["id", "flags", "name", "code", "pointer"], "confirmed text (ENGLISH/EN ...); walked from StartMovie"),
    0xB74340: ("resolutions", ["width", "height", "string hash (language table text WxH)"], "confirmed: the hash resolves to '800x480' ... in English_Global.bin; walked from cmdOptions.GetData"),
    0xCBB39C: ("result columns A", ["event type mask", "title 1", "title 2", "title 3", "title 4", "", "", "", ""], "titles are language hashes (Cost to State, Time, # Disabled, Car); reached from GetErrorMessage's callees, so the owner is a guess"),
    0xCBB64C: ("result columns B", ["event type mask", "title 1", "title 2", "title 3", "title 4", "", "", "", ""], "same layout, titles # of Points, # of Drops, # of Busts"),
    0xCBB6B8: ("result columns C", ["a", "b", "title 1", "title 2", "title 3", "title 4", "", "", ""], "same layout, other shape"),
    0x126F818: ("car slots", ["slot index", "name", "x"], "names like BADGING_BUMPER_SET_FRONT; walked from ScrollNextCarSlot/SelectSlot"),
    0xCB9140: ("slot table", ["a", "b", "c", "d", "e"], "254 entries walked from Back/CarHighlighted/CarSelected: meaning not decoded"),
}


def reader(exe):
    def rd(va, n):
        for s in exe.pe.sections:
            a = exe.base + s.VirtualAddress
            if a <= va < a + s.SizeOfRawData:
                o = s.PointerToRawData + va - a
                return exe.data[o:o + n]
        return b"\0" * n
    return rd


def word_text(exe, v, dsec):
    if v == 0:
        return "0"
    if exe.tv <= v < exe.tv + exe.tn:
        return "code:%X" % v
    s = exe.cstr(v) if v >= 0x400000 else None
    if s:
        return s
    if any(a <= v < b for a, b in dsec):
        return "data:%X" % v
    return str(v) if v < 100000 else "0x%X" % v


def find_tables(exe, rows):
    """Tables the handlers walk: mov reg, start / cmp reg, end / add reg, stride within 3 levels of calls from a handler."""
    dsec = [(exe.base + s.VirtualAddress, exe.base + s.VirtualAddress + s.SizeOfRawData) for s in exe.pe.sections[1:3]]
    indata = lambda v: any(a <= v < b for a, b in dsec)
    cache = {}

    def scan(f):
        if f not in cache:
            movs, cmps, adds, calls = [], [], [], set()
            for i in exe.insns(f, exe.func_end(f)):
                ops = i.op_str.split(", ")
                if i.mnemonic == "call" and i.operands and i.operands[0].type == x86.X86_OP_IMM:
                    v = i.operands[0].imm & 0xFFFFFFFF
                    if exe.tv <= v < exe.tv + exe.tn:
                        calls.add(v)
                if len(ops) == 2 and ops[1].startswith("0x"):
                    v = int(ops[1], 16)
                    if i.mnemonic == "mov" and indata(v):
                        movs.append((ops[0], v))
                    elif i.mnemonic == "cmp" and indata(v):
                        cmps.append((ops[0], v))
                    elif i.mnemonic == "add" and v <= 0x100:
                        adds.append((ops[0], v))
            cache[f] = (movs, cmps, adds, calls)
        return cache[f]
    found = {}
    for r in rows:
        if not r["handler"]:
            continue
        seen, frontier = {r["handler"]}, [r["handler"]]
        for _ in range(3):
            nxt = []
            for f in frontier:
                movs, cmps, adds, calls = scan(f)
                for reg, s in movs:
                    for reg2, e in cmps:
                        if reg2 == reg and 0 < e - s < 0x40000:
                            for reg3, st in adds:
                                if reg3 == reg and st >= 4 and (e - s) % st == 0:
                                    found.setdefault((s, e, st), set()).add(str(r["member"]))
                for c in calls - seen:
                    if len(seen) < 200:
                        seen.add(c)
                        nxt.append(c)
            frontier = nxt
    rd = reader(exe)
    out = []
    starts = {k[0] for k in found}
    for (s, e, st), users in sorted(found.items()):
        if any(s == o + d for o in starts for d in (4, 8)) and s not in KNOWN:
            continue                                            # the same table seen again through one of its columns
        n = (e - s) // st
        entries = [[word_text(exe, w, dsec) for w in struct.unpack("<%dI" % (st // 4), rd(s + k * st, st))] for k in range(n)]
        name, cols, how = KNOWN.get(s, ("table 0x%X" % s, [], "meaning not decoded"))
        out.append({"start": "0x%X" % s, "end": "0x%X" % e, "stride": st, "count": n, "name": name, "columns": cols, "status": how,
                    "walked_by": sorted(users)[:8], "entries": entries})
    return out


def object_sites(exe, names):
    """For each _global object name: the function that creates it and the constructor it calls with the name."""
    out = {}
    for n in names:
        sites = []
        for m in re.finditer(re.escape(n.encode()) + b"\0", exe.data):
            if exe.data[m.start() - 1:m.start()] != b"\0":
                continue
            va = None
            for s in exe.pe.sections:
                if s.PointerToRawData <= m.start() < s.PointerToRawData + s.SizeOfRawData:
                    va = exe.base + s.VirtualAddress + m.start() - s.PointerToRawData
            if va is None:
                continue
            for mm in re.finditer(re.escape(b"\x68" + struct.pack("<I", va)), exe.text):
                p = exe.t0 + mm.start()
                seg = exe.data[p + 5:p + 40]
                c = seg.find(b"\xe8")
                ctor = exe.tv + (p + 5 + c - exe.t0) + 5 + struct.unpack("<i", seg[c + 1:c + 5])[0] if c >= 0 else None
                fs = exe.data.rfind(b"\xcc\xcc", exe.t0, p) + 2
                sites.append({"site": "0x%X" % (exe.tv + mm.start()), "function": "0x%X" % (exe.tv + fs - exe.t0), "calls": ("0x%X" % ctor) if ctor else None})
        out[n] = sites
    return out


def main():
    exe_path = sys.argv[1]
    map_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "apt_engine_map.json")
    out_dir = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(os.path.abspath(__file__))
    exe = E.Exe(exe_path)
    rows = E.registrations(exe)
    emap = json.load(open(map_path))["objects"]
    tables = find_tables(exe, rows)
    sites = object_sites(exe, [o for o, v in emap.items() if v.get("members")])
    callee = collections.Counter()
    example = {}
    for r in rows:
        if r["handler"]:
            for i in exe.insns(r["handler"], exe.func_end(r["handler"])):
                if i.mnemonic == "call" and i.operands and i.operands[0].type == x86.X86_OP_IMM:
                    v = i.operands[0].imm & 0xFFFFFFFF
                    if exe.tv <= v < exe.tv + exe.tn:
                        callee[v] += 1
                        example.setdefault(v, r["member"])
    known = {h[0] for h in HELPERS}
    game_calls = [{"address": "0x%X" % a, "handlers": n, "example": example[a]} for a, n in callee.most_common(40) if a not in known]
    result = {"image_base": "0x%X" % exe.base, "sections": [(s.Name.rstrip(b"\0").decode(), "0x%X" % (exe.base + s.VirtualAddress), "0x%X" % s.Misc_VirtualSize) for s in exe.pe.sections],
              "helpers": [{"address": "0x%X" % a, "name": n, "what": w, "status": c} for a, n, w, c in HELPERS],
              "globals": [{"address": "0x%X" % a, "name": n, "what": w, "status": c} for a, n, w, c in DATA],
              "object_creation": sites, "registrars": {}, "tables": tables, "game_functions": game_calls}
    by_reg = collections.defaultdict(list)
    for r in rows:
        by_reg[r["registrar"]].append(r)
    for reg, lst in sorted(by_reg.items()):
        result["registrars"]["0x%X" % reg] = [{"member": r["member"], "handler": ("0x%X" % r["handler"]) if r["handler"] else None} for r in lst]
    json.dump(result, open(os.path.join(out_dir, "apt_exe_addresses.json"), "w"), indent=1)

    md = ["# nfs.exe addresses for the apt UI", "", "Undercover PC build, image base %s. Generated by `Apt/beamng/exe_addresses.py`; regenerate with `python Apt/apt_tools.py exe-addresses`." % result["image_base"], "",
          "Sections: " + ", ".join("%s %s (+%s)" % s for s in result["sections"]), "", "## Helpers the natives are built from", "", "| address | name | what | status |", "|---|---|---|---|"]
    md += ["| %(address)s | %(name)s | %(what)s | %(status)s |" % h for h in result["helpers"]]
    md += ["", "Globals: " + "; ".join("%(address)s %(name)s: %(what)s (%(status)s)" % g for g in result["globals"]), "",
           "## Creation of the _global objects", "",
           "One function (0x80F680) creates the objects: `new object; constructor(this, \"Name\")`, then each registrar function adds the members. Constructor calls seen:", "",
           "| object | creating function | constructor |", "|---|---|---|"]
    for o, ss in sorted(result["object_creation"].items()):
        if ss:
            md.append("| %s | %s | %s |" % (o, ss[0]["function"], ss[0]["calls"]))
    md += ["", "## Registrars and handlers (%d natives in %d registrar functions)" % (len(rows), len(by_reg)), ""]
    for reg, lst in result["registrars"].items():
        md.append("- registrar `%s`: %s" % (reg, ", ".join("%s=%s" % (x["member"], x["handler"]) for x in lst)))
    md += ["", "## Data tables the natives walk", ""]
    for t in tables:
        md.append("### %s: %s..%s, %d entries of %d bytes" % (t["name"], t["start"], t["end"], t["count"], t["stride"]))
        md.append("- %s. Walked by %s." % (t["status"], ", ".join(t["walked_by"])))
        if t["columns"]:
            md.append("- columns: " + ", ".join(t["columns"]))
        for e in t["entries"][:3]:
            md.append("  - `%s`" % e)
        md.append("")
    md += ["## Game functions the handlers call most", "", "| address | handlers | example |", "|---|---|---|"]
    md += ["| %(address)s | %(handlers)d | %(example)s |" % g for g in game_calls[:30]]
    md += ["", "## Every native: signature", "", "| object.member | handler | args | result | properties | attrib |", "|---|---|---|---|---|---|"]
    for o, v in sorted(emap.items()):
        for m, x in sorted(v["members"].items()):
            md.append("| %s.%s | %s | %s | %s | %s | %s |" % (o, m, x.get("handler"), ", ".join(x.get("args", [])), x.get("result") or "", ", ".join(dict.fromkeys(p["name"] for p in x.get("properties", [])))[:60],
                                                          ", ".join(x.get("attrib", {}).get("classes", []))[:40]))
    open(os.path.join(out_dir, "EXE_ADDRESSES.md"), "w").write("\n".join(md) + "\n")
    print("%d helpers, %d tables, %d objects with creation sites, %d natives" % (len(HELPERS), len(tables), sum(1 for s in sites.values() if s), len(rows)))


if __name__ == "__main__":
    main()
