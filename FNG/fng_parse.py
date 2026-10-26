#!/usr/bin/env python3
"""
fng_parse.py

Reads an unpacked FNG package (the bytes from fng_bun.unpack_fng / load_package) into plain
Python objects. Port of the reader side of FEngLib (github.com/NFSTools/FEngLib, FEngLib/).
Checked on one real Carbon package so far (BLACK_BACKGROUND.fng).

Layout, all little-endian:
  'FEn\\xE7' u32, size u32, then chunks (id u32, size u32, payload):
    PkHd  package header: 0x20000, 0, resource name count, version, name length, file name length,
          name, file name, aligned to 4
    TypS  (type id, size) u32 pairs
    Res*  container: RsNm (zero ended names, keyed by their offset in the chunk)
                     RsRq u32 count, then per request 0x18 bytes: id, name offset, type, 3 u32 not used
    Obj*  container: Butn (u32 count), then one Obj\\xEA container per object:
            ObjD  tags (u16 id, u16 size, payload): Ot type, Oh hash, OP guid/hash/flags/resource index,
                  If image flags, SA object data, PA parent guid, On name, St/Sb/Sj/Sl/Sw/SH/SL text
                  fields, M1-M3 / Ma-Mc multi image textures
            Scrp  tags: Sh header, Sn name, Sc chain, FI track, To track offset, Kd keys, EV events
            MsgR  tags: Mi message hash, MC count, Ri command, Ru int param, Rs string param, Rt target
    PkgR  package message responses (same tags as MsgR)
    Targ, TLs\\xF4, EDat, MsgN (message names: tag 'MN' name, tag 'MC' category)
Colors are stored as blue, green, red, alpha (int32 each). They are kept here as (r, g, b, a).
Tolerant: a chunk or tag that is not known is skipped and written to Package.warnings.
"""
import struct

MAGIC = b"FEn\xe7"

CHUNK_PKHD = 0x64486B50
CHUNK_TYPS = 0x53707954
CHUNK_RES = 0xCC736552
CHUNK_RSNM = 0x6D4E7352
CHUNK_RSRQ = 0x71527352
CHUNK_OBJ = 0xCC6A624F
CHUNK_BUTN = 0x6E747542
CHUNK_FOBJ = 0xEA624F46
CHUNK_OBJD = 0x446A624F
CHUNK_SCRP = 0x70726353
CHUNK_MSGR = 0x5267734D
CHUNK_PKGR = 0x52676B50
CHUNK_TARG = 0x67726154
CHUNK_TLS = 0xF4734C54
CHUNK_MSGN = 0x4E67734D
CHUNK_EDAT = 0x74614445

OBJECT_TYPES = {
    0x0: "None", 0x1: "Image", 0x2: "String", 0x3: "Model", 0x4: "List", 0x5: "Group", 0x6: "CodeList",
    0x7: "Movie", 0x8: "Effect", 0x9: "ColoredImage", 0xA: "AnimImage", 0xB: "SimpleImage", 0xC: "MultiImage",
}
RESOURCE_TYPES = {0: "None", 1: "Image", 2: "Font", 3: "Model", 4: "Movie", 5: "Effect", 6: "AnimatedImage", 7: "MultiImage"}

OBJECT_FLAGS = [
    (0x1, "Invisible"), (0x8, "PCOnly"), (0x40, "ConsoleOnly"), (0x20000, "MouseObject"),
    (0x40000, "SaveStaticTracks"), (0x80000, "DontNavigate"), (0x100000, "UsesLibraryObject"),
    (0x200000, "CodeSuppliedResource"), (0x400000, "DirtyCode"), (0x800000, "DirtyColor"),
    (0x1000000, "DirtyTransform"), (0x2000000, "Dirty"), (0x4000000, "IgnoreButton"), (0x8000000, "ObjectLocked"),
    (0x10000000, "IsButton"), (0x20000000, "PerspectiveProjection"), (0x40000000, "AffectAllScripts"),
    (0x80000000, "HideInEdit"),
]

TRACK_PARAM_TYPES = {0: "Boolean", 1: "Integer", 2: "Float", 3: "Vector2", 4: "Vector3", 5: "Quaternion", 6: "Color"}
TRACK_PARAM_SIZES = {3: 8, 4: 12, 5: 16, 6: 16}
TRACK_INTERP = {0: "None", 1: "Linear", 2: "Spline", 3: "MoveToLinear", 4: "MoveToSpline"}

# (name, kind, offset in 4 byte units) in the order the object data is stored
_COMMON = [("Color", "color", 0), ("Pivot", "v3", 4), ("Position", "v3", 7), ("Rotation", "quat", 10), ("Size", "v3", 14)]
_IMAGE = _COMMON + [("UpperLeft", "v2", 17), ("LowerRight", "v2", 19)]
_MULTI = _IMAGE + [("TopLeft1", "v2", 21), ("TopLeft2", "v2", 23), ("TopLeft3", "v2", 25),
                   ("BottomRight1", "v2", 27), ("BottomRight2", "v2", 29), ("BottomRight3", "v2", 31),
                   ("PivotRotation", "v3", 33)]
_COLORED = _IMAGE + [("TopLeft", "color", 21), ("TopRight", "color", 25), ("BottomRight", "color", 29),
                     ("BottomLeft", "color", 33)]
DATA_LAYOUT = {1: _IMAGE, 2: _COMMON, 5: _COMMON, 7: _COMMON, 9: _COLORED, 0xB: _COMMON, 0xC: _MULTI}
_KIND_SIZE = {"color": 16, "v2": 8, "v3": 12, "quat": 16}

# track index (when the script has no track offsets) for the first 5 tracks, then image tracks
_INDEX_NAMES = ["Color", "Pivot", "Position", "Rotation", "Size", "UpperLeft", "LowerRight"]
_COLORED_INDEX_NAMES = ["TopLeft", "TopRight", "BottomRight", "BottomLeft"]

COMMAND_NAMES = {
    0x0: "SetScript", 0x1: "PostMessageToFEng", 0x2: "PostMessageToGame", 0x3: "PostMessageToSound",
    0x100: "SetActiveButton", 0x101: "SetInputProcessing", 0x102: "RecordCurrentButton",
    0x103: "RecallRecordedButton", 0x104: "DontNavigate", 0x105: "PassControlToChildCurrent",
    0x106: "PassControlToChildGlobal", 0x107: "PassControlToParentCurrent", 0x108: "PassControlToParentGlobal",
    0x200: "SwitchToPackage", 0x201: "PushPackageGlobal", 0x202: "PushPackageCurrent", 0x203: "PopPackage",
    0x204: "PushPackageNone", 0x2C0: "RecordPackageMarker", 0x2C1: "SwitchToPackageMarker",
    0x2C2: "ClearPackageMarkers", 0x300: "IfScriptEquals", 0x301: "IfScriptNotEquals", 0x500: "Else", 0x501: "EndIf",
}


class FngParseError(Exception):
    pass


HASH_NAMES = {}                         # hash -> name from a name list, see load_hash_dictionary()


def load_hash_dictionary(path):
    """
    Reads a text file with one name per line (like common/hashes_main.txt of the stream repo) and fills HASH_NAMES
    with the FEng hash of each name (upper case, times 33, seed 0xFFFFFFFF). Lines starting with '#' are skipped.
    Returns the number of names. Names are stored in upper case, the first spelling of a hash wins.
    """
    names = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            name = line.strip()
            if not name or name[0] == "#":
                continue
            h = 0xFFFFFFFF
            for ch in name.upper():
                h = (h * 33 + ord(ch)) & 0xFFFFFFFF
            names.setdefault(h, name.upper())
    HASH_NAMES.update({h: n for h, n in names.items() if h not in HASH_NAMES})
    return len(names)


def bin_hash(text):
    h = 0xFFFFFFFF
    for ch in text:
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    return h


def flag_names(flags):
    return [name for bit, name in OBJECT_FLAGS if flags & bit]


class Resource:
    def __init__(self, index, id_, type_, name):
        self.index = index
        self.id = id_
        self.type = type_
        self.name = name

    @property
    def type_name(self):
        return RESOURCE_TYPES.get(self.type, f"0x{self.type:X}")


class Track:
    def __init__(self, name, param_type, interp_type, interp_action, length, offset):
        self.name = name
        self.param_type = param_type
        self.interp_type = interp_type
        self.interp_action = interp_action
        self.length = length
        self.offset = offset            # track offset tag, None when the script has none
        self.param_size = TRACK_PARAM_SIZES.get(param_type, 4)
        self.keys = []                  # (time, value). time -1 is the base key

    @property
    def param_name(self):
        return TRACK_PARAM_TYPES.get(self.param_type, f"0x{self.param_type:X}")

    @property
    def interp_name(self):
        return TRACK_INTERP.get(self.interp_type, f"0x{self.interp_type:X}")


class Script:
    def __init__(self):
        self.id = 0
        self.name = ""
        self.length = 0
        self.flags = 0
        self.track_count = 0
        self.chained_id = None
        self.tracks = []
        self.events = []                # (event id, target, time)


class Response:
    """One message response: when message 'id' arrives, run the commands."""
    def __init__(self, id_):
        self.id = id_
        self.commands = []              # dicts: id, target, int, str


class FngObject:
    def __init__(self, type_):
        self.type = type_
        self.guid = 0
        self.name = ""
        self.name_hash = 0
        self.flags = 0
        self.resource_index = -1
        self.resource = None
        self.parent_guid = None
        self.parent = None
        self.children = []
        self.image_flags = None
        self.props = {}                 # Color, Pivot, Position ... see DATA_LAYOUT
        self.textures = [None, None, None]
        self.texture_flags = [None, None, None]
        self.text = {}                  # value, label, label_hash, buffer_length, formatting, leading, max_width
        self.scripts = []
        self.responses = []
        self.raw_data = None            # object data of a type this parser has no layout for

    @property
    def type_name(self):
        return OBJECT_TYPES.get(self.type, f"0x{self.type:X}")


class Package:
    def __init__(self):
        self.name = ""
        self.filename = ""
        self.version = 0
        self.resource_name_count = 0
        self.type_sizes = []
        self.resources = []
        self.objects = []
        self.button_map_count = None
        self.responses = []
        self.message_definitions = []   # (name, category)
        self.targets = []               # (message hash, [target guids])
        self.chunk_tree = []            # (depth, name, offset, size) for the info view
        self.warnings = []
        self.hash_names = {}

    def find_object(self, guid):
        for o in self.objects:
            if o.guid == guid:
                return o
        return None

    def name_of(self, h):
        """Name for a hash, from names found in this package, else hex."""
        return self.hash_names.get(h) or HASH_NAMES.get(h) or f"0x{h:08X}"


class _Data:
    """Bounds checked little-endian reads at absolute offsets."""

    def __init__(self, data):
        self.data = data

    def u16(self, pos):
        return struct.unpack_from("<H", self.data, pos)[0]

    def u32(self, pos):
        return struct.unpack_from("<I", self.data, pos)[0]

    def i32(self, pos):
        return struct.unpack_from("<i", self.data, pos)[0]

    def floats(self, pos, n):
        return struct.unpack_from(f"<{n}f", self.data, pos)

    def text(self, pos, length):
        return self.data[pos:pos + length].split(b"\0")[0].decode("latin-1")

    def value(self, kind, pos):
        """(value, size) of a color / vector / quaternion at pos."""
        if kind == "color":
            b, g, r, a = struct.unpack_from("<4i", self.data, pos)
            return (r, g, b, a), 16
        n = {"v2": 2, "v3": 3, "quat": 4}[kind]
        return self.floats(pos, n), n * 4


_CHUNK_NAMES = {
    CHUNK_PKHD: "PkHd", CHUNK_TYPS: "TypS", CHUNK_RES: "Res", CHUNK_RSNM: "RsNm", CHUNK_RSRQ: "RsRq",
    CHUNK_OBJ: "Obj", CHUNK_BUTN: "Butn", CHUNK_FOBJ: "ObjContainer", CHUNK_OBJD: "ObjD", CHUNK_SCRP: "Scrp",
    CHUNK_MSGR: "MsgR", CHUNK_PKGR: "PkgR", CHUNK_TARG: "Targ", CHUNK_TLS: "TLs", CHUNK_MSGN: "MsgN",
    CHUNK_EDAT: "EDat",
}


def parse_package(data):
    """Returns a Package. Raises FngParseError if data is not an FEng package at all."""
    data = bytes(data)
    if data[:4] != MAGIC or len(data) < 8:
        raise FngParseError("data does not start with the FEng package marker")
    pkg = Package()
    rd = _Data(data)
    end = min(len(data), 8 + rd.u32(4))
    if end < len(data):
        pkg.warnings.append(f"{len(data) - end} bytes after the package end")
    try:
        _read_main(pkg, rd, 8, end, 0, {})
        _link(pkg)
    except (struct.error, IndexError, KeyError) as e:
        raise FngParseError(f"package is cut or damaged: {e!r}")
    return pkg


def _chunks(pkg, rd, pos, end):
    """Yields (id, payload start, payload end) and checks that every chunk fits."""
    while pos + 8 <= end:
        cid = rd.u32(pos)
        size = rd.u32(pos + 4)
        start = pos + 8
        if start + size > end:
            raise FngParseError(f"chunk {_CHUNK_NAMES.get(cid, hex(cid))} at 0x{pos:X} is longer than its container")
        yield cid, start, start + size
        pos = start + size
    if pos != end:
        pkg.warnings.append(f"{end - pos} stray bytes at 0x{pos:X}")


def _read_main(pkg, rd, pos, end, depth, ctx):
    for cid, start, cend in _chunks(pkg, rd, pos, end):
        pkg.chunk_tree.append((depth, _CHUNK_NAMES.get(cid, f"0x{cid:08X}"), start - 8, cend - start))
        if cid == CHUNK_PKHD:
            _read_header(pkg, rd, start, cend)
        elif cid == CHUNK_TYPS:
            pkg.type_sizes = [(rd.u32(p), rd.u32(p + 4)) for p in range(start, cend - 7, 8)]
        elif cid == CHUNK_RES:
            _read_main(pkg, rd, start, cend, depth + 1, ctx)
        elif cid == CHUNK_RSNM:
            names = {}
            p = start
            while p < cend:
                text = rd.text(p, cend - p)
                if text:
                    names[p - start] = text
                p += len(text) + 1
            ctx["names"] = names
        elif cid == CHUNK_RSRQ:
            _read_requests(pkg, rd, start, cend, ctx.get("names", {}))
        elif cid == CHUNK_OBJ:
            _read_objects(pkg, rd, start, cend, depth + 1)
        elif cid == CHUNK_PKGR:
            pkg.responses += _read_responses(pkg, rd, start, cend)
        elif cid == CHUNK_MSGN:
            _read_message_names(pkg, rd, start, cend)
        elif cid == CHUNK_TARG:
            _read_targets(pkg, rd, start, cend)
        elif cid in (CHUNK_TLS, CHUNK_EDAT):
            pass
        else:
            pkg.warnings.append(f"unknown chunk 0x{cid:08X} at 0x{start - 8:X} skipped")


def _read_header(pkg, rd, start, end):
    if rd.u32(start) != 0x20000 or rd.u32(start + 4) != 0:
        pkg.warnings.append("package header constants are not 0x20000, 0")
    pkg.resource_name_count = rd.i32(start + 8)
    pkg.version = rd.u32(start + 12)
    name_len = rd.i32(start + 16)
    file_len = rd.i32(start + 20)
    if name_len < 1 or file_len < 1:
        raise FngParseError("bad name length in the package header")
    pkg.name = rd.text(start + 24, name_len)
    pkg.filename = rd.text(start + 24 + name_len, file_len)


def _read_requests(pkg, rd, start, end, names):
    count = rd.u32(start)
    if (end - start - 4) % 0x18 != 0 or (end - start - 4) // 0x18 != count:
        pkg.warnings.append("resource request chunk size does not match its count")
    for i in range(min(count, (end - start - 4) // 0x18)):
        p = start + 4 + i * 0x18
        id_, name_off, type_ = rd.u32(p), rd.u32(p + 4), rd.u32(p + 8)
        if name_off not in names:
            pkg.warnings.append(f"resource {i} has no name at offset {name_off}")
        pkg.resources.append(Resource(len(pkg.resources), id_, type_, names.get(name_off, "")))


def _read_objects(pkg, rd, start, end, depth):
    for cid, s, e in _chunks(pkg, rd, start, end):
        pkg.chunk_tree.append((depth, _CHUNK_NAMES.get(cid, f"0x{cid:08X}"), s - 8, e - s))
        if cid == CHUNK_BUTN:
            pkg.button_map_count = rd.u32(s)
        elif cid == CHUNK_FOBJ:
            obj = _read_object(pkg, rd, s, e, depth + 1)
            if obj is not None:
                pkg.objects.append(obj)
        else:
            pkg.warnings.append(f"unknown object chunk 0x{cid:08X} at 0x{s - 8:X} skipped")


def _read_object(pkg, rd, start, end, depth):
    obj = None
    for cid, s, e in _chunks(pkg, rd, start, end):
        pkg.chunk_tree.append((depth, _CHUNK_NAMES.get(cid, f"0x{cid:08X}"), s - 8, e - s))
        if cid == CHUNK_OBJD:
            obj = _read_object_data(pkg, rd, s, e, obj)
        elif obj is None:
            pkg.warnings.append(f"chunk 0x{cid:08X} at 0x{s - 8:X} comes before the object data, skipped")
        elif cid == CHUNK_SCRP:
            obj.scripts.append(_read_script(pkg, rd, s, e, obj))
        elif cid == CHUNK_MSGR:
            obj.responses += _read_responses(pkg, rd, s, e)
        else:
            pkg.warnings.append(f"unknown chunk 0x{cid:08X} at 0x{s - 8:X} skipped")
    return obj


def _tags(pkg, rd, start, end, where):
    """Yields (tag id as 2 characters, payload start, payload size). Stops at a tag that does not fit."""
    pos = start
    while pos + 4 <= end:
        tag = rd.data[pos:pos + 2].decode("latin-1")
        size = rd.u16(pos + 2)
        if pos + 4 + size > end:
            pkg.warnings.append(f"{where}: tag '{tag}' at 0x{pos:X} is longer than its chunk")
            return
        yield tag, pos + 4, size
        pos += 4 + size
    if pos != end:
        pkg.warnings.append(f"{where}: {end - pos} stray bytes at 0x{pos:X}")


def _read_object_data(pkg, rd, start, end, obj):
    for tag, p, size in _tags(pkg, rd, start, end, "ObjD"):
        if tag == "Ot":
            type_ = rd.u32(p)
            if type_ not in DATA_LAYOUT:
                pkg.warnings.append(f"object type {OBJECT_TYPES.get(type_, hex(type_))} has no known data layout")
            obj = FngObject(type_)
        elif obj is None:
            pkg.warnings.append(f"ObjD: tag '{tag}' before the object type, skipped")
        elif tag == "Oh":
            obj.name_hash = rd.u32(p)
        elif tag == "OP":
            obj.guid = rd.u32(p)
            if not obj.name_hash:
                obj.name_hash = rd.u32(p + 4)
            obj.flags = rd.u32(p + 8)
            obj.resource_index = rd.i32(p + 12)
        elif tag == "If":
            obj.image_flags = rd.u32(p)
        elif tag == "SA":
            _read_props(pkg, rd, p, size, obj)
        elif tag == "PA":
            obj.parent_guid = rd.u32(p)
        elif tag == "On":
            obj.name = rd.text(p, size)
            obj.name_hash = bin_hash(obj.name.upper())
        elif tag == "Sb":
            obj.text["buffer_length"] = rd.u32(p)
        elif tag == "St":
            obj.text["value"] = rd.data[p:p + size].decode("utf-16-le", "replace").strip("\0")
        elif tag == "Sj":
            obj.text["formatting"] = rd.u32(p)
        elif tag == "Sl":
            obj.text["leading"] = rd.i32(p)
        elif tag == "Sw":
            obj.text["max_width"] = rd.u32(p)
        elif tag == "SH":
            obj.text["label_hash"] = rd.u32(p)
        elif tag == "SL":
            obj.text["label"] = rd.text(p, size)
            obj.text["label_hash"] = bin_hash(obj.text["label"].upper())
        elif tag in ("M1", "M2", "M3"):
            obj.textures[int(tag[1]) - 1] = rd.u32(p)
        elif tag in ("Ma", "Mb", "Mc"):
            obj.texture_flags[ord(tag[1]) - 0x61] = rd.u32(p)
        else:
            pkg.warnings.append(f"ObjD: unknown tag '{tag}' at 0x{p - 4:X} skipped")
    return obj


def _read_props(pkg, rd, p, size, obj):
    layout = DATA_LAYOUT.get(obj.type)
    if layout is None:
        obj.raw_data = rd.data[p:p + size]
        return
    need = sum(_KIND_SIZE[k] for _n, k, _o in layout)
    if need != size:
        pkg.warnings.append(f"object 0x{obj.guid:X}: data is {size} bytes, {obj.type_name} layout needs {need}")
        obj.raw_data = rd.data[p:p + size]
        return
    for name, kind, _off in layout:
        value, n = rd.value(kind, p)
        obj.props[name] = value
        p += n


def _track_name(script_obj, track, index):
    """Name of a track, from its offset tag or, without offsets, from its position in the script."""
    layout = DATA_LAYOUT.get(script_obj.type, _COMMON)
    if track.offset is not None:
        for name, _kind, off in layout:
            if off == track.offset:
                return name
        return f"offset_{track.offset}"
    if index < len(_INDEX_NAMES):
        return _INDEX_NAMES[index]
    if script_obj.type == 9 and index - 7 < len(_COLORED_INDEX_NAMES):
        return _COLORED_INDEX_NAMES[index - 7]
    return f"track_{index}"


def _read_script(pkg, rd, start, end, obj):
    script = Script()
    pending = []                        # tracks in file order
    for tag, p, size in _tags(pkg, rd, start, end, "Scrp"):
        if tag == "Sh":
            script.id, script.length, script.flags, script.track_count = struct.unpack_from("<4I", rd.data, p)
        elif tag == "Sn":
            script.name = rd.text(p, size)
            script.id = bin_hash(script.name.upper())
        elif tag == "Sc":
            script.chained_id = rd.u32(p)
        elif tag == "FI":
            ptype, psize, itype, iaction = struct.unpack_from("<4B", rd.data, p)
            bits = rd.u32(p + 4)
            track = Track("", ptype, itype, iaction, bits & 0xFFFFFF, None)
            track.param_size = TRACK_PARAM_SIZES.get(ptype, psize)
            pending.append(track)
        elif tag == "To":
            if pending:
                pending[-1].offset = rd.u32(p)
            else:
                pkg.warnings.append("Scrp: track offset before any track")
        elif tag == "Kd":
            if pending:
                _read_keys(pkg, rd, p, size, pending[-1])
            else:
                pkg.warnings.append("Scrp: keys before any track")
        elif tag == "EV":
            for q in range(p, p + size - 11, 12):
                script.events.append((rd.u32(q), rd.u32(q + 4), rd.u32(q + 8)))
        else:
            pkg.warnings.append(f"Scrp: unknown tag '{tag}' at 0x{p - 4:X} skipped")

    if any(t.offset is not None for t in pending) and any(t.offset is None for t in pending):
        pkg.warnings.append(f"script {script.name or hex(script.id)}: some tracks have offsets and some do not")
    for i, track in enumerate(pending):
        track.name = _track_name(obj, track, i)
    script.tracks = pending
    return script


_PARAM_KIND = {3: "v2", 4: "v3", 5: "quat", 6: "color"}


def _read_keys(pkg, rd, p, size, track):
    kind = _PARAM_KIND.get(track.param_type)
    key_size = track.param_size + 4
    if size % key_size:
        pkg.warnings.append(f"track {track.param_name}: key data {size} is not a multiple of {key_size}")
        return
    for q in range(p, p + size, key_size):
        time = rd.i32(q)
        if kind:
            value = rd.value(kind, q + 4)[0]
        else:
            value = rd.data[q + 4:q + key_size]
        track.keys.append((time, value))


def _read_responses(pkg, rd, start, end):
    out = []
    for tag, p, size in _tags(pkg, rd, start, end, "MsgR"):
        if tag == "Mi":
            out.append(Response(rd.u32(p)))
        elif tag == "MC":
            pass
        elif not out:
            pkg.warnings.append(f"MsgR: tag '{tag}' before the first message")
        elif tag == "Ri":
            out[-1].commands.append({"id": rd.u32(p), "target": None, "int": None, "str": None})
        elif not out[-1].commands:
            pkg.warnings.append(f"MsgR: tag '{tag}' before the first command")
        elif tag == "Ru":
            out[-1].commands[-1]["int"] = rd.u32(p)
        elif tag == "Rs":
            out[-1].commands[-1]["str"] = rd.text(p, size)
        elif tag == "Rt":
            out[-1].commands[-1]["target"] = rd.u32(p)
        else:
            pkg.warnings.append(f"MsgR: unknown tag '{tag}' skipped")
    return out


def _read_message_names(pkg, rd, start, end):
    pos = start
    while pos + 4 <= end:
        tag = rd.u16(pos)
        size = rd.u16(pos + 2)
        text = rd.text(pos + 4, size)
        if tag == 0x4E4D:               # 'MN' as stored: bytes 4D 4E
            pkg.message_definitions.append([text, ""])
        elif tag == 0x434D and pkg.message_definitions:
            pkg.message_definitions[-1][1] = text
        pos += 4 + size


def _read_targets(pkg, rd, start, end):
    for tag, p, size in _tags(pkg, rd, start, end, "Targ"):
        if tag == "Mt" and size >= 4 and size % 4 == 0:
            pkg.targets.append((rd.u32(p), [rd.u32(q) for q in range(p + 4, p + size, 4)]))


def _link(pkg):
    """Resolves resource indexes and parent guids, and collects the names behind hashes."""
    for obj in pkg.objects:
        if obj.resource_index >= 0:
            if obj.resource_index < len(pkg.resources):
                obj.resource = pkg.resources[obj.resource_index]
            else:
                pkg.warnings.append(f"object 0x{obj.guid:X} uses resource {obj.resource_index}, there are {len(pkg.resources)}")
    by_guid = {o.guid: o for o in pkg.objects}
    for obj in pkg.objects:
        if obj.parent_guid is not None:
            parent = by_guid.get(obj.parent_guid)
            if parent is None:
                pkg.warnings.append(f"object 0x{obj.guid:X} has parent 0x{obj.parent_guid:X}, which is not in the package")
            else:
                obj.parent = parent
                parent.children.append(obj)

    names = pkg.hash_names
    for obj in pkg.objects:
        if obj.name:
            names[bin_hash(obj.name.upper())] = obj.name
        if obj.text.get("label"):
            names[bin_hash(obj.text["label"].upper())] = obj.text["label"]
        for s in obj.scripts:
            if s.name:
                names[s.id] = s.name
    for n, _cat in pkg.message_definitions:
        names[bin_hash(n.upper())] = n
    for text in ("INIT", "IN", "OUT", "FOCUS", "UNFOCUS", "DEFAULT", "LOOP", "ON", "OFF"):
        names.setdefault(bin_hash(text), text)


def describe_command(pkg, cmd):
    """Text for one response command, like 'SetScript(INIT)'."""
    name = COMMAND_NAMES.get(cmd["id"], f"Command_0x{cmd['id']:X}")
    arg = ""
    if cmd["str"] is not None:
        arg = f'"{cmd["str"]}"'
    elif cmd["int"]:
        arg = pkg.name_of(cmd["int"]) if cmd["id"] in (0x0, 0x1, 0x2, 0x3, 0x300, 0x301) else f"0x{cmd['int']:X}"
    target = f" -> 0x{cmd['target']:X}" if cmd["target"] else ""
    return f"{name}({arg}){target}"
