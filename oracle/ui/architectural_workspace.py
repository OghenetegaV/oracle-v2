"""Oracle — Architectural Drawing Workspace (interface)

Purpose:
    The Architectural Drawing workflow of the Oracle application, as one frame: choose a DWG or DXF (with its name, type, size and path shown
    and the file checked before anything starts), watch Oracle interpret it in honest stages without freezing the window, then review the
    result: a drawing preview beside tabs for the summary, views, levels, observations, open questions, issues, the approved architecture
    and the engineer's decisions. The engineer accepts, rejects, renames, establishes, aligns, merges and splits through buttons that call
    the session, which records real engineer decisions; the project can be saved, closed and reopened without reinterpreting the drawing.

Role in Oracle:
    The interface for oracle.application.ArchitecturalSession. It holds NO model of its own (the session's OracleProject is the only
    state), shows uncertainty as uncertainty, and words everything as Oracle "detected" / "proposes" and the engineer "approves". It is
    mounted by the existing wizard, so it is part of Oracle rather than a separate application, and it can also stand alone.

Dependencies:
    tkinter; oracle.application; oracle.ui.review_panels, .preview_canvas, .dialogs, .theme.

Consumers:
    oracle_wizard (the Architectural Drawing entry point), tests.

Status:
    Interface (interface phase).

Migration/Notes:
    Interpretation runs on a worker thread and reports through a queue that the interface polls; no widget is touched from the worker.
    All dialogs go through self.prompts, which tests replace with a scripted object.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable, Optional

from oracle.application import (
    ActionRefused, ArchitecturalSession, Overlay, SourceChoiceRequired, STAGE_LABELS, WORKFLOW_STAGES, WorkflowError,
)

from . import theme
from .dialogs import Prompts
from .preview_canvas import PreviewCanvas
from .review_panels import ReviewPanels

DASH = "–"


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
        self._events: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._processing_file: Optional[str] = None
        self._add_source_mode = False
        self._polling = False
        self._after_ids: set = set()
        self._stage_rows: dict = {}
        self._screen = None
        self.start_frame = self._build_start()
        self.processing_frame = self._build_processing()
        self.review_frame = self._build_review()
        self.show_start()

    # =================================================================== screens

    def _switch(self, frame: tk.Frame, name: str) -> None:
        for f in (self.start_frame, self.processing_frame, self.review_frame):
            f.pack_forget()
        frame.pack(fill="both", expand=True)
        self._screen = name

    @property
    def screen(self) -> Optional[str]:
        return self._screen

    def show_start(self) -> None:
        self._refresh_start()
        self._switch(self.start_frame, "start")

    def show_processing(self) -> None:
        self._switch(self.processing_frame, "processing")

    def show_review(self) -> None:
        self._switch(self.review_frame, "review")
        self.refresh()

    # =================================================================== 1. choose a drawing

    def _build_start(self) -> tk.Frame:
        f = tk.Frame(self, bg=theme.BG)
        inner = tk.Frame(f, bg=theme.BG)
        inner.pack(fill="both", expand=True, padx=34, pady=22)
        tk.Label(inner, text="Architectural Drawing", font=theme.TITLE, bg=theme.BG).pack(anchor="w")
        tk.Label(inner, bg=theme.BG, font=theme.SUBTITLE, justify="left", wraplength=780, text=(
            "Choose an architectural DWG or DXF. Oracle reads it, proposes what it appears to contain (views, levels, observations) and lists what it is "
            "unsure of. You review each proposal and decide; nothing is approved until you approve it. Your drawing is never changed.")).pack(anchor="w", pady=(4, 14))
        box = tk.LabelFrame(inner, text=" Drawing ", font=theme.BOLD, bg=theme.BG, padx=14, pady=10)
        box.pack(fill="x")
        row = tk.Frame(box, bg=theme.BG)
        row.pack(fill="x")
        self.browse_btn = tk.Button(row, text="Browse for a drawing…", command=self.choose_file, bg=theme.ACCENT, fg="white", relief="flat",
                                    padx=14, pady=6, cursor="hand2", font=theme.BODY)
        self.browse_btn.pack(side="left")
        self.info_vars = {k: tk.StringVar(value=DASH) for k in ("name", "type", "size", "path")}
        grid = tk.Frame(box, bg=theme.BG)
        grid.pack(fill="x", pady=(10, 2))
        for r, (key, label) in enumerate((("name", "File name"), ("type", "File type"), ("size", "File size"), ("path", "Selected path"))):
            tk.Label(grid, text=label, font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, width=14, anchor="w").grid(row=r, column=0, sticky="w")
            tk.Label(grid, textvariable=self.info_vars[key], font=theme.BODY, bg=theme.BG, anchor="w", wraplength=640, justify="left").grid(row=r, column=1, sticky="w")
        self.file_status = tk.Label(box, text="No drawing selected yet.", font=theme.BODY, bg=theme.BG, fg=theme.MUTED, anchor="w", justify="left", wraplength=760)
        self.file_status.pack(fill="x", pady=(8, 0))
        eng = tk.Frame(inner, bg=theme.BG)
        eng.pack(fill="x", pady=(14, 0))
        tk.Label(eng, text="Engineer (recorded on every decision):", font=theme.BODY, bg=theme.BG).pack(side="left")
        self.engineer_var = tk.StringVar(value=self.session.engineer)
        tk.Entry(eng, textvariable=self.engineer_var, width=30, font=theme.BODY).pack(side="left", padx=8)
        actions = tk.Frame(inner, bg=theme.BG)
        actions.pack(fill="x", pady=18)
        self.interpret_btn = tk.Button(actions, text="Interpret drawing", command=self.start_interpretation, state="disabled", bg=theme.ACCENT, fg="white",
                                       relief="flat", padx=16, pady=8, cursor="hand2", font=theme.BOLD)
        self.interpret_btn.pack(side="left")
        tk.Button(actions, text="Open saved project…", command=self.open_project_dialog, relief="flat", padx=14, pady=8, cursor="hand2",
                  font=theme.BODY).pack(side="left", padx=10)
        self.continue_btn = tk.Button(actions, text="Continue review", command=self.show_review, relief="flat", padx=14, pady=8, cursor="hand2", font=theme.BODY)
        self.home_btn = tk.Button(actions, text="← Back to Oracle home", command=self.exit, relief="flat", padx=14, pady=8, cursor="hand2", font=theme.BODY)
        self.home_btn.pack(side="right")
        self.start_message = tk.Label(inner, text="", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, anchor="w", justify="left", wraplength=780)
        self.start_message.pack(fill="x")
        return f

    def _refresh_start(self) -> None:
        if self.session.has_project:
            self.continue_btn.pack(side="left", padx=4)
        else:
            self.continue_btn.pack_forget()

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
        self.info_vars["size"].set(info.size_text)
        self.info_vars["path"].set(str(info.path))
        if info.ok:
            self.file_status.config(text="✓ Oracle can read this file." + (f"  {info.note}" if info.note else ""), fg=theme.GREEN)
            self.interpret_btn.config(state="normal")
        else:
            self.file_status.config(text=f"✕ {info.problem}", fg=theme.RED)
            self.interpret_btn.config(state="disabled")

    # =================================================================== 2. processing

    def _build_processing(self) -> tk.Frame:
        f = tk.Frame(self, bg=theme.BG)
        inner = tk.Frame(f, bg=theme.BG)
        inner.pack(fill="both", expand=True, padx=34, pady=22)
        self.proc_title = tk.Label(inner, text="Interpreting drawing", font=theme.TITLE, bg=theme.BG)
        self.proc_title.pack(anchor="w")
        self.proc_file = tk.Label(inner, text="", font=theme.SUBTITLE, bg=theme.BG, fg=theme.MUTED)
        self.proc_file.pack(anchor="w", pady=(2, 10))
        self.proc_step = tk.Label(inner, text="", font=theme.BOLD, bg=theme.BG)
        self.proc_step.pack(anchor="w")
        self.proc_bar = ttk.Progressbar(inner, mode="indeterminate", length=520)
        self.proc_bar.pack(anchor="w", pady=(6, 12))
        tk.Label(inner, bg=theme.BG, fg=theme.MUTED, font=theme.SMALL, justify="left", wraplength=760, text=(
            "Each stage is marked done only when Oracle has actually finished it. Large drawings can take a minute; the window stays responsive.")).pack(anchor="w")
        self.stage_frame = tk.Frame(inner, bg=theme.BG)
        self.stage_frame.pack(anchor="w", pady=10)
        for key in WORKFLOW_STAGES:
            row = tk.Frame(self.stage_frame, bg=theme.BG)
            row.pack(anchor="w")
            glyph = tk.Label(row, text="○", width=2, font=theme.BODY, bg=theme.BG, fg=theme.MUTED)
            glyph.pack(side="left")
            label = tk.Label(row, text=STAGE_LABELS[key], font=theme.BODY, bg=theme.BG, fg=theme.MUTED)
            label.pack(side="left")
            self._stage_rows[key] = (glyph, label)
        self.error_box = tk.Frame(inner, bg=theme.RED_BG, highlightthickness=1, highlightbackground=theme.RED)
        self.error_title = tk.Label(self.error_box, text="", font=theme.BOLD, bg=theme.RED_BG, fg=theme.RED, anchor="w")
        self.error_title.pack(fill="x", padx=12, pady=(10, 0))
        self.error_file = tk.Label(self.error_box, text="", font=theme.SMALL, bg=theme.RED_BG, fg=theme.MUTED, anchor="w", wraplength=740, justify="left")
        self.error_file.pack(fill="x", padx=12)
        self.error_message = tk.Label(self.error_box, text="", font=theme.BODY, bg=theme.RED_BG, anchor="w", wraplength=740, justify="left")
        self.error_message.pack(fill="x", padx=12, pady=6)
        self.error_note = tk.Label(self.error_box, text="", font=theme.SMALL, bg=theme.RED_BG, fg=theme.MUTED, anchor="w", wraplength=740, justify="left")
        self.error_note.pack(fill="x", padx=12)
        row = tk.Frame(self.error_box, bg=theme.RED_BG)
        row.pack(fill="x", padx=12, pady=10)
        tk.Button(row, text="Try again", command=self.retry, bg=theme.ACCENT, fg="white", relief="flat", padx=14, pady=5).pack(side="left")
        tk.Button(row, text="Choose another drawing", command=self.show_start, relief="flat", padx=14, pady=5).pack(side="left", padx=8)
        return f

    def start_interpretation(self, *, add_source: bool = False) -> None:
        if not self.pending_file or not (self.file_info and self.file_info.ok):
            return
        engineer = self.engineer_var.get().strip()
        if not engineer:
            self.start_message.config(text="Enter the engineer's name: it is recorded on every decision.", fg=theme.RED)
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
        self.canvas.cancel_pending()
        super().destroy()

    def _on_stage(self, e) -> None:
        glyph, label = self._stage_rows[e.key]
        if e.state == "start":
            glyph.config(text="●", fg=theme.ACCENT)
            label.config(fg="black")
            self.proc_step.config(text=f"Step {e.index} of {e.total}: {e.label}…")
        elif e.state == "done":
            glyph.config(text="✓", fg=theme.GREEN)
            label.config(fg="black")
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

    # =================================================================== 3. review

    def _build_review(self) -> tk.Frame:
        f = tk.Frame(self, bg=theme.BG)
        bar = tk.Frame(f, bg="white")
        bar.pack(fill="x")
        for text, command in (("← Oracle home", self.exit), ("Drawings…", self.show_start), ("Add drawing…", self.add_drawing),
                              ("Open project…", self.open_project_dialog), ("Save project", self.save_project), ("Save as…", lambda: self.save_project(as_new=True))):
            tk.Button(bar, text=text, command=command, font=theme.SMALL, relief="flat", padx=9, pady=5, cursor="hand2").pack(side="left", padx=(4, 0), pady=4)
        tk.Label(bar, text="Source:", font=theme.SMALL, bg="white", fg=theme.MUTED).pack(side="left", padx=(16, 2))
        self.source_var = tk.StringVar()
        self.source_box = ttk.Combobox(bar, textvariable=self.source_var, state="readonly", width=44)
        self.source_box.pack(side="left")
        self.source_box.bind("<<ComboboxSelected>>", lambda e: self._on_source_chosen())
        self.banner = tk.Label(bar, text="", font=theme.BOLD, padx=12, pady=5, anchor="e")
        self.banner.pack(side="right", padx=8, pady=3)
        self.banner.bind("<Button-1>", lambda e: self.panels.show_tab("summary"))
        self.choice_notice = tk.Label(f, text="", font=theme.BODY, bg=theme.AMBER_BG, fg=theme.AMBER, anchor="w", padx=12, pady=6, wraplength=1000, justify="left")
        body = ttk.PanedWindow(f, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=theme.BG)
        tools = tk.Frame(left, bg=theme.BG)
        tools.pack(fill="x", padx=4, pady=(4, 2))
        self.canvas = PreviewCanvas(left)
        for text, command in (("Fit", lambda: self.canvas.fit()), ("Zoom +", lambda: self.canvas.zoom(1.4)), ("Zoom −", lambda: self.canvas.zoom(1 / 1.4))):
            tk.Button(tools, text=text, command=command, font=theme.SMALL, relief="flat", padx=8, pady=2, cursor="hand2").pack(side="left", padx=(0, 4))
        tk.Checkbutton(tools, text="Text", variable=self.canvas.show_text, command=lambda: self.canvas._schedule(), font=theme.SMALL, bg=theme.BG).pack(side="left")
        self.reload_btn = tk.Button(tools, text="Reload linework", command=self.reload_linework, font=theme.SMALL, relief="flat", padx=8, pady=2)
        self.legend = tk.Label(tools, text="▭ dashed amber: proposed view   ▭ green: approved   ▭ blue: selected   ▭ orange: unresolved",
                               font=theme.SMALL, bg=theme.BG, fg=theme.MUTED)
        self.legend.pack(side="right")
        self.canvas.pack(fill="both", expand=True, padx=4)
        self.canvas_note = tk.Label(left, text="", font=theme.SMALL, bg=theme.BG, fg=theme.MUTED, anchor="w")
        self.canvas_note.pack(fill="x", padx=6)
        self.canvas.status_callback = lambda note: self.canvas_note.config(text=note)
        self.canvas.on_pick = self._on_canvas_pick
        body.add(left, weight=3)
        self.panels = ReviewPanels(body, self)
        body.add(self.panels, weight=4)
        self.status = tk.Label(f, text="", font=theme.SMALL, bg="white", fg=theme.MUTED, anchor="w", padx=10, pady=4)
        self.status.pack(fill="x", side="bottom")
        return f

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.config(text=text, fg=theme.RED if error else theme.MUTED)

    # ------------------------------------------------------------------ refresh (everything is re-read from the session)

    def refresh(self) -> None:
        s = self.session
        sources = s.sources()
        self.source_box.config(values=[x.label for x in sources])
        active = s.active_source_id
        self.source_var.set(next((x.label for x in sources if x.id == active), ""))
        banner = s.readiness()
        fg, bg = theme.STATE_COLORS[banner.level]
        self.banner.config(text=banner.headline, fg=fg, bg=bg)
        try:
            sid = s.source_id()
        except SourceChoiceRequired as exc:
            self._show_choice(exc.sources, banner)
            return
        except ActionRefused:
            return
        self.choice_notice.pack_forget()
        summary = s.summary(sid)
        self.view_rows = s.view_rows(sid)
        self.detected_levels, built = s.levels(sid)
        self.panels.fill_summary(summary, banner)
        self.panels.fill_views(self.view_rows)
        self.panels.fill_levels(self.detected_levels, built)
        self.refresh_observations()
        self.panels.fill_questions(s.questions(sid))
        self.panels.fill_issues(s.issues(sid))
        self.panels.fill_approved(s.approved(sid))
        self.panels.fill_decisions(s.decision_rows())
        preview = s.preview(sid)
        note = s.geometry_note.get(sid)
        if preview is None:
            self.reload_btn.pack(side="left", padx=4)
        else:
            self.reload_btn.pack_forget()
        self.canvas.set_preview(preview)
        self.canvas.set_overlays(base=s.view_overlays(sid), selection=self._selection_overlay())
        self.canvas_note.config(text=note or "")
        self.set_status(f"{self.session.project_path or 'Not saved yet'}" + ("   •   unsaved decisions" if s.dirty else ""))

    def _show_choice(self, sources: list, banner) -> None:
        self.choice_notice.config(text=f"This project has {len(sources)} drawing sources. Choose which one to review in the Source box above: Oracle does not "
                                       "pick one for you. The readiness banner covers the whole project.")
        self.choice_notice.pack(fill="x", after=self.banner.master)
        self.panels.fill_decisions(self.session.decision_rows())
        self.canvas.set_preview(None)
        self.canvas.set_overlays(base=Overlay(), selection=Overlay())

    def refresh_observations(self) -> None:
        sid = self.session.source_id()
        titles = {r.id: r.title for r in self.view_rows}
        self.panels.fill_observations(self.session.observation_groups(sid), titles)

    def _on_source_chosen(self) -> None:
        label = self.source_var.get()
        for src in self.session.sources():
            if src.label == label:
                self.session.set_active_source(src.id)
                self.selected_ref = None
                self.refresh()
                return

    # ------------------------------------------------------------------ selection and navigation

    def _selection_overlay(self) -> Overlay:
        return Overlay()

    def select_object(self, object_id: str) -> None:
        self.selected_ref = object_id
        self.canvas.set_overlays(selection=self.session.overlay_for_object(object_id), focus=True)

    def select_question(self, set_id: Optional[str]) -> None:
        if not set_id:
            return
        self.canvas.set_overlays(selection=self.session.overlay_for_question(set_id), focus=True)

    def select_issue(self, issue_id: Optional[str], quiet: bool = False) -> None:
        if not issue_id:
            return
        self.canvas.set_overlays(selection=self.session.overlay_for_issue(issue_id), focus=True)

    def go_to_issue_question(self, issue_id: Optional[str]) -> None:
        row = next((r for r in self.session.issues() if r.id == issue_id), None)
        if row is None or not row.set_id:
            self.prompts.info("No linked question", "This issue is not tied to a question Oracle can ask you; accept it with a reason, or fix its cause.")
            return
        self.panels.show_tab("questions")
        self.panels.select_question(row.set_id)

    def go_to_reference(self, ref: str) -> None:
        if ref.startswith("IS-"):
            self.panels.show_tab("questions")
            self.panels.select_question(ref)
        elif ref.startswith("ARC-") or ref.startswith("BLK-"):
            self.panels.show_tab("issues")
            if self.panels.issue_tree.exists(ref):
                self.panels.issue_tree.selection_set(ref)
        elif ref.startswith("VIEW-"):
            self.panels.show_tab("views")
            if self.panels.views_tree.exists(ref):
                self.panels.views_tree.selection_set(ref)
        elif ref.startswith("level"):
            self.panels.show_tab("levels")
        elif ref == "project":
            self.panels.show_tab("levels")
        else:
            self.panels.show_tab("summary")

    def on_tab_changed(self, name: str) -> None:
        if name == "approved" and self.session.has_project:
            try:
                self.canvas.set_overlays(selection=self.session.overlay_approved(), focus=False)
            except (SourceChoiceRequired, ActionRefused):
                pass

    def _on_canvas_pick(self, view_id: Optional[str]) -> None:
        if not view_id:
            return
        self.panels.show_tab("views")
        if self.panels.views_tree.exists(view_id):
            self.panels.views_tree.selection_set(view_id)
            self.panels.views_tree.see(view_id)

    # ------------------------------------------------------------------ engineer actions (all through the session, all recorded)

    def _act(self, fn, done: str):
        try:
            result = fn()
        except ActionRefused as exc:
            self.prompts.error("Not done", str(exc))
            self.set_status(f"Not done: {exc}", error=True)
            return None
        except SourceChoiceRequired:
            self.prompts.error("Choose a source", "Choose which drawing source to work with first.")
            return None
        self.refresh()
        self.set_status(done)
        return result

    def act_review_views(self, ids: list, accept: bool) -> None:
        if not ids:
            self.prompts.info("Select a view", "Select one or more views in the table first.")
            return
        reason = self.prompts.ask_reason("Approve views" if accept else "Reject views",
                                         f"{'Approve' if accept else 'Reject'} {len(ids)} view(s)? Approving says the view is what Oracle proposes it is. Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.review_views(ids, accept=accept, reason=reason), f"{'Approved' if accept else 'Rejected'} {len(ids)} view(s).")

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

    def act_accept_alternative(self, set_id: Optional[str], alt_id: Optional[str]) -> None:
        if not set_id or not alt_id:
            self.prompts.info("Choose a reading", "Select a question and one of its possible readings first.")
            return
        card = next((c for c in self.session.questions() if c.id == set_id), None)
        alt = next((a for a in (card.alternatives if card else []) if a.id == alt_id), None)
        consequence = "; ".join(alt.consequences) if alt and alt.consequences else "the answer is recorded; no model value changes"
        reason = self.prompts.ask_reason("Accept this reading", f"You are accepting '{alt.meaning if alt else alt_id}'.\nOracle will then: {consequence}.\nThe other readings will be rejected. Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.accept_alternative(set_id, alt_id, reason), f"Accepted a reading for {set_id}.")

    def act_reject_alternative(self, set_id: Optional[str], alt_id: Optional[str]) -> None:
        if not set_id or not alt_id:
            self.prompts.info("Choose a reading", "Select a question and one of its possible readings first.")
            return
        reason = self.prompts.ask_reason("Reject this reading", "Reject this reading? Nothing in the model changes. Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.reject_alternative(set_id, alt_id, reason), f"Rejected a reading for {set_id}.")

    def act_accept_issue(self, issue_id: Optional[str]) -> None:
        if not issue_id:
            self.prompts.info("Select an issue", "Select an issue first.")
            return
        reason = self.prompts.ask_reason("Accept issue", "Accept this issue as it stands? It stays on record as accepted, not fixed. A reason is required:", required=True)
        if reason is None:
            return
        self._act(lambda: self.session.accept_issue(issue_id, reason), f"Accepted issue {issue_id}.")

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
        key = self.prompts.ask_text("Set level", "Level of this plan: GROUND, FLOOR:1, FLOOR:2, BASEMENT:1, ROOF, MEZZANINE, or NAMED:<LABEL> for any other name (for example NAMED:PODIUM).")
        if not key:
            return
        reason = self.prompts.ask_reason("Set level", "Reason (optional):")
        if reason is None:
            return
        self._act(lambda: self.session.set_view_field(ids[0], "level_key", key.strip().upper(), reason), f"Level {key.strip().upper()} set.")

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
        detected = [l for l in self.detected_levels if l.established_id is None]
        if not detected:
            self.prompts.info("No levels to establish", "Oracle has not detected any level that is not already established.")
            return
        sid = self.session.source_id()
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
        self.panels.show_tab("levels")

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

    # ------------------------------------------------------------------ files: add, open, save, reload, exit

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
            self.show_start()
            return False
        self.engineer_var.set(self.session.engineer)
        self.show_review()
        self.set_status(f"Opened {Path(str(path)).name}; the drawing was not reinterpreted.")
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
        self.set_status("Saving…")
        self.update_idletasks()
        try:
            saved = self.session.save_project(path)
        except ActionRefused as exc:
            self.prompts.error("Not saved", str(exc))
            return None
        self.set_status(f"Saved {saved}")
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
