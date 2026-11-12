"""
Game data the apt natives hand to the UI: hashes, the English string tables and the attrib (vault) dump. No dependencies.

  bstring_hash(text)     the BIN key hash and the string table key: h = 0xFFFFFFFF; h = h * 33 + byte
  vlt_hash(text)         the VLT key hash (VaultLib Vlt32Hasher): Bob Jenkins lookup2, seed 0xABCDEF00
  load_names(path)       {vlt hash: name} from a list of names (hashes_main.txt)
  Language(folder)       English_Global/Frontend/InGame.bin: Language.text(hash)
  Attrib(dump, names)    the UndercoverVLT_Dumper JSON: Attrib.collections("fe_hints_tips") ...
"""
import json
import os
import struct

M32 = 0xFFFFFFFF


def bstring_hash(text):
    h = M32
    for b in text.encode("latin1", "replace"):
        h = (h * 33 + b) & M32
    return h


def _mix(a, b, c):
    a = (a - b - c) & M32; a ^= c >> 13; b = (b - c - a) & M32; b ^= (a << 8) & M32; c = (c - a - b) & M32; c ^= b >> 13
    a = (a - b - c) & M32; a ^= c >> 12; b = (b - c - a) & M32; b ^= (a << 16) & M32; c = (c - a - b) & M32; c ^= b >> 5
    a = (a - b - c) & M32; a ^= c >> 3; b = (b - c - a) & M32; b ^= (a << 10) & M32; c = (c - a - b) & M32; c ^= b >> 15
    return a, b, c


def vlt_hash(text, init=0xABCDEF00):
    k = bytes(ord(c) & 0xFF for c in text)
    a = b = 0x9E3779B9
    c, i = init, 0
    while len(k) - i >= 12:
        a = (a + struct.unpack("<I", k[i:i + 4])[0]) & M32
        b = (b + struct.unpack("<I", k[i + 4:i + 8])[0]) & M32
        c = (c + struct.unpack("<I", k[i + 8:i + 12])[0]) & M32
        a, b, c = _mix(a, b, c)
        i += 12
    c = (c + len(k)) & M32
    t = k[i:]
    for n, shift, reg in ((11, 24, "c"), (10, 16, "c"), (9, 8, "c"), (8, 24, "b"), (7, 16, "b"), (6, 8, "b"), (5, 0, "b"),
                          (4, 24, "a"), (3, 16, "a"), (2, 8, "a"), (1, 0, "a")):
        if len(t) >= n:
            v = t[n - 1] << shift
            if reg == "a":
                a = (a + v) & M32
            elif reg == "b":
                b = (b + v) & M32
            else:
                c = (c + v) & M32
    return _mix(a, b, c)[2]


def load_names(path):
    """{vlt hash: name}; a line is a name, # starts a comment, lower case spellings are tried too."""
    out = {}
    for line in open(path, encoding="latin1"):
        line = line.rstrip("\n")
        if line and not line.startswith("#"):
            for t in {line, line.lower()}:
                out.setdefault(vlt_hash(t), line)
    return out


def read_language(path):
    """One English_*.bin: {hash: bytes}. Header u32 magic 0x39000, size, count, table offset, string base; the table is count entries of
    (hash, offset), sorted by hash, the first is (0, 0); the strings start 8 bytes after the end of the table (checked: every offset then
    lands on the start of a NUL terminated string, 1,856 of 1,856 in Frontend and 4,959 of 4,959 in Global)."""
    raw = open(path, "rb").read()
    magic, _size, count, table, base = struct.unpack("<5I", raw[:20])
    if magic != 0x39000:
        raise ValueError("%s: not a language table" % path)
    out = {}
    for i in range(count):
        h, o = struct.unpack("<II", raw[table + 8 * i:table + 8 * i + 8])
        if h == 0 and o == 0:
            continue
        s = base + 8 + o
        out[h] = raw[s:raw.index(b"\0", s)]
    return out


class Language:
    def __init__(self, folder, prefix="English_", files=("Global", "Frontend", "InGame")):
        self.table = {}
        for f in files:
            for name in os.listdir(folder):
                if name.lower() == (prefix + f + ".bin").lower():
                    for h, s in read_language(os.path.join(folder, name)).items():
                        self.table.setdefault(h, s)

    def text(self, key):
        """The string for a hash (an int, or a name that is hashed with bstring_hash); None when it is not in the tables."""
        h = key if isinstance(key, int) else bstring_hash(key)
        s = self.table.get(h)
        return None if s is None else s.decode("latin1")


class Attrib:
    """The attrib dump: classes with their collections; names are resolved with a hash name list when one is given."""

    def __init__(self, dump_path, names_path=None):
        self.dump = json.load(open(dump_path))
        self.names = load_names(names_path) if names_path else {}
        self._classes = {self.name(c["name"]): c for c in self.dump["classes"]}

    def name(self, key):
        if isinstance(key, str) and key.startswith("0x"):
            return self.names.get(int(key, 16), key)
        return key

    def collections(self, class_name):
        """{collection name: {field name: value}} of a class."""
        c = self._classes.get(class_name)
        if c is None:
            return {}
        return {self.name(col["name"]): {self.name(k): v for k, v in col["values"].items()} for col in c["collections"]}
