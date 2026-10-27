#!/usr/bin/env python3
"""
apt_view.py

Viewer for the NFS Undercover APT UI movies (UI/*.big), to find out what the missing images are.
Needs: apt_scan.py, apt_movie.py, apt_font.py, apt_resources.py, apt_render.py, common/fng_tpk.py and Pillow.
Cache, overrides and stubs files are written to outputs/apt.

  python apt_view.py UI_DIR --res FRONTEND_DIR --res GLOBAL_DIR [--decomp fng_jdlz:decompress] [--movie customizecar]

Left      movies (images found / total, missing), filter box
Middle    the picture of one frame. Click an object to see what it is. Arrow keys change the frame.
          Magenta = no pack has the texture, cyan = dynamic pack (not decoded), orange = no name entry in the .big,
          red = texture found but not decodable, yellow cross = imported character or type not drawn.
Right     Selected (details), Images (double click: choose a texture by hand), Characters
Runtime     tick the Runtime box to run the scripts of the movie (apt_vm.py): Play/Step/Reset, a seed for Math.random,
            the bar at the bottom scrubs ticks (going back re-runs from tick 0). Label/Frame apply to the static view.
            Click an object to inspect its clip chain, matrices, variables and the formulas that wrote its values.
            Tabs: Archive (BIG entries), Timeline (frames and controls), Scripts (disassembly), Behavior (pseudo-code).
            Grid / Anchors / Names draw the stage grid (100 px), clip origins and instance names.
Manual choices are saved in outputs/apt/apt_overrides.json ("<movie>|<image id>" -> texture name).
The frame starts at the label 'Start' (or the first label that is not 'enter') because frame 0 is mostly empty.
"""
import argparse
import importlib
import os
import sys
import time
import tkinter as tk
from tkinter import ttk

from PIL import ImageTk

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "common"))
import outpaths
import apt_inspect
import apt_movie
import apt_render
import apt_resources
import apt_runtime
import apt_stub_ui
import apt_hints
import apt_stubs

STATUS_TAGS = {"found": "#1b7f2b", "override": "#2a5db0", "dynamic": "#00829b", "missing": "#b0008a",
               "unnamed": "#c86400", "undecodable": "#c00000"}


def make_renderer(res, backend="auto"):
    """skia when installed (much faster), Pillow otherwise or when asked."""
    if backend in ("auto", "skia"):
        try:
            import apt_render_skia
            return apt_render_skia.SkiaRenderer(res)
        except ImportError:
            if backend == "skia":
                sys.exit("--renderer skia needs: pip install skia-python")
    return apt_render.Renderer(res)


class App:
    def __init__(self, root, ui_dir, res, backend="auto", stubs="apt_stubs.json"):
        self.root, self.res = root, res
        self.stub_store = apt_stubs.StubStore(stubs)
        self.hints = apt_hints.Hints(os.path.join(os.path.dirname(os.path.abspath(stubs)), "apt_hints.json"))   # local, see apt_hints.py
        self.renderer = make_renderer(res, backend)
        self.movies = {}                 # name -> Movie or error text
        self.movie = None
        self.hits = []
        self.photo = None
        self.rt = None                    # apt_runtime.Runtime of the current movie (runtime mode)
        self.playing = False
        self.streams, self.assign = [], {}
        self.last_tick_time = 0.0
        self.tick_acc = 0.0
        self.t_step = 0.0
        self.horizon = 600                # ticks shown on the timeline
        root.title("APT viewer")
        root.geometry("1500x860")

        files = [ui_dir] if os.path.isfile(ui_dir) else sorted(
            os.path.join(r, f) for r, _, fs in os.walk(ui_dir) for f in fs if f.lower().endswith(".big"))
        self.paths = {os.path.basename(p): p for p in files}

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=4, pady=3)
        ttk.Label(bar, text="Label").pack(side="left")
        self.label_var = tk.StringVar()
        self.label_box = ttk.Combobox(bar, textvariable=self.label_var, width=24, state="readonly")
        self.label_box.pack(side="left", padx=4)
        self.label_box.bind("<<ComboboxSelected>>", self.on_label)
        ttk.Label(bar, text="Frame").pack(side="left")
        self.frame_var = tk.IntVar(value=0)
        self.frame_spin = tk.Spinbox(bar, from_=0, to=0, width=5, textvariable=self.frame_var, command=self.redraw)
        self.frame_spin.pack(side="left", padx=4)
        self.frame_spin.bind("<Return>", lambda e: self.redraw())
        ttk.Label(bar, text="Zoom").pack(side="left", padx=(10, 0))
        self.zoom_var = tk.StringVar(value="0.75")
        zoom = ttk.Combobox(bar, textvariable=self.zoom_var, width=5, values=["0.5", "0.75", "1.0", "1.5", "2.0"], state="readonly")
        zoom.pack(side="left", padx=4)
        zoom.bind("<<ComboboxSelected>>", lambda e: self.redraw())
        self.boxes_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Boxes", variable=self.boxes_var, command=self.redraw).pack(side="left", padx=10)
        self.loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Loop sprites", variable=self.loop_var, command=self.redraw).pack(side="left", padx=6)
        self.grid_var, self.anchor_var, self.label_vis_var = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()
        for text, var in (("Grid", self.grid_var), ("Anchors", self.anchor_var), ("Names", self.label_vis_var)):
            ttk.Checkbutton(bar, text=text, variable=var, command=self.redraw).pack(side="left", padx=3)

        bar2 = ttk.Frame(root)                  # runtime controls on their own row, so the window can be narrow
        bar2.pack(fill="x", padx=4, pady=(0, 3))
        self.rt_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar2, text="Runtime", variable=self.rt_var, command=self.on_runtime_toggle).pack(side="left")
        ttk.Label(bar2, text="Seed").pack(side="left", padx=(8, 0))
        self.seed_var = tk.StringVar(value="1")
        seed = ttk.Entry(bar2, textvariable=self.seed_var, width=6)
        seed.pack(side="left", padx=3)
        seed.bind("<Return>", lambda e: self.rt_reset())
        self.play_btn = ttk.Button(bar2, text="Play", width=6, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=2)
        ttk.Button(bar2, text="Step", width=5, command=self.rt_step).pack(side="left")
        ttk.Button(bar2, text="Reset", width=6, command=self.rt_reset).pack(side="left", padx=2)
        ttk.Label(bar2, text="Space: play/pause   Left/Right: step   Wheel: scroll (Shift: sideways)",
                  foreground="#888").pack(side="left", padx=12)
        self.status = tk.StringVar(value="")

        bottom = ttk.Frame(root)
        bottom.pack(side="bottom", fill="x")
        ttk.Label(bottom, textvariable=self.status).pack(fill="x", padx=6)
        self.tl = tk.Canvas(bottom, height=48, background="#181818", highlightthickness=0)
        self.tl.pack(fill="x")
        self.tl.bind("<Button-1>", self.on_timeline)
        self.tl.bind("<B1-Motion>", self.on_timeline)
        self.tl.bind("<Configure>", lambda e: self.draw_timeline())
        self.tl_head = None                                   # playhead items, moved each tick
        self.tl_movie = None

        panes = ttk.Panedwindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes, width=260)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.fill_movies())
        ttk.Entry(left, textvariable=self.filter_var).pack(fill="x", padx=2, pady=2)
        self.movie_tree = ttk.Treeview(left, columns=("found", "total", "missing"), show="tree headings", selectmode="browse")
        self.movie_tree.heading("#0", text="Movie")
        self.movie_tree.column("#0", width=150)
        for c, t in (("found", "ok"), ("total", "all"), ("missing", "miss")):
            self.movie_tree.heading(c, text=t)
            self.movie_tree.column(c, width=42, anchor="e")
        sb = ttk.Scrollbar(left, command=self.movie_tree.yview)
        self.movie_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.movie_tree.pack(fill="both", expand=True)
        self.movie_tree.bind("<<TreeviewSelect>>", self.on_movie)
        panes.add(left, weight=0)

        mid = ttk.Frame(panes)
        self.canvas = tk.Canvas(mid, background="#202020", highlightthickness=0)
        hs = ttk.Scrollbar(mid, orient="horizontal", command=self.canvas.xview)
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hs.set, yscrollcommand=vs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self.canvas.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.canvas_img, self.canvas_size = None, None        # one image item, updated in place
        panes.add(mid, weight=1)

        right = ttk.Notebook(panes, width=430)
        self.info = tk.Text(right, wrap="word", font=("Consolas", 9), state="disabled")
        right.add(self.info, text="Selected")
        self.img_tree = ttk.Treeview(right, columns=("status", "png", "texture", "pack"), show="tree headings")
        self.img_tree.heading("#0", text="Image")
        self.img_tree.column("#0", width=70)
        for c, w in (("status", 70), ("png", 110), ("texture", 110), ("pack", 150)):
            self.img_tree.heading(c, text=c)
            self.img_tree.column(c, width=w)
        for status, color in STATUS_TAGS.items():
            self.img_tree.tag_configure(status, foreground=color)
        self.img_tree.bind("<Double-1>", self.on_assign)
        right.add(self.img_tree, text="Images")
        self.char_tree = ttk.Treeview(right, columns=("type", "detail"), show="tree headings")
        self.char_tree.heading("#0", text="Id")
        self.char_tree.column("#0", width=50)
        self.char_tree.heading("type", text="type")
        self.char_tree.column("type", width=70)
        self.char_tree.heading("detail", text="detail")
        self.char_tree.column("detail", width=260)
        right.add(self.char_tree, text="Characters")
        self.archive_txt = self._text_tab(right, "Archive")
        self.timeline_txt = self._text_tab(right, "Timeline")
        scripts = ttk.Panedwindow(right, orient="vertical")
        self.script_list = tk.Listbox(scripts, height=8, exportselection=False, font=("Consolas", 9))
        self.script_list.bind("<<ListboxSelect>>", self.on_script)
        scripts.add(self.script_list, weight=0)
        self.script_txt = tk.Text(scripts, wrap="none", font=("Consolas", 9), state="disabled")
        scripts.add(self.script_txt, weight=1)
        right.add(scripts, text="Scripts")
        self.behavior_txt = self._text_tab(right, "Behavior")
        self.stub_panel = apt_stub_ui.StubPanel(right, self)
        right.add(self.stub_panel, text="Stubs")
        panes.add(right, weight=0)

        root.bind("<Left>", lambda e: self.step(-1))
        root.bind("<Right>", lambda e: self.step(1))
        root.bind("<space>", self.on_space)
        self.fill_movies()

    def _text_tab(self, notebook, title):
        frame = ttk.Frame(notebook)
        txt = tk.Text(frame, wrap="none", font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(frame, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        notebook.add(frame, text=title)
        return txt

    @staticmethod
    def _put(txt, text):
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("1.0", text)
        txt.configure(state="disabled")

    # ---- movies
    def load(self, name):
        mv = self.movies.get(name)
        if mv is None:
            try:
                mv = apt_movie.load(self.paths[name])
            except Exception as e:      # shown in the list, the viewer goes on
                mv = "error: %s" % e
            self.movies[name] = mv
        return mv

    def counts(self, mv):
        ids = mv.image_ids()
        st = [self.res.peek(mv, i) for i in ids]
        ok = sum(1 for s in st if s in ("found", "override"))
        return ok, len(st), len(st) - ok

    def fill_movies(self):
        flt = self.filter_var.get().lower()
        self.movie_tree.delete(*self.movie_tree.get_children())
        for name in self.paths:
            if flt and flt not in name.lower():
                continue
            mv = self.load(name)
            if isinstance(mv, str):
                self.movie_tree.insert("", "end", iid=name, text=name, values=("", "", mv))
            else:
                ok, total, miss = self.counts(mv)
                self.movie_tree.insert("", "end", iid=name, text=name[:-4], values=(ok, total, miss))

    def on_movie(self, _e=None):
        sel = self.movie_tree.selection()
        if not sel:
            return
        mv = self.load(sel[0])
        if isinstance(mv, str):
            self.status.set(mv)
            return
        self.movie = mv
        labels = []
        for i, frame in enumerate(mv.frames):
            for c in frame:
                if c["op"] == "label":
                    labels.append((i, c["label"]))
        self.labels = labels
        self.label_box.configure(values=["%d: %s" % (i, n) for i, n in labels])
        self.frame_spin.configure(to=max(0, len(mv.frames) - 1))
        start = next((i for i, n in labels if n.lower() in ("start", "idle", "main", "show")), None)
        if start is None:
            start = next((i for i, n in labels if n.lower() != "enter"), 0)
        self.frame_var.set(start)
        self.label_var.set(next(("%d: %s" % (i, n) for i, n in labels if i == start), ""))
        self.fill_images()
        self.fill_chars()
        self.playing = False
        self.play_btn.configure(text="Play")
        self.rt = None
        self.streams, self.assign = apt_inspect.streams_of(mv)
        self.fill_tabs()
        if self.rt_var.get():
            self.rt_reset()
        else:
            self.redraw()

    def on_label(self, _e=None):
        text = self.label_var.get()
        if text:
            self.frame_var.set(int(text.split(":")[0]))
            self.redraw()

    def step(self, d):
        if self.movie is None:
            return
        self.frame_var.set(max(0, min(len(self.movie.frames) - 1, self.frame_var.get() + d)))
        self.redraw()

    # ---- drawing
    def redraw(self):
        mv = self.movie
        if mv is None:
            return
        t_start = time.perf_counter()
        scale = float(self.zoom_var.get())
        self.renderer.boxes = self.boxes_var.get()
        self.renderer.loop_sprites = self.loop_var.get()
        self.renderer.grid, self.renderer.anchors, self.renderer.labels = self.grid_var.get(), self.anchor_var.get(), self.label_vis_var.get()
        if self.rt_var.get() and self.rt is not None:
            img, self.hits = self.renderer.render_runtime(self.rt, scale)
            labels = [c["label"] for c in mv.frames[self.rt.root.frame] if c["op"] == "label"] if mv.frames else []
            head = "%s  tick %d  root frame %d/%d %s" % (mv.name, self.rt.tick, self.rt.root.frame, len(mv.frames) - 1, labels or "")
        else:
            try:
                frame = int(self.frame_var.get())
            except (tk.TclError, ValueError):
                frame = 0
            img, self.hits, labels = self.renderer.render(mv, frame, scale)
            head = "%s  frame %d/%d %s" % (mv.name, frame, len(mv.frames) - 1, labels or "")
        self.scale = scale
        t_drawn = time.perf_counter()
        rgb = img.convert("RGB")
        if self.photo is not None and self.canvas_size == rgb.size and self.canvas_img is not None:
            self.photo.paste(rgb)                                 # same size: no new image, no flicker
        else:
            self.photo = ImageTk.PhotoImage(rgb)
            if self.canvas_img is None:
                self.canvas_img = self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
            else:
                self.canvas.itemconfigure(self.canvas_img, image=self.photo)
            self.canvas.configure(scrollregion=(0, 0, rgb.width, rgb.height))
            self.canvas_size = rgb.size
        self.canvas.delete("sel")
        st = self.renderer.stats
        t_shown = time.perf_counter()
        timing = "step %.1f  draw %.1f  show %.1f ms" % (getattr(self, "t_step", 0.0), (t_drawn - t_start) * 1000.0, (t_shown - t_drawn) * 1000.0)
        self.status.set("%s  |  %s  |  %s  |  %s" % (head, getattr(self.renderer, "backend", "pillow"), timing,
                                                    "  ".join("%s %d" % kv for kv in sorted(st.items())) or "no images drawn"))
        if self.tl_movie is not mv:                               # new movie: draw the static layer of the bar once
            self.tl_movie = mv
            self.draw_timeline()
        else:
            self.move_playhead()

    def on_click(self, e):
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        found = [h for h in self.hits if h[0][0] <= x <= h[0][2] and h[0][1] <= y <= h[0][3]]
        self.canvas.delete("sel")
        if not found:
            return
        bbox, info = found[-1]
        self.canvas.create_rectangle(*bbox, outline="#ffff00", width=2, tags="sel")
        if self.rt_var.get() and self.rt is not None and info.get("mc") is not None:
            text = apt_inspect.inspector_text(self.rt, info, self.assign)
            for i in info.get("images", []):
                im = self.res.image(self.movie, i)
                text += "\n\nimage %d  %s  entry %d  %s  texture %s  %s" % (i, im["status"].upper(), i + 1, im["png"], im["texture"],
                                                                         "[art]" if im["status"] in ("found", "override") else "")
            self.set_info(text)
            return
        lines = ["%s  (character %d)" % (info["kind"], info["char"]), "path: " + info["path"]]
        if info.get("name"):
            lines.append("instance name: " + info["name"])
        for key in ("detail", "text", "variable", "font", "height"):
            if info.get(key) not in (None, ""):
                lines.append("%s: %s" % (key, info[key]))
        for i in info.get("images", []):
            im = self.res.image(self.movie, i)
            lines.append("")
            lines.append("image %d  %s" % (i, im["status"].upper()))
            lines.append("  entry %d  png %s" % (i + 1, im["png"]))
            lines.append("  texture %s" % im["texture"])
            if im["pack"]:
                lines.append("  pack %s" % im["pack"])
            if im["note"]:
                lines.append("  " + im["note"])
        if len(found) > 1:
            lines += ["", "also under the pointer (top first):"] + ["  %s #%d" % (h[1]["kind"], h[1]["char"]) for h in reversed(found[:-1])]
        self.set_info("\n".join(lines))

    def set_info(self, text):
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("1.0", text)
        self.info.configure(state="disabled")

    # ---- lists
    def fill_images(self):
        self.img_tree.delete(*self.img_tree.get_children())
        for i, how in sorted(self.movie.image_ids().items()):
            s = self.res.peek(self.movie, i)
            png = self.movie.png_name(i)
            key = "%s|%d" % (self.movie.name, i)
            self.img_tree.insert("", "end", iid=str(i), text="%d (%s)" % (i, how), tags=(s,),
                                 values=(s, png or "", self.res.overrides.get(key) or (png.rsplit(".", 1)[0] if png else ""), ""))

    def fill_chars(self):
        self.char_tree.delete(*self.char_tree.get_children())
        for cid, c in sorted(self.movie.chars.items()):
            if c["type"] == "Text":
                d = "%r  var %r" % (c["text"], c["variable"])
            elif c["type"] == "Font":
                d = c["name"] or ""
            elif c["type"] == "Shape":
                d = "images %s" % sorted({b["image"] for b in (c.get("blocks") or []) if b["image"]}) if c.get("blocks") \
                    else "geometry not decoded"
            elif c["type"] == "Sprite":
                d = "%d frame(s)" % len(c["frames"])
            else:
                d = ""
            self.char_tree.insert("", "end", text=str(cid), values=(c["type"], d))
        for imp in self.movie.imports:
            self.char_tree.insert("", "end", text=str(imp["id"]), values=("Import", "%s / %s" % (imp["movie"], imp["name"])))

    # ---- inspection tabs
    def fill_tabs(self):
        mv = self.movie
        self._put(self.archive_txt, apt_inspect.archive_text(mv))
        self._put(self.timeline_txt, apt_inspect.timeline_text(mv, self.streams))
        self.script_list.delete(0, "end")
        for line in apt_inspect.script_listing(self.streams):
            self.script_list.insert("end", line)
        self._put(self.script_txt, "Select a stream above.")
        self.refresh_behavior()

    def refresh_behavior(self):
        self._put(self.behavior_txt, apt_inspect.behavior_text(self.movie, self.streams, self.rt))

    def on_script(self, _e=None):
        sel = self.script_list.curselection()
        if sel:
            self._put(self.script_txt, apt_inspect.disassembly_text(self.streams[sel[0]]))

    def on_space(self, e):
        if isinstance(e.widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Spinbox, tk.Text)):
            return                                                # typing: leave the key alone
        self.toggle_play()

    # ---- runtime
    def on_runtime_toggle(self):
        if self.rt_var.get():
            self.rt_reset()
        else:
            self.playing = False
            self.play_btn.configure(text="Play")
            self.redraw()

    def rt_reset(self):
        if self.movie is None:
            return
        try:
            seed = int(self.seed_var.get())
        except ValueError:
            seed = 1
            self.seed_var.set("1")
        self.playing = False
        self.play_btn.configure(text="Play")
        cfg = self.stub_store.get(self.movie.name)
        self.rt = apt_runtime.Runtime(self.movie, seed, cfg["stubs"], cfg["scenario"] if self.stub_panel.run_var.get() else [])
        self.rt_var.set(True)
        self.redraw()
        self.refresh_behavior()
        self.stub_panel.refresh()

    def rt_step(self, n=1):
        if self.rt is None:
            self.rt_reset()
        self.rt.step(n)
        self.redraw()
        self.stub_panel.refresh()

    def toggle_play(self):
        if self.rt is None:
            self.rt_reset()
        self.playing = not self.playing
        self.play_btn.configure(text="Pause" if self.playing else "Play")
        if not self.playing:
            self.stub_panel.refresh()
        if self.playing:
            self.last_tick_time = time.time()
            self.tick_acc = 0.0                                   # milliseconds that did not make a whole tick yet
            self.root.after(1, self.play_loop)

    def play_loop(self):
        """Keeps up with the movie clock: ticks are exact, the picture skips ticks when drawing is slower than 16 ms."""
        if not self.playing or self.rt is None:
            return
        now = time.time()
        ms = max(1, self.movie.ms)
        self.tick_acc += (now - self.last_tick_time) * 1000.0
        self.last_tick_time = now
        k = int(self.tick_acc // ms)
        if k > 12:                                                # far behind: drop the rest, keep the clock
            k, self.tick_acc = 12, 0.0
        else:
            self.tick_acc -= k * ms
        if k:                                                     # draw only when a tick is due
            t0 = time.perf_counter()
            self.rt.step(k)
            self.t_step = (time.perf_counter() - t0) * 1000.0
            self.redraw()
        self.root.after(max(1, int(ms - self.tick_acc)), self.play_loop)     # wait for the next tick

    # ---- timeline bar
    def draw_timeline(self):
        c = self.tl
        c.delete("all")
        mv = self.movie
        if mv is None:
            return
        w = max(50, c.winfo_width())
        n = len(mv.frames)
        sx = lambda t: 8 + (w - 16) * min(t, self.horizon) / float(self.horizon)
        c.create_rectangle(sx(0), 22, sx(n), 36, fill="#2c4a2c", outline="")
        c.create_text(sx(n) + 4, 29, text="root %d frames" % n, anchor="w", fill="#8a8a8a", font=("Segoe UI", 8))
        for t in range(0, self.horizon + 1, 60):
            c.create_line(sx(t), 36, sx(t), 42, fill="#666")
            c.create_text(sx(t), 45, text=str(t), fill="#777", font=("Segoe UI", 7))
        for fi, ctls in enumerate(mv.frames):
            for ctl in ctls:
                if ctl["op"] == "label":
                    c.create_line(sx(fi), 6, sx(fi), 22, fill="#d0a030")
                    c.create_text(sx(fi) + 2, 10, text=ctl["label"], anchor="w", fill="#d0a030", font=("Segoe UI", 8))
        self.tl_head = None
        self.move_playhead()

    def move_playhead(self):
        """Per tick work of the timeline bar: only the red line and the right hand text move."""
        c, mv = self.tl, self.movie
        if mv is None:
            return
        w = max(50, c.winfo_width())
        tick = self.rt.tick if self.rt is not None else 0
        x = 8 + (w - 16) * min(tick, self.horizon) / float(self.horizon)
        text = ("tick %d   root frame %d" % (tick, self.rt.root.frame) if self.rt else "static: frame %d" % self.frame_var.get())
        if self.tl_head is None:
            self.tl_head = (c.create_line(x, 2, x, 46, fill="#ff4040", width=2),
                            c.create_text(w - 6, 10, anchor="e", fill="#ddd", font=("Segoe UI", 8), text=text))
        else:
            c.coords(self.tl_head[0], x, 2, x, 46)
            c.coords(self.tl_head[1], w - 6, 10)
            c.itemconfigure(self.tl_head[1], text=text)

    def on_timeline(self, e):
        if self.movie is None:
            return
        w = max(50, self.tl.winfo_width())
        t = int(round(max(0.0, min(1.0, (e.x - 8) / float(w - 16))) * self.horizon))
        if self.rt_var.get():
            if self.rt is None:
                self.rt_reset()
            self.rt.seek(t)               # going back re-runs from tick 0 with the same seed
        else:
            self.frame_var.set(min(t, len(self.movie.frames) - 1))
        self.redraw()

    # ---- manual choice
    def on_assign(self, _e=None):
        sel = self.img_tree.selection()
        if not sel or self.movie is None:
            return
        image_id = int(sel[0])
        names = self.res.texture_names()
        win = tk.Toplevel(self.root)
        win.title("Texture for image %d of %s" % (image_id, self.movie.name))
        win.geometry("360x520")
        var = tk.StringVar()
        ttk.Entry(win, textvariable=var).pack(fill="x", padx=4, pady=4)
        box = tk.Listbox(win, activestyle="dotbox")
        box.pack(fill="both", expand=True, padx=4)

        def fill(*_a):
            box.delete(0, "end")
            for n in names:
                if var.get().lower() in n.lower():
                    box.insert("end", n)
        var.trace_add("write", fill)
        fill()

        def done(clear=False):
            if clear:
                self.res.set_override(self.movie, image_id, None)
            elif box.curselection():
                self.res.set_override(self.movie, image_id, box.get(box.curselection()[0]))
            win.destroy()
            self.fill_images()
            self.fill_movies()
            self.redraw()
        row = ttk.Frame(win)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Use selected", command=done).pack(side="left", padx=4)
        ttk.Button(row, text="Clear choice", command=lambda: done(True)).pack(side="left")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right", padx=4)
        box.bind("<Double-1>", lambda e: done())


def run(ui, res_dirs, decomp=None, movie=None, cache="apt_cache", overrides="apt_overrides.json",
        stubs="apt_stubs.json", renderer="auto"):
    """Open the viewer. Used by main() (command line) and by launch.py."""
    sys.path.insert(0, os.getcwd())
    cache = outpaths.out_path("apt", cache)
    overrides = outpaths.out_path("apt", overrides)
    stubs = outpaths.out_path("apt", stubs)
    fn = None
    try:
        mod, name = (decomp or "fng_jdlz:decompress").split(":")
        fn = getattr(importlib.import_module(mod), name)
    except (ImportError, AttributeError, ValueError):
        if decomp:
            sys.exit("cannot load --decomp %s" % decomp)
    res = apt_resources.Resources(res_dirs, cache_dir=cache, override_file=overrides, decomp=fn)
    root = tk.Tk()
    app = App(root, ui, res, renderer, stubs)
    if movie:
        name = movie if movie.lower().endswith(".big") else movie + ".big"
        if name in app.paths:
            app.movie_tree.selection_set(name)
            app.movie_tree.see(name)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ui", help="folder with the UI .big files (or one .big)")
    ap.add_argument("--res", action="append", default=[], help="folder with .bun/.bin/.lzc files (repeat)")
    ap.add_argument("--decomp", default=None, help="module:function for JDLZ files (default fng_jdlz:decompress)")
    ap.add_argument("--movie", default=None, help="movie to open at start, like customizecar")
    ap.add_argument("--cache", default="apt_cache")
    ap.add_argument("--overrides", default="apt_overrides.json")
    ap.add_argument("--stubs", default="apt_stubs.json", help="what the game would give a screen (stub values and a scenario)")
    ap.add_argument("--renderer", choices=("auto", "skia", "pillow"), default="auto", help="drawing backend (auto: skia if installed)")
    a = ap.parse_args()
    run(a.ui, a.res, a.decomp, a.movie, a.cache, a.overrides, a.stubs, a.renderer)


if __name__ == "__main__":
    main()
