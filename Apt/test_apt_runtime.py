#!/usr/bin/env python3
"""Checks of the screen flow on the real UI movies: root.big loads a pushed screen with loadMovie, library imports, case-insensitive names,
the message gating of nfs.exe, messages to the screen, POP. Needs the UI folder: python test_apt_runtime.py <UI folder> (or APT_UI, or Apt/engine_data/UI);
without it the checks are skipped."""
import os
import sys

import apt_engine
import apt_movie
import apt_runtime

HERE = os.path.dirname(os.path.abspath(__file__))
ui = next((p for p in (sys.argv[1] if len(sys.argv) > 1 else None, os.environ.get("APT_UI"), os.path.join(HERE, "engine_data", "UI")) if p and os.path.isdir(p)), None)
if ui is None or not os.path.isfile(os.path.join(ui, "root.big")):
    print("SKIP no UI folder with root.big (give it as the argument or in APT_UI)")
    sys.exit(0)

ok = True


def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


eng = apt_engine.build_default(None)
apt_runtime.ENGINE = eng
root = apt_movie.load(os.path.join(ui, "root.big"))
ids = {v: k for k, v in eng.screens.items()}


def run(ops, ticks):
    rt = apt_runtime.Runtime(root, scenario=ops)
    rt.step(ticks)
    return rt


msg = lambda at, name, **k: dict({"at": at, "op": "message", "message": name, "level": 1}, **k)
push = lambda at, name: msg(at, "PUSH", screenId=ids[name])

rt = run([push(6, "PostRace")], 7)
check("a PUSH switches message processing off until the screen is up (EnableProcessing(false))", not eng.processing and rt.root.vm_get("OVERLAY_1") is not None)
rt = run([push(6, "PostRace")], 60)
ov = rt.root.vm_get("OVERLAY_1")
check("loadMovie puts postrace.big into OVERLAY_1", rt.load_log and rt.load_log[0][1] == "PostRace.swf" and ov.ctx.movie.name == "postrace.big")
check("the loaded clip has a size (the root waits for _width > 0)", ov.vm_get("_width") > 0 and ov.vm_get("_height") > 0)
check("mc.Screen finds the variable `screen` (names ignore case)", ov.vm_get("Screen") is ov.vm_get("screen") and ov.vm_get("Screen") is not None)
check("the screen ran: OnScreenConstructed, OnEnterComplete", [e[0] for e in eng.events if e[0].startswith("OnScreenC") or e[0] == "OnEnterComplete"] == ["OnScreenConstructed", "OnEnterComplete"])
check("OnEnterComplete switched processing on again (nfs.exe 0x8065d0)", eng.processing)
check("InitGui found InitGUI (case)", not any("InitGui" in k for k in rt.vm.unsupported))
check("no script error", not rt.errors)

ops = [push(6, "PostRace")] + [msg(40, "SETVISIBLE", mvClip="mContainerAnimation", visible=0), msg(42, "SETPOSITION", mvClip="mContainerAnimation", x=11, y=22),
                               msg(44, "SETTEXT", mvClip="mContainerAnimation", text="Hello"), msg(46, "SETSIZE", mvClip="mContainerAnimation", width=100, height=50)]
rt = run(ops, 60)
mc = rt.root.vm_get("OVERLAY_1").vm_get("mContainerAnimation")
check("SETVISIBLE / SETPOSITION / SETTEXT reach the clip", mc.vm_get("_visible") is False and (mc.vm_get("_x"), mc.vm_get("_y")) == (11.0, 22.0) and mc.vars.get("text") == "Hello")
check("SETSIZE sets _width and _height", abs(mc.vm_get("_width") - 100) < 1 and abs(mc.vm_get("_height") - 50) < 1)

# runtime-bound texture and script-set text
import apt_data
rt = run([push(6, "PostRace"), msg(40, "SETTEXTUREHASH", mvClip="mContainerAnimation", texture="HINT_PIC")], 60)
mc = rt.root.vm_get("OVERLAY_1").vm_get("mContainerAnimation")
check("SETTEXTUREHASH leaves TextureHash on the clip (bStringHash of the name)", mc.vars.get("TextureHash") == float(apt_data.bstring_hash("HINT_PIC")))


def clips(c):
    yield c
    for ch in list(c.children.values()):
        yield from clips(ch)


field = next((c for c in clips(rt.root.vm_get("OVERLAY_1")) if c.char is not None and c.char["type"] == "Text"), None)
check("PostRace has a text field", field is not None)
if field is not None:
    initial = field.char["text"]
    check("an unset text field reads its initial text and draws it", field.vm_get("text") == initial and field.text_value() is None)
    field.vm_set("text", "Hello")
    check("a script-set text is what the field shows", field.text_value() == "Hello" and field.vm_get("TEXT") == "Hello")
    field.vm_set("htmlText", "<b>x</b>y")
    field.vars.pop("text")
    check("htmlText is shown without its tags", field.text_value() == "xy")
    tf = field.vm_call("getTextFormat", [], rt.vm)
    field.vm_call("setTextFormat", [tf], rt.vm)
    check("getTextFormat gives an object, setTextFormat keeps it", tf is not None and field.vm_call("getTextFormat", [], rt.vm) is tf)

# the shop screen gets its cars and regions from the engine (needs the attrib dump and the English tables)
if "CarShopMenu" in ids and eng.attrib is not None and eng.language is not None:
    rt = run([push(6, "CarShopMenu")], 120)
    ov = rt.root.vm_get("OVERLAY_1")
    shop = ov.vm_get("screen")
    lst = next(c for c in clips(ov) if c.name.lower() == "mcarlist")
    tog = next(c for c in clips(ov) if c.name.lower() == "mregiontoggle")
    cars = lst.vm_get("maItemData")
    check("CarShopMenu: the scroller got the 20 American cars with their names", cars.vm_get("length") == 20 and cars.items[0].vm_get("carName") == "Cadillac CTS-V")
    check("CarShopMenu: the region toggle got American, European, Japanese", [tog.vm_get("maData").items[i].vm_get("text") for i in range(3)] == ["American", "European", "Japanese"])
    shop.vm_call("OptionSelected", [tog.vm_get("maData").items[2]], rt.vm)
    check("choosing Japanese sends UpdateRegion and refills the list (12 cars)", eng.carlot_region == 2 and lst.vm_get("maItemData").vm_get("length") == 12 and lst.vm_get("maItemData").items[0].vm_get("carName") == "Lexus IS F")

# keys: the game's pad codes go through the root's NFSEventHandler to the screen and out to the game
import apt_vm as VM


def calls(rt):
    return {k: v["count"] for k, v in rt.vm.externals.items()}


def press(rt, code):
    before = calls(rt)
    rt.key(code, True)
    rt.key(code, False)
    rt.step(2)
    return [k.replace("_global.", "") for k, v in rt.vm.externals.items() if v["count"] > before.get(k, 0)]


rt = run([push(6, "PostRace")], 120)
check("a Key listener is registered by the root", len(rt.vm.key_listeners) == 1)
check("PostRace: Down -> OnPadDown, Accept (302) -> OnButtonPressed, Back (303) -> OnPadBack",
      "EventTranslator.OnPadDown()" in press(rt, 40) and "EventTranslator.OnButtonPressed()" in press(rt, 302) and "EventTranslator.OnPadBack()" in press(rt, 303))
rt.key(40, True)
check("Key.isDown while held, Key.getCode and getController answer", rt.vm.globals.props["Key"].vm_call("isDown", [40.0], rt.vm) is True and rt.vm.globals.props["Key"].vm_call("getCode", [], rt.vm) == 40.0
      and rt.vm.globals.props["Key"].vm_call("getController", [], rt.vm) == 0.0)
rt.key(40, False)
check("Key.isDown after the release", rt.vm.globals.props["Key"].vm_call("isDown", [40.0], rt.vm) is False)
check("keys did not raise script errors", not rt.errors)
if "CarShopMenu" in ids and eng.attrib is not None:
    rt = run([push(6, "CarShopMenu")], 120)
    check("CarShopMenu: Down -> CarHighlighted, Accept -> CarSelected", "CmdFECarLot.CarHighlighted()" in press(rt, 40) and "CmdFECarLot.CarSelected()" in press(rt, 302))

# mouse: Flash button events on a real clip, _xmouse, hitTest
rt = run([push(6, "PostRace")], 120)
box_clip = next(c for c in clips(rt.root.vm_get("OVERLAY_1")) if c.name == "mContainerAnimation")
gb = box_clip.global_box()
cx, cy = (gb[0] + gb[2]) / 2, (gb[1] + gb[3]) / 2
seen = []
for name in ("onRollOver", "onRollOut", "onPress", "onRelease", "onReleaseOutside"):
    box_clip.vm_set(name, VM.Native((lambda n: lambda vm, this, a: seen.append(n))(name)))
check("a clip with a handler is found under the pointer, one outside is not", rt.root.pick(cx, cy) is box_clip and rt.root.pick(gb[2] + 50, gb[3] + 50) is not box_clip)
rt.mouse("move", cx, cy)
rt.mouse("move", cx + 1, cy)
check("moving onto the clip calls onRollOver once", seen == ["onRollOver"])
rt.mouse("down", cx, cy)
rt.mouse("up", cx, cy)
check("press and release on it call onPress, onRelease", seen[1:] == ["onPress", "onRelease"])
rt.mouse("down", cx, cy)
rt.mouse("up", gb[2] + 80, gb[3] + 80)
check("releasing elsewhere calls onReleaseOutside", seen[-2:] == ["onPress", "onReleaseOutside"])
rt.mouse("move", gb[2] + 80, gb[3] + 80)
check("moving off the clip calls onRollOut", seen[-1] == "onRollOut")
rt.mouse_x, rt.mouse_y = cx, cy
lx, ly = box_clip.to_local(cx, cy)
check("_xmouse / _ymouse are the pointer in the clip's own coordinates", abs(box_clip.vm_get("_xmouse") - lx) < 1e-6 and abs(box_clip.vm_get("_ymouse") - ly) < 1e-6)
check("hitTest(x, y) and hitTest(clip)", box_clip.hit_test([cx, cy, True]) is True and box_clip.hit_test([gb[2] + 500.0, gb[3] + 500.0, True]) is False and box_clip.hit_test([box_clip]) is True)

# swapDepths: two children of a clip trade depths
parent = next((c for c in clips(rt.root.vm_get("OVERLAY_1")) if len(c.children) >= 2), None)
check("PostRace has a clip with two or more children", parent is not None)
if parent is not None:
    c1, c2 = list(parent.children.values())[:2]
    d1, d2 = c1.depth, c2.depth
    c1.vm_call("swapDepths", [c2], rt.vm)
    check("swapDepths(clip) trades the depths", (c1.depth, c2.depth) == (d2, d1) and parent.children[d2] is c1 and parent.children[d1] is c2)
    c1.vm_call("swapDepths", [float(d1 + 500)], rt.vm)
    check("swapDepths(number) moves the clip to a free depth", c1.depth == d1 + 500 and parent.children[d1 + 500] is c1 and d2 not in parent.children)

rt = run([push(6, "PostRace"), msg(40, "POP")], 200)
check("POP removes the overlay, the destruct interval ends and OnScreenDestructed switches processing on",
      str(rt.root.vm_get("OVERLAY_1")) == "undefined" and not rt.vm.intervals and eng.processing and [e[0] for e in eng.events][-2:] == ["OnScreenDestructing", "OnScreenDestructed"])

if "CarSelectMenu" in ids:
    rt = run([push(6, "CarSelectMenu")], 80)
    check("imports: a class of a library movie (NFS_StaticItemScroller) exists and the clip got its methods", "NFS_StaticItemScroller" in rt.vm.globals.props
          and not [k for k in rt.vm.externals if "createStaticItemScroller" in k or "MoveList" in k])

# every screen of the exe's table: loads (the files that exist), no exception, no script error
bad, missing = [], []
for sid, name in sorted(eng.screens.items()):
    if not os.path.isfile(os.path.join(ui, name.lower() + ".big")):
        missing.append(name)
        continue
    try:
        rt = run([msg(6, "PUSH", screenId=sid)], 80)
    except Exception as e:
        bad.append((name, repr(e)[:60]))
        continue
    if not (rt.load_log and rt.load_log[-1][2]) or rt.errors:
        bad.append((name, "load" if not (rt.load_log and rt.load_log[-1][2]) else "errors %d" % len(rt.errors)))
check("all %d screens with a movie file load and run 80 ticks without exception or script error (%s)" % (len(eng.screens) - len(missing), ", ".join(str(b) for b in bad[:4]) or "none bad"), not bad)

sys.exit(0 if ok else 1)
