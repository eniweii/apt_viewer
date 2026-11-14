"""
Built-in script classes of the apt VM: Array, Object (registerClass), MovieClip (base for `extends`), Number, String and its methods, Key, Mouse,
TextFormat, Color, escape/unescape.

Which names exist follows the strings nfs.exe's own runtime carries: Array push pop shift unshift splice slice concat reverse join sort sortOn;
String substr substring charAt charCodeAt indexOf lastIndexOf split toUpperCase toLowerCase fromCharCode; Function call apply; Key isDown getCode
addListener removeListener; Object.registerClass. The behaviour is ActionScript 2; what is assumed (not read from the exe) is marked ASSUMED.

Array.prototype is shared by every VM in the process (arrays made by apt_vm.from_json have no VM to ask); no movie changes it.
The class of a registered symbol (Object.registerClass) is only recorded in vm.registered: applying it to clips is not done here.
"""
import functools
import urllib.parse

import apt_vm as VM
from apt_vm import Arr, Func, Native, Obj, UNDEF, to_number, to_str

BIG = 1e18
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _pos(v, n, default=0):
    """An index argument of slice/splice/substr: undefined -> default, negative counts from the end, clamped to 0..n."""
    if v is UNDEF:
        return default
    x = to_number(v)
    if x != x:
        return 0
    i = int(max(-BIG, min(BIG, x)))
    if i < 0:
        i = max(n + i, 0)
    return min(i, n)


def _int(v, default=0):
    x = to_number(v) if v is not UNDEF else float("nan")
    return default if x != x else int(max(-BIG, min(BIG, x)))


def _text(v):
    return "" if v is UNDEF or v is None else to_str(v)


# ---- Array
def _arr(fn):
    """An Array method: does nothing on a this that is not an array."""
    @functools.wraps(fn)
    def run(vm, this, a):
        return fn(vm, this, a) if isinstance(this, Arr) else UNDEF
    return run


@_arr
def a_push(vm, this, a):
    this.items.extend(a)
    return float(len(this.items))


@_arr
def a_pop(vm, this, a):
    return this.items.pop() if this.items else UNDEF


@_arr
def a_shift(vm, this, a):
    return this.items.pop(0) if this.items else UNDEF


@_arr
def a_unshift(vm, this, a):
    this.items[0:0] = a
    return float(len(this.items))


@_arr
def a_splice(vm, this, a):
    n = len(this.items)
    if not a:
        return Arr()
    start = _pos(a[0], n)
    cnt = n - start if len(a) < 2 else max(0, min(_int(a[1]), n - start))
    removed = this.items[start:start + cnt]
    this.items[start:start + cnt] = a[2:]
    return Arr(removed)


@_arr
def a_slice(vm, this, a):
    n = len(this.items)
    return Arr(this.items[_pos(a[0] if a else UNDEF, n):_pos(a[1] if len(a) > 1 else UNDEF, n, n)])


@_arr
def a_concat(vm, this, a):
    out = list(this.items)
    for v in a:
        if isinstance(v, Arr):
            out.extend(v.items)
        else:
            out.append(v)
    return Arr(out)


@_arr
def a_reverse(vm, this, a):
    this.items.reverse()
    return UNDEF


@_arr
def a_join(vm, this, a):
    sep = to_str(a[0]) if a and a[0] is not UNDEF else ","
    return sep.join(_text(x) for x in this.items)      # ASSUMED: undefined/null elements join as ""


def _sort_key(flags):
    if flags & 16:
        return lambda v: to_number(v)
    if flags & 1:
        return lambda v: to_str(v).lower()
    return lambda v: to_str(v)


def _sort_flags(vm, flags):
    if flags & 12:
        vm.note("Array.sort: UNIQUESORT/RETURNINDEXEDARRAY are not supported")
    return flags


@_arr
def a_sort(vm, this, a):
    cmp_fn = a[0] if a and isinstance(a[0], (Func, Native)) else None
    opt = a[1] if cmp_fn is not None and len(a) > 1 else (a[0] if a and cmp_fn is None else UNDEF)
    flags = _sort_flags(vm, _int(opt))
    if cmp_fn is not None:
        def cmp(x, y):
            r = to_number(vm.call(cmp_fn, None, [x, y]))
            return -1 if r < 0 else 1 if r > 0 else 0
        this.items.sort(key=functools.cmp_to_key(cmp), reverse=bool(flags & 2))
    else:
        this.items.sort(key=_sort_key(flags), reverse=bool(flags & 2))
    return UNDEF


@_arr
def a_sorton(vm, this, a):
    fields = [to_str(x) for x in a[0].items] if a and isinstance(a[0], Arr) else [to_str(a[0])] if a else []
    flags = _sort_flags(vm, _int(a[1]) if len(a) > 1 and not isinstance(a[1], Arr) else 0)
    one = _sort_key(flags)
    this.items.sort(key=lambda it: tuple(one(vm.get_member(it, f)) for f in fields), reverse=bool(flags & 2))
    return UNDEF


@_arr
def a_tostring(vm, this, a):
    return a_join(vm, this, [])


def array_new(vm, args):
    """new Array() / new Array(n) (n empty slots) / new Array(a, b, ...)."""
    if len(args) == 1 and isinstance(args[0], float):
        n = args[0]
        return Arr([UNDEF] * int(n)) if n == n and 0 <= n < 1e7 and n == int(n) else Arr()
    return Arr(args)


# ---- String (a script string is a Python str)
def s_substr(vm, s, a):
    n = len(s)
    start = _pos(a[0] if a else UNDEF, n)
    length = n - start if len(a) < 2 or a[1] is UNDEF else _int(a[1])
    return s[start:start + length] if length > 0 else ""      # ASSUMED: a length of 0 or less gives ""


def s_substring(vm, s, a):
    n = len(s)
    lo, hi = (max(0, min(_int(a[i]), n)) if len(a) > i and a[i] is not UNDEF else d for i, d in ((0, 0), (1, n)))
    return s[min(lo, hi):max(lo, hi)]


def s_slice(vm, s, a):
    n = len(s)
    return s[_pos(a[0] if a else UNDEF, n):_pos(a[1] if len(a) > 1 else UNDEF, n, n)]


def s_charat(vm, s, a):
    i = _int(a[0]) if a else 0
    return s[i] if 0 <= i < len(s) else ""


def s_charcodeat(vm, s, a):
    i = _int(a[0]) if a else 0
    return float(ord(s[i])) if 0 <= i < len(s) else float("nan")


def s_indexof(vm, s, a):
    return float(s.find(_text(a[0]) if a else "undefined", max(0, _int(a[1])) if len(a) > 1 else 0))


def s_lastindexof(vm, s, a):
    sub = _text(a[0]) if a else "undefined"
    end = len(s) if len(a) < 2 or a[1] is UNDEF else max(0, _int(a[1], len(s)))
    return float(s.rfind(sub, 0, min(len(s), end + len(sub))))


def s_split(vm, s, a):
    if not a or a[0] is UNDEF:
        return Arr([s])
    d = to_str(a[0])
    parts = list(s) if d == "" else s.split(d)
    if len(a) > 1 and a[1] is not UNDEF:
        parts = parts[:max(0, _int(a[1]))]
    return Arr(parts)


STRING_METHODS = {
    "substr": s_substr, "substring": s_substring, "slice": s_slice, "charAt": s_charat, "charCodeAt": s_charcodeat,
    "indexOf": s_indexof, "lastIndexOf": s_lastindexof, "split": s_split,
    "toUpperCase": lambda vm, s, a: s.upper(), "toLowerCase": lambda vm, s, a: s.lower(),
    "toString": lambda vm, s, a: s, "valueOf": lambda vm, s, a: s,
}


STRING_METHODS_CI = {k.lower(): v for k, v in STRING_METHODS.items()}


def num_tostring(x, a):
    radix = _int(a[0], 10) if a else 10
    if radix == 10 or not 2 <= radix <= 36 or x != x or x != int(x) or abs(x) >= 1e15:
        return to_str(x)
    n, out = abs(int(x)), ""
    while True:
        n, r = divmod(n, radix)
        out = DIGITS[r] + out
        if n == 0:
            return ("-" if x < 0 else "") + out


def primitive_call(vm, obj, name, args):
    """(True, result) when name is a method of a string or number; (False, None) otherwise."""
    if isinstance(obj, str) and name.lower() in STRING_METHODS_CI:
        return True, STRING_METHODS_CI[name.lower()](vm, obj, args)
    if isinstance(obj, float) and name.lower() == "tostring":
        return True, num_tostring(obj, args)
    return False, None


# ---- Key, Mouse (nothing is pressed headless; listeners are kept for the viewer to call)
KEY_CODES = {"BACKSPACE": 8, "TAB": 9, "ENTER": 13, "SHIFT": 16, "CONTROL": 17, "CAPSLOCK": 20, "ESCAPE": 27, "SPACE": 32, "PGUP": 33, "PGDN": 34,
             "END": 35, "HOME": 36, "LEFT": 37, "UP": 38, "RIGHT": 39, "DOWN": 40, "INSERT": 45, "DELETEKEY": 46}


def _listener_object(name, listeners_attr):
    def add(vm, this, a):
        lst = getattr(vm, listeners_attr)
        if a and a[0] not in lst:
            lst.append(a[0])
        return True

    def remove(vm, this, a):
        lst = getattr(vm, listeners_attr)
        if a and a[0] in lst:
            lst.remove(a[0])
            return True
        return False
    o = Obj(name=name)
    o.props["addListener"], o.props["removeListener"] = Native(add, name + ".addListener"), Native(remove, name + ".removeListener")
    return o


def make_key(vm):
    o = _listener_object("Key", "key_listeners")
    for k, v in KEY_CODES.items():
        o.props[k] = float(v)
    def is_down(vm, this, a):
        code = to_number(a[0]) if a else float("nan")
        return code == code and int(code) in vm.keys_down
    o.props["isDown"] = Native(is_down, "Key.isDown")
    o.props["getController"] = Native(lambda vm, this, a: float(vm.key_controller), "Key.getController")
    o.props["isToggled"] = Native(lambda vm, this, a: False, "Key.isToggled")
    o.props["getCode"] = Native(lambda vm, this, a: float(vm.key_code), "Key.getCode")
    o.props["getAscii"] = Native(lambda vm, this, a: float(vm.key_ascii), "Key.getAscii")
    return o


def make_mouse(vm):
    o = _listener_object("Mouse", "mouse_listeners")

    def show_hide(visible):
        def fn(vm, this, a):
            was, vm.mouse_visible = vm.mouse_visible, visible
            return was
        return fn
    o.props["hide"], o.props["show"] = Native(show_hide(False), "Mouse.hide"), Native(show_hide(True), "Mouse.show")
    return o


# ---- Object, MovieClip, TextFormat, Color, Number, String
def _class(name, construct, props=None):
    proto = Obj(name=name)
    n = Native(lambda vm, this, a: construct(vm, a), name, construct, dict(props or {}))
    n.props["prototype"] = proto
    proto.props["constructor"] = n
    return n


def _register_class(vm, this, a):
    name = to_str(a[0]) if a else ""
    cls = a[1] if len(a) > 1 else UNDEF
    if isinstance(cls, (Func, Native)):
        vm.registered[name] = cls
        return True
    return vm.registered.pop(name, None) is not None        # null/undefined removes the registration


def _color_class():
    def construct(vm, a):
        o = Obj(proto=color.props["prototype"], name="Color")
        o.props["_target"] = a[0] if a else UNDEF
        return o
    color = _class("Color", construct)
    p = color.props["prototype"].props
    p["setRGB"] = Native(lambda vm, this, a: this.props.__setitem__("_rgb", to_number(a[0]) if a else 0.0) if isinstance(this, Obj) else UNDEF, "Color.setRGB")
    p["getRGB"] = Native(lambda vm, this, a: this.props.get("_rgb", 0.0) if isinstance(this, Obj) else UNDEF, "Color.getRGB")
    p["setTransform"] = Native(lambda vm, this, a: this.props.__setitem__("_transform", a[0] if a else UNDEF) if isinstance(this, Obj) else UNDEF, "Color.setTransform")
    p["getTransform"] = Native(lambda vm, this, a: this.props.get("_transform", Obj(name="Object")) if isinstance(this, Obj) else UNDEF, "Color.getTransform")
    return color


def _escape(vm, this, a):
    return urllib.parse.quote(_text(a[0]) if a else "", safe="@-_.*+/").replace("~", "%7E")   # ASSUMED: UTF-8 bytes like Flash 6+


def _unescape(vm, this, a):
    return urllib.parse.unquote(_text(a[0]) if a else "")


# ---- setInterval / clearInterval: the runtime calls fire_intervals(vm) once per tick
def _set_interval(vm, this, a):
    """setInterval(function, ms, args...) or setInterval(object, "method", ms, args...); the id is a number for clearInterval."""
    if a and isinstance(a[0], (Func, Native)):
        target, name, ms, extra = None, None, a[1] if len(a) > 1 else 0.0, list(a[2:])
        fn = a[0]
    elif len(a) >= 3:
        target, name, ms, extra, fn = a[0], to_str(a[1]), a[2], list(a[3:]), None
    else:
        return UNDEF
    ms = to_number(ms)
    ms = ms if ms == ms and ms > 1.0 else 1.0
    vm.interval_count += 1
    vm.intervals[vm.interval_count] = {"fn": fn, "target": target, "name": name, "ms": ms, "args": extra, "next": getattr(vm, "now_ms", 0.0) + ms}
    return float(vm.interval_count)


def _clear_interval(vm, this, a):
    n = to_number(a[0]) if a else float("nan")
    if n == n:
        vm.intervals.pop(int(n), None)
    return UNDEF


def fire_intervals(vm):
    """Call every interval that is due (once per tick, then it waits its period again). The function gets no `this` (ASSUMED: the game's callbacks keep their object in a closure variable)."""
    now = getattr(vm, "now_ms", 0.0)
    for iid, it in list(vm.intervals.items()):
        if iid in vm.intervals and now >= it["next"]:
            it["next"] = now + it["ms"]
            fn = it["fn"] if it["fn"] is not None else (it["target"].vm_get(it["name"]) if hasattr(it["target"], "vm_get") else UNDEF)
            if isinstance(fn, (Func, Native)):
                vm.call(fn, it["target"] if it["target"] is not None else UNDEF, it["args"])


def install(vm):
    """Put the built-in classes in vm.globals; the state the natives keep (registered classes, listeners) lives on the VM."""
    vm.registered = VM.CIProps()                     # linkage name -> class (Object.registerClass); apt_runtime binds clips of that symbol to it
    vm.key_listeners, vm.mouse_listeners = [], []
    vm.key_code = vm.key_ascii = vm.key_controller = 0
    vm.keys_down = set()
    vm.mouse_visible = True
    vm.intervals, vm.interval_count = {}, 0
    g = vm.globals.props

    array = _class("Array", lambda vm, a: array_new(vm, a),
                   {"CASEINSENSITIVE": 1.0, "DESCENDING": 2.0, "UNIQUESORT": 4.0, "RETURNINDEXEDARRAY": 8.0, "NUMERIC": 16.0})
    array.props["prototype"] = VM.ARRAY_PROTO
    VM.ARRAY_PROTO.props["constructor"] = array
    g["Array"] = array

    obj = _class("Object", lambda vm, a: Obj(name="Object"), {"registerClass": Native(_register_class, "Object.registerClass")})
    g["Object"] = obj
    g["MovieClip"] = _class("MovieClip", lambda vm, a: Obj(name="MovieClip"))
    g["TextFormat"] = _class("TextFormat", lambda vm, a: Obj(name="TextFormat"))
    g["Color"] = _color_class()
    g["Number"] = _class("Number", lambda vm, a: to_number(a[0]) if a else 0.0)
    g["String"] = _class("String", lambda vm, a: to_str(a[0]) if a else "", {
        "fromCharCode": Native(lambda vm, this, a: "".join(chr(max(0, min(0x10FFFF, _int(c)))) for c in a), "String.fromCharCode")})
    g["Key"], g["Mouse"] = make_key(vm), make_mouse(vm)
    g["setInterval"], g["clearInterval"] = Native(_set_interval, "setInterval"), Native(_clear_interval, "clearInterval")
    g["escape"], g["unescape"] = Native(_escape, "escape"), Native(_unescape, "unescape")


for _name, _fn in (("push", a_push), ("pop", a_pop), ("shift", a_shift), ("unshift", a_unshift), ("splice", a_splice), ("slice", a_slice),
                   ("concat", a_concat), ("reverse", a_reverse), ("join", a_join), ("sort", a_sort), ("sortOn", a_sorton), ("toString", a_tostring)):
    VM.ARRAY_PROTO.props[_name] = Native(_fn, "Array." + _name)
