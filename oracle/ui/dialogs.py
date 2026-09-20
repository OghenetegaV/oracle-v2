"""Oracle — Interface Dialogs and Prompts

Purpose:
    Every question the architectural workspace puts to the engineer in a small window: a reason for a decision, a line of text, a yes/no
    confirmation, an error or information message, file choosers, the "establish levels" table (an elevation per detected level, the kind
    of elevation, a reason) and the "split a view" and "align a plan" forms. All of them are methods of ONE class, Prompts, which the
    workspace holds; a test replaces that object with a scripted one, so the workspace's behaviour can be driven without a modal window
    ever opening.

Role in Oracle:
    Keeps dialog code out of the workspace's logic. A dialog only COLLECTS what the engineer typed and returns it; it never changes the
    project. The workspace passes the result to the session, which records the engineer decision.

Dependencies:
    tkinter; oracle.ui.theme (fonts and colours).

Consumers:
    oracle.ui.architectural_workspace, tests (as a scripted replacement).

Status:
    Interface (interface phase).

Migration/Notes:
    Every dialog returns None when the engineer cancels. Numbers are validated here (a value that is not a number is refused in the form),
    but whether the value is acceptable to the model is the domain's decision, reported back as a refusal.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import theme


class Prompts:
    def __init__(self, parent):
        self.parent = parent

    # ---------------------------------------------------------------- simple messages and choosers

    def error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self.parent)

    def info(self, title: str, message: str) -> None:
        messagebox.showinfo(title, message, parent=self.parent)

    def confirm(self, title: str, message: str) -> bool:
        return bool(messagebox.askyesno(title, message, parent=self.parent))

    def ask_open_drawing(self, initial_dir: Optional[str] = None) -> Optional[str]:
        return filedialog.askopenfilename(parent=self.parent, title="Select an architectural drawing", initialdir=initial_dir,
                                          filetypes=[("CAD drawings", "*.dwg *.dxf"), ("All files", "*.*")]) or None

    def ask_open_project(self, initial_dir: Optional[str] = None) -> Optional[str]:
        return filedialog.askopenfilename(parent=self.parent, title="Open an Oracle project", initialdir=initial_dir,
                                          filetypes=[("Oracle projects", "*.oracle.json"), ("JSON files", "*.json"), ("All files", "*.*")]) or None

    def ask_save_project(self, initial_dir: Optional[str] = None, initial_name: str = "project.oracle.json") -> Optional[str]:
        return filedialog.asksaveasfilename(parent=self.parent, title="Save Oracle project", initialdir=initial_dir, initialfile=initial_name,
                                            defaultextension=".oracle.json", filetypes=[("Oracle projects", "*.oracle.json")]) or None

    # ---------------------------------------------------------------- forms

    def _form(self, title: str, width: int = 520):
        win = tk.Toplevel(self.parent)
        win.title(title)
        win.transient(self.parent.winfo_toplevel())
        win.configure(bg=theme.BG)
        win.resizable(False, False)
        return win

    def _finish(self, win) -> None:
        win.update_idletasks()
        top = self.parent.winfo_toplevel()
        x = top.winfo_rootx() + max(0, (top.winfo_width() - win.winfo_width()) // 2)
        y = top.winfo_rooty() + max(0, (top.winfo_height() - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.wait_window()

    def ask_reason(self, title: str, prompt: str, *, required: bool = False) -> Optional[str]:
        """The engineer's reason for a decision (kept in the project). Returns "" for none, None for cancel."""
        win = self._form(title)
        result = {"value": None}
        tk.Label(win, text=prompt, font=theme.BODY, bg=theme.BG, wraplength=460, justify="left").pack(anchor="w", padx=16, pady=(14, 6))
        entry = tk.Text(win, width=58, height=4, font=theme.BODY, wrap="word")
        entry.pack(padx=16)
        entry.focus_set()
        message = tk.Label(win, text="", font=theme.SMALL, bg=theme.BG, fg=theme.RED)
        message.pack(anchor="w", padx=16)

        def ok():
            text = entry.get("1.0", "end").strip()
            if required and not text:
                message.config(text="A reason is required for this decision.")
                return
            result["value"] = text
            win.destroy()

        row = tk.Frame(win, bg=theme.BG)
        row.pack(fill="x", padx=16, pady=12)
        tk.Button(row, text="Record decision", command=ok, bg=theme.ACCENT, fg="white", relief="flat", padx=12, pady=4).pack(side="right")
        tk.Button(row, text="Cancel", command=win.destroy, relief="flat", padx=12, pady=4).pack(side="right", padx=6)
        self._finish(win)
        return result["value"]

    def ask_text(self, title: str, prompt: str, initial: str = "") -> Optional[str]:
        win = self._form(title)
        result = {"value": None}
        tk.Label(win, text=prompt, font=theme.BODY, bg=theme.BG, wraplength=420, justify="left").pack(anchor="w", padx=16, pady=(14, 6))
        var = tk.StringVar(value=initial)
        entry = tk.Entry(win, textvariable=var, width=52, font=theme.BODY)
        entry.pack(padx=16)
        entry.focus_set()
        entry.select_range(0, "end")

        def ok():
            result["value"] = var.get().strip()
            win.destroy()

        row = tk.Frame(win, bg=theme.BG)
        row.pack(fill="x", padx=16, pady=12)
        tk.Button(row, text="OK", command=ok, bg=theme.ACCENT, fg="white", relief="flat", padx=14, pady=4).pack(side="right")
        tk.Button(row, text="Cancel", command=win.destroy, relief="flat", padx=12, pady=4).pack(side="right", padx=6)
        win.bind("<Return>", lambda e: ok())
        self._finish(win)
        return result["value"]

    def ask_establish_levels(self, keys_and_labels: list, suggestion: Optional[dict], note: str) -> Optional[dict]:
        """A table of the detected levels: one elevation entry each, prefilled ONLY from Oracle's suggestion (which is not accepted until the
        engineer presses Establish). Returns {"elevations": {key: mm}, "elevation_type": str, "reason": str} or None."""
        win = self._form("Establish building levels", 640)
        result = {"value": None}
        tk.Label(win, text="Establish building levels", font=theme.SUBTITLE, bg=theme.BG).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(win, text=note, font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, wraplength=600, justify="left").pack(anchor="w", padx=16)
        grid = tk.Frame(win, bg=theme.BG)
        grid.pack(anchor="w", padx=16, pady=8)
        for col, head in enumerate(("Level", "Drawing label", "Elevation (mm)", "")):
            tk.Label(grid, text=head, font=theme.BOLD, bg=theme.BG).grid(row=0, column=col, sticky="w", padx=4)
        entries = {}
        for row, (key, label) in enumerate(keys_and_labels, start=1):
            tk.Label(grid, text=key, font=theme.BODY, bg=theme.BG).grid(row=row, column=0, sticky="w", padx=4, pady=2)
            tk.Label(grid, text=label, font=theme.BODY, bg=theme.BG).grid(row=row, column=1, sticky="w", padx=4)
            var = tk.StringVar(value=("" if not suggestion or key not in suggestion else f"{suggestion[key]:g}"))
            tk.Entry(grid, textvariable=var, width=14, font=theme.BODY, justify="right").grid(row=row, column=2, padx=4)
            tk.Label(grid, text=("Oracle suggests" if suggestion and key in suggestion else "you supply"), font=theme.SMALL, bg=theme.BG,
                     fg=theme.MUTED).grid(row=row, column=3, sticky="w")
            entries[key] = var
        tk.Label(win, text="What kind of elevation are these?", font=theme.BODY, bg=theme.BG).pack(anchor="w", padx=16, pady=(6, 0))
        kind = tk.StringVar(value="unspecified")
        combo = ttk.Combobox(win, textvariable=kind, state="readonly", width=40,
                             values=("unspecified", "finished_floor", "structural", "datum"))
        combo.pack(anchor="w", padx=16)
        tk.Label(win, text="Oracle never turns a finished-floor level into a structural level; say what these are, or leave unspecified.",
                 font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, wraplength=600, justify="left").pack(anchor="w", padx=16)
        tk.Label(win, text="Reason (recorded with the decision)", font=theme.BODY, bg=theme.BG).pack(anchor="w", padx=16, pady=(8, 0))
        reason = tk.Entry(win, width=70, font=theme.BODY)
        reason.pack(anchor="w", padx=16)
        message = tk.Label(win, text="", font=theme.SMALL, bg=theme.BG, fg=theme.RED)
        message.pack(anchor="w", padx=16)

        def ok():
            values = {}
            for key, var in entries.items():
                try:
                    values[key] = float(var.get().replace(",", ""))
                except ValueError:
                    message.config(text=f"Enter an elevation in millimetres for {key}.")
                    return
            result["value"] = {"elevations": values, "elevation_type": kind.get(), "reason": reason.get().strip()}
            win.destroy()

        row = tk.Frame(win, bg=theme.BG)
        row.pack(fill="x", padx=16, pady=12)
        tk.Button(row, text="Establish levels", command=ok, bg=theme.ACCENT, fg="white", relief="flat", padx=12, pady=4).pack(side="right")
        tk.Button(row, text="Cancel", command=win.destroy, relief="flat", padx=12, pady=4).pack(side="right", padx=6)
        self._finish(win)
        return result["value"]

    def ask_pair(self, title: str, prompt: str, labels: tuple, initial: tuple = ("0", "0")) -> Optional[tuple]:
        """Two numbers (for a plan alignment translation) or an axis and a coordinate. Returns the two strings, or None."""
        win = self._form(title)
        result = {"value": None}
        tk.Label(win, text=prompt, font=theme.BODY, bg=theme.BG, wraplength=420, justify="left").pack(anchor="w", padx=16, pady=(14, 6))
        vars_ = []
        for label, start in zip(labels, initial):
            row = tk.Frame(win, bg=theme.BG)
            row.pack(anchor="w", padx=16, pady=2)
            tk.Label(row, text=label, width=22, anchor="w", font=theme.BODY, bg=theme.BG).pack(side="left")
            var = tk.StringVar(value=start)
            tk.Entry(row, textvariable=var, width=20, font=theme.BODY).pack(side="left")
            vars_.append(var)

        def ok():
            result["value"] = tuple(v.get().strip() for v in vars_)
            win.destroy()

        row = tk.Frame(win, bg=theme.BG)
        row.pack(fill="x", padx=16, pady=12)
        tk.Button(row, text="OK", command=ok, bg=theme.ACCENT, fg="white", relief="flat", padx=14, pady=4).pack(side="right")
        tk.Button(row, text="Cancel", command=win.destroy, relief="flat", padx=12, pady=4).pack(side="right", padx=6)
        self._finish(win)
        return result["value"]
