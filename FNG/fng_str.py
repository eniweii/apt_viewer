#!/usr/bin/env python3
"""
fng_str.py

Reads the language string blocks (STRBlocks, 0x00039000) of the language files, like
English_Frontend.bin, English_Global.bin, English_InGame.bin. Port of Darius/Nikki (Support.Carbon STRBlock).
Not tested on a real language file yet, only on blocks built here from the same layout.

Block payload, little-endian:
  i32 entry count, i32 hash table offset, i32 text offset, collection name (16 bytes, zero ended)
  hash table (at its offset from the payload start): per entry u32 key, i32 text offset
  texts (at the text offset): zero ended UTF-8, entry text = text offset + the entry's offset
The key is the hash of the label (for example of 'STR_OK'). A text object of an FNG names its label hash.
The language of a file is the part of its name before the first '_' (English_Global.bin -> English).
"""
import os
import struct

import fng_bun

STR_BLOCK = 0x00039000
MAX_BIN_SIZE = 4 * 1024 * 1024         # language files are small; bigger .bin files are not read


class StringIndex:
    def __init__(self):
        self.languages = {}             # language -> {key: text}
        self.collections = {}           # language -> [collection names]
        self.problems = []

    def language_names(self):
        return sorted(self.languages)

    def default_language(self):
        names = self.language_names()
        return "English" if "English" in names else (names[0] if names else "")

    def count(self, language):
        return len(self.languages.get(language, {}))

    def get(self, language, key):
        return self.languages.get(language, {}).get(key)


def language_of(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.split("_")[0] if "_" in stem else stem


def parse_str_block(data):
    """Returns (collection name, {key: text}). Raises ValueError if the block does not fit."""
    if len(data) < 0x1C:
        raise ValueError("block is too short")
    count, hash_off, text_off = struct.unpack_from("<iii", data, 0)
    if count < 0 or hash_off < 0x1C or text_off < 0x1C or hash_off + count * 8 > len(data) or text_off > len(data):
        raise ValueError(f"offsets are outside the block (entries {count}, hashes at {hash_off}, texts at {text_off})")
    name = data[12:28].split(b"\0")[0].decode("latin-1")
    texts = {}
    for i in range(count):
        key, offset = struct.unpack_from("<Ii", data, hash_off + i * 8)
        start = text_off + offset
        if offset < 0 or start >= len(data):
            raise ValueError(f"text of entry {i} is outside the block")
        end = data.find(b"\0", start)
        texts.setdefault(key, data[start:len(data) if end < 0 else end].decode("utf-8", "replace"))
    return name, texts


def scan_files(paths):
    index = StringIndex()
    for path in paths:
        try:
            if path.lower().endswith(".bin") and os.path.getsize(path) > MAX_BIN_SIZE:
                continue
            refs = fng_bun.scan_blocks(path, {STR_BLOCK})
        except OSError:
            continue
        for ref in refs:
            try:
                name, texts = parse_str_block(fng_bun.read_block(ref))
            except (ValueError, struct.error) as e:
                index.problems.append(f"{os.path.basename(path)} @0x{ref.offset:X}: {e}")
                continue
            language = language_of(path)
            target = index.languages.setdefault(language, {})
            for key, text in texts.items():
                target.setdefault(key, text)
            index.collections.setdefault(language, []).append(name)
    return index
