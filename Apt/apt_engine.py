"""
The natives nfs.exe registers into the apt scripts (_global.CmdUtil, _global.cmdFEHintsTipsScreen ...), fed with game data.

Which natives exist comes from apt_engine_map.json (Apt/beamng/engine_map.py reads them out of nfs.exe). Each one is installed in _global
as a native function with the owner path of the stub keys ("_global.cmdFEHintsTipsScreen.GetPageData()"), so a call is recorded in
vm.externals with its arguments. What a call answers, in this order:
  1. a stub the person wrote in apt_stubs.json for that key,
  2. a provider below, which answers from real data (the English string tables, the attrib dump, a viewer option),
  3. nothing: undefined, as before.
Only objects with at least one native proven in the exe are installed; names the scripts define themselves (gCT, gTransMan ...) are not touched.

Providers answer with JSON values (see apt_vm.from_json). Each says what it is sure of; what is an assumption is marked ASSUMED.
"""
import json
import os

import apt_data
import apt_vm as VM


class Engine:
    def __init__(self, engine_map, language=None, attrib=None, widescreen=True, hints_category="hints_career"):
        """engine_map: path of apt_engine_map.json; language: apt_data.Language; attrib: apt_data.Attrib."""
        data = json.load(open(engine_map))["objects"]
        # an object is installed when at least one of its natives is proven in the exe; natives the exe registers on two objects
        # (listed as ambiguous in the map) are installed too, unfed, so that calls to them are recorded
        self.members = {o: sorted(set(v["members"]) | set(v.get("ambiguous", []))) for o, v in data.items() if v.get("members")}
        self.language, self.attrib = language, attrib
        self.options = {"widescreen": bool(widescreen), "hints_category": hints_category}
        names = os.path.join(os.path.dirname(os.path.abspath(engine_map)), "apt_screen_names.json")
        self.screens = {}                     # screen id -> movie name (first entry of an id wins, as in the exe's linear search)
        if os.path.exists(names):
            for it in json.load(open(names)):
                if it.get("name"):
                    self.screens.setdefault(it["id"], it["name"])
        self.queue = []                       # messages the game sends to the root movie (post_message)
        self.providers = {
            ("CmdUtil", "GetWidescreen"): self.get_widescreen,
            ("cmdChyron", "IsWidescreen"): lambda args: bool(self.options["widescreen"]),
            ("ScreenMessageTranslator", "GetStringHash"): self.get_string_hash,
            ("ScreenMessageTranslator", "GetScreenName"): self.get_screen_name,
            ("ScreenMessageTranslator", "IsMessageAvailable"): lambda args: bool(self.queue),
            ("ScreenMessageTranslator", "GetMessage"): lambda args: self.queue.pop(0) if self.queue else None,
            ("cmdFEHintsTipsScreen", "GetToggleData"): self.hints_toggle_data,
            ("cmdFEHintsTipsScreen", "GetPageData"): self.hints_page_data,
        }

    # ---- install
    def install(self, vm):
        self.queue = []                                         # a reset of the movie starts with no message
        for obj, members in self.members.items():
            o = VM.Obj(name="_global." + obj)
            for member in members:
                o.props[member] = VM.Native(self._native(obj, member), member)
            vm.globals.props[obj] = o

    def _native(self, obj, member):
        key = "_global.%s.%s()" % (obj, member)
        provider = self.providers.get((obj, member))

        def fn(vm, this, args):
            vm.external("call", key, args)
            s = vm.stub(key)
            if s is not VM.MISSING:
                return s
            if provider is not None:
                value = provider(args)
                if value is not None:
                    return VM.from_json(value, key.rstrip("()"))
            vm.note("call %s (a native of the game, no data for it)" % key)
            return VM.UNDEF
        return fn

    # ---- the game's side
    def post_message(self, fields):
        """Queue a message for the root movie's loop (IsMessageAvailable / GetMessage). `message` is an id name like SETVISIBLE (hashed
        with bstring_hash); `texture` is a texture name, sent as textureHash. Other fields (level, mvClip, x, y, width, height, angle,
        text, visible, screenId ...) are passed as they are."""
        m = dict(fields)
        if isinstance(m.get("message"), str):
            m["message"] = apt_data.bstring_hash(m["message"])
        if isinstance(m.get("texture"), str):
            m["textureHash"] = apt_data.bstring_hash(m.pop("texture"))
        self.queue.append(m)

    # ---- providers
    def get_string_hash(self, args):
        """The id of a screen message: the same bstring_hash the messages carry (the scripts compare msg.message with it)."""
        return apt_data.bstring_hash(VM.to_str(args[0])) if args else None

    def get_screen_name(self, args):
        """Movie name (without extension) of a screen id: the table in nfs.exe (apt_screen_names.json, 95 screens). The root movie loads name + ".swf"."""
        if not args:
            return None
        n = VM.to_number(args[0])
        return self.screens.get(int(n)) if n == n else None

    def get_widescreen(self, args):
        """Object with widescreen: 1 for 16x9, 0 for 4x3 (the handler in nfs.exe sets exactly this one property; the value is the viewer option)."""
        return {"widescreen": 1 if self.options["widescreen"] else 0}

    def _hints(self):
        """The items of the hints category in use: [{subject, text, picture}] from attrib fe_hints_tips and the string tables.
        ASSUMED: the game shows one category at a time and the toggle options are its subjects in order (ID = index)."""
        if self.attrib is None or self.language is None:
            return None
        col = self.attrib.collections("fe_hints_tips").get(self.options["hints_category"])
        if not col:
            return None
        out = []
        for it in col.get("content", {}).get("items", []):
            out.append({"subject": self.language.text(int(it["subject_hal_id"], 16)) or "",
                        "text": self.language.text(int(it["text_hal_id"], 16)) or "", "picture": it.get("picture", "")})
        return out

    def hints_toggle_data(self, args):
        items = self._hints()
        if items is None:
            return None
        return {"Text": "", "ID": 0, "Content": [{"Text": it["subject"], "ID": i} for i, it in enumerate(items)]}

    def hints_page_data(self, args):
        items = self._hints()
        if items is None or not args:
            return None
        try:
            it = items[int(VM.to_number(args[0]))]
        except (IndexError, ValueError):
            return None
        # the exe also sets hash and imageHash from the picture name (format strings "%s%d"): how the number is made is not decoded yet
        return {"Text": it["text"], "picture": it["picture"]}


def build(engine_map, lang_dir=None, attrib_dump=None, names=None, widescreen=True, hints_category="hints_career"):
    """An Engine from the paths the viewer was given; data that is not given is simply not available (calls then answer undefined)."""
    language = apt_data.Language(lang_dir) if lang_dir else None
    attrib = apt_data.Attrib(attrib_dump, names) if attrib_dump else None
    return Engine(engine_map, language, attrib, widescreen, hints_category)


DATA_NAMES = (("map", "apt_engine_map.json", "which natives nfs.exe registers (made by beamng/engine_map.py from nfs.exe)"),
              ("screens", "apt_screen_names.json", "screen id -> movie name, beside the map (made by the same tool; GetScreenName)"),
              ("lang", "English_*.bin", "text of the strings the natives hand to the screens (hints and tips ...)"),
              ("attrib", "undercover_global_dump.json", "attrib data (UndercoverVLT_Dumper output of the 4 vault files)"),
              ("names", "hashes_main.txt", "names for the attrib hashes (classes, fields, collections)"),
              ("exe", "nfs.exe", "only to remake apt_engine_map.json (beamng/engine_map.py), not needed to run the viewer"))


def find_data(data_dir=None):
    """Where the engine data is: {role: path or None}. The folder is Apt/engine_data (or data_dir); the engine map is also looked for in Apt/beamng."""
    here = os.path.dirname(os.path.abspath(__file__))
    d = data_dir or os.path.join(here, "engine_data")
    names = {n.lower(): n for n in os.listdir(d)} if os.path.isdir(d) else {}

    def one(*wanted):
        for w in wanted:
            if w.lower() in names:
                return os.path.join(d, names[w.lower()])
        return None
    out = {"dir": d, "map": one("apt_engine_map.json") or (os.path.join(here, "beamng", "apt_engine_map.json")
                                                          if os.path.exists(os.path.join(here, "beamng", "apt_engine_map.json")) else None),
           "attrib": one("undercover_global_dump.json") or next((os.path.join(d, n) for n in names.values() if "dump" in n.lower() and n.lower().endswith(".json")), None),
           "names": one("hashes_main.txt") or next((os.path.join(d, n) for n in names.values() if n.lower().startswith("hashes") and n.lower().endswith(".txt")), None),
           "exe": one("nfs.exe"),
           "screens": None,
           "lang": d if any(n.lower().startswith("english_") and n.lower().endswith(".bin") for n in names.values()) else None}
    if out["map"]:                                              # the screen table lives next to the map
        sib = os.path.join(os.path.dirname(out["map"]), "apt_screen_names.json")
        out["screens"] = sib if os.path.exists(sib) else None
    return out


def status(paths):
    """[(role, found, text)] for the check tool and the viewer's start message."""
    rows = []
    for role, name, why in DATA_NAMES:
        p = paths.get(role)
        detail = ""
        if p and role == "lang":
            detail = "%d strings" % len(apt_data.Language(p).table)
        elif p and role == "screens":
            detail = "%d screens" % len(json.load(open(p)))
        elif p and role == "map":
            detail = "%d objects with natives" % len(json.load(open(p))["objects"])
        elif p and role == "attrib":
            detail = "%d classes" % len(json.load(open(p))["classes"])
        elif p and role == "names":
            detail = "%d names" % len(apt_data.load_names(p))
        rows.append((role, bool(p), "%s -> %s%s | %s" % (name, p or "NOT FOUND (put it in %s)" % paths["dir"], " (%s)" % detail if detail else "", why)))
    return rows


def build_default(data_dir=None, widescreen=True, hints_category="hints_career"):
    """An Engine from the data folder, or None without an engine map. Missing data only means those natives answer undefined."""
    paths = find_data(data_dir)
    if not paths["map"]:
        return None
    return build(paths["map"], paths["lang"], paths["attrib"], paths["names"], widescreen, hints_category)
