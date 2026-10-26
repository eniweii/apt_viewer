#!/usr/bin/env python3
"""
apt_stubs.py

The stub file (apt_stubs.json): what the game would give a screen, written by the person, kept per movie.

  {"postrace.big": {
      "stubs":    {"gameData": {"raceType": 2}, "cmdSound()": 0, "_root.mc.getRank()": 1},
      "scenario": [{"at": 0, "op": "set",  "path": "_root.raceType", "value": 2},
                   {"at": 0, "op": "call", "path": "_global.PostRace.prototype.InitGUI", "this": "_root", "args": []},
                   {"at": 30, "op": "goto", "path": "_root.screen", "frame": "show", "play": true}]}}

  stubs     name -> value for a variable the movie reads and does not define; "name()" or "owner.name()" -> return value
            for a call. An object value is a script object, a list is an array. Evidence level: stub.
  scenario  actions the game does, replayed from tick 0 after every reset (at = tick; set / call / goto).
"""
import json
import os


class StubStore:
    def __init__(self, path):
        self.path = path
        self.data = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                self.data = {}

    def get(self, movie_name):
        d = self.data.setdefault(movie_name, {})
        d.setdefault("stubs", {})
        d.setdefault("scenario", [])
        return d

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, ensure_ascii=False)
