#!/usr/bin/env python3
"""
apt_endian_swap.py - convert Undercover apt .BIG files between x360 (big-endian) and PC (little-endian).

  python apt_endian_swap.py OSCAR.BIG                  -> swapped/OSCAR.BIG   (direction found from the data)
  python apt_endian_swap.py UI_FOLDER -o OUT_FOLDER    -> every apt .BIG in the folder
  python apt_endian_swap.py OSCAR.BIG --to pc --set-version 1:7:4

What is swapped (only the numbers; the container, the strings and the PNG names stay as they are):
  - apt data (entry 0): every 32-bit word, except the strings and the bytecode. Bytecode fields are
    swapped one by one (16-bit lengths, 32-bit values), found by the same decoder the viewer uses.
  - const file (entry 1): every 32-bit word except the strings.
  - shape geometry entries: every 32-bit word.
The sizes do not change, so the container is rebuilt in place.

Checks (on by default): the viewer loads the original and the result and the two must give the same
movie: characters, frames, imports/exports and the disassembly of every action stream. The tool also
lists text-like bytes that it did not treat as a string, so a missed string does not stay hidden.

Needs the viewer modules (apt_scan, apt_actions, apt_movie) in the same folder.
"""

import argparse
import json
import os
import re
import struct
import sys

import apt_actions as AA
import apt_movie as M
import apt_scan as A


# ---------------------------------------------------------------- container

def directory(d):
    """[(name, offset, size)] of a BIGF file: count and header size big-endian, then offset, size, name."""
    count = struct.unpack_from(">I", d, 8)[0]
    p, out = 16, []
    for _ in range(count):
        off, size = struct.unpack_from(">II", d, p)
        p += 8
        e = d.index(b"\0", p)
        out.append((d[p:e].decode("latin-1"), off, size))
        p = e + 1
    return out


# ---------------------------------------------------------------- finding strings and bytecode

class Probe:
    """Runs the viewer's own readers on one movie and records what they read as a string, and every
    16/32-bit field they read in the bytecode (and which bytes belong to bytecode)."""

    def __init__(self, apt, const):
        self.apt, self.const = apt, const
        self.str_apt, self.str_const = [], []       # (start, end)
        self.fields = set()                         # (pos, size) in the apt data
        self.code = {}                              # cursor start -> furthest end

    def mark(self, buf, start, end):
        if buf is self.apt:
            self.str_apt.append((start, end))
        elif buf is self.const:
            self.str_const.append((start, end))


def probe_movie(name, entries, apt, const):
    pr = Probe(apt, const)
    o_cstr, o_init, o_take, o_str = A.cstr, AA.Cursor.__init__, AA.Cursor.take, AA.Cursor.string_at

    def cstr(b, o):
        r = o_cstr(b, o)
        if r is not None:
            pr.mark(b, o, o + len(r) + 1)
        return r

    def init(self, data, pos=0):
        o_init(self, data, pos)
        self._p0 = pos

    def take(self, n):
        pos = self.pos
        r = o_take(self, n)
        if self.data is apt:
            if n in (2, 4):
                pr.fields.add((pos, n))
            pr.code[self._p0] = max(pr.code.get(self._p0, 0), pos + n)
        return r

    def string_at(self, off):
        s = o_str(self, off)
        pr.mark(self.data, off, off + len(s) + 1)
        return s

    A.cstr, AA.Cursor.__init__, AA.Cursor.take, AA.Cursor.string_at = cstr, init, take, string_at
    try:
        mv = M.Movie(name, entries)
        A.parse_apt(apt, const)
        AA.read_consts(const)
        AA.collect_streams(mv)
    finally:
        A.cstr, AA.Cursor.__init__, AA.Cursor.take, AA.Cursor.string_at = o_cstr, o_init, o_take, o_str
    return pr


# ---------------------------------------------------------------- swapping

def swap_words(buf, skip, notes, label):
    """Reverse every 4-byte word that has no skipped byte. A word that is partly skipped stays as it is
    (a note is made if its other bytes are not zero)."""
    out = bytearray(buf)
    n = 0
    for i in range(0, len(buf) - 3, 4):
        s = skip[i:i + 4]
        if not any(s):
            out[i:i + 4] = buf[i:i + 4][::-1]
            n += 1
        elif not all(s) and any(b for b, k in zip(buf[i:i + 4], s) if not k):
            notes.append("%s: word at 0x%X is partly string/code and partly data" % (label, i))
    return out, n


def skip_map(length, ranges):
    m = bytearray(length)
    for a, b in ranges:
        for i in range(max(0, a), min(length, b)):
            m[i] = 1
    return m


def text_like(buf, skip, label, notes):
    tmp = bytearray(buf)
    for i, k in enumerate(skip):
        if k:
            tmp[i] = 0
    for m in list(re.finditer(rb"[A-Za-z0-9_. /-]{6,}", bytes(tmp)))[:10]:
        notes.append("%s: text-like bytes at 0x%X that were swapped as numbers: %r" % (
            label, m.start(), m.group().decode("latin-1")[:40]))


def convert(name, entries, to_le, version):
    """Return (new entries dict, report lines, notes)."""
    ents = dict(entries)
    apt, const = ents["0"], ents["1"]
    src = A.use_endian(apt, const)
    notes, report = [], []
    pr = probe_movie(name, entries, apt, const)

    # apt data
    code_ranges = [(s, e) for s, e in pr.code.items()]
    skip = skip_map(len(apt), [(0, 16)] + pr.str_apt + code_ranges)
    new_apt, n_words = swap_words(apt, skip, notes, "apt")
    for pos, size in pr.fields:
        new_apt[pos:pos + size] = apt[pos:pos + size][::-1]
    text_like(apt, skip, "apt", notes)
    report.append("apt data: %d words, %d bytecode fields, %d strings kept" % (n_words, len(pr.fields), len(pr.str_apt)))

    # const file
    cskip = skip_map(len(const), [(0, 20)] + pr.str_const)
    new_const, n_c = swap_words(const, cskip, notes, "const")
    text_like(const, cskip, "const", notes)
    report.append("const file: %d words, %d strings kept" % (n_c, len(pr.str_const)))

    # shape geometry entries
    new = {"0": bytes(new_apt), "1": bytes(new_const)}
    n_geo = 0
    for k, blob in ents.items():
        if k in ("0", "1"):
            continue
        if len(blob) >= 0x20 and len(blob) % 4 == 0:
            total, n = struct.unpack_from(src + "II", blob, 0)
            if total == len(blob) and 1 <= n <= 64:
                new[k] = bytes(swap_words(blob, bytearray(len(blob)), notes, "geometry " + k)[0])
                n_geo += 1
                continue
        new[k] = blob
    report.append("geometry entries: %d" % n_geo)

    if version:
        m = re.match(rb"Apt Data:(\d+:\d+:\d+)", new["0"])
        if not m or len(m.group(1)) != len(version):
            notes.append("version not changed (found %r)" % (m.group(1) if m else None))
        else:
            new["0"] = new["0"][:9] + version.encode() + new["0"][9 + len(version):]
            report.append("version string set to %s" % version)
    return new, report, notes


# ---------------------------------------------------------------- check

def fingerprint(name, entries):
    mv = M.Movie(name, entries)
    streams = AA.collect_streams(mv)
    fp = {
        "size": (mv.width, mv.height, mv.ms),
        "imports": mv.imports, "exports": mv.exports,
        "chars": json.dumps(mv.chars, sort_keys=True, default=str),
        "frames": json.dumps(mv.frames, sort_keys=True, default=str),
        "streams": [(s["where"], s["offset"], s["error"], "\n".join(AA.disassemble(s["insns"]))) for s in streams],
    }
    return fp, mv.endian


def compare(name, old_entries, new_entries):
    a, ea = fingerprint(name, old_entries)
    b, eb = fingerprint(name, new_entries)
    bad = [k for k in a if a[k] != b[k]]
    return bad, ea, eb


# ---------------------------------------------------------------- files

def process(path, out_path, to, version, check, force):
    d = open(path, "rb").read()
    if d[:4] not in (b"BIGF", b"BIG4"):
        return "skipped: not a BIGF file"
    dirs = directory(d)
    entries = [(n, d[o:o + s]) for n, o, s in dirs]
    names = dict(entries)
    if "0" not in names or "1" not in names or not names["0"].startswith(b"Apt Data"):
        return "skipped: no apt data"
    try:
        src = A.use_endian(names["0"], names["1"])
    except ValueError as e:
        return "skipped: %s" % e
    want = "<" if to == "pc" else ">" if to == "x360" else ("<" if src == ">" else ">")
    if src == want:
        return "skipped: already %s" % ("PC (little-endian)" if src == "<" else "x360 (big-endian)")
    new, report, notes = convert(os.path.basename(path), entries, want == "<", version)
    problems = []
    if check:
        bad, ea, eb = compare(os.path.basename(path), entries, list(new.items()))
        if eb != want:
            problems.append("the result is read as %s" % ("little-endian" if eb == "<" else "big-endian"))
        problems += ["differs after the swap: " + k for k in bad]
    out = bytearray(d)
    for n, o, s in dirs:
        assert len(new[n]) == s, "size changed for entry " + n
        out[o:o + s] = new[n]
    if problems and not force:
        return "NOT written: " + "; ".join(problems) + "".join("\n    note: " + x for x in notes)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(out)
    msg = "%s -> %s\n    %s" % ("x360" if src == ">" else "PC", "PC" if want == "<" else "x360", "\n    ".join(report))
    if check:
        msg += "\n    check: same movie after the swap (%d characters, %d streams compared)" % (
            len(M.Movie(os.path.basename(path), entries).chars), len(fingerprint(os.path.basename(path), entries)[0]["streams"]))
    for x in (problems + notes)[:20]:
        msg += "\n    note: " + x
    return msg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="an apt .BIG file or a folder")
    ap.add_argument("-o", "--out", help="output file (for one file) or folder; default: swapped/")
    ap.add_argument("--to", choices=["pc", "x360"], help="target; default: the other one")
    ap.add_argument("--set-version", metavar="1:7:4",
                    help="also write this version into the apt header (same length), for a PC game that checks it")
    ap.add_argument("--no-check", action="store_true", help="skip the comparison with the original")
    ap.add_argument("--force", action="store_true", help="write the file even if the check fails")
    a = ap.parse_args()

    jobs = []
    if os.path.isdir(a.input):
        out_dir = a.out or "swapped"
        for root, _dirs, files in os.walk(a.input):
            for fn in sorted(files):
                if fn.lower().endswith(".big"):
                    rel = os.path.relpath(os.path.join(root, fn), a.input)
                    jobs.append((os.path.join(root, fn), os.path.join(out_dir, rel)))
    else:
        out = a.out or os.path.join("swapped", os.path.basename(a.input))
        if os.path.isdir(out) or not out.lower().endswith(".big"):
            out = os.path.join(out, os.path.basename(a.input))
        jobs.append((a.input, out))

    for src, dst in jobs:
        try:
            print("%s: %s" % (os.path.basename(src), process(src, dst, a.to, a.set_version, not a.no_check, a.force)))
        except Exception as e:                          # keep going in a folder run
            print("%s: failed: %s: %s" % (os.path.basename(src), type(e).__name__, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
