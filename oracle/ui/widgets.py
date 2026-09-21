"""Oracle — Small Interface Widgets

Purpose:
    The few reusable pieces the calmer workspace is built from: buttons in three weights (one PRIMARY action per screen, quiet SECONDARY
    ones, borderless links), a label that re-wraps itself to the width it is given, a vertically scrolling frame (so a long engineer input
    or a long question never pushes the actions off the screen), and a small caption used to name the voice of a block of text
    ("ORACLE SUGGESTS", "ENGINEER INPUT", "ENGINEER DECISION").

Role in Oracle:
    Presentation helpers only; nothing here knows about drawings, projects or decisions.

Dependencies:
    tkinter; oracle.ui.theme.

Consumers:
    oracle.ui.architectural_workspace, review_screen, details_window, dialogs.

Status:
    Interface (interface refinement phase).

Migration/Notes:
    Buttons are plain tk.Button (flat, with hover) rather than ttk, so their colours are the same on every Windows theme.
"""

from __future__ import annotations

import tkinter as tk

from . import theme


def button(parent, text: str, command, kind: str = "secondary", **kw) -> tk.Button:
    """kind: primary (filled), secondary (outlined), danger (outlined, red text), link (no border)."""
    base = dict(text=text, command=command, font=theme.BODY, relief="flat", bd=0, cursor="hand2", padx=14, pady=7, takefocus=1)
    if kind == "primary":
        style = dict(bg=theme.ACCENT, fg="white", activebackground=theme.ACCENT_HOVER, activeforeground="white", font=theme.BOLD)
        hover = (theme.ACCENT_HOVER, theme.ACCENT)
    elif kind == "danger":
        style = dict(bg=theme.PANEL, fg=theme.RED, activebackground=theme.RED_BG, activeforeground=theme.RED, highlightthickness=1,
                     highlightbackground=theme.LINE_STRONG, highlightcolor=theme.RED)
        hover = (theme.RED_BG, theme.PANEL)
    elif kind == "link":
        style = dict(bg=kw.pop("bg", theme.BG), fg=theme.ACCENT, activeforeground=theme.ACCENT_HOVER, padx=4, pady=2, font=theme.BODY)
        style["activebackground"] = style["bg"]
        hover = (style["bg"], style["bg"])
    else:
        style = dict(bg=theme.PANEL, fg=theme.INK, activebackground=theme.HOVER, activeforeground=theme.INK, highlightthickness=1,
                     highlightbackground=theme.LINE_STRONG, highlightcolor=theme.ACCENT)
        hover = (theme.HOVER, theme.PANEL)
    base.update(style)
    base.update(kw)
    b = tk.Button(parent, **base)
    b._normal_bg = base["bg"]
    if kind != "link":
        b.bind("<Enter>", lambda e: b.config(bg=hover[0]) if str(b["state"]) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.config(bg=hover[1]))
    else:
        b.bind("<Enter>", lambda e: b.config(font=(theme.FONT, 10, "underline")))
        b.bind("<Leave>", lambda e: b.config(font=theme.BODY))
    return b


class WrapLabel(tk.Label):
    """A label that wraps to the width of its container (minus `pad`), so text never runs off a narrow pane or leaves a wide one empty."""

    def __init__(self, master, pad: int = 0, **kw):
        kw.setdefault("justify", "left")
        kw.setdefault("anchor", "w")
        super().__init__(master, **kw)
        self._pad = pad
        self._alive = True
        self._bind_id = master.bind("<Configure>", self._rewrap, add="+")
        self.bind("<Destroy>", self._forget, add="+")
        if master.winfo_width() > 1:                      # the container already has a width: wrap to it now
            self.config(wraplength=max(120, master.winfo_width() - pad))

    def _forget(self, event) -> None:
        if event.widget is self:
            self._alive = False

    def _rewrap(self, event) -> None:
        if not self._alive:
            return
        try:
            self.config(wraplength=max(120, event.width - self._pad))
        except tk.TclError:
            self._alive = False


class ScrollFrame(tk.Frame):
    """A frame whose `body` scrolls vertically when its content is taller than the space (mouse wheel works while the pointer is over it)."""

    def __init__(self, master, bg: str = theme.PANEL, **kw):
        super().__init__(master, bg=bg, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.bar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview, width=10)
        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        # The scrollbar is PLACED over the right edge (never packed) and the content is kept 12 px narrower than the canvas: showing or
        # hiding it can therefore never change the width that labels wrap to (which would make the layout oscillate).
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._window, width=max(50, e.width - 12)))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_scroll(self, first, last) -> None:
        self.bar.set(first, last)
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.bar.place_forget()
        else:
            self.bar.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")

    def _wheel(self, event) -> None:
        if self.body.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.canvas.yview_moveto(0)


def caption(parent, text: str, color: str = theme.MUTED, bg: str = theme.PANEL) -> tk.Label:
    return tk.Label(parent, text=text.upper(), font=theme.CAPTION, fg=color, bg=bg, anchor="w")
