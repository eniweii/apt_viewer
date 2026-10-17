#!/usr/bin/env python3
"""
apt_stub_ui.py

The "Stubs" tab of apt_view.py. Three parts:
  Externals  names the movie reads, calls or creates that it does not define (the game gives them). Double click a row
             to write a stub value (JSON) for it. A call row takes the return value.
  Command    one game side action: call a function, set a variable, or send a clip to a frame. "Run now" does it at
             the current tick. "Add to scenario" keeps it in apt_stubs.json, and it runs again after every reset.
  Scenario   the kept actions, and the log of what ran.
Stub values and actions are evidence level "stub": what the person says the game does, not what the file says.
"""
import json
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk


def _json(text, default=None):
    text = text.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return text                                   # not JSON: a plain string (a frame label, for example)


class StubPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.run_var = tk.BooleanVar(value=True)      # replay the scenario after a reset

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=2, pady=2)
        ttk.Button(bar, text="Refresh", width=8, command=self.refresh).pack(side="left")
        ttk.Button(bar, text="Reset with stubs", command=lambda: self.app.rt_reset()).pack(side="left", padx=3)
        ttk.Button(bar, text="Save file", width=9, command=self.save).pack(side="left")
        ttk.Checkbutton(bar, text="Run scenario on reset", variable=self.run_var).pack(side="left", padx=6)

        panes = ttk.Panedwindow(self, orient="vertical")
        panes.pack(fill="both", expand=True)

        ext = ttk.Frame(panes)
        ttk.Label(ext, text="Externals: read or called by the movie, not defined in it (double click: stub)").pack(anchor="w")
        self.ext = ttk.Treeview(ext, columns=("kind", "count", "stub", "args"), show="tree headings", height=7)
        self.ext.heading("#0", text="name")
        self.ext.column("#0", width=150)
        for c, w in (("kind", 40), ("count", 40), ("stub", 90), ("args", 110)):
            self.ext.heading(c, text=c)
            self.ext.column(c, width=w)
        self.ext.tag_configure("stub", foreground="#2a5db0")
        self.ext.pack(fill="both", expand=True)
        self.ext.bind("<Double-1>", self.on_ext)
        panes.add(ext, weight=1)

        fun = ttk.Frame(panes)
        row = ttk.Frame(fun)
        row.pack(fill="x")
        ttk.Label(row, text="Functions (fewest calls first: the likely entry points of the game)").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *a: self.fill_functions())
        ttk.Entry(row, textvariable=self.filter_var, width=14).pack(side="right")
        self.fun = ttk.Treeview(fun, columns=("owner", "params", "calls"), show="tree headings", height=7)
        self.fun.heading("#0", text="path")
        self.fun.column("#0", width=190)
        for c, w in (("owner", 90), ("params", 90), ("calls", 40)):
            self.fun.heading(c, text=c)
            self.fun.column(c, width=w)
        self.fun.pack(fill="both", expand=True)
        self.fun.bind("<<TreeviewSelect>>", self.on_fun)
        panes.add(fun, weight=2)

        cmd = ttk.Frame(panes)
        r1 = ttk.Frame(cmd)
        r1.pack(fill="x", pady=1)
        self.op_var = tk.StringVar(value="call")
        ttk.Combobox(r1, textvariable=self.op_var, values=("call", "set", "goto"), width=6, state="readonly").pack(side="left")
        self.path_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self.path_var).pack(side="left", fill="x", expand=True, padx=3)
        r2 = ttk.Frame(cmd)
        r2.pack(fill="x", pady=1)
        ttk.Label(r2, text="args / value / frame").pack(side="left")
        self.arg_var = tk.StringVar()
        ttk.Entry(r2, textvariable=self.arg_var).pack(side="left", fill="x", expand=True, padx=3)
        r3 = ttk.Frame(cmd)
        r3.pack(fill="x", pady=1)
        ttk.Label(r3, text="this").pack(side="left")
        self.this_var = tk.StringVar()
        ttk.Entry(r3, textvariable=self.this_var, width=22).pack(side="left", padx=3)
        ttk.Label(r3, text="at tick").pack(side="left")
        self.at_var = tk.IntVar(value=0)
        tk.Spinbox(r3, from_=0, to=100000, width=6, textvariable=self.at_var).pack(side="left", padx=3)
        r4 = ttk.Frame(cmd)
        r4.pack(fill="x", pady=1)
        ttk.Button(r4, text="Run now", command=self.run_now).pack(side="left")
        ttk.Button(r4, text="Add to scenario", command=self.add).pack(side="left", padx=3)
        ttk.Button(r4, text="Remove selected", command=self.remove).pack(side="left")
        self.scn = tk.Listbox(cmd, height=5, exportselection=False, font=("Consolas", 9))
        self.scn.pack(fill="both", expand=True)
        self.log = tk.Text(cmd, height=5, wrap="none", font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True)
        panes.add(cmd, weight=2)

    # ---- data
    def cfg(self):
        mv = self.app.movie
        return self.app.stub_store.get(mv.name) if mv is not None else {"stubs": {}, "scenario": []}

    def refresh(self):
        rt = self.app.rt
        self.ext.delete(*self.ext.get_children())
        self.fun.delete(*self.fun.get_children())
        self.scn.delete(0, "end")
        if rt is None:
            self._log("Turn Runtime on (or press Play / Step) to list externals and functions.")
            return
        stubs = self.cfg()["stubs"]
        for key, e in sorted(rt.vm.externals.items(), key=lambda kv: -kv[1]["count"]):
            has = key in stubs
            self.ext.insert("", "end", iid=key, text=key, tags=("stub",) if has else (),
                            values=(e["kind"], e["count"], json.dumps(stubs[key])[:40] if has else "", ", ".join(e["args"] or [])[:60]))
        for key in stubs:                                  # stubs for names not hit (yet)
            if key not in rt.vm.externals:
                self.ext.insert("", "end", iid=key, text=key, tags=("stub",), values=("", 0, json.dumps(stubs[key])[:40], ""))
        self.funcs = rt.functions()
        self.fill_functions()
        for op in self.cfg()["scenario"]:
            self.scn.insert("end", self._op_text(op))
        self._log("\n".join("%5d  %s" % kv for kv in rt.stub_log[-100:]) or "(nothing ran yet)")

    def fill_functions(self):
        self.fun.delete(*self.fun.get_children())
        rt = self.app.rt
        if rt is None:
            return
        flt = self.filter_var.get().strip().lower()
        rows = []
        for path, owner, f in getattr(self, "funcs", []):
            if flt and flt not in path.lower():
                continue
            rows.append((rt.vm.call_counts.get(f.offset, 0), path, owner, f))
        rows.sort(key=lambda r: (r[0], r[1]))
        for calls, path, owner, f in rows[:600]:
            self.fun.insert("", "end", iid=path, text=path, values=(owner, ", ".join(n for _r, n in f.params), calls))

    @staticmethod
    def _op_text(op):
        k = op.get("op")
        if k == "call":
            body = "(%s)" % ", ".join(json.dumps(a) for a in op.get("args", []))
        elif k == "set":
            body = " = %s" % json.dumps(op.get("value"))
        else:
            body = " -> %s" % json.dumps(op.get("frame"))
        return "@%d  %s %s%s%s" % (op.get("at", 0), k, op.get("path"), body, "   this=%s" % op["this"] if op.get("this") else "")

    def _log(self, text):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---- actions
    def on_ext(self, _e=None):
        sel = self.ext.selection()
        if not sel or self.app.rt is None:
            return
        key = sel[0]
        stubs = self.cfg()["stubs"]
        text = simpledialog.askstring("Stub", "JSON value for %s\n(empty: remove the stub)" % key,
                                      initialvalue=json.dumps(stubs[key]) if key in stubs else "", parent=self)
        if text is None:
            return
        if not text.strip():
            stubs.pop(key, None)
        else:
            try:
                stubs[key] = json.loads(text)
            except ValueError as e:
                messagebox.showerror("Stub", "Not JSON: %s" % e, parent=self)
                return
        self.app.rt.stubs = dict(stubs)                    # later reads see it; "Reset with stubs" replays from tick 0
        self.app.rt.vm.stubs = dict(stubs)
        self.app.rt.vm._stub_objs.pop(key, None)
        self.refresh()

    def on_fun(self, _e=None):
        sel = self.fun.selection()
        if not sel:
            return
        row = next((r for r in self.funcs if r[0] == sel[0]), None)
        self.op_var.set("call")
        self.path_var.set(sel[0])
        self.this_var.set(row[1] if row else "")

    def _op(self):
        op, path, arg = self.op_var.get(), self.path_var.get().strip(), self.arg_var.get()
        if not path:
            return None
        d = {"at": int(self.at_var.get() or 0), "op": op, "path": path}
        if op == "call":
            args = _json(arg, [])
            d["args"] = args if isinstance(args, list) else [args]
            if self.this_var.get().strip():
                d["this"] = self.this_var.get().strip()
        elif op == "set":
            d["value"] = _json(arg, None)
        else:
            d["frame"] = _json(arg, 1)
            d["play"] = False
        return d

    def run_now(self):
        op = self._op()
        if op is None or self.app.rt is None:
            return
        self.app.rt.apply_op(op)
        self.app.redraw()
        self.refresh()

    def add(self):
        op = self._op()
        if op is None or self.app.rt is None:
            return
        self.cfg()["scenario"].append(op)
        self.app.rt.scenario = list(self.cfg()["scenario"])
        self.refresh()

    def remove(self):
        sel = self.scn.curselection()
        if sel:
            del self.cfg()["scenario"][sel[0]]
            if self.app.rt is not None:
                self.app.rt.scenario = list(self.cfg()["scenario"])
            self.refresh()

    def save(self):
        try:
            self.app.stub_store.save()
            self._log("saved %s" % self.app.stub_store.path)
        except OSError as e:
            messagebox.showerror("Stubs", str(e), parent=self)
