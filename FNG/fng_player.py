#!/usr/bin/env python3
"""
fng_player.py

Window tool for the Carbon FrontEnd (FNG) files. Pick a game folder: it reads every .bun / .fng
file in it and lists the FNGs found. Nothing is extracted to disk.
Step 2: select an FNG to see its parsed package (resources, objects, scripts, message responses).
"Parse all" parses every listed FNG and shows the result in the Parse column.
Step 3 (this version): the texture packs (TPK) of the same folder are indexed in memory. Image
resources of an FNG show the texture they use, select one to see it. "Textures..." lists every
texture found. Needs Pillow for the pictures (pip install pillow).
Step 4: the "Player" tab draws the selected FNG (textures, fonts, colors) and plays its scripts. It starts
on INIT, pick another script (IN, OUT, ...) to run it. Script events and button logic are not run.
Fonts: the font blocks (FEngFont) of the folder are read too. A Font resource shows its font,
select it to see sample text drawn with the glyph sheet. Select a text object to see its text in
its font. "Find text..." searches all files for a text (a name, a string) and shows the file, offset, enclosing
blocks and a hex view, to find where something is stored when a reader misses it. Fonts and textures are
also read from small .bin files, not only .bun.
"Font blocks..." lists the font blocks with a parse result, a sample and a hex view.
Fonts that are not in the folder can be chosen by hand: select the font resource (or a text object) and use
"Choose font..." (or right click), with a scale for the text. The choice is kept in
outputs/fng_player/font_choices.json.
"Find text..." searches all files for a name or string and shows file, offset, enclosing blocks and hex,
to find where something is stored when a reader misses it. Fonts and textures are read from .bin files
up to 4 MB as well as from .bun files.
In the language files "^" is a line break.
Device: the game shows keyboard icons or controller icons by the object flags PCOnly / ConsoleOnly, so both sets
overlap when the flags are ignored. Pick Keyboard (PC) or Controller (console) next to Mouse (default Keyboard).
Interaction: buttons are the objects flagged IsButton. The first one gets the focus when an FNG opens (FOCUS and
UNFOCUS go to the button and its children). With the mouse over the picture: arrow keys move the focus to the
nearest button (or send the Up / Down / Left / Right message if the screen answers it), Enter or Space is
accept, Escape or Backspace is back, Tab is start. A click on a button focuses and activates it (Mouse =
Interact; Shift+click or Mouse = Inspect picks objects instead). The message each input sends is guessed
from names (PAD_ACCEPT, FE_ACTION_ACCEPT ...) that this screen answers; "Keys..." sets them by hand.
Mouse in the Player: move over the picture to see which objects are under the cursor, click to select one
(click again at the same place for the next object below it), right click for a menu: pick an object of the
stack, send one of its messages to it, hide it, show only it. The selected object is outlined and opened in
the Package tab. Use it to find out which of several overlapping objects draws what.
Navigation: a response that goes to another FNG (SwitchToPackage, PushPackage..., PopPackage) opens that FNG in
the Player; "Back" returns along the pushes. A multi image draws its three textures one over the other.
Step 7: the Player tab has a Message box and a log. Send a message (for example PRESS or BACK) and the
message responses run: SetScript on the object, messages to other objects, conditionals. Script events post
their message at their time. Commands that leave the package (PushPackage, PopPackage, game and sound
messages) are only written to the log. If common/hashes_main.txt of the stream repo is next to this folder,
its names are used to name message, script and object hashes.
Step 6: text objects show their real text from the language files (English_Global.bin and so on, any .bin or
.bun in the folder with string blocks). Pick the language in the Player tab. Without a language file, or with
"(none)", the text stored in the FNG is shown. Text is wrapped at its max width and aligned per line.
"Check all" draws every listed FNG and all its scripts and reports what is missing (textures, fonts) and what
fails, with a report you can copy. "Save frame" in the Player tab writes the current picture as a PNG.
Not done: message responses (menu logic), movies, effects, multi image blending.

Usage: python fng_player.py [folder]
"""
import json
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, ttk

import fng_bun
import fng_font
import fng_parse
import fng_render
import fng_str
import fng_tpk

try:
    from PIL import Image, ImageDraw, ImageTk
    HAVE_TK_IMAGES = True
except ImportError:
    HAVE_TK_IMAGES = False

# font choices are kept here, outside the tool folder, like the other tools keep their outputs
SETTINGS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "outputs", "fng_player"))
FONT_CHOICES_FILE = "font_choices.json"
PREVIEW_SIZE = (420, 260)


def resource_key(name):
    """Name of a resource without folders and extension, upper case: 'FONT_NFS_TITLE.ffn' -> 'FONT_NFS_TITLE'."""
    return name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0].upper()
ASPECTS = {"4:3": (640, 480), "16:9": (854, 480)}
SPEEDS = {"0.25x": 0.25, "0.5x": 0.5, "1x": 1.0, "2x": 2.0}
DEVICES = {"Keyboard (PC)": "pc", "Controller (console)": "console", "Both": "both"}
FRAME_MS = 33
INPUT_MAP_FILE = "input_map.json"
INPUT_CANDIDATES = {
    "accept": ["PAD_ACCEPT", "BUTTON_ACCEPT", "FE_ACTION_ACCEPT", "PAD_BUTTON0", "ACCEPT", "PRESS", "SELECT"],
    "back": ["PAD_BACK", "BUTTON_BACK", "FE_ACTION_CANCEL", "PAD_BUTTON1", "BACK", "CANCEL"],
    "start": ["PAD_START", "BUTTON_START", "FE_ACTION_START", "START"],
    "up": ["PAD_UP", "BUTTON_UP", "FE_ACTION_UP", "UP"],
    "down": ["PAD_DOWN", "BUTTON_DOWN", "FE_ACTION_DOWN", "DOWN"],
    "left": ["PAD_LEFT", "BUTTON_LEFT", "FE_ACTION_LEFT", "LEFT"],
    "right": ["PAD_RIGHT", "BUTTON_RIGHT", "FE_ACTION_RIGHT", "RIGHT"],
}
KEY_ACTIONS = {"Up": "up", "Down": "down", "Left": "left", "Right": "right", "Return": "accept", "KP_Enter": "accept",
               "space": "accept", "Escape": "back", "BackSpace": "back", "Tab": "start"}
_HERE = os.path.dirname(os.path.abspath(__file__))
HASH_FILES = [os.path.join(_HERE, os.pardir, "common", "hashes_main.txt"), os.path.join(_HERE, "hashes_main.txt")]


class PlayerResolver(fng_render.Resolver):
    """Gives the scene the textures and fonts of the window's indexes."""

    def __init__(self, app):
        self.app = app

    def image(self, obj):
        res = obj.resource
        tex = self.app.texture_of(res) if res is not None else None
        return self.app.texture_image(tex) if tex is not None else None

    def multi_images(self, obj):
        pkg = self.app.scene.pkg if self.app.scene is not None else None
        return [self.app.texture_image(tex) if tex is not None else None for tex in self.app.multi_textures(obj, pkg)]

    def font(self, obj):
        res = obj.resource
        font = self.app.font_of(res) if res is not None else None
        if font is None:
            return None
        sheet, _info = self.app.font_sheet(font)
        return (font, sheet) if sheet is not None else None

    def text(self, obj):
        return self.app.string_for(obj)

    def font_scale(self, obj):
        return self.app.font_scale_of(obj.resource) if obj.resource is not None else 1.0

LIST_COLUMNS = (
    # id, heading, width, anchor
    ("name", "Name", 190, "w"),
    ("file", "File", 100, "w"),
    ("size", "Size", 70, "e"),
    ("unpacked", "Unpacked", 70, "e"),
    ("state", "State", 70, "w"),
    ("parse", "Parse", 150, "w"),
)


def key_of(entry):
    return (entry.path, entry.offset)


def fmt(value):
    """Short text for a vector, color, quaternion or number."""
    if isinstance(value, (tuple, list)):
        return "(" + ", ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in value) + ")"
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


class FngPlayer(tk.Tk):
    def __init__(self, folder=None):
        super().__init__()
        self.title("Carbon FNG player")
        self.geometry("1300x700")

        self.entries = []
        self.shown = {}                 # list item id -> FngEntry
        self.sort_col = "name"
        self.sort_rev = False
        self.messages = queue.Queue()
        self.busy = False
        self.parse_status = {}          # (path, offset) -> text for the Parse column
        self.builders = {}              # package tree item -> function that adds its children
        self.cache = {}                 # (path, offset) -> Package, last few only
        self.textures = None            # fng_tpk.TextureIndex of the folder
        self.fonts = None               # fng_font.FontIndex of the folder
        self.strings = None             # fng_str.StringIndex of the folder
        self.font_choices = self._load_font_choices()   # resource name -> font name, set by hand
        self.input_map = self._load_input_map()         # input (accept, back ...) -> message name, set by hand
        self.sheets = {}                # (path, pixel_pos) -> glyph sheet as a Pillow image
        self.node_object = {}           # package tree item -> text object
        self.scene = None               # fng_render.Scene of the selected FNG
        self._draw_key = None           # what the picture on the screen was made from, to skip identical redraws
        self.selected_node = None       # scene node picked with the mouse
        self.hover_info = ""
        self.info_base = ""
        self._last_click = None
        self.object_items = {}          # package tree item -> FngObject, for the objects built so far
        self.current_entry = None       # the FNG shown
        self.nav_stack = []             # FNGs left by a push, for Back
        self._programmatic = False      # the next list selection comes from navigation, not from the user
        self.pending_note = ""
        self.playing = False
        self.dirty = False
        self.clock = 0.0                # ms since the last script start
        self.last_tick = time.perf_counter()
        self.player_photo = None
        self.folder = None
        self.node_resource = {}         # package tree item -> Resource, for the preview
        self.photos = {}                # (path, pixel_pos) -> PhotoImage, kept so Tk does not drop them
        self.shown_photo = None

        self._build()
        self.after(100, self._poll)
        self.after(FRAME_MS, self._tick)
        if folder:
            self.scan(folder)

    # ---- layout

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Button(top, text="Open folder...", command=self.pick_folder).pack(side="left")
        self.recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Include subfolders", variable=self.recursive).pack(side="left", padx=8)
        ttk.Button(top, text="Parse all", command=self.parse_all).pack(side="left", padx=(0, 4))
        ttk.Button(top, text="Check all", command=self.check_all).pack(side="left", padx=(0, 4))
        ttk.Button(top, text="Textures...", command=self.show_texture_list).pack(side="left", padx=(0, 4))
        ttk.Button(top, text="Font blocks...", command=self.show_font_blocks).pack(side="left", padx=(0, 4))
        ttk.Button(top, text="Find text...", command=self.show_find_text).pack(side="left")
        self.folder_label = ttk.Label(top, text="No folder")
        self.folder_label.pack(side="left", padx=8)

        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh())
        ttk.Entry(top, textvariable=self.filter_var, width=24).pack(side="right")
        ttk.Label(top, text="Filter").pack(side="right", padx=4)

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=6)

        left = ttk.Frame(panes)
        self.tree = ttk.Treeview(left, columns=[c[0] for c in LIST_COLUMNS], show="headings", selectmode="browse")
        for cid, heading, width, anchor in LIST_COLUMNS:
            self.tree.heading(cid, text=heading, command=lambda c=cid: self.sort_by(c))
            self.tree.column(cid, width=width, anchor=anchor)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        panes.add(left, weight=2)

        self.tabs = ttk.Notebook(panes)
        rightpane = ttk.PanedWindow(self.tabs, orient="vertical")
        right = ttk.Frame(rightpane)
        self.pkg = ttk.Treeview(right, columns=("value",), selectmode="browse")
        self.pkg.heading("#0", text="Package")
        self.pkg.heading("value", text="Value")
        self.pkg.column("#0", width=330)
        self.pkg.column("value", width=330)
        sb2 = ttk.Scrollbar(right, orient="vertical", command=self.pkg.yview)
        self.pkg.configure(yscrollcommand=sb2.set)
        self.pkg.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")
        self.pkg.bind("<<TreeviewOpen>>", self.on_open)
        self.pkg.bind("<<TreeviewSelect>>", self.on_pkg_select)
        rightpane.add(right, weight=3)

        preview = ttk.Frame(rightpane)
        bar = ttk.Frame(preview)
        bar.pack(fill="x")
        self.choose_font_button = ttk.Button(bar, text="Choose font...", command=self.choose_font_for_selection, state="disabled")
        self.choose_font_button.pack(side="left")
        self.clear_font_button = ttk.Button(bar, text="Remove font choice", command=self.clear_font_choice, state="disabled")
        self.clear_font_button.pack(side="left", padx=4)
        self.pkg_menu = tk.Menu(self, tearoff=0)
        self.pkg_menu.add_command(label="Choose font...", command=self.choose_font_for_selection)
        self.pkg_menu.add_command(label="Remove font choice", command=self.clear_font_choice)
        self.pkg.bind("<Button-3>", self.on_pkg_right_click)
        self.pkg.bind("<Button-2>", self.on_pkg_right_click)
        self.preview_image = ttk.Label(preview, anchor="center")
        self.preview_image.pack(fill="both", expand=True)
        self.preview_text = ttk.Label(preview, text="Select a resource to see its texture.", anchor="w")
        self.preview_text.pack(fill="x")
        rightpane.add(preview, weight=2)
        self.tabs.add(rightpane, text="Package")
        player = ttk.Frame(self.tabs)
        self._build_player(player)
        self.tabs.add(player, text="Player")
        self.player_frame = player
        panes.add(self.tabs, weight=3)

        self.detail = ttk.Label(self, text="", anchor="w")
        self.detail.pack(fill="x", padx=8, pady=(4, 0))
        self.status = ttk.Label(self, text="Open a folder with .bun files.", anchor="w")
        self.status.pack(fill="x", padx=8, pady=4)

    def _build_player(self, parent):
        bar = ttk.Frame(parent, padding=4)
        bar.pack(fill="x")
        self.play_button = ttk.Button(bar, text="Pause", width=7, command=self.toggle_play)
        self.play_button.pack(side="left")
        ttk.Button(bar, text="Restart", width=8, command=self.restart_scene).pack(side="left", padx=4)
        self.back_button = ttk.Button(bar, text="Back", width=6, command=self.go_back, state="disabled")
        self.back_button.pack(side="left")
        ttk.Label(bar, text="Script").pack(side="left", padx=(8, 2))
        self.script_var = tk.StringVar()
        self.script_box = ttk.Combobox(bar, textvariable=self.script_var, state="readonly", width=16)
        self.script_box.pack(side="left")
        self.script_box.bind("<<ComboboxSelected>>", lambda _e: self.run_selected_script())
        ttk.Label(bar, text="Language").pack(side="left", padx=(8, 2))
        self.language_var = tk.StringVar(value="(none)")
        self.language_box = ttk.Combobox(bar, textvariable=self.language_var, values=["(none)"], state="readonly", width=9)
        self.language_box.pack(side="left")
        self.language_box.bind("<<ComboboxSelected>>", lambda _e: self._mark_dirty())
        ttk.Label(bar, text="Speed").pack(side="left", padx=(8, 2))
        self.speed_var = tk.StringVar(value="1x")
        speed = ttk.Combobox(bar, textvariable=self.speed_var, values=list(SPEEDS), state="readonly", width=5)
        speed.pack(side="left")
        ttk.Label(bar, text="Screen").pack(side="left", padx=(8, 2))
        self.aspect_var = tk.StringVar(value="4:3")
        aspect = ttk.Combobox(bar, textvariable=self.aspect_var, values=list(ASPECTS), state="readonly", width=5)
        aspect.pack(side="left")
        aspect.bind("<<ComboboxSelected>>", lambda _e: self._mark_dirty())
        ttk.Button(bar, text="Save frame", command=self.save_frame).pack(side="left", padx=(8, 0))
        ttk.Label(bar, text="Zoom").pack(side="left", padx=(8, 2))
        self.zoom_var = tk.StringVar(value="Fit")
        zoom = ttk.Combobox(bar, textvariable=self.zoom_var, values=["Fit", "1x", "2x"], state="readonly", width=5)
        zoom.pack(side="left")
        zoom.bind("<<ComboboxSelected>>", lambda _e: self._mark_dirty())
        bottom = ttk.Frame(parent)
        bottom.pack(side="bottom", fill="x")
        msgbar = ttk.Frame(bottom, padding=(4, 2))
        msgbar.pack(fill="x")
        ttk.Label(msgbar, text="Message").pack(side="left")
        self.message_var = tk.StringVar()
        self.message_box = ttk.Combobox(msgbar, textvariable=self.message_var, width=30)
        self.message_box.pack(side="left", padx=4)
        self.message_box.bind("<Return>", lambda _e: self.send_message())
        ttk.Button(msgbar, text="Send", width=6, command=self.send_message).pack(side="left")
        ttk.Button(msgbar, text="Clear log", command=self.clear_log).pack(side="left", padx=4)
        ttk.Label(msgbar, text="(name or 0x hash, to the package)").pack(side="left")
        ttk.Button(msgbar, text="Keys...", command=self.show_input_dialog).pack(side="right")
        self.device_var = tk.StringVar(value="Keyboard (PC)")
        device_box = ttk.Combobox(msgbar, textvariable=self.device_var, values=list(DEVICES), state="readonly", width=19)
        device_box.pack(side="right", padx=4)
        device_box.bind("<<ComboboxSelected>>", lambda _e: self.on_device_change())
        ttk.Label(msgbar, text="Device").pack(side="right")
        self.mouse_mode = tk.StringVar(value="Interact")
        ttk.Combobox(msgbar, textvariable=self.mouse_mode, values=["Interact", "Inspect"], state="readonly", width=8).pack(side="right", padx=4)
        ttk.Label(msgbar, text="Mouse").pack(side="right")
        logframe = ttk.Frame(bottom)
        logframe.pack(fill="x")
        self.log_text = tk.Text(logframe, height=8, wrap="none", font=("Courier New", 9), state="disabled")
        logscroll = ttk.Scrollbar(logframe, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=logscroll.set)
        self.log_text.pack(side="left", fill="x", expand=True)
        logscroll.pack(side="right", fill="y")
        self.player_info = ttk.Label(bottom, text="Select an FNG.", anchor="w")
        self.player_info.pack(fill="x", padx=4, pady=2)
        self.selection_label = ttk.Label(bottom, text="Click an object to inspect it, right click for actions.", anchor="w")
        self.selection_label.pack(fill="x", padx=4)
        self.player_label = ttk.Label(parent, anchor="center")
        self.player_label.pack(fill="both", expand=True)
        self.player_label.bind("<Motion>", self.on_player_motion)
        self.player_label.bind("<Leave>", lambda _e: self._set_hover(""))
        self.player_label.bind("<Enter>", lambda _e: self.player_label.focus_set() if str(self.tabs.select()) == str(self.player_frame) else None)
        self.player_label.bind("<Key>", self.on_key)
        self.player_label.bind("<Button-1>", self.on_player_click)
        self.player_label.bind("<Button-3>", self.on_player_right_click)
        self.player_label.bind("<Button-2>", self.on_player_right_click)
        self.message_map = {}

    # ---- player

    def load_scene(self, pkg):
        """Builds the scene of a package and starts it on INIT."""
        try:
            self.scene = fng_render.Scene(pkg, PlayerResolver(self))
        except Exception as e:                      # a package the scene can not handle must not break the window
            self.scene = None
            self.player_label.config(image="")
            self.player_info.config(text=f"Can not play this package: {e!r}")
            return
        names = self.scene.script_names()
        self.script_box.config(values=names)
        self.script_var.set("INIT" if "INIT" in names else (names[0] if names else ""))
        self.clock = 0.0
        self.playing = True
        self.play_button.config(text="Pause")
        self.last_tick = time.perf_counter()
        self.selected_node = None
        self.hover_info = ""
        self.selection_label.config(text="Click an object to inspect it, right click for actions.")
        self.message_map = dict(self.scene.message_names())
        self.message_box.config(values=list(self.message_map))
        self.message_var.set("")
        self.clear_log()
        if self.pending_note:
            self.log_text.config(state="normal")
            self.log_text.insert("end", self.pending_note + "\n")
            self.log_text.config(state="disabled")
            self.pending_note = ""
        self.back_button.config(state="normal" if self.nav_stack else "disabled")
        self.scene.device = self.device_code()
        width, height = ASPECTS.get(self.aspect_var.get(), (640, 480))
        self.scene.render(width, height)                     # positions are needed to move the focus
        self.scene.focus_first()
        self.flush_log()
        self._mark_dirty()

    def device_code(self):
        return DEVICES.get(self.device_var.get(), "pc")

    def on_device_change(self):
        """Keyboard or controller: draws the objects of that device. The focus moves if its button is gone."""
        if self.scene is None:
            return
        self.scene.device = self.device_code()
        width, height = ASPECTS.get(self.aspect_var.get(), (640, 480))
        self.scene.render(width, height)
        if self.scene.nodes.get(self.scene.focus_guid) not in self.scene.focusable():
            self.scene.focus_guid = 0
            self.scene.focus_first()
        self.flush_log()
        self._mark_dirty()

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def flush_log(self):
        """Adds the new message lines of the scene to the log."""
        if self.scene is None:
            return
        lines = self.scene.take_log()
        if lines:
            stamp = f"{self.clock / 1000:6.2f}s "
            self.log_text.config(state="normal")
            for line in lines:
                self.log_text.insert("end", (stamp if (not line.startswith("  ") or line.startswith("  event")) else " " * len(stamp)) + line + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")

    # ---- mouse in the player

    def _show_info(self):
        self.player_info.config(text=self.info_base + (f"   |  under cursor: {self.hover_info}" if self.hover_info else ""))

    def _set_hover(self, text):
        if text != self.hover_info:
            self.hover_info = text
            if self.scene is not None:
                self._show_info()

    def _frame_point(self, event):
        """Position in the scene picture (canvas pixels) for a mouse event on the picture label, or None."""
        if self.scene is None or self.player_photo is None:
            return None
        width, height = ASPECTS.get(self.aspect_var.get(), (640, 480))
        pw, ph = self.player_photo.width(), self.player_photo.height()
        ox = (self.player_label.winfo_width() - pw) / 2
        oy = (self.player_label.winfo_height() - ph) / 2
        x, y = (event.x - ox) * width / pw, (event.y - oy) * height / ph
        return (x, y) if 0 <= x < width and 0 <= y < height else None

    def node_label(self, node):
        obj = node.obj
        name = obj.name or self.scene.pkg.hash_names.get(obj.name_hash) or fng_parse.HASH_NAMES.get(obj.name_hash) or f"0x{obj.name_hash:08X}"
        shown = (self.string_for(obj) or obj.text.get("value") or "") if obj.type_name == "String" else ""
        return f"{obj.type_name} {name}" + (f' "{shown.replace(chr(10), " ")[:24]}"' if shown else "")

    def node_summary(self, node):
        obj = node.obj
        pos = node.values.get("Position", (0, 0, 0))
        size = node.values.get("Size", (0, 0, 0))
        script = self.scene.pkg.name_of(node.script.id) if node.script is not None else "-"
        text = f"{self.node_label(node)}  guid 0x{obj.guid:X}  script {script}  pos ({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f})  "
        text += f"size ({size[0]:.0f}, {size[1]:.0f})  alpha {node.color[3]}"
        if obj.resource is not None:
            text += f"  resource {obj.resource.name}"
        if obj.text.get("label"):
            text += f"  label {obj.text['label']}"
        elif obj.text.get("label_hash"):
            text += f"  label {self.scene.pkg.name_of(obj.text['label_hash'])}"
        names = fng_parse.flag_names(obj.flags)
        return text + (f"  flags {' '.join(names)}" if names else "")

    def on_player_motion(self, event):
        point = self._frame_point(event)
        if point is None:
            self._set_hover("")
            return
        nodes = self.scene.objects_at(*point)
        self._set_hover(" | ".join(self.node_label(n) for n in nodes[:4]) + (f" | ... {len(nodes) - 4} more" if len(nodes) > 4 else ""))

    def on_player_click(self, event):
        point = self._frame_point(event)
        if point is None or self.scene is None:
            return
        self.player_label.focus_set()
        if self.mouse_mode.get() == "Interact" and not getattr(event, "state", 0) & 0x1:     # Shift+click inspects
            self.interact_click(point)
            return
        stack = self.scene.objects_at(*point)
        if not stack:
            self.select_node(None)
            return
        last = self._last_click
        if last and abs(last[0] - point[0]) < 4 and abs(last[1] - point[1]) < 4 and self.selected_node in stack:
            index = (stack.index(self.selected_node) + 1) % len(stack)      # same place again: the next object below
        else:
            index = 0
        self._last_click = point
        self.select_node(stack[index], f"   ({index + 1} of {len(stack)} under the cursor, click again for the next)" if len(stack) > 1 else "")

    def select_node(self, node, note=""):
        self.selected_node = node
        if node is None:
            self.selection_label.config(text="Click an object to inspect it, right click for actions.")
        else:
            self.selection_label.config(text=self.node_summary(node) + note)
            self.reveal_object(node.obj)
        self._mark_dirty()

    def reveal_object(self, obj):
        """Selects the object in the Package tree (opening its parents)."""
        chain = []
        o = obj
        while o is not None:
            chain.append(o)
            o = o.parent
        chain.reverse()
        parent = next((i for i in self.pkg.get_children("") if self.pkg.item(i, "text").startswith("Objects")), None)
        if parent is None:
            return
        self.on_open_item(parent)
        for o in chain:
            item = next((c for c in self.pkg.get_children(parent) if self.object_items.get(c) is o), None)
            if item is None:
                return
            if o is not obj:
                self.on_open_item(item)
                self.pkg.item(item, open=True)
            parent = item
        self.pkg.selection_set(parent)
        self.pkg.see(parent)

    def on_player_right_click(self, event):
        point = self._frame_point(event)
        if self.scene is None:
            return
        menu = self.build_player_menu(point)
        menu.tk_popup(event.x_root, event.y_root)

    def build_player_menu(self, point):
        """Menu for a right click at a point of the picture (None = outside)."""
        menu = tk.Menu(self, tearoff=0)
        stack = self.scene.objects_at(*point) if point is not None else []
        if stack:
            menu.add_command(label="Under the cursor, top first:", state="disabled")
            for node in stack[:14]:
                menu.add_command(label="   " + self.node_label(node), command=lambda n=node: self.select_node(n))
            menu.add_separator()
        node = self.selected_node
        if node is not None:
            obj = node.obj
            menu.add_command(label=f"Selected: {self.node_label(node)}", state="disabled")
            responses = sorted({(self.scene.pkg.name_of(r.id), r.id) for r in obj.responses})
            if responses:
                to_obj, to_all = tk.Menu(menu, tearoff=0), tk.Menu(menu, tearoff=0)
                for name, h in responses:
                    to_obj.add_command(label=name, command=lambda h=h, g=obj.guid: self.send_to(h, g))
                    to_all.add_command(label=name, command=lambda h=h: self.send_to(h, 0))
                menu.add_cascade(label="Send a message of this object to it", menu=to_obj)
                menu.add_cascade(label="Send a message of this object to the package", menu=to_all)
            menu.add_command(label="Hide this object (and its children)", command=lambda: self.hide_node(node))
            menu.add_command(label="Show only this object", command=lambda: self.show_only(node))
        if self.scene.hidden:
            menu.add_command(label=f"Show all hidden objects ({len(self.scene.hidden)})", command=self.show_hidden)
        if not stack and node is None and not self.scene.hidden:
            menu.add_command(label="Nothing here. Click an object first.", state="disabled")
        return menu

    def send_to(self, message, guid):
        self.scene.post_message(message, guid)
        self.playing = True
        self.play_button.config(text="Pause")
        self.flush_log()
        self.handle_navigation()
        self._mark_dirty()

    def hide_node(self, node):
        self.scene.hidden.add(node.obj.guid)
        self.selected_node = None
        self.selection_label.config(text=f"Hidden: {self.node_label(node)}. Right click > Show all hidden objects to undo.")
        self._mark_dirty()

    def show_only(self, node):
        keep = set()
        o = node.obj
        while o is not None:
            keep.add(o.guid)                            # the parents stay, their transforms move the object
            o = o.parent

        def add_children(n):
            for child in n.children:
                keep.add(child.obj.guid)
                add_children(child)
        add_children(node)
        self.scene.hidden = {g for g in self.scene.nodes if g not in keep}
        self._mark_dirty()

    def show_hidden(self):
        self.scene.hidden = set()
        self._mark_dirty()

    # ---- input

    def _load_input_map(self):
        try:
            with open(os.path.join(SETTINGS_DIR, INPUT_MAP_FILE), encoding="utf-8") as f:
                raw = json.load(f)
            return {k: str(v) for k, v in raw.items() if k in INPUT_CANDIDATES and v}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_input_map(self):
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(os.path.join(SETTINGS_DIR, INPUT_MAP_FILE), "w", encoding="utf-8") as f:
                json.dump(self.input_map, f, indent=2, sort_keys=True)
        except OSError as e:
            self.status.config(text=f"Could not save the keys: {e}")

    @staticmethod
    def input_hash(name):
        name = name.strip()
        return int(name, 16) if name.lower().startswith("0x") else fng_parse.bin_hash(name.upper())

    def resolve_input(self, action):
        """(message name, hash) an input sends: the one set by hand, else the first guess this screen answers."""
        chosen = self.input_map.get(action)
        names = [chosen] if chosen else INPUT_CANDIDATES[action]
        answered = self.scene.message_hashes() if self.scene is not None else set()
        for name in names:
            try:
                if self.input_hash(name) in answered:
                    return name, self.input_hash(name)
            except ValueError:
                continue
        return names[0], self.input_hash(names[0])

    def on_key(self, event):
        action = KEY_ACTIONS.get(event.keysym)
        if action is None or self.scene is None:
            return None
        self.input_action(action)
        return "break"

    def input_action(self, action):
        """An input (up, down, left, right, accept, back, start) as the engine would handle it."""
        if self.scene is None:
            return
        name, message = self.resolve_input(action)
        if action in ("up", "down", "left", "right"):
            if self.scene.answers_input(message):
                self.scene.post_input(message, name)             # the screen handles the direction itself (sliders, lists)
            elif not self.scene.navigate(action):
                self.log_note(f"{action}: no button there")
        elif action == "back":
            if not self.scene.post_input(message, name) and self.nav_stack:
                self.log_note("back: the screen does not answer it, going back")
                self.go_back()
        else:
            if self.scene.focus_guid == 0:
                self.scene.focus_first()
            self.scene.post_input(message, name)
        self.playing = True
        self.play_button.config(text="Pause")
        self.flush_log()
        self.handle_navigation()
        self._mark_dirty()

    def interact_click(self, point):
        """Mouse click on a button: focus it and accept."""
        buttons = (self.scene.button_of(n) for n in self.scene.objects_at(*point))
        button = next((b for b in buttons if b is not None), None)
        if button is None:
            self.selection_label.config(text="That is not a button. Shift+click inspects an object; Mouse = Inspect picks objects.")
            return
        self.scene.set_focus(button.obj.guid)
        self.input_action("accept")

    def show_input_dialog(self):
        """Window to set the message each input sends (accept, back ...)."""
        win = tk.Toplevel(self)
        win.title("Keys")
        win.transient(self)
        body = ttk.Frame(win, padding=8)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Message sent by each input. (auto) = the first of the guesses that this screen answers.\n"
                             "Keys with the mouse over the picture: arrows, Enter / Space, Esc / Backspace, Tab.", justify="left").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        screen = [n for n, _h in self.scene.message_names()] if self.scene is not None else []
        rows = {}
        for i, action in enumerate(INPUT_CANDIDATES, start=1):
            ttk.Label(body, text=action.capitalize()).grid(row=i, column=0, sticky="w")
            var = tk.StringVar(value=self.input_map.get(action, "(auto)"))
            ttk.Combobox(body, textvariable=var, values=["(auto)"] + INPUT_CANDIDATES[action] + screen, width=34).grid(row=i, column=1, padx=6, pady=2)
            now = ttk.Label(body, text="")
            now.grid(row=i, column=2, sticky="w")
            rows[action] = (var, now)
            if self.scene is not None:
                name, message = self.resolve_input(action)
                now.config(text=f"now: {name}" + (" (answered here)" if message in self.scene.message_hashes() else " (not answered here)"))

        def save():
            self.input_map = {a: v.get().strip() for a, (v, _l) in rows.items() if v.get().strip() not in ("", "(auto)")}
            self._save_input_map()
            win.destroy()
        buttons = ttk.Frame(win, padding=8)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Save", command=save).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=4)

    def find_entry(self, name):
        """The listed FNG for a package name from a command (name or path, with or without .fng)."""
        key = resource_key(name)
        matches = [e for e in self.entries if resource_key(e.name) == key]
        if not matches:
            return None
        here = self.current_entry.path if self.current_entry is not None else None
        return next((e for e in matches if e.path == here), matches[0])

    def select_entry(self, entry):
        """Selects an FNG in the list (also when the filter hides it), which shows it."""
        item = next((i for i, e in self.shown.items() if e is entry), None)
        if item is None and self.filter_var.get():
            self.filter_var.set("")                       # the filter hides it
            item = next((i for i, e in self.shown.items() if e is entry), None)
        if item is None:
            return False
        self.tree.see(item)
        if self.tree.selection() == (item,):
            self.show_package(entry)                      # already selected: no selection event will come
            return True
        self._programmatic = True
        self.tree.selection_set(item)
        return True

    def navigate(self, kind, name):
        """Goes to another FNG as a response command asks. kind: switch, push or pop."""
        if kind == "pop":
            if not self.nav_stack:
                self.log_note("-> PopPackage: nothing to go back to")
                return
            entry = self.nav_stack.pop()
        else:
            entry = self.find_entry(name) if name else None
            if entry is None:
                self.log_note(f"-> {kind} {name!r}: this FNG is not in the loaded files")
                return
            if kind == "push" and self.current_entry is not None:
                self.nav_stack.append(self.current_entry)
        self.pending_note = f"-> {kind} to {entry.name}" + (f"  (back: {len(self.nav_stack)})" if self.nav_stack else "")
        if not self.select_entry(entry):
            self.log_note(f"-> {entry.name} could not be selected")
            self.pending_note = ""

    def log_note(self, text):
        self.log_text.config(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def go_back(self):
        if self.nav_stack:
            self.navigate("pop", None)

    def handle_navigation(self):
        """Follows the first request to go to another FNG. The scene is replaced, so the rest is dropped."""
        if self.scene is None:
            return
        requests = self.scene.take_navigation()
        if requests:
            self.navigate(*requests[0])

    def multi_textures(self, obj, pkg):
        """The three textures of a multi image (None where missing). A value is a texture hash, or a resource index."""
        out = []
        for key in obj.textures:
            tex = None
            if key is not None and self.textures is not None:
                tex = self.textures.by_key.get(key)
                if tex is None and pkg is not None and key < len(pkg.resources):
                    tex = self.texture_of(pkg.resources[key])
            out.append(tex)
        return out

    def send_message(self):
        text = self.message_var.get().strip()
        if self.scene is None or not text:
            return
        try:
            self.scene.post_message(self.message_map.get(text, text))
        except ValueError:
            self.player_info.config(text=f"Not a message name or 0x hash: {text}")
            return
        self.playing = True
        self.play_button.config(text="Pause")
        self.flush_log()
        self.handle_navigation()
        self._mark_dirty()

    def _mark_dirty(self):
        self.dirty = True

    def toggle_play(self):
        self.playing = not self.playing
        self.play_button.config(text="Pause" if self.playing else "Play")
        self.last_tick = time.perf_counter()

    def restart_scene(self):
        if self.scene is not None:
            self.scene.reset()
            self.clear_log()
            self.clock = 0.0
            self.script_var.set("INIT" if "INIT" in self.script_box.cget("values") else self.script_var.get())
            self.playing = True
            self.play_button.config(text="Pause")
            self._mark_dirty()

    def run_selected_script(self):
        name = self.script_var.get()
        if self.scene is not None and name:
            self.scene.run_script(name)
            self.clock = 0.0
            self.playing = True
            self.play_button.config(text="Pause")
            self._mark_dirty()

    def _tick(self):
        started = time.perf_counter()
        busy = False
        try:
            on_player = str(self.tabs.select()) == str(self.player_frame)
            now = started
            if self.scene is not None and on_player and (self.playing or self.dirty):
                busy = True
                if self.playing:
                    dt = min((now - self.last_tick) * 1000 * SPEEDS.get(self.speed_var.get(), 1.0), 100)
                    self.scene.update(dt)
                    self.clock += dt
                self.dirty = False
                self.flush_log()
                self.handle_navigation()
                if self.scene is not None:
                    self._draw_scene()
            self.last_tick = now
        except Exception as e:                      # show a drawing problem once, then stop instead of repeating it
            self.playing = False
            self.dirty = False
            self.play_button.config(text="Play")
            self.player_info.config(text=f"Drawing stopped: {e!r}")
        elapsed = (time.perf_counter() - started) * 1000
        self.after(max(1, int(FRAME_MS - elapsed)) if busy else 60, self._tick)    # keep the frame rate when drawing is slow

    def _draw_scene(self):
        if not HAVE_TK_IMAGES:
            self.player_info.config(text="Pillow with ImageTk is needed to play (pip install pillow)")
            self.playing = False
            return
        width, height = ASPECTS.get(self.aspect_var.get(), (640, 480))
        frame = self.scene.render(width, height, background=(24, 24, 24, 255))
        zoom = self.zoom_var.get()
        label_size = (self.player_label.winfo_width(), self.player_label.winfo_height()) if zoom == "Fit" else None
        key = (id(self.scene), self.scene.frame_id, width, height, zoom, label_size,
               self.selected_node.obj.guid if self.selected_node is not None else None, self.scene.focus_guid)
        if key == self._draw_key and self.player_photo is not None:
            self._update_info(width, height)            # the picture is the same as on the screen
            return
        self._draw_key = key
        if self.selected_node is not None:
            poly = self.scene.polygon_of(self.selected_node)
            if poly:
                ImageDraw.Draw(frame).line([*poly, poly[0]], fill=(255, 255, 0, 255), width=2)
        focus = self.scene.nodes.get(self.scene.focus_guid)
        if focus is not None:
            x0, y0, x1, y1 = self.scene.node_bounds(focus)
            if x1 > x0 and y1 > y0:
                ImageDraw.Draw(frame).rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3], outline=(0, 255, 255, 255), width=2)
        if zoom == "Fit":
            fit = min(max(1, self.player_label.winfo_width()) / width, max(1, self.player_label.winfo_height()) / height)
            scale = max(0.2, fit)
        else:
            scale = float(zoom[:-1])
        if abs(scale - 1) > 0.01:
            frame = frame.resize((int(width * scale), int(height * scale)), Image.NEAREST if scale >= 1 else Image.BILINEAR)
        self.player_photo = ImageTk.PhotoImage(frame)
        self.player_label.config(image=self.player_photo)
        self._update_info(width, height)

    def _update_info(self, width, height):
        focus = self.scene.nodes.get(self.scene.focus_guid)
        self.info_base = (f"{self.script_var.get()}  t = {self.clock / 1000:.2f} s   {len(self.scene.nodes)} objects"
                          f"   {width}x{height}" + (f"   back: {len(self.nav_stack)}" if self.nav_stack else "")
                          + (f"   focus: {self.scene._label(focus)}" if focus is not None else ""))
        self._show_info()

    def string_for(self, obj):
        """Text of a text object in the chosen language, or None (then the text in the FNG is used)."""
        language = self.language_var.get()
        if self.strings is None or language in ("", "(none)"):
            return None
        key = obj.text.get("label_hash")
        text = self.strings.get(language, key) if key else None
        return text.replace("^", "\n") if text else text      # "^" is the line break of the language files

    def texture_image(self, tex):
        """Decoded RGBA image of a texture, cached. None if it can not be decoded."""
        key = (tex.path, tex.pixel_pos)
        if key not in self.sheets:
            if len(self.sheets) > 80:
                self.sheets.pop(next(iter(self.sheets)))
            try:
                self.sheets[key] = fng_tpk.decode(tex)
            except fng_tpk.TpkError:
                self.sheets[key] = None
        return self.sheets[key]

    def save_frame(self, path=None):
        """Writes the current picture, at the size of the selected screen, as a PNG."""
        if self.scene is None:
            self.player_info.config(text="Nothing to save. Select an FNG.")
            return
        if path is None:
            path = filedialog.asksaveasfilename(title="Save frame", defaultextension=".png", filetypes=[("PNG", "*.png")],
                                                initialfile=f"{self.scene.pkg.name or 'frame'}_{self.script_var.get()}.png")
            if not path:
                return
        width, height = ASPECTS.get(self.aspect_var.get(), (640, 480))
        self.scene.render(width, height, background=(24, 24, 24, 255)).save(path)
        self.player_info.config(text=f"Saved {path}")

    # ---- check all

    def check_entry(self, entry):
        """Draws one FNG and all its scripts. Returns (objects, issues, seconds, hidden): issues are problems of
        objects that can be drawn, hidden lists the missing things only used by objects that never show."""
        started = time.perf_counter()
        status, pkg = self.parse_entry(entry)
        if pkg is None:
            return 0, [status], time.perf_counter() - started, []
        issues = []
        if pkg.warnings:
            issues.append(f"{len(pkg.warnings)} parse warning(s): " + "; ".join(pkg.warnings[:3]))

        missing_tex, missing_font, missing_sheet = set(), set(), set()
        hidden = set()
        for obj in pkg.objects:
            res = obj.resource
            tex, font_, sheet = set(), set(), set()
            if obj.type in (fng_render.TYPE_IMAGE, fng_render.TYPE_COLORED, fng_render.TYPE_MULTI) and res is not None:
                if res.name:
                    if self.texture_of(res) is None:
                        tex.add(res.name)
                elif obj.type == fng_render.TYPE_MULTI:
                    if not any(self.multi_textures(obj, pkg)):
                        tex.add("(multi image textures)")
            elif obj.type == fng_render.TYPE_STRING and res is not None:
                font = self.font_of(res)
                if font is None:
                    font_.add(res.name)
                elif self.font_sheet(font)[0] is None:
                    sheet.add(font.sheet_name)
            if fng_render.can_be_visible(obj, self.device_code()):
                missing_tex |= tex
                missing_font |= font_
                missing_sheet |= sheet
            else:
                hidden |= tex | font_ | sheet
        hidden -= missing_tex | missing_font | missing_sheet
        if missing_tex:
            issues.append("textures not found: " + ", ".join(sorted(missing_tex)))
        if missing_font:
            issues.append("fonts not found: " + ", ".join(sorted(missing_font)))
        if missing_sheet:
            issues.append("font sheets not found: " + ", ".join(sorted(missing_sheet)))

        try:
            scene = fng_render.Scene(pkg, PlayerResolver(self))
            scene.device = self.device_code()
            scene.render(640, 480)
            for name in scene.script_names():
                if name != "INIT":
                    scene.run_script(name)
                    scene.update(10000)
            for _name, message in scene.message_names():
                scene.post_message(message)
                scene.update(200)
            scene.take_log()
            scene.take_navigation()
            scene.render(640, 480)
        except Exception as e:
            last = traceback.extract_tb(e.__traceback__)[-1]
            issues.append(f"drawing / playing error: {e!r} at {os.path.basename(last.filename)}:{last.lineno} in {last.name}")
        seconds = time.perf_counter() - started
        if seconds > 3:
            issues.append(f"slow: {seconds:.1f} s")
        return len(pkg.objects), issues, seconds, sorted(hidden)

    def check_all(self):
        if self.busy or not self.entries:
            return
        self.busy = True
        queue_ = list(self.entries)
        total = len(queue_)
        results = []

        win = tk.Toplevel(self)
        win.title("Check all")
        win.geometry("1100x640")
        top = ttk.Frame(win, padding=4)
        top.pack(fill="x")
        info = ttk.Label(top, text="Checking...")
        info.pack(side="left")
        panes = ttk.PanedWindow(win, orient="vertical")
        panes.pack(fill="both", expand=True)
        lst = ttk.Treeview(panes, columns=("file", "objects", "result"), selectmode="browse")
        lst.heading("#0", text="FNG")
        lst.heading("file", text="File")
        lst.heading("objects", text="Objects")
        lst.heading("result", text="Result")
        lst.column("#0", width=260)
        lst.column("file", width=120)
        lst.column("objects", width=70, anchor="e")
        lst.column("result", width=600)
        panes.add(lst, weight=3)
        detail = tk.Text(panes, height=10, wrap="word", font=("Courier New", 9))
        panes.add(detail, weight=2)
        rows = {}

        def report():
            lines = [f"Check of {len(results)} FNG in {self.folder} (objects that never show are not counted)"]
            bad = [r for r in results if r["issues"]]
            lines.append(f"{len(results) - len(bad)} without issues, {len(bad)} with issues")
            count = {}
            for r in results:
                for issue in r["issues"]:
                    for kind in ("textures not found: ", "fonts not found: ", "font sheets not found: "):
                        if issue.startswith(kind):
                            for name in issue[len(kind):].split(", "):
                                count[(kind[:-2], name)] = count.get((kind[:-2], name), 0) + 1
            if count:
                lines.append("")
                lines.append("Missing, by number of FNGs that use it:")
                for (kind, name), n in sorted(count.items(), key=lambda kv: (-kv[1], kv[0])):
                    lines.append(f"  {n:3d}  {kind}: {name}")
            lines.append("")
            for r in results:
                if r["issues"]:
                    lines.append(f"{r['name']} ({r['file']}, {r['objects']} objects)")
                    lines += [f"    {i}" for i in r["issues"]]
            leftovers = {}
            for r in results:
                for name in r.get("hidden", []):
                    leftovers[name] = leftovers.get(name, 0) + 1
            if leftovers:
                lines.append("")
                lines.append(f"Missing only in objects that never show (invisible flag or zero alpha), ignored above: {len(leftovers)} names")
                lines.append("  " + ", ".join(sorted(leftovers, key=str.lower)[:60]) + (" ..." if len(leftovers) > 60 else ""))
            return "\n".join(lines)

        def copy_report():
            win.clipboard_clear()
            win.clipboard_append(report())
            info.config(text="Report copied to the clipboard.")

        def save_report():
            path = filedialog.asksaveasfilename(title="Save report", defaultextension=".txt", filetypes=[("Text", "*.txt")])
            if path:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(report())
        ttk.Button(top, text="Copy report", command=copy_report).pack(side="right")
        ttk.Button(top, text="Save report...", command=save_report).pack(side="right", padx=4)

        def pick(_event):
            sel = lst.selection()
            if sel:
                r = rows[sel[0]]
                detail.delete("1.0", "end")
                detail.insert("end", f"{r['name']}  ({r['file']})\n" + ("\n".join(r["issues"]) if r["issues"] else "no issues"))
        lst.bind("<<TreeviewSelect>>", pick)

        def step():
            if not win.winfo_exists():
                self.busy = False
                return
            if not queue_:
                self.busy = False
                bad = sum(1 for r in results if r["issues"])
                info.config(text=f"Done: {len(results)} FNG, {bad} with issues. Copy report to share it.")
                return
            entry = queue_.pop(0)
            objects, issues, seconds, hidden = self.check_entry(entry)
            r = {"name": entry.name or "?", "file": os.path.basename(entry.path), "objects": objects, "issues": issues,
                 "hidden": hidden}
            results.append(r)
            item = lst.insert("", "end", text=r["name"], values=(r["file"], objects, "ok" if not issues else "; ".join(issues)[:300]))
            rows[item] = r
            info.config(text=f"Checking {len(results)}/{total}: {r['name']}")
            win.after(1, step)
        win.after(50, step)

    # ---- folder scan

    def pick_folder(self):
        folder = filedialog.askdirectory(title="Folder with the .bun files")
        if folder:
            self.scan(folder)

    def scan(self, folder):
        if self.busy:
            return
        self.busy = True
        self.folder_label.config(text=folder)
        self.folder = folder
        self.entries = []
        self.parse_status = {}
        self.cache = {}
        self.textures = None
        self.fonts = None
        self.strings = None
        self.refresh()
        recursive = self.recursive.get()

        def work():
            def progress(i, n, path):
                self.messages.put(("progress", f"Reading {i + 1}/{n}: {os.path.basename(path)}"))
            try:
                entries, warnings = fng_bun.scan_folder(folder, recursive, progress)
                self.messages.put(("scan_done", entries, warnings))
                everything = fng_bun.find_files(folder, recursive, exts=(".bun", ".fng", ".bin"))
                files = [p for p in everything if p.lower().endswith(".bun")]
                bins = [p for p in everything if p.lower().endswith(".bin")]
                bins = [p for p in bins if os.path.getsize(p) <= fng_bun.SMALL_BIN]
                textures = fng_tpk.scan_files(files, lambda i, n, path: self.messages.put(
                    ("progress", f"Textures {i + 1}/{n}: {os.path.basename(path)}")))
                fonts = fng_font.scan_files(files + bins)
                self.messages.put(("progress", "Language strings..."))
                strings = fng_str.scan_files(files + bins)
                if not fng_parse.HASH_NAMES:
                    path = next((p for p in HASH_FILES if os.path.isfile(p)), None)
                    if path:
                        self.messages.put(("progress", "Name list..."))
                        fng_parse.load_hash_dictionary(path)
                self.messages.put(("tex_done", textures, fonts, strings))
            except Exception as e:                      # a bug here must show in the status, not leave the window busy
                self.messages.put(("error", repr(e)))

        threading.Thread(target=work, daemon=True).start()

    # ---- parse all

    def parse_all(self):
        if self.busy or not self.entries:
            return
        self.busy = True
        entries = list(self.entries)

        def work():
            ok = bad = warned = 0
            for i, entry in enumerate(entries):
                self.messages.put(("progress", f"Parsing {i + 1}/{len(entries)}: {entry.name}"))
                status, pkg = self.parse_entry(entry)
                self.messages.put(("parsed", key_of(entry), status))
                if pkg is None:
                    bad += 1
                    print(f"{entry.name} ({os.path.basename(entry.path)} @0x{entry.offset:X}): {status}")
                else:
                    ok += 1
                    if pkg.warnings:
                        warned += 1
                        print(f"{entry.name}: " + "; ".join(pkg.warnings))
            self.messages.put(("parse_done", ok, bad, warned))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def parse_entry(entry):
        """Returns (text for the Parse column, Package or None)."""
        try:
            pkg = fng_parse.parse_package(fng_bun.load_package(entry))
        except (fng_bun.FngError, fng_parse.FngParseError) as e:
            return f"ERROR: {e}", None
        except Exception as e:                      # a parser bug must show in the list, not kill the thread
            return f"ERROR: {e!r}", None
        text = f"ok, {len(pkg.objects)} objects"
        if pkg.warnings:
            text += f", {len(pkg.warnings)} warnings"
        return text, pkg

    # ---- messages from the threads

    def _poll(self):
        # the threads only post messages, only this method touches the widgets
        try:
            while True:
                msg = self.messages.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.status.config(text=msg[1])
                elif kind == "scan_done":
                    self.entries, warnings = msg[1], msg[2]
                    self.busy = False
                    bad = sum(1 for e in self.entries if e.error)
                    text = f"{len(self.entries)} FNG found"
                    if bad:
                        text += f", {bad} could not be unpacked (see State)"
                    if warnings:
                        text += f". {len(warnings)} walk warning(s), first: {warnings[0]}"
                        print("\n".join(warnings))
                    self.status.config(text=text)
                    self.refresh()
                    self.busy = True            # the texture scan of the same thread is still running
                elif kind == "tex_done":
                    self.busy = False
                    self.textures, self.fonts, self.strings = msg[1], msg[2], msg[3]
                    languages = self.strings.language_names()
                    self.language_box.config(values=["(none)"] + languages)
                    self.language_var.set(self.strings.default_language() or "(none)")
                    text = (f"{len(self.entries)} FNG, {len(self.textures.textures)} textures in {self.textures.packs} packs, "
                            f"{len(self.fonts.fonts)} fonts, " +
                            (", ".join(f"{self.strings.count(l)} {l} strings" for l in languages) if languages else "no language strings")
                            + (f", {len(fng_parse.HASH_NAMES)} hash names" if fng_parse.HASH_NAMES else ""))
                    if self.textures.skipped:
                        text += f", {len(self.textures.skipped)} texture packs skipped (see Textures...)"
                    problems = self.textures.problems + self.fonts.problems + self.strings.problems
                    if problems:
                        text += f", {len(problems)} pack/font problem(s), first: {problems[0]}"
                        print("\n".join(problems))
                    self.status.config(text=text)
                    sel = self.tree.selection()
                    if sel:
                        self.show_package(self.shown[sel[0]])
                elif kind == "parsed":
                    self.parse_status[msg[1]] = msg[2]
                elif kind == "parse_done":
                    self.busy = False
                    self.status.config(text=f"Parsed {msg[1] + msg[2]}: {msg[1]} ok, {msg[2]} with errors, "
                                            f"{msg[3]} with warnings (details in the console)")
                    self.refresh()
                elif kind == "error":
                    self.busy = False
                    self.status.config(text=f"Error: {msg[1]}")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---- FNG list

    def sort_by(self, col):
        self.sort_rev = (not self.sort_rev) if col == self.sort_col else False
        self.sort_col = col
        self.refresh()

    def sort_key(self, e):
        return {
            "name": lambda: e.name.lower(),
            "file": lambda: os.path.basename(e.path).lower(),
            "size": lambda: e.size,
            "unpacked": lambda: e.unpacked,
            "state": lambda: e.error or e.codec,
            "parse": lambda: self.parse_status.get(key_of(e), ""),
        }[self.sort_col]()

    def refresh(self):
        needle = self.filter_var.get().strip().lower()
        rows = [e for e in self.entries
                if not needle or needle in e.name.lower() or needle in os.path.basename(e.path).lower()]
        rows.sort(key=self.sort_key, reverse=self.sort_rev)

        self.tree.delete(*self.tree.get_children())
        self.shown = {}
        for e in rows:
            state = e.error or (e.codec if e.packed else "plain")
            item = self.tree.insert("", "end", values=(
                e.name or "?", os.path.basename(e.path), f"{e.size:,}", f"{e.unpacked:,}", state,
                self.parse_status.get(key_of(e), "")))
            self.shown[item] = e

    def on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        if self._programmatic:
            self._programmatic = False
        else:
            self.nav_stack.clear()                  # the user picked an FNG by hand: Back starts again
        entry = self.shown[sel[0]]
        self.detail.config(text=f"{entry.path}  @ 0x{entry.offset:X}  (block 0x{entry.block:X})")
        self.show_package(entry)

    # ---- package tree

    def show_package(self, entry):
        self.current_entry = entry
        self.pkg.delete(*self.pkg.get_children())
        self.builders = {}
        self.node_resource = {}
        self.node_object = {}
        self.object_items = {}
        key = key_of(entry)
        pkg = self.cache.get(key)
        if pkg is None:
            status, pkg = self.parse_entry(entry)
            self.parse_status[key] = status
            if pkg is None:
                self.pkg.insert("", "end", text="Could not read this FNG", values=(status,))
                self._update_row(entry)
                self.scene = None
                self.player_label.config(image="")
                self.player_info.config(text="Could not read this FNG")
                return
            if len(self.cache) >= 8:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = pkg
            self._update_row(entry)

        self.fill_package(pkg)
        self.load_scene(pkg)

    def _update_row(self, entry):
        for item, e in self.shown.items():
            if e is entry:
                self.tree.set(item, "parse", self.parse_status.get(key_of(entry), ""))
                break

    def add(self, parent, text, value="", builder=None, open_=False):
        """Adds a node. builder(item) adds the children when the node is opened the first time."""
        item = self.pkg.insert(parent, "end", text=text, values=(value,))
        if builder:
            self.pkg.insert(item, "end", text="...")
            self.builders[item] = builder
            if open_:
                self.pkg.item(item, open=True)
                self.on_open_item(item)
        return item

    def on_open(self, _event):
        self.on_open_item(self.pkg.focus())

    def on_open_item(self, item):
        builder = self.builders.pop(item, None)
        if builder:
            self.pkg.delete(*self.pkg.get_children(item))
            builder(item)

    def fill_package(self, pkg):
        self.add("", f"Package {pkg.name}", pkg.filename, lambda it: self.fill_info(it, pkg), open_=True)
        self.add("", f"Resources ({len(pkg.resources)})", "", lambda it: self.fill_resources(it, pkg), open_=True)
        roots = [o for o in pkg.objects if o.parent is None]
        self.add("", f"Objects ({len(pkg.objects)})", "",
                 lambda it: [self.add_object(it, pkg, o) for o in roots], open_=True)
        if pkg.responses:
            self.add("", f"Package responses ({len(pkg.responses)})", "", lambda it: self.fill_responses(it, pkg, pkg.responses))
        if pkg.message_definitions:
            self.add("", f"Messages ({len(pkg.message_definitions)})", "",
                     lambda it: [self.add(it, n, c) for n, c in pkg.message_definitions])
        if pkg.warnings:
            self.add("", f"Warnings ({len(pkg.warnings)})", "", lambda it: [self.add(it, w) for w in pkg.warnings], open_=True)
        self.add("", "Chunk layout", "", lambda it: [
            self.add(it, "  " * d + name, f"@0x{off:X}, {size} bytes") for d, name, off, size in pkg.chunk_tree])

    def fill_info(self, item, pkg):
        self.add(item, "Version", pkg.version)
        self.add(item, "Resource names", pkg.resource_name_count)
        self.add(item, "Buttons (Butn)", "" if pkg.button_map_count is None else pkg.button_map_count)
        self.add(item, "Object types (id, bytes)", ", ".join(f"{a}:{b}" for a, b in pkg.type_sizes))

    def fill_resources(self, item, pkg):
        for r in pkg.resources:
            tex = self.texture_of(r)
            if tex is not None:
                value = f"{r.type_name}: {tex.width}x{tex.height} {tex.format_name} in {os.path.basename(tex.path)}"
            elif r.type_name == "Font":
                font = self.font_of(r)
                choice = self.font_choices.get(resource_key(r.name))
                chosen = (" (your choice" + (f", scale {choice['scale']:g}" if choice["scale"] != 1 else "") + ")") if choice and font is not None else ""
                value = (f"Font: {font.name}{chosen}, {len(font.glyphs)} glyphs, sheet {font.sheet_name}" if font is not None
                         else "Font: not found (Choose font...)" if self.fonts is not None else "Font, not indexed yet")
            elif self.textures is None:
                value = f"{r.type_name}, textures not indexed yet"
            else:
                value = f"{r.type_name}: texture not found"
            node = self.add(item, f"[{r.index}] {r.name}", value)
            self.node_resource[node] = r

    def add_object(self, parent, pkg, obj):
        label = obj.name or pkg.hash_names.get(obj.name_hash) or f"0x{obj.name_hash:08X}"
        extra = f"guid 0x{obj.guid:X}"
        shown = self.string_for(obj) or obj.text.get("value")
        if shown:
            extra += f"  \"{shown.replace(chr(10), ' | ')[:60]}\""
        node = self.add(parent, f"{obj.type_name}  {label}", extra, lambda it: self.fill_object(it, pkg, obj))
        if obj.resource is not None:
            self.node_resource[node] = obj.resource
        self.object_items[node] = obj
        if obj.type_name == "String":
            self.node_object[node] = obj

    def fill_object(self, item, pkg, obj):
        if obj.resource is not None:
            self.add(item, "Resource", f"{obj.resource.name} ({obj.resource.type_name})")
        names = fng_parse.flag_names(obj.flags)
        self.add(item, "Flags", f"0x{obj.flags:08X}  " + " ".join(names))
        if obj.image_flags is not None:
            self.add(item, "Image flags", f"0x{obj.image_flags:X}")
        if obj.props or obj.raw_data is not None:
            def props(it):
                for k, v in obj.props.items():
                    self.add(it, k, fmt(v))
                if obj.raw_data is not None:
                    self.add(it, f"raw data ({len(obj.raw_data)} bytes)", obj.raw_data.hex())
            self.add(item, "Properties", "", props)
        if obj.text:
            def text_fields(it):
                shown = self.string_for(obj)
                if shown is not None:
                    self.add(it, "text from language file", shown.replace("\n", " | "))
                for k, v in obj.text.items():
                    self.add(it, k, fmt(v))
            self.add(item, "Text", "", text_fields)
        if any(t is not None for t in obj.textures):
            self.add(item, "Textures", "", lambda it: [
                self.add(it, f"Texture {i + 1}", f"{fmt(t)}  flags {fmt(obj.texture_flags[i])}")
                for i, t in enumerate(obj.textures) if t is not None])
        if obj.scripts:
            self.add(item, f"Scripts ({len(obj.scripts)})", "", lambda it: [self.add_script(it, pkg, s) for s in obj.scripts])
        if obj.responses:
            self.add(item, f"Responses ({len(obj.responses)})", "", lambda it: self.fill_responses(it, pkg, obj.responses))
        for child in obj.children:
            self.add_object(item, pkg, child)

    def add_script(self, parent, pkg, script):
        value = f"length {script.length}, flags 0x{script.flags:X}"
        if script.chained_id is not None:
            value += f", then {pkg.name_of(script.chained_id)}"

        def fill(it):
            for t in script.tracks:
                text = f"{t.param_name}, {t.interp_name}, {len(t.keys)} keys, length {t.length}"
                self.add(it, t.name, text, lambda i2, t=t: [
                    self.add(i2, "base" if time == -1 else f"t={time}", fmt(v)) for time, v in t.keys])
            if script.events:
                self.add(it, f"Events ({len(script.events)})", "", lambda i2: [
                    self.add(i2, f"t={t}", f"{pkg.name_of(ev)} -> 0x{tg:X}") for ev, tg, t in script.events])
        self.add(parent, f"Script {script.name or pkg.name_of(script.id)}", value, fill)

    def fill_responses(self, item, pkg, responses):
        for r in responses:
            self.add(item, f"On {pkg.name_of(r.id)}", f"{len(r.commands)} commands", lambda it, r=r: [
                self.add(it, fng_parse.describe_command(pkg, c)) for c in r.commands])


    # ---- textures

    def texture_of(self, resource):
        """The texture of an image resource, or None."""
        if self.textures is None or resource.type_name not in ("Image", "AnimatedImage", "MultiImage") or not resource.name:
            return None
        return self.textures.find(resource.name, fng_parse.bin_hash)

    def on_pkg_select(self, _event):
        sel = self.pkg.selection()
        res = self.node_resource.get(sel[0]) if sel else None
        is_font = res is not None and res.type_name == "Font"
        self.choose_font_button.config(state="normal" if is_font else "disabled")
        self.clear_font_button.config(state="normal" if is_font and resource_key(res.name) in self.font_choices else "disabled")
        if res is None:
            return
        if res.type_name == "Font":
            obj = self.node_object.get(sel[0])
            text = ""
            if obj is not None:
                text = self.string_for(obj) or obj.text.get("value") or obj.text.get("label") or ""
            self.show_font(self.font_of(res), text)
            return
        tex = self.texture_of(res)
        if tex is None:
            self.show_picture(None, f"{res.name}: no texture found" if self.textures is not None else f"{res.name}: textures are not indexed yet")
        else:
            self.show_picture(tex)

    def show_picture(self, tex, message="", label=None, text_label=None):
        """Shows a texture (or a message) in the preview. Decodes in the main thread, textures are small."""
        label = label or self.preview_image
        text_label = text_label or self.preview_text
        if tex is None:
            label.config(image="")
            text_label.config(text=message)
            return
        if not HAVE_TK_IMAGES:
            label.config(image="")
            text_label.config(text="Pillow with ImageTk is needed to show textures (pip install pillow)")
            return
        key = (tex.path, tex.pixel_pos)
        photo = self.photos.get(key)
        try:
            if photo is None:
                photo = self.make_photo(fng_tpk.decode(tex))
                if len(self.photos) > 40:
                    self.photos.pop(next(iter(self.photos)))
                self.photos[key] = photo
        except fng_tpk.TpkError as e:
            label.config(image="")
            text_label.config(text=str(e))
            return
        label.config(image=photo)
        label.image = photo
        text_label.config(text=f"{tex.name}  {tex.width}x{tex.height}  {tex.format_name}  "
                               f"(pack {tex.tpk}, {os.path.basename(tex.path)})")

    @staticmethod
    def make_photo(img, scale=1, limit=PREVIEW_SIZE):
        """PhotoImage of an RGBA image on a checker background, scaled up by scale, then down to fit limit."""
        if scale != 1:
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        img = img.copy()
        img.thumbnail(limit)
        back = Image.new("RGBA", img.size, (60, 60, 60, 255))
        for y in range(0, img.size[1], 8):                  # checker, so transparent parts show
            for x in range(0, img.size[0], 8):
                if (x // 8 + y // 8) % 2:
                    back.paste((90, 90, 90, 255), (x, y, x + 8, y + 8))
        return ImageTk.PhotoImage(Image.alpha_composite(back, img))

    # ---- fonts

    def font_of(self, resource):
        """The font of a font resource: the one chosen by hand, else the one found by name."""
        if self.fonts is None or not resource.name:
            return None
        choice = self.font_choices.get(resource_key(resource.name))
        if choice and choice["font"] in self.fonts.fonts:
            return self.fonts.fonts[choice["font"]]
        return self.fonts.find(resource.name)

    def font_scale_of(self, resource):
        """Scale set with a hand-picked font (1.0 when none)."""
        if self.fonts is None or resource is None or not resource.name:
            return 1.0
        choice = self.font_choices.get(resource_key(resource.name))
        return float(choice["scale"]) if choice and choice["font"] in self.fonts.fonts else 1.0

    def _load_font_choices(self):
        """{resource key: {"font": name, "scale": number}}. The first version stored only the font name."""
        try:
            with open(os.path.join(SETTINGS_DIR, FONT_CHOICES_FILE), encoding="utf-8") as f:
                raw = json.load(f)
            out = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    out[str(k).upper()] = {"font": str(v.get("font", "")).upper(), "scale": float(v.get("scale", 1.0))}
                else:
                    out[str(k).upper()] = {"font": str(v).upper(), "scale": 1.0}
            return out
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def _save_font_choices(self):
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(os.path.join(SETTINGS_DIR, FONT_CHOICES_FILE), "w", encoding="utf-8") as f:
                json.dump(self.font_choices, f, indent=2, sort_keys=True)
        except OSError as e:
            self.status.config(text=f"Could not save the font choices: {e}")

    def selected_font_resource(self):
        sel = self.pkg.selection()
        res = self.node_resource.get(sel[0]) if sel else None
        return res if res is not None and res.type_name == "Font" else None

    def on_pkg_right_click(self, event):
        item = self.pkg.identify_row(event.y)
        res = self.node_resource.get(item) if item else None
        if res is not None and res.type_name == "Font":
            self.pkg.selection_set(item)
            self.pkg_menu.tk_popup(event.x_root, event.y_root)

    def choose_font_for_selection(self):
        res = self.selected_font_resource()
        if res is None:
            self.preview_text.config(text="Select a font resource or a text object first.")
            return
        self.choose_font(res)

    def set_font_choice(self, resource, font, scale=1.0):
        """Uses a font (and a text scale) for every font resource with this name, in all FNGs. Kept for the next start."""
        self.font_choices[resource_key(resource.name)] = {"font": font.name.upper(), "scale": float(scale)}
        self._save_font_choices()
        self._after_font_choice()

    def clear_font_choice(self):
        res = self.selected_font_resource()
        if res is not None and self.font_choices.pop(resource_key(res.name), None) is not None:
            self._save_font_choices()
            self._after_font_choice()

    def _after_font_choice(self):
        sel = self.tree.selection()
        if sel:
            self.show_package(self.shown[sel[0]])         # tree, scene and preview with the new font
        self._mark_dirty()

    def choose_font(self, resource):
        """Window to pick the font for a font resource that is not in the folder (or to replace one)."""
        if self.fonts is None or not self.fonts.fonts:
            self.preview_text.config(text="No fonts are loaded. Open a folder first.")
            return
        win = tk.Toplevel(self)
        win.title(f"Font for {resource.name}")
        win.geometry("780x440")
        win.transient(self)
        panes = ttk.PanedWindow(win, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        lst = ttk.Treeview(left, columns=("glyphs", "sheet"), selectmode="browse")
        lst.heading("#0", text="Font")
        lst.heading("glyphs", text="Glyphs")
        lst.heading("sheet", text="Sheet")
        lst.column("#0", width=190)
        lst.column("glyphs", width=60, anchor="e")
        lst.column("sheet", width=140)
        lst.pack(fill="both", expand=True)
        panes.add(left, weight=2)
        right = ttk.Frame(panes)
        picture = ttk.Label(right, anchor="center")
        picture.pack(fill="both", expand=True)
        caption = ttk.Label(right, text="", anchor="w")
        caption.pack(fill="x")
        scale_row = ttk.Frame(right)
        scale_row.pack(fill="x")
        ttk.Label(scale_row, text="Scale").pack(side="left")
        scale_var = tk.StringVar(value=f"{self.font_scale_of(resource):g}")
        scale_box = ttk.Combobox(scale_row, textvariable=scale_var, values=["0.25", "0.33", "0.5", "0.67", "0.75", "1", "1.25", "1.5", "2"], width=6)
        scale_box.pack(side="left", padx=4)
        ttk.Label(scale_row, text="1 = size of the font. Smaller or larger until it looks like the game.").pack(side="left")
        panes.add(right, weight=3)

        def scale_value():
            try:
                return min(4.0, max(0.1, float(scale_var.get())))
            except ValueError:
                return 1.0

        items = {}
        current = self.font_of(resource)
        for name in sorted(self.fonts.fonts):
            font = self.fonts.fonts[name]
            item = lst.insert("", "end", text=font.name, values=(len(font.glyphs), font.sheet_name))
            items[item] = font
            if font is current:
                lst.selection_set(item)
        sel_obj = next((o for o in self.node_object.values() if o.resource is resource), None)
        sample = (self.string_for(sel_obj) or sel_obj.text.get("value") or sel_obj.text.get("label")) if sel_obj else ""

        def pick(_event=None):
            sel = lst.selection()
            if sel:
                self.show_font(items[sel[0]], sample or "", label=picture, text_label=caption, scale=scale_value())

        def use():
            sel = lst.selection()
            if sel:
                font = items[sel[0]]
                scale = scale_value()
                win.destroy()
                self.set_font_choice(resource, font, scale)
        lst.bind("<<TreeviewSelect>>", pick)
        lst.bind("<Double-1>", lambda _e: use())
        scale_box.bind("<<ComboboxSelected>>", pick)
        scale_box.bind("<Return>", pick)
        buttons = ttk.Frame(win, padding=4)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Use this font", command=use).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=4)
        ttk.Label(buttons, text=f"Applies to every font resource named {resource_key(resource.name)}.").pack(side="left")
        pick()

    def font_sheet(self, font):
        """(glyph sheet as a Pillow image, texture) or (None, message)."""
        if self.textures is None:
            return None, "textures are not indexed yet"
        for name in font.sheet_candidates():
            tex = self.textures.find(name, fng_parse.bin_hash)
            if tex is not None:
                key = (tex.path, tex.pixel_pos)
                if key not in self.sheets:
                    try:
                        self.sheets[key] = fng_tpk.decode(tex)
                    except fng_tpk.TpkError as e:
                        return None, str(e)
                return self.sheets[key], tex
        return None, f"glyph sheet {font.sheet_name} not found (tried {', '.join(font.sheet_candidates())})"

    def show_font(self, font, text="", label=None, text_label=None, scale=1.0):
        """Draws sample text (or the given text) with a font in the preview."""
        label = label or self.preview_image
        text_label = text_label or self.preview_text
        if font is None:
            label.config(image="")
            text_label.config(text="Font not found")
            return
        if not HAVE_TK_IMAGES:
            label.config(image="")
            text_label.config(text="Pillow with ImageTk is needed to draw text (pip install pillow)")
            return
        sheet, tex = self.font_sheet(font)
        if sheet is None:
            label.config(image="")
            text_label.config(text=f"{font.name}: {tex}")
            return
        sample = text or "The quick brown fox jumps over the lazy dog.\nABCDEFGHIJKLM NOPQRSTUVWXYZ 0123456789 .,:;!?"
        try:
            image = fng_font.render_text(font, sheet, sample)
            if scale != 1.0:
                image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                                     Image.NEAREST if scale >= 1 else Image.BILINEAR)
            photo = self.make_photo(image, scale=2)
        except fng_font.FontError as e:
            label.config(image="")
            text_label.config(text=str(e))
            return
        label.config(image=photo)
        label.image = photo
        text_label.config(text=f"{font.name}: {len(font.glyphs)} glyphs, {len(font.kerning)} kerning pairs, line {font.line_height}, "
                               f"sheet {tex.name} {tex.width}x{tex.height}" + (f"  warnings: {'; '.join(font.warnings)}" if font.warnings else ""))

    def show_texture_list(self):
        if self.textures is None:
            self.status.config(text="Textures are not indexed yet. Open a folder first.")
            return
        win = tk.Toplevel(self)
        win.title(f"Textures ({len(self.textures.textures)})")
        win.geometry("1000x560")
        panes = ttk.PanedWindow(win, orient="horizontal")
        panes.pack(fill="both", expand=True)
        notes = self.textures.skipped + self.textures.problems
        if notes:
            ttk.Label(win, text=f"{len(notes)} packs or textures not used (compressed, empty or damaged):").pack(side="bottom", fill="x", before=panes)
            box = tk.Text(win, height=6, wrap="none", font=("Courier New", 9))
            box.insert("end", "\n".join(notes))
            box.config(state="disabled")
            box.pack(side="bottom", fill="x", before=panes)
        left = ttk.Frame(panes)
        cols = ("name", "size", "format", "pack", "file")
        lst = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for cid, heading, width in (("name", "Name", 160), ("size", "Size", 80), ("format", "Format", 80),
                                    ("pack", "Pack", 110), ("file", "File", 110)):
            lst.heading(cid, text=heading)
            lst.column(cid, width=width, anchor="w")
        sb = ttk.Scrollbar(left, orient="vertical", command=lst.yview)
        lst.configure(yscrollcommand=sb.set)
        lst.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        panes.add(left, weight=3)
        right = ttk.Frame(panes)
        picture = ttk.Label(right, anchor="center")
        picture.pack(fill="both", expand=True)
        caption = ttk.Label(right, text="", anchor="w")
        caption.pack(fill="x")
        panes.add(right, weight=2)

        items = {}
        for tex in sorted(self.textures.textures, key=lambda t: t.name.lower()):
            item = lst.insert("", "end", values=(tex.name, f"{tex.width}x{tex.height}", tex.format_name,
                                                 tex.tpk, os.path.basename(tex.path)))
            items[item] = tex

        def pick(_event):
            sel = lst.selection()
            if sel:
                self.show_picture(items[sel[0]], label=picture, text_label=caption)
        lst.bind("<<TreeviewSelect>>", pick)


    # ---- font blocks

    def show_font_blocks(self):
        if not self.folder:
            self.status.config(text="Open a folder first.")
            return
        win = tk.Toplevel(self)
        win.title("Font blocks")
        win.geometry("1100x600")
        top = ttk.Frame(win, padding=4)
        top.pack(fill="x")
        info = ttk.Label(top, text="Reading...")
        info.pack(side="left")
        panes = ttk.PanedWindow(win, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes)
        lst = ttk.Treeview(left, columns=("kind", "file", "offset", "size", "parse"), show="headings", selectmode="browse")
        for cid, heading, width in (("kind", "Block", 70), ("file", "File", 90), ("offset", "Offset", 70), ("size", "Size", 60),
                                    ("parse", "Parse", 200)):
            lst.heading(cid, text=heading)
            lst.column(cid, width=width, anchor="w")
        lst.pack(fill="both", expand=True)
        panes.add(left, weight=1)

        right = ttk.Frame(panes)
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        sample = ttk.Label(right, anchor="center")
        sample.pack(fill="x")
        sample_text = ttk.Label(right, text="", anchor="w")
        sample_text.pack(fill="x")
        hexframe = ttk.Frame(right)
        hexframe.pack(fill="both", expand=True)
        text = tk.Text(hexframe, font=("Courier New", 9), wrap="none")
        sb = ttk.Scrollbar(hexframe, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        panes.add(right, weight=3)

        refs = {}
        current = {"data": b""}
        out = queue.Queue()

        def copy_hex():
            win.clipboard_clear()
            win.clipboard_append(current["data"].hex())
            info.config(text=f"Copied {len(current['data'])} bytes as hex to the clipboard.")
        ttk.Button(bar, text="Copy hex (whole block)", command=copy_hex).pack(side="left")
        ttk.Label(bar, text="  Shows the first 4096 bytes. Copy takes the whole block.").pack(side="left")

        def pick(_event):
            sel = lst.selection()
            if not sel:
                return
            ref = refs[sel[0]]
            data = fng_bun.read_block(ref)
            current["data"] = data
            try:
                font = fng_font.parse_font(data)
                self.show_font(font, "", label=sample, text_label=sample_text)
            except (fng_font.FontError, Exception) as e:
                sample.config(image="")
                sample_text.config(text=f"Not parsed as a font: {e}")
            text.delete("1.0", "end")
            text.insert("end", self.hexdump(data[:4096]))
            if len(data) > 4096:
                text.insert("end", f"... {len(data) - 4096} more bytes\n")
        lst.bind("<<TreeviewSelect>>", pick)

        def work():
            found = []
            files = fng_bun.find_files(self.folder, self.recursive.get(), exts=(".bun", ".bin"))
            for path in files:
                try:
                    if path.lower().endswith(".bin") and os.path.getsize(path) > fng_bun.SMALL_BIN:
                        continue
                    found += fng_bun.scan_blocks(path, fng_bun.FONT_BLOCKS)
                except OSError:
                    continue
            out.put(found)
        threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                found = out.get_nowait()
            except queue.Empty:
                if win.winfo_exists():
                    win.after(100, poll)
                return
            info.config(text=f"{len(found)} font blocks")
            for ref in found:
                try:
                    font = fng_font.parse_font(fng_bun.read_block(ref))
                    status = f"{font.name}: {len(font.glyphs)} glyphs, {len(font.kerning)} kerning"
                except Exception as e:
                    status = f"ERROR: {e}"
                item = lst.insert("", "end", values=(fng_bun.FONT_BLOCKS[ref.id], os.path.basename(ref.path),
                                                     f"0x{ref.offset:X}", f"{ref.size:,}", status))
                refs[item] = ref
        win.after(100, poll)

    @staticmethod
    def hexdump(data, base=0):
        lines = []
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            hexpart = " ".join(f"{b:02X}" for b in chunk).ljust(47)
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{base + i:06X}  {hexpart}  {ascii_}")
        return "\n".join(lines) + "\n"

    # ---- find text

    def show_find_text(self):
        """Searches the files of the folder for a text and shows where it is: file, offset, enclosing blocks, hex."""
        if not self.folder:
            self.status.config(text="Open a folder first.")
            return
        win = tk.Toplevel(self)
        win.title("Find text in the files")
        win.geometry("1100x640")
        top = ttk.Frame(win, padding=4)
        top.pack(fill="x")
        ttk.Label(top, text="Text").pack(side="left")
        res = self.selected_font_resource()
        query = tk.StringVar(value=resource_key(res.name).lower() if res is not None else "font_nfs_title")
        entry = ttk.Entry(top, textvariable=query, width=34)
        entry.pack(side="left", padx=4)
        info = ttk.Label(top, text="Plain text, any case. Searches .bun and small .bin files.")
        panes = ttk.PanedWindow(win, orient="vertical")
        lst = ttk.Treeview(panes, columns=("file", "offset", "blocks"), show="headings", selectmode="browse")
        for cid, heading, width in (("file", "File", 160), ("offset", "Offset", 90), ("blocks", "Inside blocks (outer > inner)", 700)):
            lst.heading(cid, text=heading)
            lst.column(cid, width=width, anchor="w")
        panes.add(lst, weight=2)
        text = tk.Text(panes, font=("Courier New", 9), wrap="none", height=14)
        panes.add(text, weight=2)
        hits = {}
        out = queue.Queue()
        state = {"running": False, "region": b""}

        def search():
            if state["running"] or not query.get():
                return
            state["running"] = True
            lst.delete(*lst.get_children())
            hits.clear()
            needle = query.get()
            info.config(text="Searching...")

            def work():
                files = fng_bun.find_files(self.folder, self.recursive.get(), exts=(".bun", ".fng", ".bin"))
                total = 0
                for i, path in enumerate(files):
                    try:
                        if path.lower().endswith(".bin") and os.path.getsize(path) > fng_bun.SMALL_BIN:
                            continue
                        found = fng_bun.find_bytes(path, needle)
                    except (OSError, ValueError):
                        continue
                    total += len(found)
                    out.put(("hits", path, found, i + 1, len(files)))
                out.put(("done", total))
            threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                while True:
                    msg = out.get_nowait()
                    if msg[0] == "hits":
                        _, path, found, i, n = msg
                        info.config(text=f"Searching {i}/{n}: {os.path.basename(path)}")
                        for offset, blocks in found:
                            names = " > ".join(f"0x{cid:08X}" + (f" ({fng_bun.FONT_BLOCKS[cid]})" if cid in fng_bun.FONT_BLOCKS else "")
                                               for cid, _o, _s in blocks) or "(no block)"
                            item = lst.insert("", "end", values=(os.path.basename(path), f"0x{offset:X}", names))
                            hits[item] = (path, offset)
                    else:
                        state["running"] = False
                        info.config(text=f"{msg[1]} hits.")
            except queue.Empty:
                pass
            if win.winfo_exists():
                win.after(100, poll)

        def pick(_event):
            sel = lst.selection()
            if not sel:
                return
            path, offset = hits[sel[0]]
            start = max(0, offset - 96) & ~0xF
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(320)
            state["region"] = data
            text.delete("1.0", "end")
            text.insert("end", f"{path}\n\n" + self.hexdump(data, start))

        def copy_hex():
            win.clipboard_clear()
            win.clipboard_append(state["region"].hex())
            info.config(text=f"Copied {len(state['region'])} bytes as hex.")
        ttk.Button(top, text="Search", command=search).pack(side="left")
        ttk.Button(top, text="Copy hex of the area", command=copy_hex).pack(side="left", padx=4)
        info.pack(side="left", padx=8)
        panes.pack(fill="both", expand=True)
        lst.bind("<<TreeviewSelect>>", pick)
        entry.bind("<Return>", lambda _e: search())
        win.after(100, poll)
        search()


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else None
    FngPlayer(folder).mainloop()


if __name__ == "__main__":
    main()
