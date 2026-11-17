"""
The natives nfs.exe registers into the apt scripts (_global.CmdUtil, _global.cmdFEHintsTipsScreen ...), fed with game data.

Which natives exist comes from apt_engine_map.json (Apt/game_stuff/engine_map.py reads them out of nfs.exe). Each one is installed in _global
as a native function with the owner path of the stub keys ("_global.cmdFEHintsTipsScreen.GetPageData()"), so a call is recorded in
vm.externals with its arguments. What a call answers, in this order:
  1. a stub the person wrote in apt_stubs.json for that key,
  2. a provider below, which answers from real data (the English string tables, the attrib dump, a viewer option),
  3. nothing: undefined, as before.
Only objects with at least one native proven in the exe are installed; names the scripts define themselves (gCT, gTransMan ...) are not touched.

Providers answer with JSON values (see apt_vm.from_json). Each says what it is sure of; what is an assumption is marked ASSUMED.
"""
import datetime
import json
import re
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
        exe = os.path.join(os.path.dirname(os.path.abspath(engine_map)), "apt_exe_addresses.json")
        self.exe_tables = {t["name"]: t for t in json.load(open(exe)).get("tables", [])} if os.path.exists(exe) else {}
        self.options = {"widescreen": bool(widescreen), "hints_category": hints_category}
        names = os.path.join(os.path.dirname(os.path.abspath(engine_map)), "apt_screen_names.json")
        self.screens = {}                     # screen id -> movie name (first entry of an id wins, as in the exe's linear search)
        if os.path.exists(names):
            for it in json.load(open(names)):
                if it.get("name"):
                    self.screens.setdefault(it["id"], it["name"])
        self.queue = []                       # messages the game sends to the root movie (post_message)
        self.processing = True                # ScreenMessageTranslator's flag (byte at +0x3c in nfs.exe); its start value is ASSUMED to be on
        self.events = []                      # what the UI told the game: (name, args), capped; see UI_EVENTS
        self.loaded = []                      # CmdScreenManagement.MovieClipLoaded(name, level) calls
        self.main_screen_loaded = False
        self.carlot_region = 0                # CmdFECarLot.UpdateRegion: the region the shop lists
        self.tree_groups = self._tree_groups()  # menu tree levels from the slot table of nfs.exe: group id -> [(item id, label hash)]
        self.screen_ids = {v.lower(): k for k, v in self.screens.items()}      # screen name (lower case) -> id
        self.trees = {}                       # tree id -> {"stack": [[group, selected index]], "pending": bool}; see the tree section below
        self.active_tree = None               # the tree the UI asked data for last: OnOptionSelected & co carry no tree id, ASSUMED one tree on screen at a time
        if self.tree_groups:                  # MainMenu is an object of the TreeMenu script; the exe registers GetPauseTreeID for it (0x556880) but the map has no name for it
            self.members.setdefault("MainMenu", []).append("GetPauseTreeID")
        self.providers = {
            ("CmdUtil", "GetWidescreen"): self.get_widescreen,
            ("cmdChyron", "IsWidescreen"): lambda args: bool(self.options["widescreen"]),
            ("ScreenMessageTranslator", "GetStringHash"): self.get_string_hash,
            ("ScreenMessageTranslator", "GetScreenName"): self.get_screen_name,
            ("ScreenMessageTranslator", "IsMessageAvailable"): lambda args: bool(self.queue) and self.processing,
            ("ScreenMessageTranslator", "EnableProcessing"): self.enable_processing,
            ("CmdFECarLot", "GetRegions"): self.carlot_regions,
            ("CmdFECarLot", "GetCarList"): self.carlot_cars,
            ("CmdFECarLot", "UpdateRegion"): self.carlot_update_region,
            ("cmdPCCreateAccount", "GetYearInfo"): self.year_info,
            ("cmdLanguageSelect", "GetLanguageList"): self.language_list,
            ("CmdScreenManagement", "GetMovieClipLoaderLevel"): lambda args: LOADER_LEVEL,
            ("CmdScreenManagement", "GetMaxMovieClipLoaderLevels"): lambda args: LOADER_LEVEL_MAX,
            ("CmdScreenManagement", "MovieClipLoaded"): self.movie_clip_loaded,
            ("CmdScreenManagement", "MovieClipUnloaded"): lambda args: self.event("MovieClipUnloaded", args),
            ("CmdUtil", "MainScreenLoaded"): self.main_loaded,
            ("CmdUtil", "IsInputNecessary"): lambda args: True,
            ("EventTranslator", "OnScreenTick"): lambda args: VM.UNDEF,
            ("ScreenMessageTranslator", "GetMessage"): lambda args: self.queue.pop(0) if self.queue else None,
            ("MainMenu", "GetPauseTreeID"): lambda args: TREE_ID_PAUSE,
            ("CustMainMenu", "GetTreeID"): lambda args: TREE_ID_CUSTOMIZE,
            ("MenuTree", "GetTreeData"): self.tree_data,
            ("MenuTree", "Busy"): self.tree_busy,
            ("EventTranslator", "OnOptionSelected"): self.tree_open,
            ("EventTranslator", "OnTreePadRight"): self.tree_open,
            ("EventTranslator", "OnPadBack"): self.tree_back,
            ("EventTranslator", "OnPadLeft"): self.tree_back,
            ("EventTranslator", "OnOptionHighlighted"): lambda args: self.event("OnOptionHighlighted", args),
            ("cmdFEHintsTipsScreen", "GetToggleData"): self.hints_toggle_data,
            ("cmdFEHintsTipsScreen", "GetPageData"): self.hints_page_data,
        }
        for name in UI_EVENTS:
            self.providers[("EventTranslator", name)] = self.screen_event(name)

    # ---- install
    def install(self, vm):
        self.queue = []                                         # a reset of the movie starts with no message
        self.processing, self.events, self.loaded, self.main_screen_loaded = True, [], [], False
        self.carlot_region = 0
        self.trees, self.active_tree = {}, None
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
                if value is VM.UNDEF:                  # done, nothing to give back
                    return VM.UNDEF
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
    def localize(self, text):
        """Text the way the game draws it: a `~KEY` is the string of that name in the language tables (bStringHash of the key without the `~`);
        a key that is in no table stays as it is. Checked: ~COMMON_BACK is BACK (Global), ~WM_TRI_CITY_BAY is Tri-City Bay (Frontend)."""
        if self.language is None or "~" not in text:
            return text
        return re.sub(r"~([A-Za-z0-9_]+)", lambda m: self.language.text(m.group(1)) or m.group(0), text)

    # ---- car lot (the shop): stock from the attrib dump, names from the language tables
    CAR_REGIONS = ((0, "CAR_REGION_AMERICAN"), (1, "CAR_REGION_EUROPEAN"), (2, "CAR_REGION_JAPANESE"))

    def car_frontend(self, car):
        """The `frontend` collection of a car (its pvehicle row points at it): cost, region, manufacturer."""
        pv = self.attrib.collections("pvehicle").get(car)
        ref = pv.get("frontend") if pv else None
        name = ref.get("collection") if isinstance(ref, dict) else None
        return name, self.attrib.collections("frontend").get(name, {})

    CAR_MAKES = ("ALFA_ROMEO", "ASTON_MARTIN", "AUDI", "BMW", "BUGATTI", "CADILLAC", "CHEVROLET", "CORVETTE", "CHRYSLER", "DODGE", "FERRARI", "FORD", "HONDA", "HUMMER",
                 "JAGUAR", "KOENIGSEGG", "LAMBORGHINI", "LEXUS", "LOTUS", "MASERATI", "MAZDA", "MCLAREN", "MERCEDES", "MERCEDES_BENZ", "MITSUBISHI", "NISSAN",
                 "PAGANI", "PLYMOUTH", "PONTIAC", "PORSCHE", "RENAULT", "SHELBY", "SUBARU", "TOYOTA", "VOLKSWAGEN", "VOLVO", "AC", "AMC", "MINI", "OPEL", "SEAT")

    def car_name(self, car):
        """The name the lists show: the string CARNAME_<MAKE>_<MODEL> of the language tables (nfs.exe has the formats CARNAME_%s_%s and CARNAME_%s_%s_EUROPE),
        MODEL being the car's frontend collection in capitals ("AUDI_R8" = Audi R8). The tables hold hashes only, so the make is found by trying the makes
        of the game (CAR_MAKES); ASSUMED: the full name is what the list shows. Some cars also have CARNAME_<MODEL>_SHORT. Without a hit: the car's own name."""
        name, _fe = self.car_frontend(car)
        if not name or self.language is None:
            return car
        model = name.upper()
        for make in self.CAR_MAKES:
            text = self.language.text("CARNAME_%s_%s" % (make, model))
            if text:
                return text
        return self.language.text("CARNAME_%s_SHORT" % model) or car

    def carlot_stock(self):
        """The cars of the shop: fe_carlist.StockCars (59) of the attrib class car_list, with the region of each (0 American, 1 European, 2 Japanese)."""
        if self.attrib is None:
            return None
        stock = self.attrib.collections("car_list").get("fe_carlist", {}).get("StockCars")
        return [(it["collection"], self.car_frontend(it["collection"])[1].get("Region")) for it in stock["items"]] if isinstance(stock, dict) else None

    def carlot_regions(self, args):
        """GetRegions: one toggle entry per region, in the order of the enum. ASSUMED: the three regions that have stock (region 3 holds only vehicles that are not for sale)."""
        if self.language is None:
            return None
        return [{"index": i, "type": "Toggle", "text": self.language.text(key) or key, "regionEnum": n} for i, (n, key) in enumerate(self.CAR_REGIONS)]

    def carlot_cars(self, args):
        """GetCarList: the stock of the selected region, entries {index, carName}; `index` is the position CarHighlighted/CarSelected get back."""
        stock = self.carlot_stock()
        if stock is None:
            return None
        return [{"index": i, "carName": self.car_name(car)} for i, (car, _r) in enumerate(c for c in stock if c[1] == self.carlot_region)]

    def carlot_update_region(self, args):
        self.carlot_region = int(VM.to_number(args[0])) if args else 0
        return VM.UNDEF

    def year_info(self, args):
        """GetYearInfo (0x71F5B0): GetLocalTime, the year but at least 2008; max is that year and min is a hundred years before."""
        year = max(datetime.date.today().year, 2008)
        return {"min": year - 100, "max": year}

    def language_list(self, args):
        """GetLanguageList (0x539520) hands over the language names the game has built at run time (a count and a pointer array in its screen object).
        ASSUMED: those are the first 15 rows of the exe's language table (ENGLISH ... NORWEGIAN); the screen has 15 option fields (mMaxMenuOptions)
        and the table's next rows repeat ENGLISH and add the Asian languages."""
        table = self.exe_tables.get("languages")
        return [row[2] for row in table["entries"][:15]] if table else None

    def event(self, name, args=()):
        """A call the UI makes to tell the game something; kept for the viewer (the game's own reaction is not part of the UI)."""
        self.events.append((name, [VM.to_str(a) for a in args]))
        del self.events[:-200]
        return VM.UNDEF

    def enable_processing(self, args):
        """The handler (0x5393b0) stores its boolean argument in the translator; IsMessageAvailable (0x5393d0) is true only when the queue is not
        empty AND that flag is set. The root clears it after a PUSH and waits for the screen to say it is ready."""
        self.processing = bool(args) and VM.truthy(args[0])
        return VM.UNDEF

    def movie_clip_loaded(self, args):
        self.loaded.append((VM.to_str(args[0]) if args else "", VM.to_number(args[1]) if len(args) > 1 else 0.0))
        return self.event("MovieClipLoaded", args)

    def main_loaded(self, args):
        self.main_screen_loaded = True                          # the handler (0x806190) sets a flag in the game's screen manager
        return self.event("MainScreenLoaded", args)

    def screen_event(self, name):
        """OnEnterComplete, OnExitComplete and OnScreenDestructed set the translator's flag again (0x8065d0, 0x8065f0, 0x806630) and tell the game;
        OnScreenConstructed and OnScreenDestructing only tell the game."""
        def run(args):
            if name in UI_EVENTS_ENABLING:
                self.processing = True
            return self.event(name, args)
        return run

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

    # ---- menu trees (MenuTree.GetTreeData and the events of NFS_Tree): the levels come from the slot table of nfs.exe
    def _tree_groups(self):
        """The slot table (0xCB9140, 254 rows) is the table of the tree menus. Row: [item id, group, label hash, 0, 0]; a group is one level of a tree,
        its rows are the items in the order the tree shows them (checked against the game: group 0x11 is the Main list Cars ... Options, 0x8E is
        Paint, Vinyls, Aftermarket, 0x92 is Body ... Exhaust). The first three rows (group 7 and 8, no label) are not items. An item opens the group
        with its own id when there is one (0x2D Options -> group 0x2D, 0x92 Aftermarket -> group 0x92); the other items are leaves, where the game
        does something else (pushes a screen)."""
        table = self.exe_tables.get("slot table")
        groups = {}
        for item, group, label, _, _ in (table or {}).get("entries", ()):
            if int(label, 16):
                groups.setdefault(int(group, 0), []).append((int(item, 0), int(label, 16)))
        return groups

    def tree_items(self, group):
        """Items of a level: the rows of the table plus the ones the code adds. Visuals (0x8E) and Performance (0x8F) are built in code (0x5897BD,
        0x589819: node(id, text of the label hash)); that they come after the table rows of the Cars level is ASSUMED from the game's order."""
        rows = list(self.tree_groups.get(group, ())) + list(TREE_CODE_ITEMS.get(group, ()))
        return [r for r in rows if r[0] not in TREE_HIDDEN_ITEMS]

    def _first_selected(self, group):
        """Index highlighted when a level opens. ASSUMED 1 (the second item) from the game: Main starts on GPS Map, Cars on Shop, Visuals on Vinyls, Aftermarket on Hood."""
        return 1 if len(self.tree_items(group)) > 1 else 0

    def _tree(self, tree_id):
        t = self.trees.get(tree_id)
        if t is None and tree_id in TREE_ROOTS and self.tree_groups:
            t = self.trees[tree_id] = {"stack": [[TREE_ROOTS[tree_id], self._first_selected(TREE_ROOTS[tree_id]), None]], "pending": True}      # the exe sets the screen's data flag (+0x84) when it builds the tree
        return t

    @staticmethod
    def _int_arg(args, i):
        n = VM.to_number(args[i]) if len(args) > i else float("nan")
        return int(n) if n == n and abs(n) < 1e9 else None

    def tree_data(self, args):
        """GetTreeData (0x80BAF0): undefined unless the game has new data (flag +0x84), which the call clears; else {maItems, length, selected, mbScreenMode},
        each item {text, Enabled, id, data} as built at 0x808EC0 (data is -1 for the items seen). ASSUMED: every item is enabled, mbScreenMode is false."""
        tree_id = self._int_arg(args, 0)
        t = self._tree(tree_id) if tree_id is not None else None
        if t is None or not t["pending"]:
            return None
        t["pending"] = False
        self.active_tree = tree_id
        group, selected, screen_label = t["stack"][-1]
        rows = [(group, screen_label)] if screen_label is not None else self.tree_items(group)      # a screen level has one item, the leaf: its text is the newest lookback
        items = [{"text": self._label(h), "Enabled": True, "id": i, "data": -1} for i, h in rows]
        return {"maItems": items, "length": len(items), "selected": selected, "mbScreenMode": screen_label is not None}

    def _label(self, label_hash):
        text = self.language.text(label_hash) if self.language else None
        return text if text is not None else TREE_LABELS.get(label_hash, "0x%08X" % label_hash)

    def tree_busy(self, args):
        tree_id = self._int_arg(args, 0)
        t = self._tree(tree_id) if tree_id is not None else None
        if t is not None:
            t["busy"] = len(args) > 1 and VM.truthy(args[1])
        return VM.UNDEF

    def tree_open(self, args):
        """OnOptionSelected / OnTreePadRight (id, data): an item with a group of its own opens it (the script then asks for the data again and slides).
        A leaf in TREE_SCREEN_LEAVES pushes its screen and the tree goes to screen mode (the list gives way to the screen, the lookbacks stay).
        Other leaves only go to the event log: what the game does there is not known yet."""
        self.event("OnOptionSelected", args)
        t = self.trees.get(self.active_tree)
        item = self._int_arg(args, 0)
        if t is None or item is None:
            return VM.UNDEF
        here = self.tree_items(t["stack"][-1][0]) if t["stack"][-1][2] is None else []
        index = next((n for n, (i, _) in enumerate(here) if i == item), None)
        if index is None:
            return VM.UNDEF
        if item in self.tree_groups:
            t["stack"][-1][1] = index
            t["stack"].append([item, self._first_selected(item), None])
            t["pending"] = True
        elif item in TREE_SCREEN_LEAVES and self.screen_ids.get(TREE_SCREEN_LEAVES[item][0].lower()) is not None:
            name, category = TREE_SCREEN_LEAVES[item]
            if category:
                self.options["hints_category"] = category
            t["stack"][-1][1] = index
            t["stack"].append([item, 0, here[index][1]])
            t["pending"] = True
            self.post_message({"message": "PUSH", "screenId": self.screen_ids[name.lower()], "level": TREE_SCREEN_LEVEL})
        return VM.UNDEF

    def tree_back(self, args):
        """OnPadBack / OnPadLeft: one level up, with the item we came from selected (ASSUMED). At the top the game leaves the screen: not done here."""
        self.event("OnPadBack", args)
        t = self.trees.get(self.active_tree)
        if t is not None and len(t["stack"]) > 1:
            if t["stack"].pop()[2] is not None:
                self.post_message({"message": "POP", "level": TREE_SCREEN_LEVEL})
            t["pending"] = True
        return VM.UNDEF

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


LOADER_LEVEL, LOADER_LEVEL_MAX = 400, 450                      # the two constants returned by the handlers at 0x806540 and 0x806550
TREE_ID_PAUSE, TREE_ID_CUSTOMIZE = 0x27, 0x35                  # returned by the handlers at 0x5367C0 (MainMenu.GetPauseTreeID) and 0x5367D0 (CustMainMenu.GetTreeID)
TREE_ROOTS = {TREE_ID_PAUSE: 0x11, TREE_ID_CUSTOMIZE: 0x8B}    # first level of each tree: ASSUMED from the game's Main list (career, group 0x11) and the Cars list (0x8B)
TREE_CODE_ITEMS = {0x8B: ((0x8E, 0xA56C372E), (0x8F, 0x28A85C99))}    # Visuals, Performance
TREE_HIDDEN_ITEMS = (0x36,)                                    # Exit: ASSUMED hidden on PC, the game's Main list has 8 items and ends with Options
TREE_SCREEN_LEAVES = {0x91: ("PaintMenu", None)}               # leaf item id -> (screen it pushes, hints category); the exe does this in the event handler 0x54A9C0, the pairs are ASSUMED from the item and screen names
TREE_SCREEN_LEAVES.update({0xD5 + n: ("HintsTips", c) for n, c in enumerate(("hints_hud", "hints_pursuits", "hints_career", "hints_heroicdriving", "hints_online", "hints_quickrace", "hints_customization"))})    # Hints and Tips topics, in the order of the table and of the 7 collections of fe_hints_tips
TREE_LABELS = {0x824E664E: "Hints and Tips"}                   # labels whose hash is in none of the language tables: the text is the one in his game screenshot (ASSUMED); 0x3C26D196, 0x7D975F9D, 0x9B1EB517, 0xCB3AA30C, 0xD0C6E951 (Online levels) are still unknown
TREE_SCREEN_LEVEL = 10                                         # level (OVERLAY_n) a leaf screen is pushed to: ASSUMED, any free one
UI_EVENTS_ENABLING = ("OnEnterComplete", "OnExitComplete", "OnScreenDestructed")
UI_EVENTS = UI_EVENTS_ENABLING + ("OnScreenConstructed", "OnScreenDestructing")


def build(engine_map, lang_dir=None, attrib_dump=None, names=None, widescreen=True, hints_category="hints_career"):
    """An Engine from the paths the viewer was given; data that is not given is simply not available (calls then answer undefined)."""
    language = apt_data.Language(lang_dir) if lang_dir else None
    attrib = apt_data.Attrib(attrib_dump, names) if attrib_dump else None
    return Engine(engine_map, language, attrib, widescreen, hints_category)


DATA_NAMES = (("map", "apt_engine_map.json", "which natives nfs.exe registers (made by game_stuff/engine_map.py from nfs.exe)"),
              ("screens", "apt_screen_names.json", "screen id -> movie name, beside the map (made by the same tool; GetScreenName)"),
              ("lang", "English_*.bin", "text of the strings the natives hand to the screens (hints and tips ...)"),
              ("attrib", "undercover_global_dump.json", "attrib data (UndercoverVLT_Dumper output of the 4 vault files)"),
              ("names", "hashes_main.txt", "names for the attrib hashes (classes, fields, collections)"),
              ("exe", "nfs.exe", "only to remake apt_engine_map.json (game_stuff/engine_map.py), not needed to run the viewer"))


def find_data(data_dir=None):
    """Where the engine data is: {role: path or None}. The folder is Apt/engine_data (or data_dir); the engine map is also looked for in Apt/game_stuff."""
    here = os.path.dirname(os.path.abspath(__file__))
    d = data_dir or os.path.join(here, "engine_data")
    names = {n.lower(): n for n in os.listdir(d)} if os.path.isdir(d) else {}

    def one(*wanted):
        for w in wanted:
            if w.lower() in names:
                return os.path.join(d, names[w.lower()])
        return None
    out = {"dir": d, "map": one("apt_engine_map.json") or (os.path.join(here, "game_stuff", "apt_engine_map.json")
                                                          if os.path.exists(os.path.join(here, "game_stuff", "apt_engine_map.json")) else None),
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
