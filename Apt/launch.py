#!/usr/bin/env python3
"""
launch.py

Start the APT viewer without a command line. Double click it, or run: python launch.py
It keeps the paths in outputs/apt/launch_settings.json, so the next start has them again.

  UI            the folder with the UI .big files (or one .big file)
  Data folders  folders with .bun / .bin / .lzc files (textures, fonts, names). Add as many as you like.
                Each folder is searched with all its sub folders.
  Scan game     pick the game folder: finds the folders with .big files and the folders with data files
  Options       movie to open first, drawing backend, cache folder, overrides file, stubs file, decompressor

launch.py --last starts the viewer at once with the saved settings (no window).
"""
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
import outpaths

SETTINGS = os.path.join(outpaths.out_dir("apt"), "launch_settings.json")
DATA_EXT = (".bun", ".bin", ".lzc")
DEFAULTS = {"ui": "", "res": [], "movie": "", "renderer": "auto", "cache": "apt_cache", "overrides": "apt_overrides.json",
            "stubs": "apt_stubs.json", "decomp": "fng_jdlz:decompress", "game": ""}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
    except OSError:
        pass


def scan_game(root, stop=None):
    """Walk the game folder. Returns (ui_dirs, data_dirs): [(folder, file count)].
    ui_dirs: folders with .big files. data_dirs: the top most folders with data files (their sub folders come with them)."""
    ui, data = {}, {}
    for r, dirs, files in os.walk(root):
        if stop is not None and stop.is_set():
            break
        low = [f.lower() for f in files]
        big = sum(1 for f in low if f.endswith(".big"))
        dat = sum(1 for f in low if f.endswith(DATA_EXT))
        if big:
            ui[r] = big
        if dat:
            data[r] = dat
    tops = []
    for d in sorted(data, key=lambda p: (len(p), p)):                         # keep only the top most folder of a chain
        if not any(d == t or d.startswith(t + os.sep) for t, _n in tops):
            tops.append((d, sum(n for k, n in data.items() if k == d or k.startswith(d + os.sep))))
    return sorted(ui.items(), key=lambda kv: -kv[1]), tops


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
        self.q, self.stop = queue.Queue(), threading.Event()
        root.title("APT viewer launcher")
        root.geometry("760x640")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        pad = {"padx": 8, "pady": 3}

        box = ttk.LabelFrame(root, text="UI (.big files)")
        box.pack(fill="x", **pad)
        self.ui_var = tk.StringVar(value=self.s["ui"])
        ttk.Entry(box, textvariable=self.ui_var).pack(side="left", fill="x", expand=True, padx=4, pady=4)
        ttk.Button(box, text="Folder...", command=self.pick_ui_dir).pack(side="left", padx=2)
        ttk.Button(box, text="File...", command=self.pick_ui_file).pack(side="left", padx=(2, 4))

        box = ttk.LabelFrame(root, text="Data folders (.bun / .bin / .lzc, searched with their sub folders)")
        box.pack(fill="both", expand=True, **pad)
        self.lst = tk.Listbox(box, selectmode="extended", exportselection=False, height=8)
        sb = ttk.Scrollbar(box, command=self.lst.yview)
        self.lst.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.lst.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        for p in self.s["res"]:
            self.lst.insert("end", p)
        col = ttk.Frame(box)
        col.pack(side="left", fill="y", padx=4, pady=4)
        for text, cmd in (("Add folder...", self.add_dir), ("Add file...", self.add_file), ("Remove", self.remove), ("Clear", self.clear)):
            ttk.Button(col, text=text, command=cmd, width=14).pack(pady=2)

        box = ttk.LabelFrame(root, text="Scan the game")
        box.pack(fill="x", **pad)
        self.game_var = tk.StringVar(value=self.s["game"])
        ttk.Entry(box, textvariable=self.game_var).pack(side="left", fill="x", expand=True, padx=4, pady=4)
        ttk.Button(box, text="Folder...", command=self.pick_game).pack(side="left", padx=2)
        self.scan_btn = ttk.Button(box, text="Scan", command=self.scan)
        self.scan_btn.pack(side="left", padx=(2, 4))
        self.scan_msg = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.scan_msg, foreground="#666").pack(anchor="w", padx=12)

        box = ttk.LabelFrame(root, text="Options")
        box.pack(fill="x", **pad)
        self.vars = {}
        rows = (("movie", "Open movie first (optional)"), ("cache", "Cache folder"), ("overrides", "Overrides file"),
                ("stubs", "Stubs file"), ("decomp", "Decompressor (module:function)"))
        for i, (key, label) in enumerate(rows):
            ttk.Label(box, text=label).grid(row=i, column=0, sticky="w", padx=4, pady=1)
            self.vars[key] = tk.StringVar(value=self.s[key])
            ttk.Entry(box, textvariable=self.vars[key]).grid(row=i, column=1, sticky="ew", padx=4, pady=1)
        ttk.Label(box, text="Drawing").grid(row=len(rows), column=0, sticky="w", padx=4, pady=1)
        self.vars["renderer"] = tk.StringVar(value=self.s["renderer"])
        fr = ttk.Frame(box)
        fr.grid(row=len(rows), column=1, sticky="w", padx=4, pady=1)
        ttk.Combobox(fr, textvariable=self.vars["renderer"], values=("auto", "skia", "pillow"), width=8, state="readonly").pack(side="left")
        ttk.Label(fr, text="skia installed" if skia_installed() else "skia not installed (pip install skia-python): Pillow is used",
                  foreground="#666").pack(side="left", padx=8)
        box.columnconfigure(1, weight=1)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="Launch", command=self.launch).pack(side="right")
        ttk.Button(bar, text="Save", command=lambda: (self.collect(), save_settings(self.s))).pack(side="right", padx=6)
        ttk.Button(bar, text="Quit", command=self.quit).pack(side="left")
        self.root.after(150, self.poll)

    # ---- settings
    def collect(self):
        self.s.update(ui=self.ui_var.get().strip(), res=list(self.lst.get(0, "end")), game=self.game_var.get().strip())
        for k, v in self.vars.items():
            self.s[k] = v.get().strip()
        return self.s

    def quit(self):
        self.stop.set()
        self.collect()
        save_settings(self.s)
        self.root.destroy()

    # ---- pickers
    def _start(self, current):
        return current if current and os.path.exists(current) else (self.s.get("game") or HERE)

    def pick_ui_dir(self):
        d = filedialog.askdirectory(title="Folder with the UI .big files", initialdir=self._start(self.ui_var.get()))
        if d:
            self.ui_var.set(os.path.normpath(d))

    def pick_ui_file(self):
        f = filedialog.askopenfilename(title="One .big file", filetypes=[("BIG", "*.big"), ("All", "*.*")], initialdir=self._start(self.ui_var.get()))
        if f:
            self.ui_var.set(os.path.normpath(f))

    def pick_game(self):
        d = filedialog.askdirectory(title="Game folder", initialdir=self._start(self.game_var.get()))
        if d:
            self.game_var.set(os.path.normpath(d))

    def add_path(self, p):
        p = os.path.normpath(p)
        if p not in self.lst.get(0, "end"):
            self.lst.insert("end", p)

    def add_dir(self):
        d = filedialog.askdirectory(title="Data folder", initialdir=self._start(self.game_var.get()))
        if d:
            self.add_path(d)

    def add_file(self):
        for f in filedialog.askopenfilenames(title="Data files", filetypes=[("Data", "*.bun *.bin *.lzc"), ("All", "*.*")], initialdir=self._start(self.game_var.get())):
            self.add_path(f)

    def remove(self):
        for i in reversed(self.lst.curselection()):
            self.lst.delete(i)

    def clear(self):
        self.lst.delete(0, "end")

    # ---- scan (in a thread, so the window stays alive)
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
        self.scan_msg.set("Found %d folders with .big files, %d data folders." % (len(ui), len(data)))
        self.show_results(ui, data)
        self.root.after(150, self.poll)

    def show_results(self, ui, data):
        win = tk.Toplevel(self.root)
        win.title("Scan result")
        win.geometry("700x460")
        win.transient(self.root)
        ttk.Label(win, text="UI folder (the .big files)").pack(anchor="w", padx=8, pady=(8, 0))
        names = ["%s   (%d .big)" % kv for kv in ui]
        ui_var = tk.StringVar(value=names[0] if names else "")
        pref = next((n for n, (p, _c) in zip(names, ui) if "ui" in os.path.basename(p).lower()), None)
        if pref:
            ui_var.set(pref)
        ttk.Combobox(win, textvariable=ui_var, values=names, state="readonly").pack(fill="x", padx=8)
        ttk.Label(win, text="Data folders to add (select the ones you want)").pack(anchor="w", padx=8, pady=(8, 0))
        lb = tk.Listbox(win, selectmode="extended", exportselection=False)
        lb.pack(fill="both", expand=True, padx=8, pady=3)
        for p, n in data:
            lb.insert("end", "%s   (%d files)" % (p, n))
        lb.selection_set(0, "end")

        def apply():
            if names and ui_var.get() in names:
                self.ui_var.set(os.path.normpath(ui[names.index(ui_var.get())][0]))
            for i in lb.curselection():
                self.add_path(data[i][0])
            win.destroy()
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Button(bar, text="Use", command=apply).pack(side="right")
        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right", padx=6)

    # ---- start
    def launch(self):
        s = self.collect()
        if not s["ui"] or not os.path.exists(s["ui"]):
            messagebox.showerror("Launch", "The UI path is empty or does not exist.", parent=self.root)
            return
        missing = [p for p in s["res"] if not os.path.exists(p)]
        if missing and not messagebox.askyesno("Launch", "These data paths do not exist:\n\n%s\n\nStart without them?" % "\n".join(missing[:8]), parent=self.root):
            return
        s["res"] = [p for p in s["res"] if os.path.exists(p)]
        save_settings(s)
        self.stop.set()
        self.root.destroy()
        start(s)


def start(s):
    """Open the viewer with the settings. Relative cache, overrides and stubs paths go to outputs/apt."""
    os.chdir(HERE)
    sys.path.insert(0, HERE)
    try:
        import apt_view
    except ImportError as e:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("APT viewer", "Cannot start the viewer:\n%s\n\nThe viewer needs Pillow (pip install pillow)." % e)
        return
    apt_view.run(s["ui"], s["res"], s["decomp"] or None, s["movie"] or None, s["cache"] or "apt_cache",
                 s["overrides"] or "apt_overrides.json", s["stubs"] or "apt_stubs.json", s["renderer"] or "auto")


def main():
    os.chdir(HERE)
    if "--last" in sys.argv:
        s = load_settings()
        if s["ui"] and os.path.exists(s["ui"]):
            start(s)
            return
    root = tk.Tk()
    Launcher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
