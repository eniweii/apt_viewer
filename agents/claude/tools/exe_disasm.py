#!/usr/bin/env python3
"""Disassemble handlers of Apt/engine_data/nfs.exe with the helper names of Apt/game_stuff/apt_exe_addresses.json and the strings they push.
Usage: python exe_disasm.py 0x71F5B0 [0x539520 ...] [--len 1536]      Needs: pip install pefile capstone
A linear sweep: it runs to the padding (int3), so early `ret`s of a handler do not stop it. The pair `push value; push "name"; call SetProp` is how a
handler builds its result (see Apt/APT_DECOMPILER_NOTES.md)."""
import json
import os
import sys

import _paths

try:
    import capstone
    import pefile
except ImportError:
    sys.exit("pip install pefile capstone")

exe = os.path.join(_paths.REPO, "Apt", "engine_data", "nfs.exe")
addr = os.path.join(_paths.REPO, "Apt", "game_stuff", "apt_exe_addresses.json")
pe = pefile.PE(exe)
base = pe.OPTIONAL_HEADER.ImageBase
names = {int(h["address"], 16): h["name"] for h in json.load(open(addr))["helpers"]}
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
data = pe.__data__


def text_at(va):
    try:
        off = pe.get_offset_from_rva(va - base)
        s = data[off:data.index(b"\0", off)]
        return s.decode("latin1") if 1 <= len(s) < 60 and all(32 <= c < 127 for c in s) else None
    except Exception:
        return None


length = 0x600
args = sys.argv[1:]
if "--len" in args:
    i = args.index("--len")
    length = int(args[i + 1], 0)
    del args[i:i + 2]
for arg in args:
    va = int(arg, 0)
    print("--", hex(va))
    off = pe.get_offset_from_rva(va - base)
    for ins in md.disasm(data[off:off + length], va):
        if ins.mnemonic == "int3":
            break
        note = ""
        if ins.mnemonic == "call" and ins.op_str.startswith("0x"):
            note = "  ; " + names.get(int(ins.op_str, 16), "")
        else:
            for tok in ins.op_str.replace(",", " ").split():
                if tok.startswith("0x") and len(tok) > 6 and ins.mnemonic in ("push", "mov"):
                    s = text_at(int(tok, 16))
                    note = '  ; "%s"' % s if s else note
        print("  %x: %-6s %s%s" % (ins.address, ins.mnemonic, ins.op_str, note))
