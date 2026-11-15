#!/usr/bin/env python3
"""Launch the APT viewer with a file-picker GUI instead of command-line arguments.

Run ``python launch.py`` or double-click this file. Paths and options are saved in
``outputs/apt/launch_settings.json``. Run ``python launch.py --last`` to reuse them
without opening the launcher window.

The controls mirror the arguments accepted by apt_view.py, including its optional
engine feed (engine map, language tables, attrib dump and hash-name list).
"""
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "common"))
import outpaths

SETTINGS = os.path.join(outpaths.out_dir("apt"), "launch_settings.json")
DATA_EXT = (".bun", ".bin", ".lzc")
JSON_TYPES = (("JSON files", "*.json"), ("All files", "*.*"))
DEFAULTS = {
    "ui": "",
    "res": [],
    "movie": "",
    "renderer": "auto",
    "cache": "apt_cache",
    "overrides": "apt_overrides.json",
    "stubs": "apt_stubs.json",
    "decomp": "fng_jdlz:decompress",
    "game": "",
    "engine_map": "",
    "lang": "",
    "attrib": "",
    "names": "",
    "hints_category": "hints_career",
    "engine_data": "",
    "no_engine": False,
    "four_three": False,
}


def load_settings():
    """Load old and current launcher settings, filling in newly added defaults."""
    settings = dict(DEFAULTS)
    try:
        with open(SETTINGS, "r", encoding="utf-8") as stream:
            saved = json.load(stream)
        if isinstance(saved, dict):
            settings.update(saved)
    except (OSError, ValueError):
        pass
    if not isinstance(settings.get("res"), list):
        settings["res"] = []
    return settings


def save_settings(settings):
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=1)
    except OSError as exc:
        print("Could not save launcher settings: %s" % exc)


def scan_game(root, stop=None):
    """Return (UI folders, data folders), with nested data folders collapsed."""
    ui, data = {}, {}
    for current, _dirs, files in os.walk(root):
        if stop is not None and stop.is_set():
            break
        lower = [name.lower() for name in files]
        big_count = sum(name.endswith(".big") for name in lower)
        data_count = sum(name.endswith(DATA_EXT) for name in lower)
        if big_count:
            ui[current] = big_count
        if data_count:
            data[current] = data_count

    tops = []
    for folder in sorted(data, key=lambda path: (len(path), path)):
        if not any(folder == top or folder.startswith(top + os.sep) for top, _count in tops):
            count = sum(n for path, n in data.items() if path == folder or path.startswith(folder + os.sep))
            tops.append((folder, count))
    return sorted(ui.items(), key=lambda item: -item[1]), tops


def skia_installed():
    try:
        import skia  # noqa: F401
        return True
    except ImportError:
        return False


class Launcher:
    def __init__(self, root):
        self.root = root
        self.s = load_settings()
        self.q = queue.Queue()
        self.stop = threading.Event()

        root.title("APT viewer launcher")
        root.geometry("900x820")
        root.minsize(720, 560)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        # Keep the launch/save buttons visible while the inputs scroll.
        shell = ttk.Frame(root)
        shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(shell, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.form = ttk.Frame(canvas)
        self.form_window = canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.canvas = canvas
        self.form.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(self.form_window, width=event.width))
        root.bind_all("<MouseWheel>", self._mousewheel)

        pad = {"padx": 8, "pady": 4}

        # UI archives
        box = ttk.LabelFrame(self.form, text="UI (.big files)")
        box.pack(fill="x", **pad)
        self.ui_var = tk.StringVar(value=self.s["ui"])
        ttk.Entry(box, textvariable=self.ui_var).pack(side="left", fill="x", expand=True, padx=4, pady=4)
        ttk.Button(box, text="Folder...", command=self.pick_ui_dir).pack(side="left", padx=2)
        ttk.Button(box, text="File...", command=self.pick_ui_file).pack(side="left", padx=(2, 4))

        # Resources: apt_view.py accepts --res more than once; each entry may be a folder or file.
        box = ttk.LabelFrame(self.form, text="Resource paths (--res: .bun / .bin / .lzc files or folders)")
        box.pack(fill="both", expand=True, **pad)
        self.lst = tk.Listbox(box, selectmode="extended", exportselection=False, height=7)
        scroll = ttk.Scrollbar(box, command=self.lst.yview)
        self.lst.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.lst.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        for path in self.s["res"]:
            self.lst.insert("end", path)
        col = ttk.Frame(box)
        col.pack(side="left", fill="y", padx=4, pady=4)
        for label, command in (("Add folder...", self.add_dir), ("Add file...", self.add_file),
                               ("Remove", self.remove), ("Clear", self.clear)):
            ttk.Button(col, text=label, command=command, width=14).pack(pady=2)

        # Automatic game-folder discovery
        box = ttk.LabelFrame(self.form, text="Scan the game")
        box.pack(fill="x", **pad)
        self.game_var = tk.StringVar(value=self.s["game"])
        ttk.Entry(box, textvariable=self.game_var).pack(side="left", fill="x", expand=True, padx=4, pady=4)
        ttk.Button(box, text="Folder...", command=self.pick_game).pack(side="left", padx=2)
        self.scan_btn = ttk.Button(box, text="Scan", command=self.scan)
        self.scan_btn.pack(side="left", padx=(2, 4))
        self.scan_msg = tk.StringVar(value="")
        ttk.Label(self.form, textvariable=self.scan_msg, foreground="#666").pack(anchor="w", padx=12)

        # Standard arguments
        box = ttk.LabelFrame(self.form, text="Viewer options")
        box.pack(fill="x", **pad)
        self.vars = {}
        self._entry_row(box, 0, "movie", "Open movie first (--movie)",
                        browse=lambda: self.pick_movie(), button="Choose .big...")
        self._entry_row(box, 1, "cache", "Cache folder (--cache)",
                        browse=lambda: self.pick_dir("cache", "Cache folder"), button="Folder...")
        self._entry_row(box, 2, "overrides", "Overrides JSON (--overrides)",
                        browse=lambda: self.pick_file("overrides", "Overrides JSON", JSON_TYPES), button="File...")
        self._entry_row(box, 3, "stubs", "Stubs JSON (--stubs)",
                        browse=lambda: self.pick_file("stubs", "Stubs JSON", JSON_TYPES), button="File...")
        self._entry_row(box, 4, "decomp", "Decompressor (--decomp, module:function)")
        ttk.Label(box, text="Drawing backend (--renderer)").grid(row=5, column=0, sticky="w", padx=4, pady=2)
        self.vars["renderer"] = tk.StringVar(value=self.s["renderer"])
        renderer_row = ttk.Frame(box)
        renderer_row.grid(row=5, column=1, sticky="ew", padx=4, pady=2)
        ttk.Combobox(renderer_row, textvariable=self.vars["renderer"], values=("auto", "skia", "pillow"),
                     width=9, state="readonly").pack(side="left")
        ttk.Label(renderer_row,
                  text="skia installed" if skia_installed() else "skia not installed (pip install skia-python); Pillow will be used",
                  foreground="#666").pack(side="left", padx=8)
        box.columnconfigure(1, weight=1)

        # Engine arguments: mirrors the engine-related options in apt_view.py's CLI.
        box = ttk.LabelFrame(self.form, text="Game engine feed")
        box.pack(fill="x", **pad)
        self._entry_row(box, 0, "engine_map", "Engine map JSON (--engine-map)",
                        browse=lambda: self.pick_file("engine_map", "APT engine map", JSON_TYPES), button="File...")
        self._entry_row(box, 1, "lang", "Language tables folder (--lang)",
                        browse=lambda: self.pick_dir("lang", "Folder containing English_*.bin"), button="Folder...")
        self._entry_row(box, 2, "attrib", "Attrib dump JSON (--attrib)",
                        browse=lambda: self.pick_file("attrib", "Attrib dump", JSON_TYPES), button="File...")
        self._entry_row(box, 3, "names", "Hash names TXT (--names)",
                        browse=lambda: self.pick_file("names", "Hash names", (("Text files", "*.txt"), ("All files", "*.*"))),
                        button="File...")
        self._entry_row(box, 4, "hints_category", "Hints category (--hints-category)")
        self._entry_row(box, 5, "engine_data", "Engine data folder (--engine-data)",
                        browse=lambda: self.pick_dir("engine_data", "Engine data folder"), button="Folder...")

        flags = ttk.Frame(box)
        flags.grid(row=6, column=1, sticky="w", padx=4, pady=(5, 3))
        self.no_engine_var = tk.BooleanVar(value=bool(self.s["no_engine"]))
        self.four_three_var = tk.BooleanVar(value=bool(self.s["four_three"]))
        ttk.Checkbutton(flags, text="Skip default engine feed (--no-engine; explicit engine map takes priority)", variable=self.no_engine_var).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(flags, text="Force 4:3 (--4x3)", variable=self.four_three_var).pack(side="left")
        box.columnconfigure(1, weight=1)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="Launch", command=self.launch).pack(side="right")
        ttk.Button(bar, text="Save", command=self.save).pack(side="right", padx=6)
        ttk.Button(bar, text="Quit", command=self.quit).pack(side="left")

        self.root.after(150, self.poll)

    def _mousewheel(self, event):
        if self.canvas.winfo_exists():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _entry_row(self, parent, row, key, label, browse=None, button="Browse..."):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=4, pady=2)
        self.vars[key] = tk.StringVar(value=str(self.s.get(key, "")))
        ttk.Entry(parent, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", padx=4, pady=2)
        if browse:
            ttk.Button(parent, text=button, command=browse).grid(row=row, column=2, padx=4, pady=2)

    # ---- settings
    def collect(self):
        self.s.update(
            ui=self.ui_var.get().strip(),
            res=list(self.lst.get(0, "end")),
            game=self.game_var.get().strip(),
            no_engine=bool(self.no_engine_var.get()),
            four_three=bool(self.four_three_var.get()),
        )
        for key, variable in self.vars.items():
            self.s[key] = variable.get().strip()
        return self.s

    def save(self):
        self.collect()
        save_settings(self.s)
        self.scan_msg.set("Settings saved.")

    def quit(self):
        self.stop.set()
        self.collect()
        save_settings(self.s)
        try:
            self.root.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        self.root.destroy()

    # ---- picker helpers
    def _start(self, current=""):
        """File dialogs need a directory, even when the current value is a file."""
        if current and os.path.isdir(current):
            return current
        if current and os.path.isfile(current):
            return os.path.dirname(os.path.abspath(current))
        game = self.s.get("game", "")
        if game and os.path.isdir(game):
            return game
        return HERE

    def pick_file(self, key, title, filetypes):
        path = filedialog.askopenfilename(title=title, filetypes=filetypes,
                                          initialdir=self._start(self.vars[key].get()))
        if path:
            self.vars[key].set(os.path.normpath(path))

    def pick_dir(self, key, title):
        path = filedialog.askdirectory(title=title, initialdir=self._start(self.vars[key].get()))
        if path:
            self.vars[key].set(os.path.normpath(path))

    def pick_movie(self):
        path = filedialog.askopenfilename(title="Choose the movie's .big archive",
                                          filetypes=(("BIG archives", "*.big"), ("All files", "*.*")),
                                          initialdir=self._start(self.vars["movie"].get()))
        if path:
            # apt_view.py expects the archive's basename, not its complete path.
            self.vars["movie"].set(os.path.splitext(os.path.basename(path))[0])

    def pick_ui_dir(self):
        path = filedialog.askdirectory(title="Folder with the UI .big files",
                                       initialdir=self._start(self.ui_var.get()))
        if path:
            self.ui_var.set(os.path.normpath(path))

    def pick_ui_file(self):
        path = filedialog.askopenfilename(title="One UI .big file",
                                          filetypes=(("BIG archives", "*.big"), ("All files", "*.*")),
                                          initialdir=self._start(self.ui_var.get()))
        if path:
            self.ui_var.set(os.path.normpath(path))

    def pick_game(self):
        path = filedialog.askdirectory(title="Game folder", initialdir=self._start(self.game_var.get()))
        if path:
            self.game_var.set(os.path.normpath(path))

    def add_path(self, path):
        path = os.path.normpath(path)
        if path not in self.lst.get(0, "end"):
            self.lst.insert("end", path)

    def add_dir(self):
        path = filedialog.askdirectory(title="Resource folder", initialdir=self._start(self.game_var.get()))
        if path:
            self.add_path(path)

    def add_file(self):
        paths = filedialog.askopenfilenames(
            title="Resource files",
            filetypes=(("Resource files", "*.bun *.bin *.lzc"), ("All files", "*.*")),
            initialdir=self._start(self.game_var.get()),
        )
        for path in paths:
            self.add_path(path)

    def remove(self):
        for index in reversed(self.lst.curselection()):
            self.lst.delete(index)

    def clear(self):
        self.lst.delete(0, "end")

    # ---- game scan
    def scan(self):
        root = self.game_var.get().strip()
        if not os.path.isdir(root):
            messagebox.showerror("Scan", "Pick the game folder first.", parent=self.root)
            return
        self.scan_btn.configure(state="disabled")
        self.scan_msg.set("Scanning %s ..." % root)
        self.stop.clear()
        threading.Thread(target=lambda: self.q.put(scan_game(root, self.stop)), daemon=True).start()

    def poll(self):
        try:
            ui, data = self.q.get_nowait()
        except queue.Empty:
            self.root.after(150, self.poll)
            return
        self.scan_btn.configure(state="normal")
        self.scan_msg.set("Found %d folders with .big files and %d data folders." % (len(ui), len(data)))
        self.show_results(ui, data)
        self.root.after(150, self.poll)

    def show_results(self, ui, data):
        win = tk.Toplevel(self.root)
        win.title("Scan results")
        win.geometry("720x460")
        win.transient(self.root)

        ttk.Label(win, text="UI folder (contains .big archives)").pack(anchor="w", padx=8, pady=(8, 0))
        names = ["%s   (%d .big)" % item for item in ui]
        ui_var = tk.StringVar(value=names[0] if names else "")
        preferred = next((name for name, (path, _count) in zip(names, ui)
                          if "ui" in os.path.basename(path).lower()), None)
        if preferred:
            ui_var.set(preferred)
        ttk.Combobox(win, textvariable=ui_var, values=names, state="readonly").pack(fill="x", padx=8)

        ttk.Label(win, text="Resource folders to add (select any number)").pack(anchor="w", padx=8, pady=(8, 0))
        listing = tk.Listbox(win, selectmode="extended", exportselection=False)
        listing.pack(fill="both", expand=True, padx=8, pady=3)
        for path, count in data:
            listing.insert("end", "%s   (%d files)" % (path, count))
        if data:
            listing.selection_set(0, "end")

        def apply():
            if names and ui_var.get() in names:
                self.ui_var.set(os.path.normpath(ui[names.index(ui_var.get())][0]))
            for index in listing.curselection():
                self.add_path(data[index][0])
            win.destroy()

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=8, pady=8)
        ttk.Button(buttons, text="Use", command=apply).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=6)

    # ---- validation and launch
    def _validate_optional_path(self, settings, key, label, want_directory):
        path = settings.get(key, "")
        if not path:
            return True
        valid = os.path.isdir(path) if want_directory else os.path.isfile(path)
        if not valid:
            expected = "folder" if want_directory else "file"
            messagebox.showerror("Launch", "%s must point to an existing %s:\n%s" % (label, expected, path),
                                 parent=self.root)
            return False
        return True

    def launch(self):
        settings = self.collect()
        ui_path = settings["ui"]
        if not ui_path or not os.path.exists(ui_path):
            messagebox.showerror("Launch", "The UI path is empty or does not exist.", parent=self.root)
            return
        if os.path.isfile(ui_path) and not ui_path.lower().endswith(".big"):
            messagebox.showerror("Launch", "The UI file must be a .big archive.", parent=self.root)
            return

        missing = [path for path in settings["res"] if not os.path.exists(path)]
        if missing and not messagebox.askyesno(
                "Launch", "These resource paths do not exist:\n\n%s\n\nStart without them?" % "\n".join(missing[:8]),
                parent=self.root):
            return
        settings["res"] = [path for path in settings["res"] if os.path.exists(path)]

        path_checks = (
            ("engine_map", "Engine map (--engine-map)", False),
            ("lang", "Language tables (--lang)", True),
            ("attrib", "Attrib dump (--attrib)", False),
            ("names", "Hash names (--names)", False),
            ("engine_data", "Engine data (--engine-data)", True),
        )
        for key, label, is_dir in path_checks:
            if not self._validate_optional_path(settings, key, label, is_dir):
                return

        save_settings(settings)
        self.stop.set()
        self.root.destroy()
        start(settings)


def _show_start_error(message):
    dialog_root = tk.Tk()
    dialog_root.withdraw()
    messagebox.showerror("APT viewer", str(message), parent=dialog_root)
    dialog_root.destroy()


def start(settings):
    """Apply the same engine setup as apt_view.py's CLI, then open the viewer."""
    os.chdir(HERE)
    sys.path.insert(0, HERE)
    try:
        import apt_view
        import apt_engine
        import apt_render
        import apt_runtime
    except ImportError as exc:
        _show_start_error("Cannot import the APT viewer:\n%s\n\nCheck its Python dependencies (including Pillow)." % exc)
        return

    try:
        hints_category = settings.get("hints_category") or "hints_career"
        widescreen = not bool(settings.get("four_three", False))
        engine_map = settings.get("engine_map") or None

        # Match apt_view.main(): a supplied map uses the explicitly selected data;
        # otherwise the engine-data folder (or Apt/engine_data) is auto-detected.
        if engine_map:
            apt_runtime.ENGINE = apt_engine.build(
                engine_map,
                settings.get("lang") or None,
                settings.get("attrib") or None,
                settings.get("names") or None,
                widescreen,
                hints_category,
            )
        elif not settings.get("no_engine", False):
            apt_runtime.ENGINE = apt_engine.build_default(
                settings.get("engine_data") or None,
                widescreen,
                hints_category,
            )
        else:
            apt_runtime.ENGINE = None

        apt_render.LOCALIZE = (
            apt_runtime.ENGINE.localize
            if apt_runtime.ENGINE is not None and apt_runtime.ENGINE.language is not None
            else None
        )
        print("engine feed: %s" % ("on" if apt_runtime.ENGINE is not None else "off"))

        apt_view.run(
            settings["ui"],
            settings.get("res", []),
            settings.get("decomp") or None,
            settings.get("movie") or None,
            settings.get("cache") or "apt_cache",
            settings.get("overrides") or "apt_overrides.json",
            settings.get("stubs") or "apt_stubs.json",
            settings.get("renderer") or "auto",
        )
    except (Exception, SystemExit) as exc:
        _show_start_error("Could not start the APT viewer:\n%s" % exc)


def main():
    os.chdir(HERE)
    if "--last" in sys.argv:
        settings = load_settings()
        if settings["ui"] and os.path.exists(settings["ui"]):
            start(settings)
            return
    root = tk.Tk()
    Launcher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
