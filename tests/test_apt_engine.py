#!/usr/bin/env python3
"""Checks for apt_data (hashes, language tables) and the VM ops added with apt_engine. Run: python test_apt_engine.py"""
import struct
import sys

import apt_actions as AA
import apt_data
import apt_vm as VM

ok = True


def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# hashes, checked against the game data: PERFORMBACK is the example in apt_scan, frontend_sfx is the class hash in the attrib dump
check("bstring_hash", apt_data.bstring_hash("PERFORMBACK") == 0x02D13D0B)
check("vlt_hash", apt_data.vlt_hash("frontend_sfx") == 0xE0B75B79)

# a language table with two strings: header, table (first entry (0, 0)), 8 bytes, then the strings
strings = [b"Hello", b"World!"]
hashes = sorted([apt_data.bstring_hash("A"), apt_data.bstring_hash("B")])
table_at = 0x1C
base = table_at + 8 * 3
pool = b"\0" * 0 + b"Hello\0World!\0"
raw = struct.pack("<5I", 0x39000, 0, 3, table_at, base) + b"Test\0\0\0\0" + b"\0" * (table_at - 28)
raw = raw[:table_at] + struct.pack("<II", 0, 0) + struct.pack("<II", hashes[0], 0) + struct.pack("<II", hashes[1], 6) + b"\0" * 8 + pool
open("_t.bin", "wb").write(raw)
t = apt_data.read_language("_t.bin")
check("language table (strings 8 bytes after the table)", t == {hashes[0]: b"Hello", hashes[1]: b"World!"})
import os
os.remove("_t.bin")

# VM ops: extends links prototypes, super calls the superclass, enumerate2 lists keys, getProperty reads a clip property
def run(insns, fr_this=None):
    vm = VM.VM(1)
    return vm, vm.run(insns, VM.Frame(fr_this or VM.Obj(), fr_this or VM.Obj(), None, None))


def I(op, args=None, body=None, target=None, note=""):
    return AA.Insn(0, op, args or [], target=target, body=body, note=note) if "body" in AA.Insn.__init__.__code__.co_varnames else AA.Insn(0, op, args or [], target=target)


vm = VM.VM(1)
sup, sub = VM.Func([], [], 0, 0, None, None, 0, "Sup"), VM.Func([], [], 0, 0, None, None, 1, "Sub")
vm.globals.props["Sup"], vm.globals.props["Sub"] = sup, sub
vm._run([AA.Insn(0, 0x96, [("str", "Sub")]), AA.Insn(1, 0x1C, []), AA.Insn(2, 0x96, [("str", "Sup")]), AA.Insn(3, 0x1C, []), AA.Insn(4, 0x69, [])],
        VM.Frame(vm.globals, vm.globals, None, None))
check("extends: prototype chain", sub.props["prototype"].proto is sup.props["prototype"] and sub.props["prototype"].props["constructor"] is sup)
inst = VM.Obj(proto=sub.props["prototype"])
check("super of a constructor", isinstance(vm._super_for(sub, inst), VM.SuperProxy) and vm._super_for(sub, inst).ctor is sup)
o = VM.Obj(); o.props["a"] = 1.0; o.props["length"] = 2.0
check("enumerate2 keys (no length)", vm._enum_keys(o) == ["a"])
sys.exit(0 if ok else 1)
