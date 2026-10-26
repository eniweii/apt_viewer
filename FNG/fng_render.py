#!/usr/bin/env python3
"""
fng_render.py

Plays the scripts of a parsed FNG package and draws the objects with Pillow.
Port of FEngRender (RenderTreeNode.Update, TrackHelpers.Interpolate, GLRenderTreeRenderer).
Rules taken from there:
  - The screen is centered: x from -320 to 320 and y from -240 to 240 (y down) for 4:3.
  - Every object starts on its INIT script at time 0. A script that ends starts its chained script,
    or loops (flag 1), or stays on its last values.
  - A track has a base key (time -1) and delta keys. The value is the base plus the interpolated delta.
  - Object matrix = scale(size) * rotation around the pivot * translation(position) * parent matrix.
  - Colors multiply down the parent chain (FEngRender uses (a * b + 128) >> 8, here a * b / 255 so white stays white).
  - Drawing order: far (high z) first. Alpha blend, nearest sampling.
Messages: post_message() runs the message responses (SetScript on the owner object, PostMessageToFEng to an
object or the package, IfScriptEquals / Else / EndIf). Script events post their message when the script time
reaches them. Commands that go to another package (SwitchToPackage, PushPackage..., PopPackage) are collected
for the window (take_navigation). Game and sound messages and button commands are only written to the log.
A multi image draws its three textures one over the other, each lerped by its own alpha (texture 1 at the bottom).
objects_at(x, y) gives the objects drawn under a point (top first), and hidden holds guids the user hid.
Input device: scene.device is "pc" (keyboard), "console" (controller) or "both". Objects with the ConsoleOnly flag
are not drawn for "pc", objects with PCOnly are not drawn for "console". The game shows the keyboard icons or
the controller icons this way, so both sets overlap when the device is ignored.
Buttons and focus (no layout is stored in a package, so this follows what the engine does): an object with the
IsButton flag (and not DontNavigate / IgnoreButton) that is drawn can have the focus. set_focus() posts UNFOCUS to
the old button and FOCUS to the new one, to the button and its children (their responses, else the script of
that name). navigate() moves the focus to the nearest button in a direction. post_input() sends an input message
to the focused button and its children and to the package. SetActiveButton, RecordCurrentButton and
RecallRecordedButton commands work.
Speed: an object keeps its values and matrix while its script has no tracks (or has ended) and its parents
do not move; its picture (layer) is cached under a signature of everything that shapes it; a frame whose
signatures are all unchanged is returned from the last render. invalidate() clears this (after a language or
texture change).
Not done: movies, effects, models.
Differences on purpose: a colored image multiplies its corner colors with the blended object color.
"""
import math

from PIL import Image, ImageChops, ImageDraw

import fng_font
import fng_parse

IDENTITY_Q = (0.0, 0.0, 0.0, 1.0)
FLAG_INVISIBLE = 0x1
FLAG_HIDE_IN_EDIT = 0x80000000
FLAG_PC_ONLY = 0x8
FLAG_CONSOLE_ONLY = 0x40
FLAG_IS_BUTTON = 0x10000000
FLAG_DONT_NAVIGATE = 0x80000
FLAG_IGNORE_BUTTON = 0x4000000
INIT_ID = fng_parse.bin_hash("INIT")

TYPE_IMAGE, TYPE_STRING, TYPE_GROUP, TYPE_MOVIE, TYPE_COLORED, TYPE_SIMPLE, TYPE_MULTI = 1, 2, 5, 7, 9, 0xB, 0xC


# ---- small vector / quaternion / matrix helpers (row vectors, like System.Numerics)

def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + (ay * bz - az * by),
            aw * by + ay * bw + (az * bx - ax * bz),
            aw * bz + az * bw + (ax * by - ay * bx),
            aw * bw - (ax * bx + ay * by + az * bz))


def _qlerp(a, b, t):
    dot = sum(x * y for x, y in zip(a, b))
    sign = 1.0 if dot >= 0 else -1.0
    q = tuple((1 - t) * x + sign * t * y for x, y in zip(a, b))
    n = math.sqrt(sum(x * x for x in q))
    return tuple(x / n for x in q) if n else IDENTITY_Q


def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _identity():
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _translation(v):
    m = _identity()
    m[3][0], m[3][1], m[3][2] = v
    return m


def _scale(v):
    m = _identity()
    m[0][0], m[1][1], m[2][2] = v
    return m


def _rotation(q):
    x, y, z, w = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    m = _identity()
    m[0][0], m[0][1], m[0][2] = 1 - 2 * (yy + zz), 2 * (xy + wz), 2 * (xz - wy)
    m[1][0], m[1][1], m[1][2] = 2 * (xy - wz), 1 - 2 * (zz + xx), 2 * (yz + wx)
    m[2][0], m[2][1], m[2][2] = 2 * (xz + wy), 2 * (yz - wx), 1 - 2 * (yy + xx)
    return m


def _point(m, x, y, z):
    return (x * m[0][0] + y * m[1][0] + z * m[2][0] + m[3][0],
            x * m[0][1] + y * m[1][1] + z * m[2][1] + m[3][1])


# ---- track values

def _add(kind, a, b):
    if kind == "quat":
        return _qmul(a, b)
    return tuple(x + y for x, y in zip(a, b))


def _lerp(kind, a, b, t, base):
    if kind == "color":
        return tuple(o + c1 + int((c2 - c1) * t + 0.5) for o, c1, c2 in zip(base, a, b))
    if kind == "quat":
        return _qmul(_qlerp(a, b, t), base)
    return tuple(x + (y - x) * t + o for x, y, o in zip(a, b, base))


_KIND = {3: "v2", 4: "v3", 5: "quat", 6: "color"}
_NAVIGATION = {0x200: "switch", 0x201: "push", 0x202: "push", 0x204: "push", 0x203: "pop"}


def interpolate(track, time):
    """Value of a track at a time, or None if the track has no usable keys."""
    kind = _KIND.get(track.param_type)
    if kind is None or not track.keys:
        return None
    base, deltas = track.keys[0][1], track.keys[1:]          # the first key (time -1) is the base
    if not deltas:
        return base

    i = 0                                           # TrackHelpers: first delta key with time >= time
    while i + 1 < len(deltas) and deltas[i][0] < time:
        i += 1
    nxt_time, nxt = deltas[i]
    length = track.length
    if track.interp_action == 0:
        if i > 0 and time < nxt_time:
            prev_time, prev = deltas[i - 1]
            span = nxt_time - prev_time
            t = (time - prev_time) / span if span else 1.0
        else:
            prev_time, prev, t = nxt_time, nxt, 1.0
    else:
        if time == nxt_time:
            prev_time, prev, t = nxt_time, nxt, 0.0
        elif time < nxt_time:
            if i > 0:
                prev_time, prev = deltas[i - 1]
                span = nxt_time - prev_time
                t = (time - prev_time) / span if span else 1.0
            else:
                j = 0
                while j + 1 < len(deltas) and deltas[j][0] < length:
                    j += 1
                prev_time, prev = deltas[j]
                dist = length - prev_time
                div = dist + nxt_time
                t = (time + dist) / div if div else 1.0
        else:
            prev_time, prev = nxt_time, nxt
            div = nxt_time + length - prev_time
            t = (time - prev_time) / div if div else 1.0

    if t == 0:
        return _add(kind, base, prev)
    if abs(t - 1) < 1e-12:
        return _add(kind, base, nxt)
    return _lerp(kind, prev, nxt, t, base)


def _script_tracks(script):
    cache = getattr(script, "_by_name", None)
    if cache is None:
        cache = {t.name: t for t in script.tracks}
        script._by_name = cache
    return cache


def _inside(poly, x, y):
    """True if the point is inside a convex polygon (any winding)."""
    sign = 0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if cross:
            if sign and (cross > 0) != (sign > 0):
                return False
            sign = 1 if cross > 0 else -1
    return sign != 0


def _alpha_possible(obj):
    """False if the color alpha of an object is 0 in its data and in every color track (it can never show)."""
    if obj.props.get("Color", (255, 255, 255, 255))[3] > 0:
        return True
    for script in obj.scripts:
        for track in script.tracks:
            if track.name == "Color" and track.keys and track.param_type == 6:
                base, deltas = track.keys[0][1], [v for _t, v in track.keys[1:]]
                alphas = [base[3] + v[3] for v in deltas] if deltas else [base[3]]
                if any(a > 0 for a in alphas):
                    return True
    return False


def device_hides(flags, device):
    """True if an object with these flags is not shown for an input device ("pc", "console" or "both")."""
    return (device == "pc" and bool(flags & FLAG_CONSOLE_ONLY)) or (device == "console" and bool(flags & FLAG_PC_ONLY))


def can_be_visible(obj, device="both"):
    """False for objects that are never drawn: an invisible flag or zero alpha on the object or a parent, or
    flags for the other input device. Many FNGs keep such leftovers from beta builds and earlier games."""
    while obj is not None:
        if obj.flags & (FLAG_INVISIBLE | FLAG_HIDE_IN_EDIT) or device_hides(obj.flags, device) or not _alpha_possible(obj):
            return False
        obj = obj.parent
    return True


class Resolver:
    """What the scene needs from outside: pictures and fonts. The window supplies the real ones."""

    def image(self, obj):
        """RGBA Pillow image for an image object, or None."""
        return None

    def multi_image(self, obj):
        return None

    def multi_images(self, obj):
        """Up to three RGBA images (None for one that is missing) for the textures of a multi image."""
        return [self.multi_image(obj), None, None]

    def font(self, obj):
        """(Font, glyph sheet image) for a text object, or None."""
        return None

    def text(self, obj):
        """The text to show for a text object (from the language files), or None to use the text in the FNG."""
        return None

    def font_scale(self, obj):
        """Extra scale for the text of an object (1.0 unless a font was chosen by hand with a scale)."""
        return 1.0


class Node:
    def __init__(self, obj):
        self.obj = obj
        self.children = []
        self.script = None
        self.time = -1
        self.values = {}                # property name -> value at the current time
        self.matrix = _identity()
        self.color = (255, 255, 255, 255)   # blended with the parents
        self.fired = set()              # indexes of the script events already posted in this run of the script
        self.fired_script = None
        self.fired_time = 0
        self.values_script = None       # script and time the values were computed for (the speed cache)
        self.values_time = None
        self.view_cache = None          # parent matrix and parent color the matrix was computed for
        self.parent_color_cache = None
        self.sig = None                 # everything that shapes the picture of the node
        self.cache = None               # (signature, canvas size, layer, x0, y0, polygon) of the last draw

    def find_script(self, script_id):
        for s in self.obj.scripts:
            if s.id == script_id:
                return s
        return None


class Scene:
    def __init__(self, pkg, resolver=None):
        self.pkg = pkg
        self.resolver = resolver or Resolver()
        self.nodes = {o.guid: Node(o) for o in pkg.objects}
        self.roots = []
        self.log = []                   # lines about messages, taken by take_log()
        self.navigation = []            # ("switch" | "push" | "pop", package name) requested by responses
        self.device = "both"            # "pc", "console" or "both": which input device the screen is drawn for
        self.focus_guid = 0             # the button that has the focus
        self.recorded_focus = 0         # RecordCurrentButton
        self._visible_nodes = set()     # nodes that can be drawn in the last render
        self._drawn_map = {}            # node -> polygon of the last render
        self._canvas_size = (640, 480)
        self.revision = 0               # changes with invalidate(), part of every picture signature
        self.frame_id = 0               # changes whenever render() makes a new picture (not when it reuses one)
        self._frame_key = None
        self._frame = None
        self.hidden = set()             # guids of objects the user hid (with their children)
        self._drawn = []                # (node, polygon in canvas pixels) of the last render, back to front
        self._pending = []              # (message, target guid, origin text) posted by script events
        for o in pkg.objects:
            node = self.nodes[o.guid]
            if o.parent is None:
                self.roots.append(node)
            else:
                self.nodes[o.parent.guid].children.append(node)
        self.reset()

    # ---- scripts

    def script_names(self):
        """Names of all scripts in the package, INIT first."""
        names = {}
        for o in self.pkg.objects:
            for s in o.scripts:
                names[s.id] = s.name or self.pkg.name_of(s.id)
        return sorted(names.values(), key=lambda n: (n != "INIT", n.upper()))

    def reset(self):
        """Every object on INIT at time 0."""
        for node in self.nodes.values():
            node.script = None
            node.time = -1
            node.fired = set()
            node.fired_script = None
            node.fired_time = 0
            node.values = {}
            node.values_script = None
            node.cache = None
        self._frame_key = None
        self.log = []
        self.navigation = []
        self._pending = []
        self.update(0)

    def run_script(self, name):
        """Starts a script by name on every object that has it, at time 0."""
        script_id = fng_parse.bin_hash(name.upper())
        for node in self.nodes.values():
            script = node.find_script(script_id)
            if script is not None:
                node.script = script
                node.time = 0
        self.update(0)

    def update(self, dt_ms):
        self._update_nodes(self.roots, _identity(), None, int(dt_ms))
        rounds = 0
        while self._pending and rounds < 6:             # events post messages, those can start scripts with events
            rounds += 1
            pending, self._pending = self._pending, []
            for message, target, origin in pending:
                self._deliver(message, target, origin, 0)
            self._update_nodes(self.roots, _identity(), None, 0)
        self._pending = []

    # ---- messages

    def take_log(self):
        lines, self.log = self.log, []
        return lines

    def take_navigation(self):
        """Requests to go to another package, in the order they were made."""
        requests, self.navigation = self.navigation, []
        return requests

    def message_names(self):
        """[(name, hash)] of every message the package answers, sends or defines, sorted by name."""
        hashes = {r.id for r in self.pkg.responses}
        for o in self.pkg.objects:
            hashes |= {r.id for r in o.responses}
            for sc in o.scripts:
                hashes |= {ev for ev, _t, _tm in sc.events}
        for name, _cat in self.pkg.message_definitions:
            hashes.add(fng_parse.bin_hash(name.upper()))
        return sorted(((self.pkg.name_of(h), h) for h in hashes), key=lambda nh: nh[0].upper())

    def post_message(self, message, target=0):
        """Sends a message (hash or name) to the package (target 0) or to one object, then updates at time 0."""
        if isinstance(message, str):
            message = int(message, 16) if message.lower().startswith("0x") else fng_parse.bin_hash(message.upper())
        self._deliver(message, target, "", 0)
        self.update(0)

    # ---- buttons and focus

    def focusable(self):
        """Buttons that are drawn now, in object order."""
        visible = self._visible_nodes
        if not visible:                                 # no render yet
            order = []
            self._collect(self.roots, order)
            visible = set(order)
        return [n for n in self.nodes.values()
                if n.obj.flags & FLAG_IS_BUTTON and not n.obj.flags & (FLAG_DONT_NAVIGATE | FLAG_IGNORE_BUTTON)
                and n in visible and n.color[3] > 0]

    def subtree(self, node):
        out = [node]
        for child in node.children:
            out += self.subtree(child)
        return out

    def node_bounds(self, node):
        """(x0, y0, x1, y1) in canvas pixels of a node and its children as drawn, or None."""
        points = [pt for n in self.subtree(node) for pt in (self._drawn_map.get(n) or [])]
        if not points:                                  # not drawn (a group, or no render yet): use its position
            m = node.matrix
            w, h = self._canvas_size
            x, y = w / 2 + m[3][0], h / 2 + m[3][1]
            return x, y, x, y
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return min(xs), min(ys), max(xs), max(ys)

    def node_center(self, node):
        x0, y0, x1, y1 = self.node_bounds(node)
        return (x0 + x1) / 2, (y0 + y1) / 2

    def button_of(self, node):
        """The button a node belongs to (itself or the nearest parent that is a focusable button), or None."""
        buttons = {id(n) for n in self.focusable()}
        while node is not None:
            if id(node) in buttons:
                return node
            node = self.nodes.get(node.obj.parent.guid) if node.obj.parent is not None else None
        return None

    def focus_first(self):
        buttons = self.focusable()
        if buttons and self.nodes.get(self.focus_guid) not in buttons:
            self.set_focus(buttons[0].obj.guid)

    def set_focus(self, guid):
        node = self.nodes.get(guid)
        if node is None or guid == self.focus_guid:
            return
        old = self.nodes.get(self.focus_guid)
        if old is not None:
            self._focus_event(old, "UNFOCUS")
        self.focus_guid = guid
        self.log.append(f"focus: [{self._label(node)}]")
        self._focus_event(node, "FOCUS")
        self.update(0)

    def _focus_event(self, node, name):
        """UNFOCUS / FOCUS: to the button and its children. Without a response, the script of that name runs."""
        message = fng_parse.bin_hash(name)
        if self._deliver_subtree(message, node, "  "):
            return
        started = []
        for n in self.subtree(node):
            script = n.find_script(message)
            if script is not None:
                n.script = script
                n.time = 0
                started.append(self._label(n))
        if started:
            self.log.append(f"  {name}: no response, script {name} started on {len(started)} object(s)")

    def _deliver_subtree(self, message, node, origin):
        """Runs the responses to a message of a node and its children. Returns True if there were any."""
        answered = False
        for n in self.subtree(node):
            for r in n.obj.responses:
                if r.id == message:
                    if not answered:
                        self.log.append(f"{origin}message {self.pkg.name_of(message)} to [{self._label(node)}]")
                        answered = True
                    self._run_commands(n, r, 0)
        return answered

    def message_hashes(self):
        """Every message the package answers."""
        hashes = {r.id for r in self.pkg.responses}
        for o in self.pkg.objects:
            hashes |= {r.id for r in o.responses}
        return hashes

    def answers_input(self, message):
        """True if the focused button (or its children) or the package has a response to the message."""
        node = self.nodes.get(self.focus_guid)
        if any(r.id == message for r in self.pkg.responses):
            return True
        return node is not None and any(r.id == message for n in self.subtree(node) for r in n.obj.responses)

    def post_input(self, message, name=""):
        """An input message (accept, back, ...): to the focused button and its children, then to the package."""
        node = self.nodes.get(self.focus_guid)
        self.log.append(f"input {name or self.pkg.name_of(message)}")
        answered = node is not None and self._deliver_subtree(message, node, "  ")
        for r in self.pkg.responses:
            if r.id == message:
                answered = True
                self._run_commands(None, r, 0)
        if not answered:
            self.log.append("  (no response)")
        self.update(0)
        return answered

    def navigate(self, direction):
        """Moves the focus to the nearest button in a direction (up, down, left, right). Returns True if it moved."""
        buttons = self.focusable()
        if not buttons:
            return False
        current = self.nodes.get(self.focus_guid)
        if current is None or current not in buttons:
            self.set_focus(buttons[0].obj.guid)
            return True
        cx, cy = self.node_center(current)
        best = None
        for n in buttons:
            if n is current:
                continue
            x, y = self.node_center(n)
            dx, dy = x - cx, y - cy
            primary, off = {"right": (dx, dy), "left": (-dx, dy), "down": (dy, dx), "up": (-dy, dx)}[direction]
            if primary <= 1:
                continue
            score = primary + 2 * abs(off)
            if best is None or score < best[0]:
                best = (score, n)
        if best is None:
            return False
        self.set_focus(best[1].obj.guid)
        return True

    def _label(self, node):
        obj = node.obj
        return obj.name or self.pkg.hash_names.get(obj.name_hash) or f"0x{obj.guid:X}"

    def _deliver(self, message, target, origin, depth):
        if depth > 8:
            self.log.append("  (stopped: messages are nested too deep)")
            return
        where = f" to object 0x{target:X}" if target else ""
        self.log.append(f"{origin}message {self.pkg.name_of(message)}{where}")
        answered = False
        if not target:
            for r in self.pkg.responses:
                if r.id == message:
                    answered = True
                    self._run_commands(None, r, depth)
        if target:
            nodes = [self.nodes[target]] if target in self.nodes else []
            if not nodes:
                self.log.append(f"  (object 0x{target:X} is not in this package)")
        else:
            nodes = list(self.nodes.values())
        for node in nodes:
            for r in node.obj.responses:
                if r.id == message:
                    answered = True
                    self._run_commands(node, r, depth)
        if not answered and (not target or target in self.nodes):
            self.log.append("  (no response)")

    def _run_commands(self, owner, response, depth):
        who = f"  [{self._label(owner)}] " if owner is not None else "  [package] "
        active = True
        stack = []                                      # (active before the If, result of the If)
        for cmd in response.commands:
            cid = cmd["id"]
            text = fng_parse.describe_command(self.pkg, cmd)
            if cid in (0x300, 0x301):
                current = owner.script.id if owner is not None and owner.script is not None else None
                result = (current == cmd["int"]) if cid == 0x300 else (current != cmd["int"])
                stack.append((active, result))
                active = active and result
                self.log.append(f"{who}{text} -> {'yes' if result else 'no'}")
            elif cid == 0x500 and stack:
                before, result = stack[-1]
                active = before and not result
            elif cid == 0x501 and stack:
                active = stack.pop()[0]
            elif not active:
                continue
            elif cid == 0x0:
                self._set_script(owner, cmd["int"], who, text)
            elif cid == 0x1:
                self.log.append(f"{who}{text}")
                self._deliver(cmd["int"] or 0, cmd["target"] or 0, "  ", depth + 1)
            elif cid == 0x100:
                self.log.append(f"{who}{text}")
                self.set_focus(cmd["int"] or cmd["target"] or 0)
            elif cid == 0x102:
                self.recorded_focus = self.focus_guid
                self.log.append(f"{who}{text}")
            elif cid == 0x103:
                self.log.append(f"{who}{text}")
                self.set_focus(self.recorded_focus or cmd["int"] or cmd["target"] or 0)
            elif cid in _NAVIGATION:
                kind = _NAVIGATION[cid]
                self.navigation.append((kind, cmd["str"]))
                self.log.append(f"{who}{text}   (goes to another FNG)")
            else:
                self.log.append(f"{who}{text}   (not run)")

    def _set_script(self, owner, script_id, who, text):
        targets = [owner] if owner is not None else list(self.nodes.values())
        done = False
        for node in targets:
            script = node.find_script(script_id)
            if script is not None:
                node.script = script
                node.time = 0
                done = True
        self.log.append(f"{who}{text}" + ("" if done else "   (script not found)"))

    def _update_nodes(self, nodes, view, parent_color, dt):
        for node in nodes:
            self._update_node(node, view, parent_color, dt)
            if node.obj.type == TYPE_GROUP:
                self._update_nodes(node.children, node.matrix, node.color, dt)

    def _update_node(self, node, view, parent_color, dt):
        obj = node.obj
        if node.script is None:
            node.script = node.find_script(INIT_ID)
            node.time = 0 if node.script else -1
        script = node.script
        if script is not None:
            node.time = max(0, dt + node.time)
            length = script.length
            if node.time >= length:
                chained = node.find_script(script.chained_id) if script.chained_id is not None else None
                if chained is not None:
                    node.time -= length
                    node.script = script = chained
                elif script.flags & 1:
                    node.time = node.time % length if length > 0 else 0
                elif script.chained_id is None:
                    node.time = length + 1

        if script is not None:
            if node.fired_script is not script or node.time < node.fired_time:
                node.fired = set()                      # a new script or a new loop: its events can fire again
                node.fired_script = script
            for index, (event_id, target, at) in enumerate(script.events):
                if index not in node.fired and node.time >= at:
                    node.fired.add(index)
                    self._pending.append((event_id, target, f"  event of [{self._label(node)}] "))
            node.fired_time = node.time

        tracks = _script_tracks(script) if script is not None else {}
        # values stay the same while the script has no tracks, or its time did not move (it ended)
        same_values = bool(node.values) and node.values_script is script and (not tracks or node.values_time == node.time)
        if not same_values:
            values = {}
            for name, base in obj.props.items():
                track = tracks.get(name)
                value = interpolate(track, node.time) if track is not None else None
                values[name] = base if value is None else value
            node.values = values
            node.values_script = script
            node.values_time = node.time
        if same_values and node.sig is not None and node.view_cache == view and node.parent_color_cache == parent_color:
            return                                      # nothing changed: matrix, color and signature are still right

        values = node.values
        pos = values.get("Position", (0.0, 0.0, 0.0))
        size = values.get("Size", (1.0, 1.0, 1.0))
        pivot = values.get("Pivot", (0.0, 0.0, 0.0))
        rot = values.get("Rotation", IDENTITY_Q)
        m = _scale(size)
        if tuple(rot) != IDENTITY_Q:
            m = _mat_mul(_mat_mul(_mat_mul(m, _translation([-v for v in pivot])), _rotation(rot)), _translation(pivot))
        m = _mat_mul(_mat_mul(m, _translation(pos)), view)
        node.matrix = m

        color = values.get("Color", (255, 255, 255, 255))
        color = tuple(max(0, min(255, int(c))) for c in color)
        if parent_color is not None:
            color = tuple((a * b + 127) // 255 for a, b in zip(color, parent_color))
        node.color = color
        node.view_cache = view
        node.parent_color_cache = parent_color
        node.sig = (tuple(tuple(row) for row in m), color, tuple(values.items()))

    # ---- drawing

    def _visible(self, node):
        flags = node.obj.flags
        return (not flags & (FLAG_INVISIBLE | FLAG_HIDE_IN_EDIT) and node.obj.guid not in self.hidden
                and not device_hides(flags, self.device))

    def objects_at(self, x, y):
        """Nodes drawn under a point of the last render, topmost first."""
        return [node for node, poly in reversed(self._drawn) if _inside(poly, x, y)]

    def polygon_of(self, node):
        return self._drawn_map.get(node)

    def _collect(self, nodes, out):
        for node in nodes:
            if not self._visible(node):
                continue
            out.append(node)
            if node.obj.type == TYPE_GROUP:
                self._collect(node.children, out)

    def invalidate(self):
        """Forgets every cached picture. Call it after something outside the scene changed (a texture, a font)."""
        self.revision += 1
        self._frame_key = None
        for node in self.nodes.values():
            node.cache = None

    def _node_signature(self, node):
        text = self.resolver.text(node.obj) if node.obj.type == TYPE_STRING else None     # the language can change
        return (node.sig, text)

    def render(self, width=640, height=480, background=(0, 0, 0, 255)):
        """RGBA image of the current state. (0, 0) of the package is the center of the image."""
        order = []
        self._collect(self.roots, order)

        def key(node):
            z = node.matrix[3][2]
            return (-int(max(0, min(255, z))), -z)
        order.sort(key=key)
        self._visible_nodes = set(order)
        self._canvas_size = (width, height)
        sigs = [self._node_signature(n) for n in order]
        frame_key = (width, height, background, frozenset(self.hidden), self.device, self.revision, tuple(sigs))
        if frame_key == self._frame_key and self._frame is not None:
            return self._frame.copy()                       # nothing changed since the last picture

        canvas = Image.new("RGBA", (width, height), background)
        self._drawn = []
        for node, sig in zip(order, sigs):
            kind = node.obj.type
            if kind in (TYPE_IMAGE, TYPE_COLORED, TYPE_SIMPLE, TYPE_MULTI, TYPE_MOVIE):
                self._draw_quad(canvas, node, sig)
            elif kind == TYPE_STRING:
                self._draw_text(canvas, node, sig)
        self._drawn_map = {n: poly for n, poly in self._drawn}
        self._frame_key = frame_key
        self._frame = canvas.copy()
        self.frame_id += 1
        return canvas

    def _quad_frame(self, node, width, height):
        """Dest position O and edge vectors Ex, Ey of the quad, in canvas pixels: p(qx, qy) = O + qx Ex + qy Ey."""
        m = node.matrix
        cx, cy = width / 2, height / 2
        px, py = _point(m, 0, 0, 1)
        ex = _point(m, 1, 0, 1)
        ey = _point(m, 0, 1, 1)
        return (cx + px, cy + py), (ex[0] - px, ex[1] - py), (ey[0] - px, ey[1] - py)

    def _draw_quad(self, canvas, node, sig):
        cache = node.cache
        if cache is None or cache[0] != sig or cache[1] != canvas.size:
            node.cache = cache = (sig, canvas.size) + self._make_quad(node, *canvas.size)
        _sig, _size, layer, x0, y0, corners = cache
        if node.color[3] > 0:
            self._drawn.append((node, corners))
        if layer is not None:
            canvas.alpha_composite(layer, (x0, y0))

    def _make_quad(self, node, width, height):
        """The picture of a quad object: (layer or None, x0, y0, corners). The layer is placed at (x0, y0)."""
        obj = node.obj
        origin, ex, ey = self._quad_frame(node, width, height)
        corners = [(origin[0] + qx * ex[0] + qy * ey[0], origin[1] + qx * ex[1] + qy * ey[1])
                   for qx, qy in ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5))]
        x0 = max(0, int(math.floor(min(c[0] for c in corners))))
        y0 = max(0, int(math.floor(min(c[1] for c in corners))))
        x1 = min(width, int(math.ceil(max(c[0] for c in corners))))
        y1 = min(height, int(math.ceil(max(c[1] for c in corners))))
        if x1 <= x0 or y1 <= y0 or node.color[3] == 0:
            return None, 0, 0, corners
        det = ex[0] * ey[1] - ex[1] * ey[0]
        if abs(det) < 1e-9:
            return None, 0, 0, corners
        bw, bh = x1 - x0, y1 - y0

        # dest pixel (X, Y) -> quad coords (qx, qy)
        ia, ib = ey[1] / det, -ey[0] / det
        ic, id_ = -ex[1] / det, ex[0] / det

        def sampler(img, uv0, uv1):
            """Pixels of img over the bbox: quad coords map linearly onto the uv rectangle."""
            tw, th = img.size
            su, sv = (uv1[0] - uv0[0]) * tw, (uv1[1] - uv0[1]) * th
            # qx = ia (X - ox) + ib (Y - oy);  sx = tw uv0x + (qx + 0.5) su
            ox, oy = origin
            a, b = ia * su, ib * su
            c = tw * uv0[0] + 0.5 * su - a * ox - b * oy + a * x0 + b * y0
            d, e = ic * sv, id_ * sv
            f = th * uv0[1] + 0.5 * sv - d * ox - e * oy + d * x0 + e * y0
            return img.transform((bw, bh), Image.AFFINE, (a, b, c, d, e, f), resample=Image.NEAREST, fillcolor=(0, 0, 0, 0))

        values = obj.props if not node.values else node.values
        uv0 = values.get("UpperLeft", (0.0, 0.0))
        uv1 = values.get("LowerRight", (1.0, 1.0))
        picture = None
        multi = None
        if obj.type in (TYPE_IMAGE, TYPE_COLORED):
            picture = self.resolver.image(obj)
        elif obj.type == TYPE_MULTI:
            multi = [(i, img) for i, img in enumerate(self.resolver.multi_images(obj)) if img is not None]
            if not multi:
                picture = self.resolver.image(obj)

        if obj.type == TYPE_SIMPLE:
            layer = Image.new("RGBA", (bw, bh), (255, 255, 255, 255))
        elif obj.type == TYPE_MOVIE:
            layer = Image.new("RGBA", (bw, bh), (20, 40, 90, 255))
        elif multi:
            layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            for i, img in multi:                                                   # texture 1 first, 2 and 3 over it
                tl, br = values.get(f"TopLeft{i + 1}"), values.get(f"BottomRight{i + 1}")
                rect = (tl, br) if tl and br and tl != br else (uv0, uv1)
                layer = Image.alpha_composite(layer, sampler(img, rect[0], rect[1]))
        elif picture is None and obj.resource is None and obj.type != TYPE_MULTI:
            layer = Image.new("RGBA", (bw, bh), (255, 255, 255, 255))           # no texture requested: plain color
        elif picture is None:
            layer = Image.new("RGBA", (bw, bh), (200, 0, 200, 110))             # texture not found: magenta
        else:
            layer = sampler(picture, uv0, uv1)

        # keep only the quad itself (the uv rectangle can be smaller than the texture)
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).polygon([(cx - x0, cy - y0) for cx, cy in corners], fill=255)
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))

        tint = node.color
        if obj.type == TYPE_COLORED and all(k in values for k in ("TopLeft", "TopRight", "BottomRight", "BottomLeft")):
            def mix(c):
                return tuple(max(0, min(255, int(a))) for a in c)
            corners_rgba = [mix(values[k]) for k in ("TopLeft", "TopRight", "BottomRight", "BottomLeft")]
            small = Image.new("RGBA", (2, 2))
            small.putdata([corners_rgba[0], corners_rgba[1], corners_rgba[3], corners_rgba[2]])
            gradient = small.resize((64, 64), Image.BILINEAR)
            tint_layer = sampler(gradient, (0.0, 0.0), (1.0, 1.0))
            layer = ImageChops.multiply(layer, tint_layer)
        layer = ImageChops.multiply(layer, Image.new("RGBA", (bw, bh), tint))
        return layer, x0, y0, corners

    def _draw_text(self, canvas, node, sig):
        cache = node.cache
        if cache is None or cache[0] != sig or cache[1] != canvas.size:
            node.cache = cache = (sig, canvas.size) + self._make_text(node, *canvas.size)
        _sig, _size, img, left, top, poly = cache
        if img is None:
            return
        self._drawn.append((node, poly))
        fng_font.paste_clipped(canvas, img, left, top)

    def _make_text(self, node, width, height):
        """The picture of a text object: (image or None, left, top, polygon in canvas pixels)."""
        obj = node.obj
        text = self.resolver.text(obj) or obj.text.get("value") or obj.text.get("label") or ""
        found = self.resolver.font(obj)
        if not text or found is None or node.color[3] == 0:
            return None, 0, 0, None
        font, sheet = found
        origin, ex, ey = self._quad_frame(node, width, height)

        font_scale = self.resolver.font_scale(obj) or 1.0
        max_width = obj.text.get("max_width", 0)
        if 0 < max_width < 4096:
            text = fng_font.wrap_text(font, text, max_width / font_scale)
        leading = obj.text.get("leading", 0)
        if not -50 < leading < 50:
            leading = 0
        formatting = obj.text.get("formatting", 0)
        align = 1 if formatting & 1 else (2 if formatting & 2 else 0)
        pad = 4
        img = fng_font.render_text(font, sheet, text, node.color[:3], pad=pad, align=align, leading=leading)
        w, h = fng_font.measure_text(font, text, leading)
        x_off = -w * 0.5 if formatting & 1 else (-w if formatting & 2 else 0)
        y_off = -h * 0.5 if formatting & 4 else (-h if formatting & 8 else 0)

        # Size of a text object is taken as a scale when it looks like one (0.1 to 8)
        size = node.values.get("Size", (1.0, 1.0, 1.0))
        sx, sy = (size[0], size[1]) if 0.1 <= size[0] <= 8 and 0.1 <= size[1] <= 8 else (1.0, 1.0)
        sx, sy = sx * font_scale, sy * font_scale
        if (sx, sy) != (1.0, 1.0):
            img = img.resize((max(1, int(img.width * sx)), max(1, int(img.height * sy))),
                             Image.NEAREST if sx >= 1 and sy >= 1 else Image.BILINEAR)
            x_off, y_off, pad_x, pad_y = x_off * sx, y_off * sy, pad * sx, pad * sy
        else:
            pad_x = pad_y = pad
        img.putalpha(img.getchannel("A").point(lambda v: v * node.color[3] // 255))
        left0, top0 = origin[0] + x_off, origin[1] + y_off
        poly = [(left0, top0), (left0 + w * sx, top0), (left0 + w * sx, top0 + h * sy), (left0, top0 + h * sy)]
        # origin of the text is the object position
        return img, int(round(origin[0] + x_off - pad_x)), int(round(origin[1] + y_off - pad_y)), poly
