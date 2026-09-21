"""Oracle — Review Screen (interface)

Purpose:
    The calm main screen of the Architectural Drawing workflow: the drawing preview as the visual centre, and beside it a narrow column that
    answers three questions in order: Where do things stand? (three short status lines), What needs me? (a review queue, with the views
    one click away), and What should I do about the selected item? (ONE action card with a single primary action). Nothing else is on the
    screen: ids, provenance, confidence numbers, coordinates and the decision history live in "Evidence & Details".

Role in Oracle:
    A view only. It draws what oracle.application.guide models say and reports the engineer's clicks to the workspace (its controller); it holds
    no project state and makes no decision. The three voices are kept visibly apart: ORACLE SUGGESTS (amber), ENGINEER INPUT (blue) and
    ENGINEER DECISION (green); Oracle's suggestion is never worded as fact.

Dependencies:
    tkinter; oracle.ui.theme, widgets, preview_canvas; oracle.application.guide (models only).

Consumers:
    oracle.ui.architectural_workspace.

Status:
    Interface (interface refinement phase).

Migration/Notes:
    Actions on the card are chosen by the guide model (card.actions), so a screen never offers an operation that makes no sense for the item.
    The action buttons live in a fixed footer of the card while the text above them scrolls: a long question or long engineer input can
    never push the actions off the screen.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional

from oracle.application import guide

from . import theme
from .preview_canvas import ORIGINAL, STRUCTURAL, PreviewCanvas
from .widgets import ScrollFrame, WrapLabel, button, caption

QUEUE, VIEWS = "queue", "views"
ACTION_LABEL = {
    "accept_suggestion": "Accept suggestion", "choose_another": "Choose another…", "enter_value": "Enter engineer value…",
    "ask_engineer": "Ask Engineer", "review_evidence": "Review evidence", "accept_view": "Accept", "reject_view": "Reject View",
    "reconsider": "Reconsider", "review_views": "Review one by one", "set_levels": "Set building levels…", "set_view_level": "Set floor…",
    "acknowledge_issue": "Acknowledge…", "save_project": "Save project", "view_actions": "View Actions  ▾",
}
MENU_LABEL = {"change_type": "Change View Type…", "align_plan": "Align Plan…", "split_view": "Split View…", "reject_view": "Reject View…",
              "ask_engineer": "Ask Engineer…", "review_evidence": "Review Evidence"}
_TONE_BG = {"oracle": (theme.AMBER_BG, theme.AMBER), "engineer_input": (theme.ACCENT_SOFT, theme.ACCENT), "decision": (theme.GREEN_BG, theme.GREEN),
            "rejected": (theme.RED_BG, theme.RED)}


class ReviewScreen(tk.Frame):
    def __init__(self, master, ws):
        super().__init__(master, bg=theme.BG)
        self.ws = ws
        self.mode = QUEUE
        self.current_key: Optional[str] = None
        self._silent = False
        self._card = None
        self._flash_id = None
        self._note_override = ""
        self._style_tree()
        self._build_topbar()
        self._build_body()

    # ------------------------------------------------------------------ construction

    @staticmethod
    def _style_tree() -> None:
        style = ttk.Style()
        style.configure("Oracle.Treeview", font=theme.BODY, rowheight=27, borderwidth=0, background=theme.PANEL, fieldbackground=theme.PANEL, foreground=theme.INK)
        style.map("Oracle.Treeview", background=[("selected", theme.ACCENT_SOFT)], foreground=[("selected", theme.INK)])
        style.layout("Oracle.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    def _build_topbar(self) -> None:
        bar = tk.Frame(self, bg=theme.PANEL, highlightthickness=1, highlightbackground=theme.LINE)
        bar.pack(fill="x")
        button(bar, "←  Oracle home", self.ws.exit, "link", bg=theme.PANEL).pack(side="left", padx=(10, 6), pady=8)
        tk.Label(bar, text="Architectural Drawing Review", font=theme.H2, bg=theme.PANEL, fg=theme.INK).pack(side="left", padx=8)
        self.flash_label = tk.Label(bar, text="", font=theme.SMALL, bg=theme.PANEL, fg=theme.GREEN)
        self.flash_label.pack(side="left", padx=16)
        self.details_btn = button(bar, "Evidence & Details", lambda: self.ws.open_details(), "secondary", padx=12, pady=5)
        self.details_btn.pack(side="right", padx=(6, 12), pady=8)
        self.project_btn = tk.Menubutton(bar, text="Project  ▾", font=theme.BODY, bg=theme.PANEL, fg=theme.INK, relief="flat", bd=0, padx=12, pady=5,
                                         highlightthickness=1, highlightbackground=theme.LINE_STRONG, cursor="hand2", activebackground=theme.HOVER)
        menu = tk.Menu(self.project_btn, tearoff=0, font=theme.BODY)
        menu.add_command(label="Save project", command=self.ws.save_project)
        menu.add_command(label="Save project as…", command=lambda: self.ws.save_project(as_new=True))
        menu.add_separator()
        menu.add_command(label="Open project…", command=self.ws.open_project_dialog)
        menu.add_command(label="Add another drawing…", command=self.ws.add_drawing)
        menu.add_command(label="Choose a different drawing…", command=self.ws.show_import)
        self.project_btn.config(menu=menu)
        self.project_btn.pack(side="right", padx=4, pady=8)
        self.save_btn = button(bar, "Save", self.ws.save_project, "secondary", padx=14, pady=5)
        self.save_btn.pack(side="right", padx=4, pady=8)

    def _build_body(self) -> None:
        self.paned = tk.PanedWindow(self, orient="horizontal", sashwidth=7, bg=theme.BG, bd=0, sashrelief="flat", opaqueresize=True)
        self.paned.pack(fill="both", expand=True)
        left = tk.Frame(self.paned, bg=theme.BG)
        tools = tk.Frame(left, bg=theme.BG)
        tools.pack(fill="x", padx=14, pady=(10, 6))
        self.canvas = PreviewCanvas(left)
        for text, command in (("Fit", lambda: self.canvas.fit()), ("Zoom −", lambda: self.canvas.zoom(1 / 1.4)), ("Zoom +", lambda: self.canvas.zoom(1.4))):
            button(tools, text, command, "secondary", padx=12, pady=4, font=theme.SMALL).pack(side="left", padx=(0, 6))
        self.mode_buttons = {}
        for mode, text in ((STRUCTURAL, "Structural Review"), (ORIGINAL, "Original Drawing")):
            b = tk.Button(tools, text=text, command=lambda m=mode: self.set_drawing_mode(m), font=theme.SMALL, relief="flat", bd=0, padx=10, pady=4, cursor="hand2")
            b.pack(side="left", padx=(0, 2))
            self.mode_buttons[mode] = b
        tk.Frame(tools, width=10, bg=theme.BG).pack(side="left")
        self.select_mode = tk.BooleanVar(value=True)
        tk.Checkbutton(tools, text="Select view", variable=self.select_mode, command=self._toggle_select, font=theme.SMALL, bg=theme.BG, fg=theme.INK,
                       activebackground=theme.BG, selectcolor=theme.PANEL).pack(side="left", padx=(6, 0))
        tk.Label(tools, text="Drag to pan  ·  scroll to zoom", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED).pack(side="right")
        self.reload_btn = button(tools, "Reload linework", self.ws.reload_linework, "secondary", padx=10, pady=4, font=theme.SMALL)
        self.canvas.pack(fill="both", expand=True, padx=(14, 4), pady=0)
        self.banner = tk.Label(self.canvas, text="", font=theme.BOLD, bg=theme.ACCENT, fg="white", padx=14, pady=6, wraplength=700)
        self.set_drawing_mode(STRUCTURAL)
        self.canvas_note = tk.Label(left, text="", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, anchor="w")
        self.canvas_note.pack(fill="x", padx=14, pady=(4, 8))
        self.canvas.status_callback = lambda note: self.canvas_note.config(text=self._note_override or note)
        self.canvas.on_pick = self.ws.on_canvas_pick
        self.paned.add(left, minsize=420, stretch="always")

        right = tk.Frame(self.paned, bg=theme.BG)
        self.paned.add(right, minsize=400, width=470, stretch="never")
        self._build_header(right)
        # the queue and the action card share the rest of the column; the engineer can drag the divider (and on a short screen both stay usable)
        self.vpaned = tk.PanedWindow(right, orient="vertical", sashwidth=8, bg=theme.BG, bd=0, sashrelief="flat", opaqueresize=True)
        self.vpaned.pack(fill="both", expand=True, padx=(4, 14), pady=(0, 12))
        self._build_list(self.vpaned)
        self._build_card(self.vpaned)
        self.choice = self._build_choice(right)
        self.vpaned.bind("<Configure>", self._place_vsash, add="+")
        self._vsash_done = False
        self.paned.bind("<Configure>", self._place_sash, add="+")
        self._sash_done = False

    def _place_sash(self, event) -> None:
        if not self._sash_done and event.width > 900:
            try:
                self.paned.sash_place(0, event.width - 480, 1)
                self._sash_done = True
            except tk.TclError:
                pass

    def _place_vsash(self, event) -> None:
        if not self._vsash_done and event.height > 300:
            try:
                self.vpaned.sash_place(0, 1, max(160, int(event.height * 0.32)))
                self._vsash_done = True
            except tk.TclError:
                pass

    @staticmethod
    def _card_frame(master, **pack) -> tk.Frame:
        frame = tk.Frame(master, bg=theme.PANEL, highlightthickness=1, highlightbackground=theme.LINE)
        if pack:
            frame.pack(**pack)
        return frame

    def _build_header(self, right) -> None:
        self.header = self._card_frame(right, fill="x", padx=(4, 14), pady=(10, 8))
        inner = tk.Frame(self.header, bg=theme.PANEL)
        inner.pack(fill="x", padx=16, pady=10)
        self.drawing_label = WrapLabel(inner, font=(theme.FONT, 11, "bold"), bg=theme.PANEL, fg=theme.INK)
        self.drawing_label.pack(fill="x")
        self.source_row = tk.Frame(inner, bg=theme.PANEL)
        tk.Label(self.source_row, text="Drawing source", font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED).pack(side="left")
        self.source_var = tk.StringVar()
        self.source_box = ttk.Combobox(self.source_row, textvariable=self.source_var, state="readonly", width=34)
        self.source_box.pack(side="left", padx=8)
        self.source_box.bind("<<ComboboxSelected>>", lambda e: self.ws.on_source_chosen(self.source_var.get()))
        self.lines_frame = tk.Frame(inner, bg=theme.PANEL)
        self.lines_frame.pack(fill="x", pady=(6, 0))

    def _build_list(self, parent) -> None:
        card = self._card_frame(parent)
        parent.add(card, minsize=150, height=230, stretch="never")
        switch = tk.Frame(card, bg=theme.PANEL)
        switch.pack(fill="x", padx=12, pady=(10, 4))
        self.queue_btn = self._segment(switch, "Review queue", lambda: self.set_mode(QUEUE))
        self.views_btn = self._segment(switch, "Views", lambda: self.set_mode(VIEWS))
        self.tree = ttk.Treeview(card, show="tree", selectmode="browse", height=3, style="Oracle.Treeview")
        self.tree.column("#0", width=380, stretch=True)
        self.tree.tag_configure("group", font=theme.BOLD, foreground=theme.MUTED)
        self.tree.tag_configure("todo", foreground=theme.INK)
        self.tree.tag_configure("done", foreground=theme.MUTED)
        self.tree.tag_configure("accepted", foreground=theme.GREEN)
        self.tree.tag_configure("rejected", foreground=theme.RED)
        bar = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        bar.pack(side="right", fill="y", pady=(0, 10), padx=(0, 4))
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _segment(self, parent, text: str, command) -> tk.Button:
        b = tk.Button(parent, text=text, command=command, font=theme.BOLD, relief="flat", bd=0, padx=12, pady=5, cursor="hand2")
        b.pack(side="left", padx=(0, 4))
        return b

    def _build_card(self, parent) -> None:
        self.card = self._card_frame(parent)
        parent.add(self.card, minsize=230, stretch="always")
        self.card_scroll = ScrollFrame(self.card)
        self.footer = tk.Frame(self.card, bg=theme.PANEL)
        self.footer.pack(side="bottom", fill="x", padx=16, pady=(2, 10))
        self.card_scroll.pack(side="top", fill="both", expand=True, padx=16, pady=(14, 0))

    def _build_choice(self, right) -> tk.Frame:
        frame = self._card_frame(right, fill="both", expand=True, padx=(4, 14), pady=(0, 12))
        frame.pack_forget()
        return frame

    # ------------------------------------------------------------------ drawing mode, tool banner and panel

    def set_drawing_mode(self, mode: str) -> None:
        """Structural Review (furnishing and presentation clutter hidden) or Original Drawing (everything). Rendering only: nothing is changed."""
        self.canvas.set_mode(mode)
        for name, b in self.mode_buttons.items():
            on = name == mode
            b.config(bg=theme.ACCENT if on else theme.PANEL, fg="white" if on else theme.INK, activebackground=theme.ACCENT if on else theme.HOVER)

    def set_banner(self, text) -> None:
        """An instruction across the top of the drawing while a tool waits for a click (None hides it)."""
        if text:
            self.banner.config(text=text)
            self.banner.place(relx=0.5, y=10, anchor="n")
            self.banner.lift()
        else:
            self.banner.place_forget()

    def show_tool_panel(self, caption_text: str, title: str, lines: list, buttons: list, options=None) -> None:
        """The card while a tool is in use: instruction lines, an optional chooser (label, choices, current, callback) and the tool's buttons."""
        self._card = None
        self.card_scroll.clear()
        for child in self.footer.winfo_children():
            child.destroy()
        inner = self.card_scroll.body
        caption(inner, caption_text, theme.ACCENT).pack(fill="x")
        WrapLabel(inner, text=title, font=theme.H2, bg=theme.PANEL, fg=theme.INK).pack(fill="x", pady=(2, 6))
        colors = {"text": theme.INK, "muted": theme.MUTED, "warn": theme.AMBER}
        for text, tone in lines:
            WrapLabel(inner, text=text, font=theme.BODY if tone == "text" else theme.SMALL, bg=theme.PANEL, fg=colors.get(tone, theme.INK)).pack(fill="x", pady=2)
        if options:
            label, values, current, callback = options
            tk.Label(inner, text=label, font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED).pack(anchor="w", pady=(8, 2))
            var = tk.StringVar(value=current)
            box = ttk.Combobox(inner, textvariable=var, values=values, state="readonly")
            box.pack(fill="x")
            box.bind("<<ComboboxSelected>>", lambda e: callback(var.get()))
            self.tool_chooser = box
        for i, (text, kind, callback) in enumerate(buttons):
            if kind == "link":
                button(self.footer, text, callback, "link", bg=theme.PANEL, font=theme.SMALL).pack(anchor="w", pady=(2, 0))
            else:
                button(self.footer, text, callback, kind, pady=7 if kind == "primary" else 4, font=theme.BODY if kind == "primary" else theme.SMALL).pack(fill="x", pady=(0, 4))

    # ------------------------------------------------------------------ messages

    def set_note(self, text: str) -> None:
        """A standing note under the drawing (why there is no linework, for instance)."""
        self._note_override = text
        self.canvas_note.config(text=text)

    def flash(self, text: str, error: bool = False) -> None:
        """A short message in the top bar that fades after a few seconds."""
        if self._flash_id is not None:
            try:
                self.after_cancel(self._flash_id)
            except tk.TclError:
                pass
        self.flash_label.config(text=text, fg=theme.RED if error else theme.GREEN)
        self._flash_id = self.after(6000, lambda: self.flash_label.config(text=""))

    def cancel_timers(self) -> None:
        if self._flash_id is not None:
            try:
                self.after_cancel(self._flash_id)
            except tk.TclError:
                pass
            self._flash_id = None

    # ------------------------------------------------------------------ modes and selection

    def set_mode(self, mode: str, *, refresh: bool = True) -> None:
        self.mode = mode
        for btn, name in ((self.queue_btn, QUEUE), (self.views_btn, VIEWS)):
            on = name == mode
            btn.config(bg=theme.ACCENT_SOFT if on else theme.PANEL, fg=theme.ACCENT if on else theme.MUTED, activebackground=theme.ACCENT_SOFT)
        if refresh:
            self.ws.refresh()

    def _on_tree_select(self, event=None) -> None:
        if self._silent:
            return
        sel = self.tree.selection()
        if not sel or sel[0].startswith("grp:") or sel[0] == self.current_key:
            return                                # (a programmatic selection is echoed back as an event; showing the same item again would loop)
        self.ws.on_select(sel[0])

    def _toggle_select(self) -> None:
        self.canvas.pick_enabled = bool(self.select_mode.get())

    def select(self, key: str) -> None:
        """Select a row without re-triggering the controller (used after a refresh)."""
        if self.tree.exists(key):
            self._silent = True
            try:
                if tuple(self.tree.selection()) != (key,):
                    self.tree.selection_set(key)
                self.tree.see(key)
            finally:
                self._silent = False

    def selected_key(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel and not sel[0].startswith("grp:") else None

    # ------------------------------------------------------------------ filling

    def fill_header(self, overview, sources: list, active_label: str) -> None:
        self.drawing_label.config(text=overview.drawing)
        for child in self.lines_frame.winfo_children():
            child.destroy()
        settled = tk.Frame(self.lines_frame, bg=theme.PANEL)              # what is done sits on one quiet line; what needs the engineer gets its own
        settled.pack(fill="x")
        for icon, text, tone in overview.lines:
            if tone == "attention":
                row = tk.Frame(self.lines_frame, bg=theme.PANEL)
                row.pack(fill="x", pady=(3, 0))
                tk.Label(row, text=icon, font=theme.BOLD, fg=theme.AMBER, bg=theme.PANEL).pack(side="left")
                tk.Label(row, text=" " + text, font=theme.BOLD, fg=theme.AMBER, bg=theme.PANEL).pack(side="left")
            else:
                tk.Label(settled, text=icon, font=theme.BOLD, fg=theme.TONE.get(tone, theme.MUTED), bg=theme.PANEL).pack(side="left")
                tk.Label(settled, text=" " + text + "    ", font=theme.BODY, fg=theme.INK, bg=theme.PANEL).pack(side="left")
        if len(sources) > 1:
            self.source_box.config(values=[label for _sid, label in sources])
            self.source_var.set(active_label)
            self.source_row.pack(fill="x", pady=(6, 0), before=self.lines_frame)
        else:
            self.source_row.pack_forget()
        if overview.revision:
            self.drawing_label.config(text=f"{overview.drawing}")

    def fill_queue(self, todo: list, done: list) -> None:
        self._silent = True
        try:
            self.tree.delete(*self.tree.get_children())
            self.tree.insert("", "end", iid="grp:todo", text=f"Needs your attention ({len(todo)})" if todo else "Nothing needs your attention", open=True, tags=("group",))
            for item in todo:
                self.tree.insert("grp:todo", "end", iid=item.key, text=f"⚠  {item.label}", tags=("todo",))
            if done:
                self.tree.insert("", "end", iid="grp:done", text=f"Completed ({len(done)})", open=not todo, tags=("group",))
                for item in done:
                    self.tree.insert("grp:done", "end", iid=item.key, text=f"✓  {item.label}", tags=("done",))
        finally:
            self._silent = False

    def fill_views(self, entries: list, rejected: list) -> None:
        self._silent = True
        try:
            self.tree.delete(*self.tree.get_children())
            groups = {}
            for e in entries:
                if e.state != "rejected":
                    groups.setdefault(e.group, []).append(e)
            for title, items in groups.items():
                needs = sum(1 for e in items if e.state == "needs_review")
                text = f"{title} ({len(items)})" + (f"  —  {needs} to review" if needs else "")
                gid = f"grp:{title}"
                self.tree.insert("", "end", iid=gid, text=text, open=title != "Sheet furniture", tags=("group",))
                for e in items:
                    self.tree.insert(gid, "end", iid=f"view:{e.id}", text=e.label, tags=(("accepted",) if e.state == "accepted" else ("todo",)))
            if rejected:
                self.tree.insert("", "end", iid="grp:rejected", text=f"Rejected ({len(rejected)})", open=False, tags=("group",))
                for r in rejected:
                    self.tree.insert("grp:rejected", "end", iid=f"view:{r.id}", text=f"✕  {r.name}  —  {r.reason}", tags=("rejected",))
        finally:
            self._silent = False

    def set_counts(self, todo_count: int, views_count: int) -> None:
        self.queue_btn.config(text=f"Review queue ({todo_count})")
        self.views_btn.config(text=f"Views ({views_count})")

    def show_choice(self, sources: list) -> None:
        """Several drawing sources and none chosen: ask, never pick."""
        self.vpaned.pack_forget()
        self.choice.pack(fill="both", expand=True, padx=(4, 14), pady=(0, 12))
        for child in self.choice.winfo_children():
            child.destroy()
        inner = tk.Frame(self.choice, bg=theme.PANEL)
        inner.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(inner, text=f"This project has {len(sources)} drawings", font=theme.H2, bg=theme.PANEL, anchor="w").pack(fill="x")
        WrapLabel(inner, text="Choose which drawing to review. Oracle does not pick one for you.", font=theme.BODY, bg=theme.PANEL, fg=theme.MUTED).pack(fill="x", pady=(2, 10))
        for sid, label in sources:
            button(inner, f"Review  {label}", lambda s=label: self.ws.on_source_chosen(s), "secondary").pack(fill="x", pady=3)
        self.tree.delete(*self.tree.get_children())

    def hide_choice(self) -> None:
        if self.choice.winfo_manager():
            self.choice.pack_forget()
            self.vpaned.pack(fill="both", expand=True, padx=(4, 14), pady=(0, 12))

    # ------------------------------------------------------------------ the action card

    def show_card(self, card) -> None:
        self._card = card
        body = self.card_scroll
        body.clear()
        for child in self.footer.winfo_children():
            child.destroy()
        inner = body.body
        if card.kind == "view":
            tone = {"needs_review": "oracle", "accepted": "decision", "rejected": "rejected"}[card.state]
            caption(inner, guide.STATE_WORDS[card.state], _TONE_BG[tone][1]).pack(fill="x")
        elif card.kind == "answered":
            caption(inner, "Decided", theme.GREEN).pack(fill="x")
        elif card.kind == "complete":
            caption(inner, "All done", theme.GREEN).pack(fill="x")
        else:
            caption(inner, "Next step", theme.MUTED).pack(fill="x")
        WrapLabel(inner, text=card.title, font=theme.H2, bg=theme.PANEL, fg=theme.INK).pack(fill="x", pady=(2, 4))
        if card.kind == "view":
            for name, value in (("Type", card.type_label), ("Status", guide.STATE_WORDS[card.state])):
                row = tk.Frame(inner, bg=theme.PANEL)
                row.pack(fill="x")
                tk.Label(row, text=name, width=7, anchor="w", font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED).pack(side="left")
                tk.Label(row, text=value, font=theme.BOLD, bg=theme.PANEL, fg=theme.INK).pack(side="left")
        if card.body:
            WrapLabel(inner, text=card.body, font=theme.BODY, bg=theme.PANEL, fg=theme.MUTED).pack(fill="x", pady=(0, 6))
        for fact in card.facts[:6]:
            row = tk.Frame(inner, bg=theme.PANEL)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="•", bg=theme.PANEL, fg=theme.MUTED, font=theme.BODY).pack(side="left", anchor="n")
            WrapLabel(row, text=fact, pad=18, font=theme.SMALL, bg=theme.PANEL, fg=theme.INK).pack(side="left", fill="x", expand=True)
        if card.suggestions:
            self._suggestion_block(inner, card)
        if card.rejection is not None:
            self._voice_block(inner, "rejected", "Engineer decision", [("Rejected — " + card.rejection.reason, theme.BOLD, theme.RED)]
                              + ([(card.rejection.explanation, theme.BODY, theme.INK)] if card.rejection.explanation else [])
                              + [(f"{card.rejection.engineer}  ·  {card.rejection.when}", theme.SMALL, theme.MUTED),
                                 ("Oracle keeps the drawing evidence. Restoring it needs another engineer decision.", theme.SMALL, theme.MUTED)])
        elif card.engineer_status:
            self._voice_block(inner, "decision", "Engineer decision", [(card.engineer_status, theme.BOLD, theme.INK)])
        for who, when, text, fate in card.engineer_notes:
            self._voice_block(inner, "engineer_input", "Engineer input", [(text, theme.BODY, theme.INK), (f"{who}  ·  {when}", theme.SMALL, theme.MUTED),
                                                                            (fate, theme.SMALL, theme.MUTED)])
        self._build_actions(card)

    def _voice_block(self, parent, tone: str, title: str, lines: list) -> None:
        bg, fg = _TONE_BG[tone]
        block = tk.Frame(parent, bg=bg)
        block.pack(fill="x", pady=(8, 0))
        caption(block, title, fg, bg).pack(fill="x", padx=12, pady=(8, 2))
        for text, font, color in lines:
            WrapLabel(block, text=text, pad=24, font=font, bg=bg, fg=color).pack(fill="x", padx=12, pady=(0, 3))
        tk.Frame(block, bg=bg, height=6).pack()

    def _suggestion_block(self, parent, card) -> None:
        top = card.suggestions[0]
        lines = [(top.label, (theme.FONT, 11, "bold"), theme.INK), (f"Confidence: {top.confidence}", theme.SMALL, theme.MUTED)]
        if top.consequence:
            lines.append((f"If you accept: {top.consequence}", theme.SMALL, theme.MUTED))
        if len(card.suggestions) > 1:
            lines.append(("Other possibilities: " + "; ".join(s.label for s in card.suggestions[1:4]), theme.SMALL, theme.MUTED))
        self._voice_block(parent, "oracle", "Oracle suggests", lines)

    def _build_actions(self, card) -> None:
        actions = [a for a in card.actions]
        labels = dict(ACTION_LABEL)
        if "confirm_all" in actions:
            labels["confirm_all"] = f"Confirm all {len(card.view_ids)} views"
        primary = actions[0] if actions and actions[0] != "review_evidence" else None
        rest = actions[1:] if primary else list(actions)
        if primary:
            kind = "danger" if primary == "reject_view" else "primary"
            self._action_button(self.footer, primary, labels, card, kind, pady=7).pack(fill="x", pady=(0, 4))
        secondary = [a for a in rest if a != "review_evidence"]
        grid = tk.Frame(self.footer, bg=theme.PANEL)
        grid.pack(fill="x")
        for i, action in enumerate(secondary):
            kind = "danger" if action == "reject_view" else "secondary"
            self._action_button(grid, action, labels, card, kind, pady=3, font=theme.SMALL).grid(row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else 6, 0), pady=2)
        grid.columnconfigure(0, weight=1, uniform="a")
        grid.columnconfigure(1, weight=1, uniform="a")
        if "review_evidence" in actions:
            button(self.footer, "Review evidence", lambda: self.ws.run_action("review_evidence", card), "link", bg=theme.PANEL, font=theme.SMALL).pack(anchor="w", pady=(2, 0))

    def _action_button(self, parent, action: str, labels: dict, card, kind: str, **kw) -> tk.Button:
        holder = {}

        def run():
            if action == "choose_another":
                self.popup_alternatives(card, holder["b"])
            elif action == "view_actions":
                self.popup_view_actions(card, holder["b"])
            else:
                self.ws.run_action(action, card)

        holder["b"] = button(parent, labels.get(action, action), run, kind, **kw)
        return holder["b"]

    def popup_view_actions(self, card, widget) -> None:
        """The contextual View Actions menu: correcting a view is kept apart from rejecting it, and each entry opens its own small flow."""
        menu = tk.Menu(self, tearoff=0, font=theme.BODY)
        for action in card.menu:
            if action in ("ask_engineer", "review_evidence"):
                menu.add_separator()
            menu.add_command(label=MENU_LABEL[action], command=lambda a=action: self.ws.run_action(a, card))
        try:
            menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        finally:
            menu.grab_release()

    def popup_alternatives(self, card, widget) -> None:
        menu = tk.Menu(self, tearoff=0, font=theme.BODY)
        for s in card.suggestions:
            menu.add_command(label=f"{s.label}   ({s.confidence})", command=lambda sid=s.id: self.ws.act_accept_alternative(card.set_id, sid))
        try:
            menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        finally:
            menu.grab_release()
