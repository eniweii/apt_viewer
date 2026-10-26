#!/usr/bin/env python3
"""
apt_hints.py

Hints from the strings of the x360 alpha build (apt_hints.json, made by extract_dev_symbols.py).
A local file: it holds names taken from a game build, so it stays out of the repository.

  objects        cmd* name -> names that follow it in the string pool of the build. The grouping is an inference,
                 evidence level "inferred": a name was probably used with the object that comes before it.
  clip_commands  the clip commands of the message queue (SETPOSITION, ChangeMCVisibility, ...)
  queue          IsMessageAvailable, GetMessage, EnableProcessing
  call_paths     path formats the game uses to call a screen, like _level0.%s.Screen
  screens, log_formats, apt_names, clip_names   names found in the build, kept for reading

Nothing here is a value for a stub. The Stubs tab only shows where a name appears in the build.
"""
import json
import os


class Hints:
    def __init__(self, path):
        self.path = path
        self.data = {}
        self.by_function = {}                     # name -> [cmd object, ...]
        self.other = {}                           # name -> short label (clip command, queue function, ...)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                self.data = {}
        for obj, e in self.data.get("objects", {}).items():
            for fn in e.get("functions", []):
                self.by_function.setdefault(fn, []).append(obj)
        for key, label in (("clip_commands", "clip command"), ("queue", "message queue")):
            for e in self.data.get(key, []):
                self.other[e["name"]] = label

    @property
    def ok(self):
        return bool(self.data)

    def summary(self):
        if not self.ok:
            return "Dev hints: none (no %s)" % os.path.basename(self.path)
        o = self.data.get("objects", {})
        return "Dev hints (inferred from %s): %d cmd objects, %d names" % (
            self.data.get("source") or "the build strings", len(o), len(self.by_function))

    def note(self, name):
        """Short text for a name: the cmd objects that list it (with ?), or what else the build says about it."""
        if not self.ok or not name:
            return ""
        base = name[:-2] if name.endswith("()") else name
        last = base.rsplit(".", 1)[-1]
        if base in self.data.get("objects", {}) or last in self.data.get("objects", {}):
            return "cmd object"
        owners = self.by_function.get(last)
        if owners:
            return ", ".join(o + "?" for o in owners[:2]) + (" +%d" % (len(owners) - 2) if len(owners) > 2 else "")
        return self.other.get(last, "")
