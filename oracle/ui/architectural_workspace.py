"""Oracle — Architectural Drawing Workspace (interface)

Purpose:
    The Architectural Drawing workflow of the Oracle application, as one frame with four calm screens: a WELCOME ("Turn a DWG/DXF into a reviewed
    architectural model"), a very simple IMPORT (choose a drawing; technical detail behind "Details"), PROCESSING (honest stages, no freezing) and the
    REVIEW (oracle.ui.review_screen: the drawing at the centre, a short queue of what needs the engineer, one action card). It is also the controller:
    it turns the engineer's clicks into session calls (each a recorded engineer decision), refreshes the screen from the project after every action, and
    opens "Evidence & Details" (oracle.ui.details_window) when the engineer wants the technical picture.

Role in Oracle:
    The interface for oracle.application.ArchitecturalSession. It holds NO model of its own (the session's OracleProject is the only state), shows
    uncertainty as uncertainty, and keeps three voices apart: Oracle SUGGESTS, the engineer INPUTS or DECIDES. It is mounted by the existing wizard,
    so it is part of Oracle rather than a separate application, and it can also stand alone.

Dependencies:
    tkinter; oracle.application; oracle.ui.review_screen, details_window, dialogs, widgets, theme.

Consumers:
    oracle_wizard (the Architectural Drawing entry point), tests.

Status:
    Interface (interface refinement phase).

Migration/Notes:
    Interpretation runs on a worker thread and reports through a queue that the interface polls; no widget is touched from the worker. All dialogs go
    through self.prompts, which tests replace with a scripted object. The screens are named welcome, import, processing and review (self.screen).
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable, Optional

from oracle.application import guide
from oracle.application import (
    ActionRefused, ArchitecturalSession, Overlay, SourceChoiceRequired, STAGE_LABELS, WORKFLOW_STAGES, WorkflowError,
)

from . import theme
from .details_window import DetailsWindow
from .dialogs import Prompts
from .point_tools import AlignTool, SplitTool
from .review_screen import QUEUE, VIEWS, ReviewScreen
from .widgets import button

LEVEL_OPTIONS = [("Ground floor", "GROUND"), ("First floor", "FLOOR:1"), ("Second floor", "FLOOR:2"), ("Third floor", "FLOOR:3"),
                 ("Basement", "BASEMENT:1"), ("Mezzanine", "MEZZANINE"), ("Roof", "ROOF")]


class ArchitecturalWorkspace(tk.Frame):
    def __init__(self, master, *, session: Optional[ArchitecturalSession] = None, on_exit: Optional[Callable[[], None]] = None,
                 initial_dir: Optional[str] = None, logger: Optional[Callable[[str, str], None]] = None):
        super().__init__(master, bg=theme.BG)
        self.session = session or ArchitecturalSession(logger=logger)
        self.on_exit = on_exit
        self.initial_dir = initial_dir
        self.prompts = Prompts(self)
        self.pending_file = None
        self.file_info = None
        self.selected_ref: Optional[str] = None
        self.view_rows: list = []
        self.detected_levels: list = []
        self.details_window: Optional[DetailsWindow] = None
        self.tool = None                                  # the Align Plan / Split View tool while one is in use
        self._events: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._processing_file: Optional[str] = None
        self._add_source_mode = False
        self._polling = False
        self._after_ids: set = set()
        self._stage_rows: dict = {}
        self._screen = None
        self.engineer_var = tk.StringVar(value=self.session.engineer)
        self.welcome_frame = self._build_welcome()
        self.import_frame = self._build_import()
        self.processing_frame = self._build_processing()
        self.review = ReviewScreen(self, self)
        self.review_frame = self.review
        self.canvas = self.review.canvas
        self.show_welcome()

    # =================================================================== screens

    def _switch(self, frame: tk.Frame, name: str) -> None:
        for f in (self.welcome_frame, self.import_frame, self.processing_frame, self.review_frame):
            f.pack_forget()
        frame.pack(fill="both", expand=True)
        self._screen = name

    @property
    def screen(self) -> Optional[str]:
        return self._screen

    @property
    def panels(self):
        """The Evidence & Details tabs while that window is open (else None)."""
        return self.details_window.panels if self.details_window is not None else None

    def show_welcome(self) -> None:
        if self.session.has_project:
            self.continue_btn.pack(anchor="w", pady=(10, 0), before=self.home_link)
        else:
            self.continue_btn.pack_forget()
        self._switch(self.welcome_frame, "welcome")

    def show_import(self) -> None:
        self._switch(self.import_frame, "import")

    def show_processing(self) -> None:
        self._switch(self.processing_frame, "processing")

    def show_review(self) -> None:
        self._switch(self.review_frame, "review")
        self.refresh()

    # =================================================================== 1. welcome

    def _build_welcome(self) -> tk.Frame:
        f = tk.Frame(self, bg=theme.BG)
        column = tk.Frame(f, bg=theme.BG)
        column.place(relx=0.5, rely=0.46, anchor="center")
        tk.Label(column, text="ORACLE", font=theme.CAPTION, bg=theme.BG, fg=theme.ACCENT).pack(anchor="w")
        tk.Label(column, text="Architectural Drawing Review", font=theme.DISPLAY, bg=theme.BG, fg=theme.INK).pack(anchor="w", pady=(2, 4))
        tk.Label(column, text="Turn a DWG/DXF into a reviewed architectural model.", font=theme.SUBTITLE, bg=theme.BG, fg=theme.MUTED).pack(anchor="w", pady=(0, 22))
        tk.Label(column, text="Oracle will:", font=theme.BOLD, bg=theme.BG, fg=theme.INK).pack(anchor="w")
        for line in ("identify drawing views", "identify levels and dimensions", "highlight ambiguities", "preserve the original evidence",
                     "ask you when it is uncertain"):
            tk.Label(column, text=f"•  {line}", font=theme.BODY, bg=theme.BG, fg=theme.INK).pack(anchor="w", pady=1, padx=(6, 0))
        tk.Label(column, text="You remain in control of every engineering decision.", font=theme.BOLD, bg=theme.BG, fg=theme.INK).pack(anchor="w", pady=(18, 24))
        self.open_btn = button(column, "Open Architectural Drawing", self.show_import, "primary", padx=22, pady=10)
        self.open_btn.pack(anchor="w")
        self.open_project_btn = button(column, "Open Existing Project", self.open_project_dialog, "secondary", padx=22, pady=9)
        self.open_project_btn.pack(anchor="w", pady=(10, 0))
        self.continue_btn = button(column, "Continue reviewing the current drawing", self.show_review, "secondary", padx=22, pady=9)
        self.home_link = button(column, "←  Back to Oracle home", self.exit, "link")
        self.home_link.pack(anchor="w", pady=(22, 0))
        return f

    # =================================================================== 2. import a drawing

    def _build_import(self) -> tk.Frame:
        f = tk.Frame(self, bg=theme.BG)
        column = tk.Frame(f, bg=theme.BG)
        column.place(relx=0.5, rely=0.42, anchor="center")
        button(column, "←  Back", self.show_welcome, "link").pack(anchor="w")
        tk.Label(column, text="Architectural Drawing", font=theme.TITLE, bg=theme.BG, fg=theme.INK).pack(anchor="w", pady=(6, 2))
        tk.Label(column, text="Choose a drawing to review.", font=theme.SUBTITLE, bg=theme.BG, fg=theme.MUTED).pack(anchor="w", pady=(0, 14))
        self.browse_btn = button(column, "Browse for drawing…", self.choose_file, "primary", padx=20, pady=9)
        self.browse_btn.pack(anchor="w")
        self.file_card = tk.Frame(column, bg=theme.PANEL, highlightthickness=1, highlightbackground=theme.LINE)
        self.info_vars = {k: tk.StringVar(value="") for k in ("name", "type", "status", "path", "size")}
        grid = tk.Frame(self.file_card, bg=theme.PANEL)
        grid.pack(fill="x", padx=18, pady=14)
        for r, (key, label) in enumerate((("name", "Drawing"), ("type", "Type"), ("status", "Status"))):
            tk.Label(grid, text=label, font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED, width=9, anchor="w").grid(row=r, column=0, sticky="nw", pady=2)
            tk.Label(grid, textvariable=self.info_vars[key], font=theme.BODY if key != "name" else theme.BOLD, bg=theme.PANEL, fg=theme.INK, anchor="w",
                     justify="left", wraplength=420).grid(row=r, column=1, sticky="w", pady=2)
        self.file_status = grid.grid_slaves(row=2, column=1)[0]
        self.details_toggle = button(self.file_card, "Details ▾", self._toggle_details, "link", bg=theme.PANEL)
        self.details_toggle.pack(anchor="w", padx=14)
        self.import_details = tk.Frame(self.file_card, bg=theme.PANEL)
        for r, (key, label) in enumerate((("path", "Path"), ("size", "Size"))):
            tk.Label(self.import_details, text=label, font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED, width=9, anchor="w").grid(row=r, column=0, sticky="nw", padx=(18, 0), pady=1)
            tk.Label(self.import_details, textvariable=self.info_vars[key], font=theme.SMALL, bg=theme.PANEL, fg=theme.INK, anchor="w", justify="left",
                     wraplength=420).grid(row=r, column=1, sticky="w", pady=1)
        self.import_note = tk.Label(self.import_details, text="", font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED, anchor="w", justify="left", wraplength=470)
        self.import_note.grid(row=2, column=0, columnspan=2, sticky="w", padx=18, pady=(2, 0))
        tk.Label(self.import_details, text="Engineer", font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED, width=9, anchor="w").grid(row=3, column=0, sticky="w", padx=(18, 0), pady=(6, 8))
        tk.Entry(self.import_details, textvariable=self.engineer_var, width=28, font=theme.BODY, relief="flat", highlightthickness=1,
                 highlightbackground=theme.LINE_STRONG).grid(row=3, column=1, sticky="w", pady=(6, 8))
        self.interpret_btn = button(column, "Interpret Drawing", self.start_interpretation, "primary", padx=22, pady=9, state="disabled")
        self.start_message = tk.Label(column, text="", font=theme.SMALL, bg=theme.BG, fg=theme.RED, anchor="w", justify="left", wraplength=480)
        return f

    def _toggle_details(self) -> None:
        if self.import_details.winfo_manager():
            self.import_details.pack_forget()
            self.details_toggle.config(text="Details ▾")
        else:
            self.import_details.pack(fill="x", pady=(0, 6))
            self.details_toggle.config(text="Details ▴")

    def choose_file(self) -> None:
        path = self.prompts.ask_open_drawing(self.initial_dir)
        if path:
            self.set_file(path)

    def set_file(self, path) -> None:
        """Show what the chosen file is and whether Oracle can read it (nothing is interpreted yet)."""
        info = self.session.inspect_file(path)
        self.pending_file, self.file_info = str(path), info
        self.info_vars["name"].set(info.name)
        self.info_vars["type"].set(info.kind)
        self.info_vars["path"].set(str(info.path))
        self.info_vars["size"].set(info.size_text)
        self.import_note.config(text=(info.note or ""))
        self.file_card.pack(fill="x", pady=(14, 0), after=self.browse_btn)
        self.interpret_btn.pack(anchor="w", pady=(16, 0))
        self.start_message.pack(anchor="w", pady=(6, 0))
        self.browse_btn.config(text="Choose a different drawing…", bg=theme.PANEL, fg=theme.INK, font=theme.BODY, highlightthickness=1,
                               highlightbackground=theme.LINE_STRONG)
        if info.ok:
            self.info_vars["status"].set("Ready to interpret")
            self.file_status.config(fg=theme.GREEN)
            self.interpret_btn.config(state="normal")
        else:
            self.info_vars["status"].set(info.problem or "Oracle cannot read this file.")
            self.file_status.config(fg=theme.RED)
            self.interpret_btn.config(state="disabled")
            if not self.import_details.winfo_manager():
                self._toggle_details()

    # =================================================================== 3. processing

    def _build_processing(self) -> tk.Frame:
        f = tk.Frame(self, bg=theme.BG)
        column = tk.Frame(f, bg=theme.BG)
        column.place(relx=0.5, rely=0.42, anchor="center")
        self.proc_title = tk.Label(column, text="Interpreting drawing", font=theme.TITLE, bg=theme.BG, fg=theme.INK)
        self.proc_title.pack(anchor="w")
        self.proc_file = tk.Label(column, text="", font=theme.SUBTITLE, bg=theme.BG, fg=theme.MUTED)
        self.proc_file.pack(anchor="w", pady=(2, 14))
        self.proc_step = tk.Label(column, text="", font=theme.BOLD, bg=theme.BG, fg=theme.INK)
        self.proc_step.pack(anchor="w")
        self.proc_bar = ttk.Progressbar(column, mode="indeterminate", length=460)
        self.proc_bar.pack(anchor="w", pady=(6, 10))
        tk.Label(column, bg=theme.BG, fg=theme.MUTED, font=theme.SMALL, justify="left", wraplength=460, anchor="w",
                 text="Each stage is ticked only when Oracle has finished it. Large drawings can take a minute; the window stays responsive.").pack(anchor="w")
        self.stage_frame = tk.Frame(column, bg=theme.BG)
        self.stage_frame.pack(anchor="w", pady=10)
        for key in WORKFLOW_STAGES:
            row = tk.Frame(self.stage_frame, bg=theme.BG)
            row.pack(anchor="w")
            glyph = tk.Label(row, text="○", width=2, font=theme.SMALL, bg=theme.BG, fg=theme.MUTED)
            glyph.pack(side="left")
            label = tk.Label(row, text=STAGE_LABELS[key], font=theme.SMALL, bg=theme.BG, fg=theme.MUTED)
            label.pack(side="left")
            self._stage_rows[key] = (glyph, label)
        self.error_box = tk.Frame(column, bg=theme.RED_BG, highlightthickness=1, highlightbackground=theme.RED)
        self.error_title = tk.Label(self.error_box, text="", font=theme.BOLD, bg=theme.RED_BG, fg=theme.RED, anchor="w")
        self.error_title.pack(fill="x", padx=14, pady=(10, 0))
        self.error_file = tk.Label(self.error_box, text="", font=theme.SMALL, bg=theme.RED_BG, fg=theme.MUTED, anchor="w", wraplength=460, justify="left")
        self.error_file.pack(fill="x", padx=14)
        self.error_message = tk.Label(self.error_box, text="", font=theme.BODY, bg=theme.RED_BG, fg=theme.INK, anchor="w", wraplength=460, justify="left")
        self.error_message.pack(fill="x", padx=14, pady=6)
        self.error_note = tk.Label(self.error_box, text="", font=theme.SMALL, bg=theme.RED_BG, fg=theme.MUTED, anchor="w", wraplength=460, justify="left")
        self.error_note.pack(fill="x", padx=14)
        row = tk.Frame(self.error_box, bg=theme.RED_BG)
        row.pack(fill="x", padx=14, pady=12)
        button(row, "Try again", self.retry, "primary", padx=14, pady=5).pack(side="left")
        button(row, "Choose another drawing", self.show_import, "secondary", padx=14, pady=5).pack(side="left", padx=8)
        return f

    def start_interpretation(self, *, add_source: bool = False) -> None:
        if not self.pending_file or not (self.file_info and self.file_info.ok):
            return
        engineer = self.engineer_var.get().strip()
        if not engineer:
            self.start_message.config(text="Enter the engineer's name under Details: it is recorded on every decision.")
            if not self.import_details.winfo_manager():
                self._toggle_details()
            return
        self.start_message.config(text="")
        self._begin(self.pending_file, engineer, add_source)

    def _begin(self, path: str, engineer: str, add_source: bool) -> None:
        self._processing_file = path
        self._add_source_mode = add_source
        self.error_box.pack_forget()
        self.proc_title.config(text="Interpreting drawing")
        self.proc_file.config(text=Path(path).name)
        self.proc_step.config(text="Starting…")
        for glyph, label in self._stage_rows.values():
            glyph.config(text="○", fg=theme.MUTED)
            label.config(fg=theme.MUTED)
        self.proc_bar.start(12)
        self.show_processing()
        events = self._events

        def work():
            try:
                self.session.interpret(path, engineer=engineer, add_source=add_source, progress=lambda e: events.put(("stage", e)))
                events.put(("ok", None))
            except WorkflowError as exc:
                events.put(("error", exc))
            except Exception as exc:                                    # noqa: BLE001 - the session wraps expected failures; this is the last resort
                events.put(("error", WorkflowError("Oracle could not interpret this drawing", str(exc), file=path)))

        self._worker = threading.Thread(target=work, daemon=True, name="oracle-architectural-interpretation")
        self._polling = True
        self._worker.start()
        self._schedule_poll(60)

    def _poll(self) -> None:
        finished = False
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "stage":
                self._on_stage(payload)
            elif kind == "ok":
                finished = True
                self.proc_bar.stop()
                self.session.engineer = self.engineer_var.get().strip() or self.session.engineer
                self.review.set_mode(QUEUE, refresh=False)
                self.review.current_key = None
                self.show_review()
            elif kind == "error":
                finished = True
                self._on_failure(payload)
        if finished:
            self._polling = False
        else:
            self._schedule_poll(80)

    def _schedule_poll(self, ms: int) -> None:
        self._after_ids.add(self.after(ms, self._poll_tick))

    def _poll_tick(self) -> None:
        self._after_ids.clear()
        self._poll()

    def destroy(self) -> None:
        for after_id in list(self._after_ids):
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
        self.review.cancel_timers()
        if self.details_window is not None:
            try:
                self.details_window.destroy()
            except tk.TclError:
                pass
            self.details_window = None
        self.canvas.cancel_pending()
        self.tool = None
        super().destroy()

    def _on_stage(self, e) -> None:
        glyph, label = self._stage_rows[e.key]
        if e.state == "start":
            glyph.config(text="●", fg=theme.ACCENT)
            label.config(fg=theme.INK)
            self.proc_step.config(text=f"Step {e.index} of {e.total}: {e.label}…")
        elif e.state == "done":
            glyph.config(text="✓", fg=theme.GREEN)
            label.config(fg=theme.INK)
        elif e.state == "failed":
            glyph.config(text="✕", fg=theme.RED)
            label.config(fg=theme.RED)

    def _on_failure(self, exc: WorkflowError) -> None:
        self.proc_bar.stop()
        self.proc_title.config(text="Oracle could not interpret this drawing")
        self.proc_step.config(text="")
        self.error_title.config(text=exc.title)
        self.error_file.config(text=f"File: {exc.file or self._processing_file}")
        self.error_message.config(text=exc.message)
        self.error_note.config(text="The technical details were recorded in Oracle's log (logs/oracle.log)." if exc.logged else "")
        self.error_box.pack(fill="x", pady=8)
        self.last_error = exc

    def retry(self) -> None:
        if self._processing_file:
            self._begin(self._processing_file, self.engineer_var.get().strip() or self.session.engineer, self._add_source_mode)

    def wait_for_worker(self, timeout: float = 120.0) -> bool:
        """Pump the interface until the interpretation has finished AND its outcome is on screen (tests, and scripted use)."""
        import time
        end = time.time() + timeout
        while time.time() < end:
            self.update()
            if self._worker is not None and not self._worker.is_alive() and not self._polling:
                self.update()
                return True
            time.sleep(0.02)
        return False

    # =================================================================== 4. review: showing the project

    def refresh(self, advance: bool = False) -> None:
        """Redraw everything from the project. The selected item stays selected if it still exists; otherwise the NEXT thing that needs the engineer
        is selected (or 'Architectural review complete'). With `advance` (after an engineer action) an item that is no longer waiting for the engineer is
        left behind for the next one, so the engineer is always looking at what to do next."""
        s = self.session
        try:
            sid = s.source_id()
        except SourceChoiceRequired:
            self.review.show_choice(s.source_choices())
            self._refresh_details()
            return
        except ActionRefused:
            return
        self.review.hide_choice()
        choices = s.source_choices()
        active = next((label for i, label in choices if i == sid), "")
        overview = s.overview(sid)
        todo, done = s.queue(sid)
        entries = s.view_entries(sid)
        rejected = s.rejected_views(sid)
        self.review.fill_header(overview, choices, active)
        self.review.set_counts(len(todo), len([e for e in entries if e.state != "rejected"]))
        if self.review.mode == QUEUE:
            self.review.fill_queue(todo, done)
            valid = {i.key for i in todo} | {i.key for i in done} | {"complete"}
        else:
            self.review.fill_views(entries, rejected)
            valid = {f"view:{e.id}" for e in entries} | {f"view:{r.id}" for r in rejected}
        self.review.set_mode(self.review.mode, refresh=False)
        preview = s.preview(sid)
        if preview is None:
            self.review.reload_btn.pack(side="right", padx=8)
        else:
            self.review.reload_btn.pack_forget()
        self.canvas.set_preview(preview)
        self.canvas.set_overlays(base=s.view_overlays(sid), selection=Overlay())
        self.review.set_note(s.geometry_note.get(sid, "") if preview is None else "")
        key = self.review.current_key
        if advance and self.review.mode == QUEUE and key not in {i.key for i in todo}:
            key = None
        if key not in valid:
            if self.review.mode == QUEUE:
                key = todo[0].key if todo else "complete"
            else:
                first = next((e for e in entries if e.state == "needs_review"), entries[0] if entries else None)
                key = f"view:{first.id}" if first else None
        self._show_key(key)
        self.review.save_btn.config(text="Save •" if s.dirty else "Save")
        self._refresh_details()

    def _show_key(self, key: Optional[str]) -> None:
        self.review.current_key = key
        if key is None:
            return
        s = self.session
        try:
            card = s.card(key)
        except (StopIteration, KeyError, ActionRefused):
            card = s.card("complete")
            key = "complete"
        self.review.current_key = key
        self.review.show_card(card)
        self.review.select(key)
        self.selected_ref = key
        overlay = Overlay()
        try:
            if card.set_id and card.kind in ("question", "answered"):
                overlay = s.overlay_for_question(card.set_id)
            else:
                for vid in card.view_ids[:40]:
                    overlay.merge(s.overlay_for_object(vid))
        except (ActionRefused, KeyError):
            overlay = Overlay()
        self.canvas.set_overlays(selection=overlay, focus=bool(overlay.focus))

    def on_select(self, key: str) -> None:
        """The engineer chose a row of the queue or the views list."""
        self.cancel_tool(restore=False)
        self._show_key(key)

    def cancel_tool(self, restore: bool = True) -> None:
        if self.tool is not None:
            tool, self.tool = self.tool, None
            tool.finish()
            if restore and self.review.current_key:
                self._show_key(self.review.current_key)

    def start_alignment(self, view_id: str) -> bool:
        """Align Plan: point-picking on the drawing; nothing is recorded until the engineer accepts the preview."""
        self.cancel_tool(restore=False)
        tool = AlignTool(self, view_id)
        if tool.begin():
            self.tool = tool
            return True
        return False

    def start_split(self, view_id: str) -> bool:
        self.cancel_tool(restore=False)
        if not self.session.preview(self.session.source_id_of(view_id)):
            self.prompts.error("Not available", "Splitting needs the drawing linework. Use Reload linework first.")
            return False
        tool = SplitTool(self, view_id)
        tool.begin()
        self.tool = tool
        return True

    def on_canvas_pick(self, view_id: Optional[str]) -> None:
        """A click on the drawing selects the view under it, in the Views list."""
        if not view_id:
            return
        self.review.set_mode(VIEWS, refresh=False)
        self.review.current_key = f"view:{view_id}"
        self.refresh()

    def on_source_chosen(self, label: str) -> None:
        for sid, text in self.session.source_choices():
            if text == label:
                self.session.set_active_source(sid)
                self.review.current_key = None
                self.refresh()
                return

    def flash(self, text: str, error: bool = False) -> None:
        self.review.flash(text, error)

    # =================================================================== the action dispatcher

    def run_action(self, action: str, card) -> None:
        """One click on the action card. Every path that changes the project goes through a dialog and then a session method."""
        top = card.suggestions[0] if card.suggestions else None
        if action == "accept_suggestion" and top:
            self.act_accept_alternative(card.set_id, top.id)
        elif action == "enter_value":
            self.act_enter_height(card.set_id)
        elif action == "ask_engineer":
            self.act_ask_engineer(card.ask_target, card.set_id)
        elif action == "review_evidence":
            self._open_evidence_for(card)
        elif action == "accept_view":
            self.act_review_views(card.view_ids, True)
        elif action == "reject_view":
            self.act_reject_views(card.view_ids)
        elif action == "change_type":
            self.act_change_type(card.view_ids[0] if card.view_ids else None)
        elif action == "align_plan":
            self.start_alignment(card.view_ids[0])
        elif action == "split_view":
            self.start_split(card.view_ids[0])
        elif action == "reconsider":
            self.act_reconsider(card.view_ids)
        elif action == "confirm_all":
            self.act_confirm_all(card.view_ids)
        elif action == "review_views":
            self.review.set_mode(VIEWS, refresh=False)
            self.review.current_key = None
            self.refresh()
        elif action == "set_levels":
            self.act_establish_levels()
        elif action == "set_view_level":
            self.act_set_floor(card.view_ids[0] if card.view_ids else None)
        elif action == "acknowledge_issue":
            self.act_accept_issue(card.key.split(":", 1)[1])
        elif action == "save_project":
            self.save_project()

    def _open_evidence_for(self, card) -> None:
        if card.set_id:
            self.open_details("questions", card.set_id)
        elif card.kind == "view" or card.view_ids:
            self.open_details("views", (card.view_ids or [None])[0])
        else:
            self.open_details("summary")

    # =================================================================== Evidence & Details

    def open_details(self, tab: Optional[str] = None, select: Optional[str] = None) -> None:
        if self.details_window is None:
            self.details_window = DetailsWindow(self)
            self._refresh_details()
        else:
            self.details_window.deiconify()
            self.details_window.lift()
        if self.panels is None:
            return
        if tab:
            self.panels.show_tab(tab)
        if select:
            tree = {"views": self.panels.views_tree, "questions": self.panels.q_tree}.get(tab or "")
            if tree is not None and tree.exists(select):
                tree.selection_set(select)
                tree.see(select)

    def close_details(self) -> None:
        if self.details_window is not None:
            self.details_window.destroy()
            self.details_window = None

    def _refresh_details(self) -> None:
        """Refill the technical tabs, but only while their window is open."""
        panels = self.panels
        if panels is None:
            return
        s = self.session
        try:
            sid = s.source_id()
        except (SourceChoiceRequired, ActionRefused):
            panels.fill_decisions(s.decision_rows() if s.has_project else [])
            return
        self.view_rows = s.view_rows(sid)
        self.detected_levels, built = s.levels(sid)
        panels.fill_summary(s.summary(sid), s.readiness())
        panels.fill_views(self.view_rows)
        panels.fill_levels(self.detected_levels, built)
        self.refresh_observations()
        panels.fill_questions(s.questions(sid))
        panels.fill_issues(s.issues(sid))
        panels.fill_approved(s.approved(sid))
        panels.fill_input(s.clarification_rows())
        panels.fill_decisions(s.decision_rows())

    def refresh_observations(self) -> None:
        if self.panels is None:
            return
        sid = self.session.source_id()
        titles = {r.id: r.title for r in self.view_rows}
        self.panels.fill_observations(self.session.observation_groups(sid), titles)

    # ------------------------------------------------------------------ host methods used by the Details tabs

    def select_object(self, object_id: str) -> None:
        self.selected_ref = object_id
        self.canvas.set_overlays(selection=self.session.overlay_for_object(object_id), focus=True)

    def select_question(self, set_id: Optional[str]) -> None:
        if set_id:
            self.canvas.set_overlays(selection=self.session.overlay_for_question(set_id), focus=True)

    def select_issue(self, issue_id: Optional[str], quiet: bool = False) -> None:
        if issue_id:
            self.canvas.set_overlays(selection=self.session.overlay_for_issue(issue_id), focus=True)

    def go_to_issue_question(self, issue_id: Optional[str]) -> None:
        row = next((r for r in self.session.issues() if r.id == issue_id), None)
        if row is None or not row.set_id:
            self.prompts.info("No linked question", "This issue is not tied to a question Oracle can ask you; accept it with a reason, or fix its cause.")
            return
        self.panels.show_tab("questions")
        self.panels.select_question(row.set_id)

    def go_to_reference(self, ref: str) -> None:
        if self.panels is None:
            return
        if ref.startswith("IS-"):
            self.panels.show_tab("questions")
            self.panels.select_question(ref)
        elif ref.startswith(("ARC-", "BLK-")):
            self.panels.show_tab("issues")
            if self.panels.issue_tree.exists(ref):
                self.panels.issue_tree.selection_set(ref)
        elif ref.startswith("VIEW-"):
            self.panels.show_tab("views")
            if self.panels.views_tree.exists(ref):
                self.panels.views_tree.selection_set(ref)
        elif ref.startswith("level") or ref == "project":
            self.panels.show_tab("levels")
        else:
            self.panels.show_tab("summary")

    def on_tab_changed(self, name: str) -> None:
        if name == "approved" and self.session.has_project:
            try:
                self.canvas.set_overlays(selection=self.session.overlay_approved(), focus=False)
            except (SourceChoiceRequired, ActionRefused):
                pass

    # =================================================================== engineer actions (all through the session, all recorded)

    def _act(self, fn, done: str):
        try:
            result = fn()
        except ActionRefused as exc:
            self.prompts.error("Not done", str(exc))
            self.flash(f"Not done: {exc}", error=True)
            return None
        except SourceChoiceRequired:
            self.prompts.error("Choose a drawing", "Choose which drawing to work with first.")
            return None
        self.refresh(advance=True)
        self.flash(done)
        return result

    def _names(self, ids: list) -> list:
        out = []
        for i in ids:
            try:
                out.append(self.session.view_name(i))
            except ActionRefused:
                out.append(i)
        return out

    # ---- views

    def act_review_views(self, ids: list, accept: bool) -> None:
        """Accept views (with an optional note), or reject them (with a reason: see act_reject_views)."""
        if not ids:
            self.prompts.info("Select a view", "Select one or more views first.")
            return
        if not accept:
            self.act_reject_views(ids)
            return
        names = self._names(ids)
        subject = f"“{names[0]}”" if len(ids) == 1 else f"these {len(ids)} views"
        note = self.prompts.ask_reason("Accept view", f"Accept {subject}? You are confirming that Oracle's reading of it is right. Note (optional):", ok_text="Accept")
        if note is None:
            return
        self._act(lambda: self.session.review_views(ids, accept=True, reason=note), "Accepted." if len(ids) == 1 else f"Accepted {len(ids)} views.")

    def act_confirm_all(self, ids: list) -> None:
        names = self._names(ids)
        shown = "\n".join(f"• {n}" for n in names[:12]) + (f"\n… and {len(names) - 12} more" if len(names) > 12 else "")
        if not self.prompts.confirm(f"Confirm {len(ids)} views", f"Confirm that Oracle read these {len(ids)} views correctly?\n\n{shown}\n\nYou can still reject or reconsider any of them later."):
            return
        self._act(lambda: self.session.review_views(ids, accept=True, reason="Confirmed together by the engineer."), f"Confirmed {len(ids)} views.")

    def act_reject_views(self, ids: list) -> None:
        if not ids:
            self.prompts.info("Select a view", "Select one or more views first.")
            return
        answer = self.prompts.ask_reject_view(self._names(ids), self.session.rejection_consequences(ids))
        if answer is None:
            return
        code, explanation = answer
        self._act(lambda: self.session.reject_views(ids, code, explanation), "View rejected: the evidence and your decision are kept." if len(ids) == 1
                  else f"{len(ids)} views rejected: the evidence and your decision are kept.")

    def act_reconsider(self, ids: list) -> None:
        if not ids:
            return
        note = self.prompts.ask_reason("Reconsider view", "Take your earlier decision back to “needs review”? The earlier decision stays on record. Note (optional):",
                                       ok_text="Reconsider")
        if note is None:
            return
        self._act(lambda: self.session.reconsider_views(ids, note), "Back to needs review.")

    def act_change_type(self, view_id: Optional[str]) -> None:
        """Change View Type: the view is kept and re-classified by an engineer decision (this is not a rejection)."""
        if not view_id:
            return
        arch = self.session.project.architecture_of(self.session.source_id_of(view_id))
        current = arch.get(view_id).view_type.value
        shown = current if current in dict(guide.VIEW_TYPE_CHOICES) else "unknown"
        answer = self.prompts.ask_view_type(self._names([view_id])[0], guide.type_label(current), list(guide.VIEW_TYPE_CHOICES), shown)
        if answer is None:
            return
        key, note = answer
        self._act(lambda: self.session.change_view_type(view_id, key, note), f"View type changed to {guide.type_label(key).lower()}.")

    def act_set_floor(self, view_id: Optional[str]) -> None:
        if not view_id:
            return
        key = self.prompts.ask_level(self._names([view_id])[0], LEVEL_OPTIONS)
        if not key:
            return
        self._act(lambda: self.session.set_view_field(view_id, "level_key", key, "Set by the engineer."), "Floor set.")

    # ---- questions: Oracle suggests, the engineer decides or answers

    def act_accept_alternative(self, set_id: Optional[str], alt_id: Optional[str]) -> None:
        if not set_id or not alt_id:
            self.prompts.info("Choose a suggestion", "Choose one of Oracle's suggestions first, or use Ask Engineer.")
            return
        card = self.session.card(f"set:{set_id}")
        pick = next((s for s in card.suggestions if s.id == alt_id), None)
        if pick is None:
            self.prompts.error("Not done", "That suggestion is no longer available.")
            return
        consequence = pick.consequence or "the answer is recorded and no model value changes"
        note = self.prompts.ask_reason("Accept suggestion", f"Oracle suggests: {pick.label}.\nIf you accept, Oracle will: {consequence}.\n"
                                       "The other suggestions will be set aside. Note (optional):", ok_text="Accept suggestion")
        if note is None:
            return
        self._act(lambda: self.session.accept_alternative(set_id, alt_id, note), "Suggestion accepted.")

    def act_reject_alternative(self, set_id: Optional[str], alt_id: Optional[str]) -> None:
        if not set_id or not alt_id:
            self.prompts.info("Choose a reading", "Select a question and one of its possible readings first.")
            return
        reason = self.prompts.ask_reason("Reject this reading", "Reject this reading? Nothing in the model changes. Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.reject_alternative(set_id, alt_id, reason), f"Rejected a reading for {set_id}.")

    def act_ask_engineer(self, target_id: Optional[str] = None, set_id: Optional[str] = None) -> None:
        """Ask Engineer: the engineer says in their own words what Oracle should understand. Kept verbatim as engineer input."""
        suggestions, subject = None, "Anything you tell Oracle here is saved as engineer input."
        if set_id:
            try:
                card = self.session.card(f"set:{set_id}")
            except (StopIteration, KeyError):
                card = None
            if card is not None:
                subject = card.title
                suggestions = [s.label for s in card.suggestions] or None
                target_id = target_id or card.ask_target
        elif target_id:
            try:
                subject = f"About “{self.session.view_name(target_id)}”" if target_id.startswith("VIEW-") else "About the selected item"
            except ActionRefused:
                pass
        if target_id is None and set_id is None:
            target_id = self.session.source_id()
        answer = self.prompts.ask_engineer_input(subject, suggestions)
        if answer is None:
            return
        statement, notes = answer
        self._act(lambda: self.session.ask_engineer(target_id, statement, notes, set_id), "Saved as engineer input.")

    def act_enter_height(self, set_id: Optional[str]) -> None:
        if not set_id:
            return
        card = self.session.card(f"set:{set_id}")
        text = self.prompts.ask_number("Enter engineer value", card.title.rstrip("?") + "?", "millimetres")
        if not text:
            return
        self._act(lambda: self.session.answer_height(set_id, text.replace(",", "").strip()), "Height recorded.")

    # ---- observations and the less common operations (Details)

    def act_review_observations(self, ids: list, accept: bool) -> None:
        if not ids:
            self.prompts.info("Select observations", "Select one or more observations (or a category) first.")
            return
        reason = self.prompts.ask_reason("Approve observations" if accept else "Reject observations",
                                         f"{'Approve' if accept else 'Reject'} {len(ids)} observation(s)? This says the drawing shows them; it says nothing structural. Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.review_observations(ids, accept=accept, reason=reason), f"{'Approved' if accept else 'Rejected'} {len(ids)} observation(s).")

    def act_approve_hint(self, ids: list) -> None:
        if len(ids) != 1:
            self.prompts.info("Select one observation", "Select a single observation that carries a proposed reading.")
            return
        reason = self.prompts.ask_reason("Approve proposed reading", "Approve Oracle's proposed reading of this observation? It approves the proposal only. Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.approve_hint(ids[0], reason), "Proposed reading approved.")

    def act_accept_issue(self, issue_id: Optional[str]) -> None:
        if not issue_id:
            self.prompts.info("Select an issue", "Select an issue first.")
            return
        reason = self.prompts.ask_reason("Acknowledge", "Accept this as it stands? It stays on record as accepted, not fixed. A reason is required:", required=True)
        if reason is None:
            return
        self._act(lambda: self.session.accept_issue(issue_id, reason), "Accepted, with your reason.")

    def act_view_title(self, ids: list) -> None:
        if len(ids) != 1:
            self.prompts.info("Select one view", "Select a single view to rename.")
            return
        row = next((r for r in self.view_rows if r.id == ids[0]), None)
        title = self.prompts.ask_text("Rename view", "Title for this view:", row.title if row else "")
        if not title:
            return
        reason = self.prompts.ask_reason("Rename view", "Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.set_view_field(ids[0], "title", title, reason), "View renamed.")

    def act_view_level(self, ids: list) -> None:
        if len(ids) != 1:
            self.prompts.info("Select one view", "Select a single floor plan to name its level.")
            return
        self.act_set_floor(ids[0])

    def act_align(self, ids: list) -> None:
        if len(ids) != 1:
            self.prompts.info("Select one plan", "Select a single floor plan to align to the building frame.")
            return
        pair = self.prompts.ask_pair("Align plan", "Translation that places this plan in the building frame, in the drawing's own units (building = plan + translation).",
                                     ("Translation x", "Translation y"))
        if pair is None:
            return
        try:
            dx, dy = float(pair[0]), float(pair[1])
        except ValueError:
            self.prompts.error("Not a number", "Enter the translation as two numbers.")
            return
        reason = self.prompts.ask_reason("Align plan", "How did you determine this alignment? (optional)")
        if reason is None:
            return
        self._act(lambda: self.session.align_view(ids[0], dx, dy, reason), "Alignment recorded.")

    def act_merge(self, ids: list) -> None:
        if len(ids) < 2:
            self.prompts.info("Select two or more views", "Select the views that are really one view.")
            return
        reason = self.prompts.ask_reason("Merge views", f"Merge {len(ids)} views into one? The originals stay on record as superseded. Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.merge_views(ids, reason), "Views merged.")

    def act_split(self, ids: list) -> None:
        if len(ids) != 1:
            self.prompts.info("Select one view", "Select a single view to split.")
            return
        pair = self.prompts.ask_pair("Split view", "Split this view along x = value or y = value, in the drawing's own units. Each entity goes to the side its centre is on.",
                                     ("Axis (x or y)", "Coordinate"), ("x", ""))
        if pair is None:
            return
        try:
            coordinate = float(pair[1])
        except ValueError:
            self.prompts.error("Not a number", "Enter the coordinate as a number.")
            return
        reason = self.prompts.ask_reason("Split view", "Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.split_view(ids[0], pair[0], coordinate, reason), "View split.")

    def act_establish_levels(self) -> None:
        try:
            sid = self.session.source_id()
        except (SourceChoiceRequired, ActionRefused):
            return
        detected, _built = self.session.levels(sid)
        detected = [l for l in detected if l.established_id is None]
        if not detected:
            self.prompts.info("No levels to establish", "Oracle has not detected any level that is not already established.")
            return
        suggestion = self.session.suggest_level_elevations(sid)
        if suggestion is None:
            note = ("Oracle cannot suggest elevations: the evidence is disputed, incomplete, or a question about it is still open. Resolve the questions first, or "
                    "supply the elevations you are establishing yourself. Nothing is established until you press Establish.")
        else:
            note = ("These elevations are Oracle's SUGGESTION from the drawing's written levels, relative to the lowest plan level. They are not accepted until you "
                    "press Establish, and they are not a site datum or a structural level.")
        answer = self.prompts.ask_establish_levels([(l.key, "; ".join(l.plan_titles[:1]) or l.label) for l in detected], suggestion, note)
        if answer is None:
            return
        self._act(lambda: self.session.establish_levels(answer["elevations"], elevation_type=answer["elevation_type"], reason=answer["reason"], source_id=sid),
                  f"Established {len(answer['elevations'])} level(s).")

    def act_level_rename(self, ids: list) -> None:
        if len(ids) != 1:
            self.prompts.info("Select a level", "Select an established level to rename.")
            return
        current = next((b for b in self.session.levels()[1] if b.id == ids[0]), None)
        name = self.prompts.ask_text("Rename level", "Engineer-facing name for this level (its identity does not change):", current.name if current else "")
        if not name:
            return
        self._act(lambda: self.session.rename_level(ids[0], name), "Level renamed.")

    def act_level_value(self, ids: list, field: str) -> None:
        if len(ids) != 1:
            self.prompts.info("Select a level", "Select an established level first.")
            return
        label = "structural elevation" if field.startswith("structural") else "elevation"
        text = self.prompts.ask_text(f"Set {label}", f"New {label} in millimetres (recorded as an engineer decision; levels above may move if a storey height changes):")
        if not text:
            return
        try:
            value = float(text.replace(",", ""))
        except ValueError:
            self.prompts.error("Not a number", "Enter the value in millimetres.")
            return
        reason = self.prompts.ask_reason(f"Set {label}", "Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.set_level_value(ids[0], field, value, reason), f"{label.capitalize()} set.")

    # =================================================================== files: add, open, save, reload, exit

    def add_drawing(self) -> None:
        path = self.prompts.ask_open_drawing(self.initial_dir)
        if not path:
            return
        info = self.session.inspect_file(path)
        if not info.ok:
            self.prompts.error("This file cannot be used", info.problem or "")
            return
        self.pending_file, self.file_info = path, info
        self._begin(path, self.session.engineer, True)

    def open_project_dialog(self) -> None:
        path = self.prompts.ask_open_project(self.initial_dir)
        if path:
            self.open_project(path)

    def open_project(self, path) -> bool:
        if self.session.dirty and self.session.has_project and not self.prompts.confirm("Unsaved decisions", "Opening another project discards decisions you have not saved. Continue?"):
            return False
        try:
            self.session.open_project(path)
        except WorkflowError as exc:
            self.prompts.error(exc.title, exc.message)
            return False
        if not self.session.has_project:
            self.prompts.info("No architectural interpretation", "This project has no interpreted drawing. Choose a drawing to interpret.")
            self.show_import()
            return False
        self.engineer_var.set(self.session.engineer)
        self.review.set_mode(QUEUE, refresh=False)
        self.review.current_key = None
        self.show_review()
        self.flash(f"Opened {Path(str(path)).name}; the drawing was not reinterpreted.")
        return True

    def save_project(self, as_new: bool = False) -> Optional[Path]:
        if not self.session.has_project:
            return None
        path = None if (as_new or not self.session.project_path) else self.session.project_path
        if path is None:
            first = self.session.sources()[0].file if self.session.sources() else "project"
            path = self.prompts.ask_save_project(self.initial_dir, f"{Path(first).stem}.oracle.json")
            if not path:
                return None
        self.flash("Saving…")
        self.update_idletasks()
        try:
            saved = self.session.save_project(path)
        except ActionRefused as exc:
            self.prompts.error("Not saved", str(exc))
            return None
        self.review.save_btn.config(text="Save")
        self.flash(f"Saved {saved.name}")
        return saved

    def reload_linework(self) -> None:
        sid = self.session.source_id()
        try:
            if Path(str(self.session.source_paths.get(sid) or "")).is_file():
                self.session.reload_geometry(sid)
            else:
                path = self.prompts.ask_open_drawing(self.initial_dir)
                if not path:
                    return
                self.session.reload_geometry(sid, path)
        except ActionRefused as exc:
            self.prompts.error("Not reloaded", str(exc))
            return
        except WorkflowError as exc:
            self.prompts.error(exc.title, exc.message)
            return
        self.refresh()

    def exit(self) -> None:
        if self.session.dirty and self.session.has_project:
            if not self.prompts.confirm("Unsaved decisions", "You have decisions that are not saved to a project file. Leave without saving?"):
                return
        if self.on_exit:
            self.on_exit()
