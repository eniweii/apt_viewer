#!/usr/bin/env python3
"""Checks for the built-in classes of the VM (apt_builtins): Array, String/number methods, Function.call/apply, extends MovieClip, registerClass, Key, Mouse.
Run: python test_apt_builtins.py"""
import sys

import apt_actions as AA
import apt_vm as VM

ok = True


def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


vm = VM.VM(1)
A = lambda *x: VM.Arr(list(x))
call = lambda obj, name, *args: vm._call_method(obj, name, list(args))
items = lambda arr: arr.items


def run(insns):
    return vm._run(insns, VM.Frame(vm.globals, vm.globals, None, None))


# arrays: literal, index, length
a = A(1.0, 2.0, 3.0)
check("Arr get: index, length, past the end", a.vm_get("1") == 2.0 and a.vm_get("length") == 3.0 and a.vm_get("9") is VM.UNDEF)
a.vm_set("5", 9.0)
check("Arr set past the end fills with undefined", len(a.items) == 6 and a.items[4] is VM.UNDEF and a.items[5] == 9.0)
a.vm_set("length", 2.0)
check("Arr length set truncates", items(a) == [1.0, 2.0])
check("from_json list is an Arr", isinstance(VM.from_json([1, [2]], "x"), VM.Arr) and VM.from_json([1, [2]], "x").items[1].items == [2.0])
check("Arr to string joins", VM.to_str(A(1.0, VM.UNDEF, "a")) == "1,,a")

# constructors through the opcodes (new Array(), new Array(3), new Array(1, 2), new Object())
new = lambda name, *args: run([AA.Insn(i, 0x96, [("int", 0)]) for i in ()] + [AA.Insn(0, 0x96, [("float", float(x))]) if isinstance(x, float) else AA.Insn(0, 0x96, [("str", x)]) for x in reversed(args)]
                              + [AA.Insn(1, 0x96, [("int", len(args))]), AA.Insn(2, 0x96, [("str", name)]), AA.Insn(3, 0x40, [])])
check("new Array()", isinstance(new("Array"), VM.Arr) and new("Array").items == [])
check("new Array(3) has 3 empty slots", len(new("Array", 3.0).items) == 3)
check("new Array(1, 2) has those elements", new("Array", 1.0, 2.0).items == [1.0, 2.0])
check("new Object() is a plain object", type(new("Object")) is VM.Obj)
check("new TextFormat() / new Color(x)", new("TextFormat").name == "TextFormat" and new("Color", "t").vm_call("getRGB", [], vm) == 0.0)

# methods
a = A(1.0, 2.0)
check("push returns the length", call(a, "push", 3.0, 4.0) == 4.0 and items(a) == [1.0, 2.0, 3.0, 4.0])
check("pop / shift", call(a, "pop") == 4.0 and call(a, "shift") == 1.0 and items(a) == [2.0, 3.0])
check("pop on empty is undefined", call(A(), "pop") is VM.UNDEF)
check("unshift keeps the order and returns the length", call(a, "unshift", 7.0, 8.0) == 4.0 and items(a) == [7.0, 8.0, 2.0, 3.0])
b = A(0.0, 1.0, 2.0, 3.0, 4.0)
r = call(b, "splice", 1.0, 2.0, "x")
check("splice removes and inserts", items(r) == [1.0, 2.0] and items(b) == [0.0, "x", 3.0, 4.0])
check("splice without a count removes to the end", items(call(b, "splice", -2.0)) == [3.0, 4.0] and items(b) == [0.0, "x"])
check("slice (negatives count from the end)", items(call(A(1.0, 2.0, 3.0, 4.0), "slice", 1.0, -1.0)) == [2.0, 3.0] and items(call(A(1.0, 2.0), "slice")) == [1.0, 2.0])
c = call(A(1.0), "concat", A(2.0, 3.0), 4.0, A(5.0))
check("concat flattens arrays one level", items(c) == [1.0, 2.0, 3.0, 4.0, 5.0])
d = A(1.0, 2.0, 3.0)
call(d, "reverse")
check("reverse in place", items(d) == [3.0, 2.0, 1.0])
check("join", call(A(1.0, 2.0), "join", "-") == "1-2" and call(A(1.0, 2.0), "join") == "1,2")
s = A(10.0, 9.0, 1.0)
call(s, "sort")
check("sort default is by text", items(s) == [1.0, 10.0, 9.0])
call(s, "sort", 16.0)
check("sort NUMERIC", items(s) == [1.0, 9.0, 10.0])
call(s, "sort", 18.0)
check("sort NUMERIC | DESCENDING", items(s) == [10.0, 9.0, 1.0])
call(s, "sort", VM.Native(lambda vm, this, args: args[0] - args[1]))
check("sort with a compare function", items(s) == [1.0, 9.0, 10.0])
objs = A(*[VM.Obj() for _ in range(3)])
for o, v in zip(objs.items, (3.0, 1.0, 2.0)):
    o.props["n"] = v
call(objs, "sortOn", "n", 16.0)
check("sortOn a field", [o.props["n"] for o in objs.items] == [1.0, 2.0, 3.0])
check("a method on a non-array does nothing", VM.Native(lambda *x: 0).vm_call("push", [], vm) is VM.UNDEF)

# for-in keys of an array and the ObjectToArray pattern (length + index)
check("enumerate keys of an array", vm._enum_keys(A("a", "b")) == ["0", "1"])
src = VM.from_json([{"id": 1}, {"id": 2}], "list")
out = VM.Arr()
i = 0.0
while i < src.vm_get("length"):
    call(out, "push", src.vm_get(VM.to_str(i)))
    i += 1
check("ObjectToArray-style copy", len(out.items) == 2 and out.items[1].props["id"] == 2.0)

# callMethod through the opcodes: stack is args..., argc, object, name
vm.globals.props["arr"] = A(1.0)
res = run([AA.Insn(0, 0x96, [("float", 7.0)]), AA.Insn(1, 0x96, [("int", 1)]), AA.Insn(2, 0x96, [("str", "arr")]), AA.Insn(3, 0x1C, []),
           AA.Insn(4, 0x96, [("str", "concat")]), AA.Insn(5, 0x52, [])])
check("arr.concat(7) through callMethod", isinstance(res, VM.Arr) and items(res) == [1.0, 7.0])

# strings
check("substr", call("hello", "substr", 1.0, 3.0) == "ell" and call("hello", "substr", -3.0) == "llo" and call("hello", "substr", 2.0) == "llo")
check("substring clamps and swaps", call("hello", "substring", 3.0, 1.0) == "el" and call("hello", "substring", -2.0, 2.0) == "he" and call("hello", "substring", 2.0) == "llo")
check("slice with negatives", call("hello", "slice", 1.0, -1.0) == "ell" and call("hello", "slice", -2.0) == "lo")
check("charAt / charCodeAt out of range", call("ab", "charAt", 1.0) == "b" and call("ab", "charAt", 5.0) == "" and call("ab", "charCodeAt", 0.0) == 97.0 and call("ab", "charCodeAt", 9.0) != call("ab", "charCodeAt", 9.0))
check("indexOf / lastIndexOf", call("abcabc", "indexOf", "c") == 2.0 and call("abcabc", "indexOf", "c", 3.0) == 5.0 and call("abcabc", "indexOf", "z") == -1.0
      and call("abcabc", "lastIndexOf", "b") == 4.0 and call("abcabc", "lastIndexOf", "b", 3.0) == 1.0)
check("split", items(call("a b c", "split", " ")) == ["a", "b", "c"] and items(call("abc", "split", "")) == ["a", "b", "c"]
      and items(call("a,b,c", "split", ",", 2.0)) == ["a", "b"] and items(call("abc", "split")) == ["abc"])
check("toUpperCase / toLowerCase", call("aB", "toUpperCase") == "AB" and call("aB", "toLowerCase") == "ab")
check("String.fromCharCode", vm.globals.props["String"].vm_call("fromCharCode", [72.0, 105.0], vm) == "Hi")
check("number toString with a radix", call(255.0, "toString", 16.0) == "ff" and call(5.0, "toString") == "5" and call(-5.0, "toString", 2.0) == "-101")
check("escape / unescape", vm.globals.props["escape"].fn(vm, None, ["a b~é"]) == "a%20b%7E%C3%A9" and vm.globals.props["unescape"].fn(vm, None, ["a%20b"]) == "a b")

# Function.call / apply
seen = []
f = VM.Native(lambda vm, this, args: seen.append((this, list(args))) or 5.0)
who = VM.Obj(name="who")
check("f.call(this, a)", call(f, "call", who, 1.0) == 5.0 and seen[-1] == (who, [1.0]))
check("f.apply(this, [a, b])", call(f, "apply", who, A(1.0, 2.0)) == 5.0 and seen[-1] == (who, [1.0, 2.0]))
fn = VM.Func([], [], 0, 0, None, None, 0, "fn")
check("a script function answers call", call(fn, "call", who) is VM.UNDEF and "function.call" not in " ".join(vm.externals))

# extends a built-in class, registerClass, Key and Mouse
sub = VM.Func([], [], 0, 0, None, None, 1, "Sub")
vm.globals.props["Sub"] = sub
run([AA.Insn(0, 0x96, [("str", "Sub")]), AA.Insn(1, 0x1C, []), AA.Insn(2, 0x96, [("str", "MovieClip")]), AA.Insn(3, 0x1C, []), AA.Insn(4, 0x69, [])])
mc = vm.globals.props["MovieClip"]
check("extends MovieClip links the built-in prototype", sub.props["prototype"].proto is mc.props["prototype"] and sub.props["prototype"].props["constructor"] is mc
      and not any(k.startswith("extends") for k in vm.externals))
inst = VM.Obj(proto=sub.props["prototype"])
check("super of a class that extends MovieClip", isinstance(vm._super_for(sub, inst), VM.SuperProxy))
obj_cls = vm.globals.props["Object"]
check("Object.registerClass records the class", call(obj_cls, "registerClass", "Sym", sub) is True and vm.registered["Sym"] is sub)
check("registerClass with null removes it", call(obj_cls, "registerClass", "Sym", None) is True and "Sym" not in vm.registered)
key = vm.globals.props["Key"]
listener = VM.Obj()
call(key, "addListener", listener)
call(key, "addListener", listener)
check("Key.addListener keeps one entry; removeListener drops it", vm.key_listeners == [listener] and call(key, "removeListener", listener) is True and vm.key_listeners == [])
check("Key codes, getCode, isDown", key.vm_get("LEFT") == 37.0 and call(key, "getCode") == 0.0 and call(key, "isDown", 37.0) is False)
mouse = vm.globals.props["Mouse"]
check("Mouse.hide / show return the previous state", call(mouse, "hide") is True and call(mouse, "show") is False and vm.mouse_visible is True)
check("no built-in is listed as an external", not [k for k in vm.externals if k in ("Array", "Object", "MovieClip", "Key", "Mouse", "TextFormat", "Color", "String", "Number")])

# case-insensitive names (the game's script engine ignores case: InitGui finds InitGUI)
o = VM.Obj()
o.vm_set("InitGUI", 1.0)
check("object members ignore case, the first spelling is kept", o.vm_get("initgui") == 1.0 and list(o.props) == ["InitGUI"])
o.vm_set("INITGUI", 2.0)
check("assigning in another case changes the same member", o.vm_get("InitGUI") == 2.0 and len(o.props) == 1)
check("method names ignore case", call(A(1.0, 2.0), "PUSH", 3.0) == 3.0 and call("hello", "TOUPPERCASE") == "HELLO" and A(1.0).vm_get("LENGTH") == 1.0)
check("variables ignore case", (vm.globals.props.__setitem__("MyVar", 5.0), vm.get_var(VM.Frame(vm.globals, vm.globals, None), "myvar"))[1] == 5.0)

# dotted variable paths (eval("_root.a.b"))
vm.globals.props["pathroot"] = VM.Obj()
vm.globals.props["pathroot"].props["child"] = VM.Obj()
vm.globals.props["pathroot"].props["child"].props["v"] = 7.0
check("eval of a dotted path", vm.get_var(VM.Frame(vm.globals, vm.globals, None), "pathRoot.child.V") == 7.0 and vm.get_var(VM.Frame(vm.globals, vm.globals, None), "pathRoot.none.v") is VM.UNDEF)

# Math on NaN / out of range answers like the player (no exception)
M = vm.globals.props["Math"]
nan = float("nan")
check("Math.round/floor of NaN and infinity", M.vm_call("round", [nan], vm) != M.vm_call("round", [nan], vm) and M.vm_call("floor", [float("inf")], vm) == float("inf"))
check("Math.sqrt of a negative is NaN", M.vm_call("sqrt", [-1.0], vm) != M.vm_call("sqrt", [-1.0], vm))

# an operation on an empty stack stops the stream with Unsupported, not an exception
try:
    vm.run([AA.Insn(0, 0x3B, [])], VM.Frame(vm.globals, vm.globals, None, None))
    underflow = False
except VM.Unsupported:
    underflow = True
check("stack underflow is Unsupported", underflow)

# setInterval / clearInterval
import apt_builtins as AB
hits = []
fn = VM.Native(lambda vm, this, args: hits.append(list(args)))
vm.now_ms = 0.0
iid = vm.globals.props["setInterval"].fn(vm, None, [fn, 100.0, "x"])
AB.fire_intervals(vm)
check("an interval waits its period", hits == [])
vm.now_ms = 100.0
AB.fire_intervals(vm)
check("an interval fires when due, with its arguments", hits == [["x"]])
AB.fire_intervals(vm)
vm.now_ms = 150.0
AB.fire_intervals(vm)
check("an interval waits its period again", len(hits) == 1)
vm.globals.props["clearInterval"].fn(vm, None, [iid])
vm.now_ms = 1000.0
AB.fire_intervals(vm)
check("clearInterval stops it", len(hits) == 1 and not vm.intervals)
target = VM.Obj()
target.props["tick"] = VM.Native(lambda vm, this, args: hits.append(["method"]))
vm.globals.props["setInterval"].fn(vm, None, [target, "TICK", 10.0])
vm.now_ms = 1100.0
AB.fire_intervals(vm)
check("setInterval(object, method, ms)", hits[-1] == ["method"])

# Key.isDown / getController follow what the runtime sets for a key event
K = vm.globals.props["Key"]
vm.keys_down.add(40)
vm.key_controller = 2
check("Key.isDown(code) is true while the code is held, false otherwise or for a non-number", K.vm_call("isDown", [40.0], vm) is True and K.vm_call("isDown", [38.0], vm) is False and K.vm_call("isDown", [], vm) is False)
check("Key.getController answers the controller of the event", K.vm_call("getController", [], vm) == 2.0)

sys.exit(0 if ok else 1)
