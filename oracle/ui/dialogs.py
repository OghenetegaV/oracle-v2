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
from .widgets import button


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

    def ask_reason(self, title: str, prompt: str, *, required: bool = False, ok_text: str = "Record decision") -> Optional[str]:
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
        button(row, ok_text, ok, "primary").pack(side="right")
        button(row, "Cancel", win.destroy).pack(side="right", padx=6)
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

    # ---------------------------------------------------------------- the calmer review

    def ask_reject_view(self, names: list, consequences: Optional[dict] = None) -> Optional[tuple]:
        """Why should Oracle exclude these views? Returns (reason_code, explanation) or None. Nothing is deleted: the dialog says so."""
        from oracle.application.guide import REJECT_REASONS
        win = self._form("Reject view")
        result = {"value": None}
        subject = f"\u201c{names[0]}\u201d" if len(names) == 1 else f"these {len(names)} views"
        tk.Label(win, text="Reject View", font=theme.H2, bg=theme.BG).pack(anchor="w", padx=20, pady=(16, 0))
        tk.Label(win, text=f"Why should Oracle exclude {subject}?", font=theme.BODY, bg=theme.BG, wraplength=440, justify="left").pack(anchor="w", padx=20, pady=(6, 6))
        code = tk.StringVar(value="")
        for value, label in REJECT_REASONS:
            tk.Radiobutton(win, text=label, value=value, variable=code, font=theme.BODY, bg=theme.BG, activebackground=theme.BG, anchor="w",
                           selectcolor=theme.PANEL).pack(anchor="w", padx=28)
        tk.Label(win, text="Optional explanation", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED).pack(anchor="w", padx=20, pady=(10, 2))
        box = tk.Text(win, width=56, height=3, font=theme.BODY, wrap="word", relief="flat", highlightthickness=1, highlightbackground=theme.LINE_STRONG)
        box.pack(padx=20)
        note = ("Rejecting this view removes it from the active architectural interpretation. The original drawing evidence and your decision "
                "remain stored.")
        if consequences and (consequences.get("questions") or consequences.get("issues")):
            parts = []
            if consequences.get("questions"):
                parts.append(f"{consequences['questions']} open question(s)")
            if consequences.get("issues"):
                parts.append(f"{consequences['issues']} note(s)")
            note += " It also closes " + " and ".join(parts) + " that only concern " + ("this view." if len(names) == 1 else "these views.")
        tk.Label(win, text=note, font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, wraplength=440, justify="left").pack(anchor="w", padx=20, pady=(10, 0))
        message = tk.Label(win, text="", font=theme.SMALL, bg=theme.BG, fg=theme.RED)
        message.pack(anchor="w", padx=20)

        def ok():
            if not code.get():
                message.config(text="Choose a reason.")
                return
            text = box.get("1.0", "end").strip()
            if code.get() == "other" and not text:
                message.config(text="Say why in the explanation.")
                return
            result["value"] = (code.get(), text)
            win.destroy()

        row = tk.Frame(win, bg=theme.BG)
        row.pack(fill="x", padx=20, pady=14)
        button(row, "Reject View", ok, "danger").pack(side="right")
        button(row, "Cancel", win.destroy).pack(side="right", padx=8)
        self._finish(win)
        return result["value"]

    def ask_engineer_input(self, subject: str, suggestions: Optional[list] = None) -> Optional[tuple]:
        """Ask Engineer: what should Oracle understand? Free text, not limited to Oracle's suggestions. Returns (statement, notes) or None."""
        win = self._form("Ask Engineer")
        result = {"value": None}
        tk.Label(win, text="Ask Engineer", font=theme.H2, bg=theme.BG).pack(anchor="w", padx=20, pady=(16, 0))
        tk.Label(win, text=subject, font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, wraplength=500, justify="left").pack(anchor="w", padx=20, pady=(2, 6))
        if suggestions:
            tk.Label(win, text="Oracle's suggestions: " + "; ".join(suggestions[:4]), font=theme.SMALL, bg=theme.BG, fg=theme.AMBER,
                     wraplength=500, justify="left").pack(anchor="w", padx=20, pady=(0, 6))
        tk.Label(win, text="What should Oracle understand this to be?", font=theme.BOLD, bg=theme.BG).pack(anchor="w", padx=20)
        statement = tk.Text(win, width=62, height=5, font=theme.BODY, wrap="word", relief="flat", highlightthickness=1, highlightbackground=theme.LINE_STRONG)
        statement.pack(padx=20, pady=(2, 8))
        statement.focus_set()
        tk.Label(win, text="Additional notes (optional)", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED).pack(anchor="w", padx=20)
        notes = tk.Text(win, width=62, height=3, font=theme.BODY, wrap="word", relief="flat", highlightthickness=1, highlightbackground=theme.LINE_STRONG)
        notes.pack(padx=20, pady=(2, 6))
        tk.Label(win, text="Your words are saved exactly as written, with your name and the time, as engineer input. Oracle does not change the model "
                           "from free text; it keeps it as guidance for the next stage.", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, wraplength=500,
                 justify="left").pack(anchor="w", padx=20)
        message = tk.Label(win, text="", font=theme.SMALL, bg=theme.BG, fg=theme.RED)
        message.pack(anchor="w", padx=20)

        def ok():
            text = statement.get("1.0", "end").strip()
            if not text:
                message.config(text="Write what Oracle should understand.")
                return
            result["value"] = (text, notes.get("1.0", "end").strip())
            win.destroy()

        row = tk.Frame(win, bg=theme.BG)
        row.pack(fill="x", padx=20, pady=14)
        button(row, "Submit Engineer Input", ok, "primary").pack(side="right")
        button(row, "Cancel", win.destroy).pack(side="right", padx=8)
        self._finish(win)
        return result["value"]

    def ask_number(self, title: str, prompt: str, unit: str = "") -> Optional[str]:
        """One number typed by the engineer (the caller validates it). Returns the text or None."""
        return self.ask_text(title, prompt + (f" ({unit})" if unit else ""), "")

    def ask_level(self, view_name: str, options: list) -> Optional[str]:
        """Which floor is this plan? `options` is [(label, key)]; 'Another name...' lets the engineer type one. Returns a level key or None."""
        win = self._form("Set floor")
        result = {"value": None}
        tk.Label(win, text=f"Which floor is \u201c{view_name}\u201d?", font=theme.H2, bg=theme.BG, wraplength=420, justify="left").pack(anchor="w", padx=20, pady=(16, 6))
        choice = tk.StringVar(value="")
        for label, key in options:
            tk.Radiobutton(win, text=label, value=key, variable=choice, font=theme.BODY, bg=theme.BG, activebackground=theme.BG, anchor="w",
                           selectcolor=theme.PANEL).pack(anchor="w", padx=28)
        other = tk.Frame(win, bg=theme.BG)
        other.pack(anchor="w", padx=28, pady=(2, 0))
        tk.Radiobutton(other, text="Another name:", value="__other__", variable=choice, font=theme.BODY, bg=theme.BG, activebackground=theme.BG,
                       selectcolor=theme.PANEL).pack(side="left")
        typed = tk.Entry(other, width=22, font=theme.BODY)
        typed.pack(side="left", padx=4)
        message = tk.Label(win, text="", font=theme.SMALL, bg=theme.BG, fg=theme.RED)
        message.pack(anchor="w", padx=20)

        def ok():
            if not choice.get():
                message.config(text="Choose a floor.")
                return
            if choice.get() == "__other__":
                name = "_".join(typed.get().strip().upper().split())
                if not name:
                    message.config(text="Type the floor's name.")
                    return
                result["value"] = f"NAMED:{name}"
            else:
                result["value"] = choice.get()
            win.destroy()

        row = tk.Frame(win, bg=theme.BG)
        row.pack(fill="x", padx=20, pady=14)
        button(row, "Set floor", ok, "primary").pack(side="right")
        button(row, "Cancel", win.destroy).pack(side="right", padx=8)
        self._finish(win)
        return result["value"]

    def ask_view_type(self, view_name: str, current_label: str, choices: list, current_key: str) -> Optional[tuple]:
        """Change View Type: the view is NOT rejected, only re-classified. `choices` is [(key, label)]. Returns (key, note) or None."""
        win = self._form("Change View Type")
        result = {"value": None}
        tk.Label(win, text="Change View Type", font=theme.H2, bg=theme.BG).pack(anchor="w", padx=20, pady=(16, 0))
        tk.Label(win, text=f"\u201c{view_name}\u201d", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED).pack(anchor="w", padx=20)
        tk.Label(win, text=f"Oracle currently identifies this view as:  {current_label.upper()}", font=theme.BODY, bg=theme.BG, wraplength=440,
                 justify="left").pack(anchor="w", padx=20, pady=(8, 2))
        tk.Label(win, text="What should it be?", font=theme.BOLD, bg=theme.BG).pack(anchor="w", padx=20, pady=(4, 2))
        choice = tk.StringVar(value=current_key)
        for key, label in choices:
            tk.Radiobutton(win, text=label, value=key, variable=choice, font=theme.BODY, bg=theme.BG, activebackground=theme.BG, anchor="w",
                           selectcolor=theme.PANEL).pack(anchor="w", padx=28)
        tk.Label(win, text="Optional note", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED).pack(anchor="w", padx=20, pady=(8, 2))
        note = tk.Text(win, width=56, height=3, font=theme.BODY, wrap="word", relief="flat", highlightthickness=1, highlightbackground=theme.LINE_STRONG)
        note.pack(padx=20)
        tk.Label(win, text="The view stays; only its type changes. Oracle's original reading and your correction are both kept in the history. "
                           "To exclude a view instead, use Reject View.", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, wraplength=440,
                 justify="left").pack(anchor="w", padx=20, pady=(8, 0))
        message = tk.Label(win, text="", font=theme.SMALL, bg=theme.BG, fg=theme.RED)
        message.pack(anchor="w", padx=20)

        def ok():
            if choice.get() == current_key:
                message.config(text="Choose a different type, or Cancel.")
                return
            result["value"] = (choice.get(), note.get("1.0", "end").strip())
            win.destroy()

        row = tk.Frame(win, bg=theme.BG)
        row.pack(fill="x", padx=20, pady=14)
        button(row, "Apply Engineer Decision", ok, "primary").pack(side="right")
        button(row, "Cancel", win.destroy).pack(side="right", padx=8)
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
