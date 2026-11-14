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

# the engine feed: providers, the message queue, a user stub wins, an unfed native answers undefined and is listed
import os
import apt_engine

eng = apt_engine.Engine(os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_stuff", "apt_engine_map.json"))
evm = VM.VM(1)
eng.install(evm)
call = lambda obj, member, *args: evm.call(evm.globals.props[obj].props[member], evm.globals.props[obj], list(args))
check("GetStringHash", call("ScreenMessageTranslator", "GetStringHash", "SETVISIBLE") == apt_data.bstring_hash("SETVISIBLE"))
check("no message", call("ScreenMessageTranslator", "IsMessageAvailable") is False)
eng.post_message({"message": "SETVISIBLE", "level": 1, "mvClip": "mcX", "visible": 0})
check("message queued", call("ScreenMessageTranslator", "IsMessageAvailable") is True)
msg = call("ScreenMessageTranslator", "GetMessage")
check("message id and fields", evm.get_member(msg, "message") == apt_data.bstring_hash("SETVISIBLE") and evm.get_member(msg, "mvClip") == "mcX")
check("queue empty again", call("ScreenMessageTranslator", "IsMessageAvailable") is False)
check("widescreen 16x9", evm.get_member(call("CmdUtil", "GetWidescreen"), "widescreen") == 1)
eng.options["widescreen"] = False
check("widescreen 4x3", evm.get_member(call("CmdUtil", "GetWidescreen"), "widescreen") == 0)
evm.stubs["_global.CmdUtil.GetWidescreen()"] = {"widescreen": 1}
check("a stub wins over the provider", evm.get_member(call("CmdUtil", "GetWidescreen"), "widescreen") == 1)
obj, member = next((o, m) for o, ms in sorted(eng.members.items()) for m in ms if (o, m) not in eng.providers)
check("unfed native: undefined and listed", call(obj, member) is VM.UNDEF and "_global.%s.%s()" % (obj, member) in evm.externals)
check("ambiguous native is installed (GetCarList on CmdFECarLot)", "GetCarList" in eng.members.get("CmdFECarLot", []))

check("GetScreenName: id -> movie name (table of nfs.exe)", call("ScreenMessageTranslator", "GetScreenName", 4.0) == "PostRace" and call("ScreenMessageTranslator", "GetScreenName", 0.0) == "main")
check("GetScreenName: unknown id -> undefined", call("ScreenMessageTranslator", "GetScreenName", 1234.0) is VM.UNDEF)
# ~KEY localisation, GetYearInfo, GetLanguageList
import datetime


class FakeLang:
    table = {"COMMON_BACK": "BACK", "WM_TRI_CITY_BAY": "Tri-City Bay"}

    def text(self, key):
        return self.table.get(key)


loc = apt_engine.Engine(os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_stuff", "apt_engine_map.json"), language=FakeLang())
check("~KEY is replaced by the string of that name", loc.localize("~COMMON_BACK") == "BACK" and loc.localize("Go ~COMMON_BACK now") == "Go BACK now")
check("a key in no table stays, text without ~ is untouched", loc.localize("~NO_SUCH_KEY") == "~NO_SUCH_KEY" and loc.localize("plain") == "plain")
check("two keys in one text", loc.localize("~COMMON_BACK / ~WM_TRI_CITY_BAY") == "BACK / Tri-City Bay")
year = loc.year_info([])
check("GetYearInfo: max is this year (at least 2008), min a hundred years before", year == {"min": max(datetime.date.today().year, 2008) - 100, "max": max(datetime.date.today().year, 2008)})
langs = loc.language_list([])
check("GetLanguageList: the first 15 rows of the exe's language table", langs is not None and len(langs) == 15 and langs[0] == "ENGLISH" and langs[-1] == "NORWEGIAN" and len(set(langs)) == 15)
vm_l = VM.VM(1)
loc.install(vm_l)
res = vm_l.globals.props["cmdPCCreateAccount"].vm_call("GetYearInfo", [], vm_l)
check("GetYearInfo reaches the script as an object with min and max", res.vm_get("max") == year["max"] and res.vm_get("min") == year["min"])

# the car lot (needs the data folder: attrib dump and English tables; skipped without them)
data = apt_engine.build_default(None)
if data is not None and data.attrib is not None and data.language is not None:
    stock = data.carlot_stock()
    check("the shop stock is the 59 cars of car_list.fe_carlist", stock is not None and len(stock) == 59)
    check("GetRegions: American, European, Japanese as toggle entries", [(r["text"], r["regionEnum"], r["type"]) for r in data.carlot_regions([])] == [("American", 0, "Toggle"), ("European", 1, "Toggle"), ("Japanese", 2, "Toggle")])
    per = {}
    for n in (0, 1, 2):
        data.carlot_update_region([float(n)])
        per[n] = data.carlot_cars([])
    check("the three regions together list every stock car once (20 + 27 + 12)", sorted(len(v) for v in per.values()) == [12, 20, 27] and sum(len(v) for v in per.values()) == 59)
    check("entries carry index (position in the region list) and carName", all(c["index"] == i for v in per.values() for i, c in enumerate(v)) and all(isinstance(c["carName"], str) and c["carName"] for v in per.values() for c in v))
    names = [c["carName"] for v in per.values() for c in v]
    check("car names come from the language tables (Audi R8, Plymouth HEMI Cuda, Lexus IS F)", "Audi R8" in names and "Lexus IS F" in names and any("Cuda" in n and "Plymouth" in n for n in names))
    check("at least 56 of the 59 names resolve (Stingray '67, SLR 722 and GT500 '67 keys are not known)", sum(1 for (car, _r) in stock if data.car_name(car) != car) >= 56)
    data.carlot_update_region([3.0])
    check("region 3 holds no shop car", data.carlot_cars([]) == [])
else:
    print("SKIP car lot checks: no attrib dump / language tables in the data folder")

# CmdUtil.IsInputNecessary (0x808D10): true when the game has nothing pending, which is the case here
check("IsInputNecessary answers true, so the root hands keys to the screen", loc.providers[("CmdUtil", "IsInputNecessary")]([]) is True)

sys.exit(0 if ok else 1)
