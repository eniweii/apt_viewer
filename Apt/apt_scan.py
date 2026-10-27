#!/usr/bin/env python3
"""
apt_scan.py - NFS Undercover APT scanner (parser + reference resolver, no viewer).

Reads the UI .big files (BIGF/BIG4), parses the apt + const entries, and
resolves what each movie refers to:
  - bitmap characters  -> PNG name entry in the .big -> texture name in the
                          TPKs found in the bun/bin files
  - shape characters   -> geometry entry in the .big (present or not)
  - font characters    -> font name, matched against texture names
  - imports            -> other .big files in the UI folder (by movie name)
Everything is written to a JSON report and a short console summary.

Usage:
  python apt_scan.py UI_DIR --res FRONTEND_DIR --res GLOBAL_DIR [--out apt_report.json]   (reports go to outputs/apt)
  python apt_scan.py customizecar.big --res .          (single file)

Options:
  --res DIR      Folder (searched recursively) with .bun/.bin files. Repeat it.
  --out FILE     JSON report (default apt_report.json)
  --csv FILE     Missing-texture list as CSV
  --decomp M:F   Hook "module:function" -> bytes = function(bytes) for JDLZ files
                 (.lzc). Default: fng_jdlz:decompress, found in common/.
  --verbose      Print every character
"""
import argparse
import csv
import importlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
import outpaths

# ---------------------------------------------------------------- helpers

# Byte order of the apt/const data of the movie being read: '<' PC, '>' x360 (the container, the
# resource packs and the fonts are not affected). use_endian() sets it for each movie.
ENDIAN = '<'


def use_endian(apt, const):
    """Set ENDIAN from the data. The 'main' offset in the const file (at 20) must point inside the
    apt data and be 4-aligned; only the right byte order gives that."""
    global ENDIAN
    for e in ('<', '>'):
        main = struct.unpack_from(e + 'I', const, 20)[0]
        if 0 < main < len(apt) and main % 4 == 0:
            ENDIAN = e
            return e
    raise ValueError('apt data: byte order not recognised')


def au32(b, o):
    return struct.unpack_from(ENDIAN + 'I', b, o)[0]


def af32(b, o):
    return struct.unpack_from(ENDIAN + 'f', b, o)[0]


def u32(b, o):
    return struct.unpack_from('<I', b, o)[0]


def f32(b, o):
    return struct.unpack_from('<f', b, o)[0]


def cstr(b, o):
    if o <= 0 or o >= len(b):
        return None
    e = b.find(b'\0', o)
    if e < 0:
        return None
    return b[o:e].decode('latin1')


def bin_hash(name):
    """Black Box string hash (checked: PERFORMBACK -> 0x02D13D0B)."""
    h = 0xFFFFFFFF
    for c in name.encode('latin1'):
        h = (c + h * 33) & 0xFFFFFFFF
    return h

# ---------------------------------------------------------------- BIG

def read_big(path):
    """Return list of (name, bytes). Header: magic, size(LE), count(BE), header size(BE).
    Entry: offset(BE), size(BE), NUL-terminated name."""
    d = open(path, 'rb').read()
    if d[:4] not in (b'BIGF', b'BIG4'):
        raise ValueError('not a BIG file: %r' % d[:4])
    count = struct.unpack_from('>I', d, 8)[0]
    p = 16
    out = []
    for _ in range(count):
        off, size = struct.unpack_from('>II', d, p)
        p += 8
        e = d.index(b'\0', p)
        out.append((d[p:e].decode('latin1'), d[off:off + size]))
        p = e + 1
    return d[:4].decode(), out

# ---------------------------------------------------------------- APT

TYPES = {1: 'Shape', 2: 'Text', 3: 'Font', 4: 'Button', 5: 'Sprite', 6: 'Sound',
         7: 'Bitmap', 8: 'Morph', 9: 'Animation', 10: 'StaticText', 11: 'None', 12: 'Video'}


def parse_apt(apt, const):
    """Parse the main movie. Offsets observed on Undercover files ("Apt Data:1:7:4")."""
    if not apt.startswith(b'Apt Data') or not const.startswith(b'Apt constant file'):
        raise ValueError('apt/const magic not found')
    use_endian(apt, const)
    main = au32(const, 20)
    n_const, a_const = au32(const, 24), au32(const, 28)
    strings = []
    for i in range(n_const):
        t, v = au32(const, a_const + 8 * i), au32(const, a_const + 8 * i + 4)
        if t == 1:
            s = cstr(const, v)
            if s is not None:
                strings.append(s)
    m = main
    (n_frames, _af, _lab, n_chars, ap_chars, width, height, ms,
     n_imp, a_imp, n_exp, a_exp) = [au32(apt, m + 16 + 4 * i) for i in range(12)]
    movie = {'frames': n_frames, 'width': width, 'height': height, 'ms_per_frame': ms,
             'const_strings': strings, 'characters': [], 'imports': [], 'exports': []}
    for i in range(n_chars):
        p = au32(apt, ap_chars + 4 * i)
        if not p:
            continue
        t = au32(apt, p)
        c = {'id': i, 'type': TYPES.get(t, str(t))}
        if t == 7:      # Bitmap
            c['bitmap_id'] = au32(apt, p + 16)
        elif t == 1:    # Shape
            c['bounds'] = [round(af32(apt, p + 16 + 4 * k), 2) for k in range(4)]
            c['geometry_id'] = au32(apt, p + 32)
        elif t == 3:    # Font
            c['name'] = cstr(apt, au32(apt, p + 16))
            c['raw'] = [au32(apt, p + 8 + 4 * k) for k in range(6)]
        elif t == 2:    # Text
            c['bounds'] = [round(af32(apt, p + 16 + 4 * k), 2) for k in range(4)]
            c['font_id'] = au32(apt, p + 32)
            c['text'] = cstr(apt, au32(apt, p + 60))
            c['variable'] = cstr(apt, au32(apt, p + 64))
        movie['characters'].append(c)
    for i in range(n_imp):
        q = a_imp + 16 * i
        movie['imports'].append({'movie': cstr(apt, au32(apt, q)), 'name': cstr(apt, au32(apt, q + 4)),
                                 'id': au32(apt, q + 8)})
    for i in range(n_exp):
        q = a_exp + 8 * i
        movie['exports'].append({'name': cstr(apt, au32(apt, q)), 'id': au32(apt, q + 4)})
    return movie

# ---------------------------------------------------------------- TPK index

def walk_chunks(b, p, end):
    while p + 8 <= end:
        t, s = struct.unpack_from('<II', b, p)
        if p + 8 + s > end:
            return
        yield t, s, p
        p += 8 + s


def tpk_scan(b, p, end):
    """One TPK (chunk 0xB3300000).
    Part 2 (0x33310002) = key list, 8 bytes per texture (key = Black Box hash of the name).
    Part 4 (0x33310004) = texture records with names (normal packs).
    Dynamic/compressed packs have no part 4: names are only inside the compressed texture data,
    so only the keys are known. Returns (names, keys, sub_chunk_ids)."""
    names, keys, sub_ids = [], set(), []
    for t, s, q in walk_chunks(b, p, end):
        if t != 0xB3310000:
            continue
        subs = {t2: (q2 + 8, s2) for t2, s2, q2 in walk_chunks(b, q + 8, q + 8 + s)}
        sub_ids = ['0x%08X:%d' % (k, v[1]) for k, v in subs.items()]
        if 0x33310002 not in subs:
            continue
        o, n = subs[0x33310002]
        keys = {u32(b, o + 8 * i) for i in range(n // 8)}
        if 0x33310004 not in subs:
            continue
        o, n = subs[0x33310004]
        blob = b[o:o + n]
        seen = set()
        i = 0
        while i < len(blob):
            j = i
            while j < len(blob) and 0x20 <= blob[j] <= 0x7E:
                j += 1
            if j - i >= 3 and j < len(blob) and blob[j] == 0:
                run = blob[i:j]
                for k in range(len(run) - 2):       # skip a length byte that looks printable
                    nm = run[k:].decode('latin1')
                    if bin_hash(nm) in keys and nm not in seen:
                        seen.add(nm)
                        names.append(nm)
                        break
            i = max(j, i + 1)
    return names, keys, sub_ids


def refpack_decode(src, out_len):
    """EA RefPack (QFS). NOT tested on a real RFPK file yet (Darius calls a native DLL for it).
    The stream may start with the 0x10FB header; both forms are accepted."""
    pos = 0
    if len(src) > 5 and src[1] == 0xFB and (src[0] & 0x7E) == 0x10:
        size_len = 4 if src[0] & 0x80 else 3
        pos = 2 + size_len * (2 if src[0] & 0x01 else 1)   # optional packed size, then unpacked size
    out = bytearray()
    n = len(src)
    stopped = False
    while pos < n:
        c = src[pos]
        pos += 1
        if c < 0x80:                                 # 2 bytes
            b1 = src[pos]; pos += 1
            lit, ln, dist = c & 3, ((c & 0x1C) >> 2) + 3, ((c & 0x60) << 3) + b1 + 1
        elif c < 0xC0:                               # 3 bytes
            b1, b2 = src[pos], src[pos + 1]; pos += 2
            lit, ln, dist = b1 >> 6, (c & 0x3F) + 4, ((b1 & 0x3F) << 8) + b2 + 1
        elif c < 0xE0:                               # 4 bytes
            b1, b2, b3 = src[pos], src[pos + 1], src[pos + 2]; pos += 3
            lit, ln, dist = c & 3, ((c & 0x0C) << 6) + b3 + 5, ((c & 0x10) << 12) + (b1 << 8) + b2 + 1
        elif c < 0xFC:                               # literals only
            lit, ln, dist = ((c & 0x1F) << 2) + 4, 0, 0
        else:                                        # stop code
            lit, ln, dist = c & 3, 0, 0
        out += src[pos:pos + lit]
        pos += lit
        if ln:
            if dist > len(out):
                raise ValueError('RefPack: back reference before start of output')
            st = len(out) - dist
            for k in range(ln):
                out.append(out[st + k])
        if c >= 0xFC:
            stopped = True
            break
    # The header size is not used to cut the output: on real files it is a few bytes short of
    # the data before the stop code (the last TPK chunk would be cut). The stop code ends the block.
    return bytes(out), stopped


def rfpk_decompress(data):
    """LZ block header (16 bytes): magic, ?, unpacked size (u32 at 8), packed size (u32 at 12).
    Several blocks in a row are joined."""
    out, pos, blocks, sizes = bytearray(), 0, 0, []
    while pos + 16 <= len(data) and data[pos:pos + 4] == b'RFPK':
        out_len, in_len = u32(data, pos + 8), u32(data, pos + 12)
        end = pos + in_len if 16 < in_len <= len(data) - pos else len(data)
        if end < len(data) and data[end:end + 4] != b'RFPK':
            end = len(data)                         # the packed size in the header can be short
        part, stopped = refpack_decode(data[pos + 16:end], out_len)
        out += part
        sizes.append((out_len, len(part), stopped))
        pos = end
        blocks += 1
    if not out:
        raise ValueError('no RFPK block')
    return bytes(out), {'blocks': blocks, 'unpacked': len(out), 'trailing': len(data) - pos,
                        'next': data[pos:pos + 16].hex(' '), 'sizes': sizes}


def load_resource_file(path, decomp):
    data = open(path, 'rb').read()
    magic = data[:4]
    if magic == b'RFPK':
        try:
            out, info = rfpk_decompress(data)
            print('  RFPK %s: %d block(s), %d bytes unpacked (header: %s), stop code %s' % (
                os.path.basename(path), info['blocks'], info['unpacked'],
                ','.join(str(h) for h, _, _ in info['sizes']),
                ','.join('yes' if st else 'NO' for _, _, st in info['sizes'])))
            return out, None
        except Exception as ex:
            return None, 'RFPK decompress failed: %s (file %d bytes, header %s)' % (
                ex, len(data), data[:32].hex(' '))
    if magic == b'JDLZ':
        if not decomp:
            return None, 'compressed (use --decomp)'
        try:
            return decomp(data), None
        except Exception as ex:
            return None, 'JDLZ decompress failed: %s' % ex
    if path.lower().endswith('.lzc') and not (len(data) >= 8 and data[3] & 0x80):
        return None, 'unknown .lzc header %r' % magic
    return data, None                                # plain chunk file (also .lzc that is not compressed)


def find_tpks(b, p, end, depth=0):
    """Yield (offset, size) of every TPK chunk; descend into other container chunks (bit 31 set)."""
    for t, s, q in walk_chunks(b, p, end):
        if t == 0xB3300000:
            yield q, s
        elif t & 0x80000000 and depth < 4 and s >= 8:
            yield from find_tpks(b, q + 8, q + 8 + s, depth + 1)


def chunk_summary(b, limit=6):
    """Top-level chunk ids by size, to see what a file without TPKs holds."""
    tot = {}
    for t, s, q in walk_chunks(b, 0, len(b)):
        c, sz = tot.get(t, (0, 0))
        tot[t] = (c + 1, sz + s)
    top = sorted(tot.items(), key=lambda kv: -kv[1][1])[:limit]
    if not top:
        return ['no valid chunk; first bytes: ' + b[:32].hex(' ')]
    return ['0x%08X x%d %dB' % (t, c, sz) for t, (c, sz) in top]


def build_index(dirs, decomp):
    """name index (upper name -> hits), hash index (key -> hits) and a scan log."""
    index, hindex, log = {}, {}, []
    for d in dirs:
        paths = [d] if os.path.isfile(d) else [os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs]
        for path in sorted(paths):
            if not path.lower().endswith(('.bun', '.bin', '.lzc')):
                continue
            data, err = load_resource_file(path, decomp)
            if data is None:
                log.append({'file': path, 'status': 'skipped: ' + err})
                continue
            n_packs = n_tex = n_hash_only = 0
            notes = []
            for p, s in find_tpks(data, 0, len(data)):
                if True:
                    names, keys, sub_ids = tpk_scan(data, p + 8, p + 8 + s)
                    by_hash = {bin_hash(nm): nm for nm in names}
                    for k in keys:
                        hit = {'file': os.path.basename(path), 'pack': n_packs, 'hash': '0x%08X' % k,
                               'name': by_hash.get(k)}
                        hindex.setdefault(k, []).append(hit)
                    for nm in names:
                        index.setdefault(nm.upper(), []).append(
                            {'file': os.path.basename(path), 'pack': n_packs, 'hash': '0x%08X' % bin_hash(nm)})
                    unnamed = len(keys) - len(by_hash)
                    n_hash_only += unnamed
                    if unnamed:
                        notes.append({'pack': n_packs, 'keys': len(keys), 'names_found': len(names),
                                      'hash_only': unnamed, 'subchunks': sub_ids})
                    n_packs += 1
                    n_tex += len(keys)
            entry = {'file': path, 'status': 'ok', 'packs': n_packs, 'textures': n_tex,
                     'hash_only': n_hash_only, 'notes': notes}
            if n_packs == 0 and len(data) > 4096:
                entry['chunks'] = chunk_summary(data)
                print('  no TPK in %s (%d bytes): %s' % (os.path.basename(path), len(data), '; '.join(entry['chunks'])))
            log.append(entry)
            for g in notes:
                print('  hash-only entries in %s pack %d: %d keys, %d names, %d without name (sub-chunks %s)' % (
                    os.path.basename(path), g['pack'], g['keys'], g['names_found'], g['hash_only'],
                    ' '.join(g['subchunks'])))
    return index, hindex, log

# ---------------------------------------------------------------- resolver

def geometry_images(blob):
    """Fill blocks of a shape geometry entry. Layout seen on Undercover files:
    header: total size, block count, 0, offset of block 0; offsets of blocks 1.. at 0x10;
    block: size, ..., colour at +0x20, image id at +0x24 (0 = no image).
    Returns a list of (colour, image_id) or None if the blob does not fit this layout."""
    try:
        total, n, _, first = struct.unpack_from(ENDIAN + '4I', blob, 0)
        if total != len(blob) or not 1 <= n <= 64:
            return None
        offs = [first] + [au32(blob, 0x10 + 4 * i) for i in range(n - 1)]
        return [(au32(blob, o + 0x20), au32(blob, o + 0x24)) for o in offs]
    except struct.error:
        return None


def resolve_image(img_id, ents, index, hindex):
    """Image id -> BIG entry (id + 1) holding the PNG name -> texture in the TPK index."""
    key = str(img_id + 1)
    raw = ents.get(key)
    png = raw.rstrip(b'\0').decode('latin1') if raw is not None and len(raw) < 64 else None
    stem = os.path.splitext(png)[0].upper() if png else None
    hits = index.get(stem, []) if stem else []
    how = 'name'
    if not hits and stem:                           # dynamic packs: keys only
        hits = hindex.get(bin_hash(stem), []) or hindex.get(bin_hash(png[:-4]), [])
        how = 'hash'
    return key, {'id': img_id, 'entry': key, 'png': png, 'texture': stem,
                 'found': hits[0] if hits else None, 'by': how if hits else None, 'copies': len(hits)}


def scan_big(path, index, hindex, exports_by_movie):
    kind, entries = read_big(path)
    ents = dict(entries)
    if '0' not in ents or '1' not in ents:
        return None
    movie = parse_apt(ents['0'], ents['1'])
    used = {'0', '1'}
    image_ids = {}
    res = {'file': os.path.basename(path), 'container': kind, 'entries': len(entries),
           'movie': {k: movie[k] for k in ('frames', 'width', 'height', 'ms_per_frame')},
           'counts': {}, 'bitmaps': [], 'images': [], 'shapes': [], 'fonts': [], 'texts': [],
           'imports': [], 'exports': movie['exports'], 'orphan_entries': []}
    for c in movie['characters']:
        res['counts'][c['type']] = res['counts'].get(c['type'], 0) + 1
        if c['type'] == 'Bitmap':
            image_ids.setdefault(c['id'], 'bitmap')
            key, info = resolve_image(c['id'], ents, index, hindex)
            used.add(key)
            res['bitmaps'].append(info)
        elif c['type'] == 'Shape':
            key = str(c['geometry_id'] + 1)
            used.add(key)
            blob = ents.get(key)
            blocks = geometry_images(blob) if blob else None
            imgs = sorted({img for _, img in blocks if 0 < img < 0x10000}) if blocks else []
            for img in imgs:
                image_ids.setdefault(img, 'shape')
            res['shapes'].append({'id': c['id'], 'geometry_id': c['geometry_id'], 'entry': key,
                                  'geometry_bytes': len(blob) if blob else None,
                                  'blocks': len(blocks) if blocks else None,
                                  'images': imgs, 'bounds': c['bounds']})
        elif c['type'] == 'Font':
            nm = (c.get('name') or '').upper()
            res['fonts'].append({'id': c['id'], 'name': c.get('name'),
                                 'texture_match': index.get(nm, [None])[0] if nm else None,
                                 'raw': c['raw']})
        elif c['type'] == 'Text':
            res['texts'].append({'id': c['id'], 'font_id': c['font_id'], 'text': c['text'],
                                 'variable': c['variable'], 'bounds': c['bounds']})
    for im in movie['imports']:
        target = exports_by_movie.get((im['movie'] or '').lower())
        res['imports'].append({**im, 'big_found': target is not None,
                               'export_found': bool(target and im['name'] in target)})
    for img in sorted(image_ids):                    # every image the movie uses (bitmap characters + shape fills)
        key, info = resolve_image(img, ents, index, hindex)
        used.add(key)
        info['used_by'] = image_ids[img]
        res['images'].append(info)
    res['orphan_entries'] = [n for n, _ in entries if n not in used]
    return res, {e['name'] for e in movie['exports']}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ui', help='folder with .big files, or one .big file')
    ap.add_argument('--res', action='append', default=[], help='folder with .bun/.bin files (repeatable)')
    ap.add_argument('--out', default='apt_report.json')
    ap.add_argument('--csv', default=None)
    ap.add_argument('--decomp', default=None)
    ap.add_argument('--verbose', action='store_true')
    a = ap.parse_args()

    # modules next to the script and in the current folder can be imported
    sys.path.insert(0, os.getcwd())
    decomp = None
    spec = a.decomp or 'fng_jdlz:decompress'      # default: common/fng_jdlz.py
    try:
        mod, fn = spec.split(':')
        decomp = getattr(importlib.import_module(mod), fn)
        print('decompressor: %s' % spec)
    except (ImportError, AttributeError, ValueError) as ex:
        if a.decomp:
            sys.exit('cannot load --decomp %s: %s' % (a.decomp, ex))
        print('no decompressor (fng_jdlz.py not found); compressed files will be skipped')

    a.out = outpaths.out_path('apt', a.out)
    a.csv = outpaths.out_path('apt', a.csv)
    index, hindex, scan_log = build_index(a.res, decomp)
    print('texture index: %d names, %d keys from %d resource files (%d skipped)' % (
        len(index), len(hindex), sum(1 for l in scan_log if l['status'] == 'ok'),
        sum(1 for l in scan_log if l['status'] != 'ok')))
    for l in scan_log:
        if l['status'] != 'ok' and a.verbose:
            print('  %s  %s' % (l['status'], l['file']))

    bigs = [a.ui] if os.path.isfile(a.ui) else sorted(
        os.path.join(r, f) for r, _, fs in os.walk(a.ui) for f in fs if f.lower().endswith('.big'))

    # pass 1: export names per movie (for import resolution)
    exports_by_movie = {}
    for path in bigs:
        try:
            _, ents = read_big(path)
            e = dict(ents)
            if '0' in e and '1' in e:
                mv = parse_apt(e['0'], e['1'])
                exports_by_movie[os.path.splitext(os.path.basename(path))[0].lower()] = \
                    {x['name'] for x in mv['exports']}
        except Exception:
            pass

    reports, errors = [], []
    for path in bigs:
        try:
            r = scan_big(path, index, hindex, exports_by_movie)
        except Exception as ex:  # keep going, list the failure
            errors.append({'file': path, 'error': str(ex)})
            continue
        if r:
            reports.append(r[0])

    missing = {}
    tot_b = tot_f = 0
    for r in reports:
        nb = len(r['images'])
        nf = sum(1 for b in r['images'] if b['found'])
        tot_b += nb
        tot_f += nf
        bad_imp = [i for i in r['imports'] if not i['big_found']]
        print('%-34s images %2d/%-2d  shapes %2d (geom %2d)  fonts %d  texts %2d  imports %d (%d unresolved)%s' % (
            r['file'], nf, nb, len(r['shapes']), sum(1 for s in r['shapes'] if s['geometry_bytes']),
            len(r['fonts']), len(r['texts']), len(r['imports']), len(bad_imp),
            '  orphans:%d' % len(r['orphan_entries']) if r['orphan_entries'] else ''))
        for b in r['images']:
            if not b['found']:
                missing.setdefault(b['texture'] or '(no name entry)', []).append(r['file'])
        if a.verbose:
            for key in ('images', 'shapes', 'fonts', 'imports'):
                for x in r[key]:
                    print('    ', key, x)
    print('\nimages resolved: %d of %d; distinct missing textures: %d; BIG files with errors: %d' % (
        tot_f, tot_b, len(missing), len(errors)))
    for nm, files in sorted(missing.items(), key=lambda kv: (-len(set(kv[1])), kv[0])):
        fl = sorted(set(files))
        print('  missing %-26s %2d movie(s): %s%s' % (nm, len(fl), ', '.join(fl[:4]), ' ...' if len(fl) > 4 else ''))
    for e in errors:
        print('  error', e['file'], e['error'])

    json.dump({'resources': scan_log, 'movies': reports, 'errors': errors,
               'missing_textures': {k: sorted(set(v)) for k, v in missing.items()}},
              open(a.out, 'w'), indent=1)
    print('report ->', a.out)
    if a.csv:
        with open(a.csv, 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['texture', 'movie_count', 'used_by'])
            for nm, files in sorted(missing.items(), key=lambda kv: (-len(set(kv[1])), kv[0])):
                w.writerow([nm, len(set(files)), ';'.join(sorted(set(files)))])


if __name__ == '__main__':
    main()
