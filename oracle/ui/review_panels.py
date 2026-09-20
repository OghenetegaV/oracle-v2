"""Oracle — Review Panels (interface)

Purpose:
    The tabs of the Architectural Interpretation Review screen: Summary (what Oracle read, and what stands between the project and readiness),
    Views, Levels (detected, and engineer-established), Observations (grouped by the interpreter's own vocabulary), Questions (open
    interpretations with their alternatives, evidence and linked issues, where the engineer accepts or rejects a reading), Issues, the
    Approved architecture (built from project.approved_architecture(), separated from what is still proposed or unresolved) and the
    engineer Decisions recorded in the project. Each tab shows rows read from the session and offers the engineer actions that apply to it.

Role in Oracle:
    The presentation of oracle.application.review_models. A panel never computes what is ready, never edits the project and never invents
    a label: it asks the session for rows and, when the engineer acts, calls the matching session method (which records the decision).
    Wording is deliberately cautious: Oracle "detected" and "proposes"; the engineer "approves".

Dependencies:
    tkinter; oracle.application (rows, actions and their errors); oracle.ui.theme.

Consumers:
    oracle.ui.architectural_workspace.

Status:
    Interface (interface phase).

Migration/Notes:
    Panels are rebuilt from the session after every action (refresh), keeping the selection when the row still exists. There is no
    interface-side copy of any model.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from oracle.application.review_models import OBSERVATION_CATEGORIES, category_title

from . import theme


DASH = "–"


def make_tree(parent, columns: list, *, height: int = 8, selectmode: str = "extended", tags: Optional[dict] = None, show_tree: bool = False):
    """A Treeview with scrollbars. `columns` is [(id, heading, width, anchor, stretch)]. Returns (frame, tree)."""
    frame = tk.Frame(parent, bg=theme.PANEL)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="tree headings" if show_tree else "headings", height=height,
                        selectmode=selectmode)
    if show_tree:
        tree.column("#0", width=22, minwidth=22, stretch=False)
    for col in columns:
        cid, heading, width = col[0], col[1], col[2]
        tree.heading(cid, text=heading, anchor="w")
        tree.column(cid, width=width, minwidth=30, anchor=col[3] if len(col) > 3 else "w", stretch=col[4] if len(col) > 4 else True)
    ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ys.set)
    tree.pack(side="left", fill="both", expand=True)
    ys.pack(side="right", fill="y")
    for name, (fg, bg) in (tags or {}).items():
        tree.tag_configure(name, foreground=fg, background=bg)
    return frame, tree


def readonly_text(parent, height: int = 6, font=theme.BODY):
    text = tk.Text(parent, height=height, wrap="word", font=font, bg=theme.PANEL, relief="flat", highlightthickness=1,
                   highlightbackground=theme.LINE, padx=8, pady=6)
    text.tag_configure("h", font=theme.BOLD, spacing1=6)
    text.tag_configure("muted", foreground=theme.MUTED)
    text.tag_configure("warn", foreground=theme.AMBER)
    text.tag_configure("bad", foreground=theme.RED)
    text.tag_configure("good", foreground=theme.GREEN)
    text.config(state="disabled")
    return text


def fill_text(text: tk.Text, parts: list) -> None:
    """parts: [(string, tag-or-None)]"""
    text.config(state="normal")
    text.delete("1.0", "end")
    for string, tag in parts:
        text.insert("end", string, tag or ())
    text.config(state="disabled")


class ReviewPanels(ttk.Notebook):
    def __init__(self, master, workspace):
        super().__init__(master)
        self.ws = workspace
        self._tabs: dict = {}
        self._build_summary()
        self._build_views()
        self._build_levels()
        self._build_observations()
        self._build_questions()
        self._build_issues()
        self._build_approved()
        self._build_decisions()
        self.bind("<<NotebookTabChanged>>", lambda e: self.ws.on_tab_changed(self.tab_name()))

    # ---------------------------------------------------------------- plumbing

    def _add(self, key: str, title: str) -> tk.Frame:
        frame = tk.Frame(self, bg=theme.PANEL)
        self.add(frame, text=title)
        self._tabs[key] = frame
        return frame

    def tab_name(self) -> str:
        current = self.select()
        for key, frame in self._tabs.items():
            if str(frame) == current:
                return key
        return ""

    def show_tab(self, key: str) -> None:
        self.select(self._tabs[key])

    @staticmethod
    def _selected(tree) -> list:
        return list(tree.selection())

    def _buttons(self, parent, spec: list) -> tk.Frame:
        row = tk.Frame(parent, bg=theme.PANEL)
        for text, command, primary in spec:
            tk.Button(row, text=text, command=command, font=theme.SMALL, relief="flat", padx=9, pady=3, cursor="hand2",
                      bg=theme.ACCENT if primary else "#e5e7eb", fg="white" if primary else "black").pack(side="left", padx=(0, 5))
        return row

    # ---------------------------------------------------------------- summary

    def _build_summary(self):
        f = self._add("summary", "Summary")
        self.summary_text = readonly_text(f, height=17)
        self.summary_text.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(f, text="What stands between this project and readiness (from Oracle's readiness check):", font=theme.BOLD, bg=theme.PANEL,
                 anchor="w").pack(fill="x", padx=8)
        frame, self.blockers_tree = make_tree(f, [("kind", "Kind", 170), ("ref", "Reference", 90), ("msg", "Detail", 320)], height=8, selectmode="browse")
        frame.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.blockers_tree.bind("<Double-1>", lambda e: self._go_to_blocker())

    def _go_to_blocker(self):
        sel = self._selected(self.blockers_tree)
        if sel:
            self.ws.go_to_reference(self.blockers_tree.set(sel[0], "ref"))

    def fill_summary(self, s, banner) -> None:
        d = s.source
        unit_state = ("good" if s.unit_confirmed else "warn")
        parts = [("Drawing\n", "h"), (f"  File: {d.file}\n  Source: {d.id}   Revision: {d.revision or 'not stated'}   Interpretation run: {d.interpretation_id}\n"
                                    f"  Source hash (SHA-256): {d.sha256}\n", None),
                 ("Units\n", "h"), (f"  Oracle reads this drawing as {s.unit} (factor {s.unit_factor_to_mm:g} to millimetres), confidence {s.unit_confidence:.2f}: ", None),
                 (f"{s.unit_status}\n", unit_state)]
        if d.declared_unit:
            parts.append((f"  The file declares {d.declared_unit}.\n", "muted"))
        if s.unit_note:
            parts.append((f"  {s.unit_note}\n", "warn"))
        parts += [("Coordinates and frames\n", "h")] + [(f"  {line}\n", None) for line in s.frame_lines]
        by_type = ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in sorted(s.views_by_type.items()))
        parts += [("What Oracle detected\n", "h"),
                  (f"  {s.views_total} view(s): {by_type}\n  {s.plan_levels_detected} level(s) named on plans; {s.levels_established} building level(s) established by the engineer\n"
                   f"  {s.observations} architectural observation(s) of {s.observation_kinds} kind(s)\n", None),
                  ("What needs the engineer\n", "h"),
                  (f"  {s.open_interpretations} unresolved interpretation(s)   ({s.resolved_interpretations} settled)\n"
                   f"  {s.issues_open} open issue(s): {s.issues_blocking} blocking, {s.issues_error} error, {s.issues_warning} warning\n"
                   f"  {s.views_unreviewed} view(s) not yet reviewed, {s.views_accepted} approved\n", "warn" if (s.open_interpretations or s.issues_open) else "good")]
        if s.warnings:
            parts += [("Reader notes\n", "h")] + [(f"  {w}\n", "muted") for w in s.warnings[:4]]
        fill_text(self.summary_text, parts)
        self.blockers_tree.delete(*self.blockers_tree.get_children())
        for kind, ref, message in banner.blockers:
            self.blockers_tree.insert("", "end", values=(kind, ref, message))

    # ---------------------------------------------------------------- views

    def _build_views(self):
        f = self._add("views", "Views")
        cols = [("id", "ID", 64), ("title", "Title", 190), ("type", "Type", 78), ("level", "Level", 88), ("conf", "Conf.", 44, "e", False),
                ("review", "Review", 130), ("notes", "Notes", 140)]
        frame, self.views_tree = make_tree(f, cols, height=12, tags={"open": (theme.AMBER, "#fffbeb"), "approved": (theme.GREEN, "#f0fdf4"),
                                                                       "rejected": (theme.RED, "#fef2f2")})
        frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.views_tree.bind("<<TreeviewSelect>>", lambda e: self._on_view_selected())
        self._buttons(f, [("Approve selected", lambda: self.ws.act_review_views(self._selected(self.views_tree), True), True),
                          ("Reject selected", lambda: self.ws.act_review_views(self._selected(self.views_tree), False), False),
                          ("Rename…", lambda: self.ws.act_view_title(self._selected(self.views_tree)), False),
                          ("Set level…", lambda: self.ws.act_view_level(self._selected(self.views_tree)), False),
                          ("Align…", lambda: self.ws.act_align(self._selected(self.views_tree)), False),
                          ("Merge…", lambda: self.ws.act_merge(self._selected(self.views_tree)), False),
                          ("Split…", lambda: self.ws.act_split(self._selected(self.views_tree)), False)]).pack(fill="x", padx=8, pady=2)
        self.view_detail = readonly_text(f, height=6, font=theme.SMALL)
        self.view_detail.pack(fill="x", padx=8, pady=(4, 8))

    def fill_views(self, rows: list) -> None:
        keep = set(self._selected(self.views_tree))
        self.views_tree.delete(*self.views_tree.get_children())
        for r in rows:
            tag = "approved" if r.review == "accepted" else "rejected" if r.review == "rejected" else "open" if r.open_sets or r.flags else ""
            title = r.title + (f"  [{r.variant}]" if r.variant else "")
            self.views_tree.insert("", "end", iid=r.id, values=(r.id, title, r.type, r.level_label or "–", f"{r.confidence:.2f}", r.review_words,
                                                                 "; ".join(r.flags)), tags=(tag,) if tag else ())
        for iid in keep:
            if self.views_tree.exists(iid):
                self.views_tree.selection_add(iid)

    def _on_view_selected(self):
        sel = self._selected(self.views_tree)
        if not sel:
            return
        self.ws.select_object(sel[0])
        arch_rows = {r.id: r for r in self.ws.view_rows}
        r = arch_rows.get(sel[0])
        if r is None:
            return
        evidence = self.ws.session.evidence_for_object(r.id)
        parts = [(f"{r.id}  {r.title}\n", "h"), (f"Type: {r.type}   Level: {r.level_key or 'not named'}   Confidence: {r.confidence:.2f}   Entities: {r.entity_count}\n", None)]
        if r.aligned is not None:
            parts.append((f"Alignment to the building frame: {'set' if r.aligned else 'not set'}\n", "good" if r.aligned else "warn"))
        if r.open_sets:
            parts.append((f"Open question(s): {', '.join(r.open_sets)}\n", "warn"))
        parts.append(("Evidence Oracle recorded:\n", "h"))
        parts += [(f"  • {e}\n", "muted") for e in evidence] or [("  none recorded\n", "muted")]
        fill_text(self.view_detail, parts)

    # ---------------------------------------------------------------- levels

    def _build_levels(self):
        f = self._add("levels", "Levels")
        tk.Label(f, text="Detected by Oracle from the drawing (a reading, not confirmed):", font=theme.BOLD, bg=theme.PANEL, anchor="w").pack(fill="x", padx=8, pady=(8, 0))
        cols = [("key", "Level", 84), ("label", "Drawing label", 130), ("above", "Height above previous", 120, "e"), ("elev", "From lowest plan", 100, "e"),
                ("status", "Status", 150), ("note", "Evidence / conflict", 200)]
        frame, self.detected_tree = make_tree(f, cols, height=6, selectmode="browse", tags={"conflict": (theme.RED, "#fef2f2"), "est": (theme.GREEN, "#f0fdf4")})
        frame.pack(fill="x", padx=8)
        self.detected_tree.bind("<<TreeviewSelect>>", lambda e: self._on_detected_selected())
        tk.Label(f, text="Established by the engineer (in the building model):", font=theme.BOLD, bg=theme.PANEL, anchor="w").pack(fill="x", padx=8, pady=(8, 0))
        cols2 = [("id", "ID", 56), ("name", "Engineer label", 110), ("src", "Drawing label", 110), ("elev", "Elevation (mm)", 90, "e"), ("type", "Elevation is", 120),
                 ("h", "Storey height", 80, "e"), ("st", "Structural elev.", 90, "e"), ("status", "Status", 130)]
        frame2, self.built_tree = make_tree(f, cols2, height=5, selectmode="browse", tags={"eng": (theme.GREEN, "#f0fdf4"), "der": (theme.AMBER, "#fffbeb")})
        frame2.pack(fill="x", padx=8)
        self._buttons(f, [("Establish levels…", self.ws.act_establish_levels, True),
                          ("Rename…", lambda: self.ws.act_level_rename(self._selected(self.built_tree)), False),
                          ("Set elevation…", lambda: self.ws.act_level_value(self._selected(self.built_tree), "elevation_mm"), False),
                          ("Set structural elevation…", lambda: self.ws.act_level_value(self._selected(self.built_tree), "structural_elevation_mm"), False)]
                      ).pack(fill="x", padx=8, pady=6)
        self.level_note = readonly_text(f, height=5, font=theme.SMALL)
        self.level_note.pack(fill="x", padx=8, pady=(0, 8))

    def fill_levels(self, detected: list, built: list) -> None:
        self.detected_tree.delete(*self.detected_tree.get_children())
        for l in detected:
            above = f"{l.height_above_previous:g} mm" if l.height_above_previous is not None else "–"
            elev = f"{l.elevation_from_lowest_mm:g} mm" if l.elevation_from_lowest_mm is not None else "–"
            note = l.conflict_note or (f"{len(l.height_evidence)} height record(s)" if l.height_evidence else "")
            self.detected_tree.insert("", "end", iid=l.key, values=(l.key, "; ".join(l.plan_titles[:1]) or l.label, above, elev, l.status, note),
                                      tags=("conflict",) if l.conflict else (("est",) if l.established_id else ()))
        self.built_tree.delete(*self.built_tree.get_children())
        for b in built:
            self.built_tree.insert("", "end", iid=b.id, values=(b.id, b.name, b.source_label or "–", f"{b.elevation_mm:g}", b.elevation_type_words,
                                                                 f"{b.storey_height_mm:g}" if b.storey_height_mm is not None else "–",
                                                                 f"{b.structural_elevation_mm:g}" if b.structural_elevation_mm is not None else "not established",
                                                                 "Engineer established" if b.engineer_established else "Derived, not engineer confirmed"),
                                   tags=("eng",) if b.engineer_established else ("der",))
        parts = [("Detected levels are what the drawing appears to show. ", None),
                 ("An elevation here is relative to the lowest plan level and is not a datum, not a structural level and not confirmed. ", "muted"),
                 ("Only levels the engineer establishes exist in the building model.\n", "muted")]
        if any(l.conflict for l in detected):
            parts.append(("A level marked in red has conflicting or disputed evidence: resolve its question (Questions tab) before establishing it.\n", "bad"))
        if built:
            parts.append(("A structural elevation is never derived from a finished-floor level; it stays ‘not established’ until supplied.\n", "muted"))
        fill_text(self.level_note, parts)

    def _on_detected_selected(self):
        sel = self._selected(self.detected_tree)
        if not sel:
            return
        row = next((l for l in self.ws.detected_levels if l.key == sel[0]), None)
        if row and row.plan_view_ids:
            self.ws.select_object(row.plan_view_ids[0])
        if row:
            evidence = [f"{h:g} mm from {s} ({b})" for h, s, b in row.height_evidence]
            fill_text(self.level_note, [(f"{row.key}: ", "h"), (row.status + "\n", None)] + ([("Height evidence: " + "; ".join(evidence) + "\n", "muted")] if evidence else [])
                      + ([(row.conflict_note + "\n", "bad")] if row.conflict_note else []))

    # ---------------------------------------------------------------- observations

    def _build_observations(self):
        f = self._add("observations", "Observations")
        top = tk.Frame(f, bg=theme.PANEL)
        top.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(top, text="View:", font=theme.SMALL, bg=theme.PANEL).pack(side="left")
        self.obs_filter = tk.StringVar(value="All views")
        self.obs_filter_box = ttk.Combobox(top, textvariable=self.obs_filter, state="readonly", width=34, values=["All views"])
        self.obs_filter_box.pack(side="left", padx=4)
        self.obs_filter_box.bind("<<ComboboxSelected>>", lambda e: self.ws.refresh_observations())
        self.obs_count = tk.Label(top, text="", font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED)
        self.obs_count.pack(side="right")
        cols = [("id", "ID", 78), ("kind", "Kind", 110), ("label", "Label", 90), ("n", "Count", 44, "e", False), ("conf", "Conf.", 44, "e", False), ("basis", "Basis", 62),
                ("hint", "Proposed reading", 110), ("review", "Review", 110)]
        frame, self.obs_tree = make_tree(f, cols, height=13, show_tree=True, tags={"cat": (theme.ACCENT, "#eff6ff"), "approved": (theme.GREEN, "#f0fdf4"), "rejected": (theme.RED, "#fef2f2")})
        frame.pack(fill="both", expand=True, padx=8, pady=2)
        self.obs_tree.bind("<<TreeviewSelect>>", lambda e: self._on_obs_selected())
        self._buttons(f, [("Approve selected", lambda: self.ws.act_review_observations(self.selected_observation_ids(), True), True),
                          ("Reject selected", lambda: self.ws.act_review_observations(self.selected_observation_ids(), False), False),
                          ("Approve proposed reading", lambda: self.ws.act_approve_hint(self.selected_observation_ids()), False)]).pack(fill="x", padx=8, pady=4)
        self.obs_detail = readonly_text(f, height=4, font=theme.SMALL)
        self.obs_detail.pack(fill="x", padx=8, pady=(0, 8))

    def selected_observation_ids(self) -> list:
        ids = []
        for iid in self._selected(self.obs_tree):
            if iid.startswith("cat:"):
                ids += [c for c in self.obs_tree.get_children(iid)]
            else:
                ids.append(iid)
        return ids

    def fill_observations(self, groups, view_titles: dict) -> None:
        current = self.obs_filter.get()
        self.obs_filter_box.config(values=["All views"] + [f"{vid}  {title}" for vid, title in view_titles.items()])
        wanted = None if current == "All views" else current.split("  ")[0]
        self.obs_tree.delete(*self.obs_tree.get_children())
        total = shown = 0
        for key, rows in groups.items():
            total += sum(r.count for r in rows)
            rows = [r for r in rows if wanted is None or r.view_id == wanted]
            if not rows:
                continue
            shown += sum(r.count for r in rows)
            parent = self.obs_tree.insert("", "end", iid=f"cat:{key}", values=(category_title(key), "", "", sum(r.count for r in rows), "", "", "", ""), open=False, tags=("cat",))
            for r in rows:
                tag = "approved" if r.review == "accepted" else "rejected" if r.review == "rejected" else ""
                hint = (r.hint.replace("_", " ") + (" (approved)" if r.hint_approved else " (proposed)")) if r.hint else ""
                self.obs_tree.insert(parent, "end", iid=r.id, values=(r.id, r.kind, r.label or "", r.count, f"{r.confidence:.2f}", r.basis, hint, r.review_words),
                                     tags=(tag,) if tag else ())
        self.obs_count.config(text=f"{shown:,} item(s) shown of {total:,}")

    def _on_obs_selected(self):
        sel = [i for i in self._selected(self.obs_tree) if not i.startswith("cat:")]
        if not sel:
            return
        self.ws.select_object(sel[0])
        evidence = self.ws.session.evidence_for_object(sel[0])
        fill_text(self.obs_detail, [(f"{sel[0]}\n", "h")] + [(f"  • {e}\n", "muted") for e in evidence] or [("  no evidence recorded\n", "muted")])

    # ---------------------------------------------------------------- questions

    def _build_questions(self):
        f = self._add("questions", "Questions")
        tk.Label(f, text="Oracle proposes; the engineer decides. Nothing here is accepted until you accept it.", font=theme.SMALL, bg=theme.PANEL, fg=theme.MUTED,
                 anchor="w").pack(fill="x", padx=8, pady=(8, 0))
        cols = [("id", "ID", 60), ("kind", "About", 100), ("status", "Status", 70), ("q", "Question", 300)]
        frame, self.q_tree = make_tree(f, cols, height=5, selectmode="browse", tags={"open": (theme.AMBER, "#fffbeb"), "done": (theme.GREEN, "#f0fdf4")})
        frame.pack(fill="x", padx=8, pady=4)
        self.q_tree.bind("<<TreeviewSelect>>", lambda e: self._on_question_selected())
        self.q_observed = readonly_text(f, height=4, font=theme.SMALL)
        self.q_observed.pack(fill="x", padx=8)
        tk.Label(f, text="Possible readings (select one):", font=theme.BOLD, bg=theme.PANEL, anchor="w").pack(fill="x", padx=8, pady=(4, 0))
        cols2 = [("id", "ID", 76), ("meaning", "Reading", 120), ("conf", "Conf.", 44, "e", False), ("does", "If accepted, Oracle would", 260), ("state", "State", 70)]
        frame2, self.alt_tree = make_tree(f, cols2, height=4, selectmode="browse", tags={"lead": (theme.ACCENT, "#eff6ff"), "acc": (theme.GREEN, "#f0fdf4"), "rej": (theme.MUTED, "#f9fafb")})
        frame2.pack(fill="x", padx=8)
        self._buttons(f, [("Accept selected reading", lambda: self.ws.act_accept_alternative(self.current_question(), self.current_alternative()), True),
                          ("Reject selected reading", lambda: self.ws.act_reject_alternative(self.current_question(), self.current_alternative()), False),
                          ("Show on drawing", lambda: self.ws.select_question(self.current_question()), False)]).pack(fill="x", padx=8, pady=4)
        self.q_evidence = readonly_text(f, height=5, font=theme.SMALL)
        self.q_evidence.pack(fill="x", padx=8, pady=(0, 8))

    def current_question(self) -> Optional[str]:
        sel = self._selected(self.q_tree)
        return sel[0] if sel else None

    def current_alternative(self) -> Optional[str]:
        sel = self._selected(self.alt_tree)
        return sel[0] if sel else None

    def fill_questions(self, cards: list) -> None:
        keep = self.current_question()
        self.q_tree.delete(*self.q_tree.get_children())
        self._cards = {c.id: c for c in cards}
        for c in cards:
            self.q_tree.insert("", "end", iid=c.id, values=(c.id, c.kind, "OPEN" if c.status == "open" else c.status, c.question),
                               tags=("open",) if c.status == "open" else ("done",))
        if keep and self.q_tree.exists(keep):
            self.q_tree.selection_set(keep)
        elif cards:
            self.q_tree.selection_set(cards[0].id)
        else:
            self._clear_question()

    def _clear_question(self):
        self.alt_tree.delete(*self.alt_tree.get_children())
        fill_text(self.q_observed, [("No interpretation questions.\n", "muted")])
        fill_text(self.q_evidence, [])

    def _on_question_selected(self):
        qid = self.current_question()
        card = getattr(self, "_cards", {}).get(qid)
        if card is None:
            return
        self.ws.select_question(qid)
        parts = [("What Oracle observed\n", "h")] + [(f"  • {o}\n", None) for o in card.observed] or [("  (no summary recorded)\n", "muted")]
        fill_text(self.q_observed, parts)
        self.alt_tree.delete(*self.alt_tree.get_children())
        for a in card.alternatives:
            state = "ACCEPTED" if a.status == "accepted" else a.status
            does = "; ".join(a.consequences) or "records the answer only"
            tag = "acc" if a.status == "accepted" else "rej" if a.status == "rejected" else "lead" if a.is_leading else ""
            label = a.meaning + ("  ★ Oracle's leading reading" if a.is_leading and a.status == "proposed" else "")
            self.alt_tree.insert("", "end", iid=a.id, values=(a.id, label, f"{a.confidence:.2f}", does, state), tags=(tag,) if tag else ())
        ev = [("Evidence\n", "h")] + [(f"  • {e}\n", "muted") for e in card.evidence[:8]] or [("  none recorded\n", "muted")]
        ev.append(("Affected: ", "h"))
        ev.append((", ".join(card.affected) or "the drawing as a whole", None))
        ev.append(("\nRelated issue(s): ", "h"))
        ev.append((", ".join(f"{i.id} ({i.severity}, {i.status})" for i in card.issues) or "none", None))
        if card.status != "open" and card.decision_id:
            ev.append((f"\nSettled by engineer decision {card.decision_id}.", "good"))
        fill_text(self.q_evidence, ev)

    def select_question(self, set_id: str) -> None:
        if self.q_tree.exists(set_id):
            self.q_tree.selection_set(set_id)
            self.q_tree.see(set_id)

    # ---------------------------------------------------------------- issues

    def _build_issues(self):
        f = self._add("issues", "Issues")
        cols = [("id", "ID", 76), ("sev", "Severity", 66), ("status", "Status", 70), ("msg", "Issue", 330)]
        frame, self.issue_tree = make_tree(f, cols, height=10, selectmode="browse",
                                           tags={"blocking": (theme.RED, "#fef2f2"), "error": (theme.RED, "#fef2f2"), "warning": (theme.AMBER, "#fffbeb"), "closed": (theme.MUTED, "#f9fafb")})
        frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.issue_tree.bind("<<TreeviewSelect>>", lambda e: self._on_issue_selected())
        self.issue_tree.bind("<Double-1>", lambda e: self.ws.select_issue(self.current_issue()))
        self._buttons(f, [("Show on drawing", lambda: self.ws.select_issue(self.current_issue()), False),
                          ("Go to its question", lambda: self.ws.go_to_issue_question(self.current_issue()), False),
                          ("Accept issue…", lambda: self.ws.act_accept_issue(self.current_issue()), True)]).pack(fill="x", padx=8, pady=2)
        self.issue_detail = readonly_text(f, height=7, font=theme.SMALL)
        self.issue_detail.pack(fill="x", padx=8, pady=(4, 8))

    def current_issue(self) -> Optional[str]:
        sel = self._selected(self.issue_tree)
        return sel[0] if sel else None

    def fill_issues(self, rows: list) -> None:
        keep = self.current_issue()
        self.issue_tree.delete(*self.issue_tree.get_children())
        self._issues = {r.id: r for r in rows}
        for r in rows:
            tag = "closed" if r.status != "open" else r.severity
            self.issue_tree.insert("", "end", iid=r.id, values=(r.id, r.severity.upper(), r.status, r.message), tags=(tag,))
        if keep and self.issue_tree.exists(keep):
            self.issue_tree.selection_set(keep)

    def _on_issue_selected(self):
        r = getattr(self, "_issues", {}).get(self.current_issue())
        if r is None:
            return
        parts = [(f"{r.id}  {r.severity.upper()}  ({r.category})\n", "h"), (r.message + "\n", None), (f"Concerns: {r.target}" + (f"; also {', '.join(r.related)}" if r.related else "") + "\n", "muted")]
        if r.set_id:
            parts.append((f"This issue is the visible face of question {r.set_id}: resolving that question resolves this issue.\n", "muted"))
        if r.status != "open":
            parts.append((f"{r.status.capitalize()} under decision {r.decision_id or DASH}: {r.resolution or ''}\n", "good"))
        else:
            parts.append(("Engineer action: resolve its question, or accept the issue with a reason (it then stays on record as accepted, not fixed).\n", "warn"))
        fill_text(self.issue_detail, parts)
        self.ws.select_issue(r.id, quiet=True)

    # ---------------------------------------------------------------- approved

    def _build_approved(self):
        f = self._add("approved", "Approved")
        self.approved_text = readonly_text(f, height=30)
        self.approved_text.pack(fill="both", expand=True, padx=8, pady=8)

    def fill_approved(self, a) -> None:
        d = a.source
        parts = [("APPROVED ARCHITECTURE", "h"), (f"   (project.approved_architecture, {d.id}: {d.file}" + (f", revision {d.revision}" if d.revision else "") + ")\n", "muted"),
                 (f"Unit: {a.unit}. ", None), ("The drawing's unit has been confirmed.\n" if a.unit_confirmed else "The drawing's unit is NOT confirmed; coordinates rest on an assumption.\n", "good" if a.unit_confirmed else "warn")]
        parts.append(("✓ Ready for structural work: the approved model has no blockers.\n" if a.ready else "✕ Not ready: the approved model has blockers (below).\n", "good" if a.ready else "bad"))
        parts.append((f"\nApproved views ({len(a.views)})\n", "h"))
        parts += [(f"  {v[0]}  {v[1]}  [{v[2]}]  level {v[3] or DASH}  frame: {v[4]}  conf. {v[5]:.2f}\n", "good") for v in a.views] or [("  none approved yet\n", "muted")]
        kinds: dict = {}
        for o in a.observations:
            kinds[o[2]] = kinds.get(o[2], 0) + o[4]
        parts.append((f"\nApproved observations ({sum(kinds.values()):,} item(s) in {len(a.observations)} record(s))\n", "h"))
        parts += [(f"  {k}: {n:,}\n", "good") for k, n in sorted(kinds.items())] or [("  none approved yet\n", "muted")]
        parts.append((f"\nReadings of observations ({len(a.hints)})\n", "h"))
        approved_hints = [h for h in a.hints if h[2]]
        parts.append((f"  {len(approved_hints)} approved by the engineer, {len(a.hints) - len(approved_hints)} still only proposed\n", "good" if approved_hints else "muted"))
        parts.append((f"\nBuilding levels ({len(a.levels)})\n", "h"))
        for l in a.levels:
            structural = f"{l[7]:g}" if l[7] is not None else "not established"
            parts.append((f"  {l[0]}  {l[1]}  drawing label {l[2] or DASH}  elevation {l[4]:g} mm ({l[5].replace('_', ' ')})  storey height {('%g' % l[6]) if l[6] is not None else DASH}  structural: {structural}\n", "good"))
        if not a.levels:
            parts.append(("  none established\n", "muted"))
        parts.append((f"\nApproved relationships ({len(a.relationships)})\n", "h"))
        parts += [(f"  {r}\n", "good") for r in a.relationships] or [("  none yet (evidence links are made when structural objects are derived from the drawing)\n", "muted")]
        parts.append(("\nPROPOSED / UNRESOLVED\n", "h"))
        parts.append((f"  {len(a.proposed_views)} view(s) not approved; {a.proposed_observation_count:,} observation(s) not approved; {a.open_questions} open question(s)\n", "warn"))
        parts += [(f"  {v[0]}  {v[1]}  [{v[2]}]  {v[3]}\n", "warn") for v in a.proposed_views[:12]]
        if len(a.proposed_views) > 12:
            parts.append((f"  … and {len(a.proposed_views) - 12} more\n", "muted"))
        if a.blockers:
            parts.append(("\nWhy the approved model is not ready\n", "h"))
            parts += [(f"  • {b}\n", "bad") for b in a.blockers]
        parts.append(("\nNot yet available from the approved architecture\n", "h"))
        parts += [(f"  • {n}\n", "muted") for n in a.not_yet_available]
        fill_text(self.approved_text, parts)

    # ---------------------------------------------------------------- decisions

    def _build_decisions(self):
        f = self._add("decisions", "Decisions")
        tk.Label(f, text="Engineer decisions recorded in this project (they are saved with it):", font=theme.BOLD, bg=theme.PANEL, anchor="w").pack(fill="x", padx=8, pady=(8, 0))
        cols = [("id", "ID", 84), ("by", "By", 90), ("src", "Source", 70), ("status", "Status", 74), ("target", "About", 130), ("what", "Decision", 300), ("reason", "Reason", 160)]
        frame, self.dec_tree = make_tree(f, cols, height=14, selectmode="browse")
        frame.pack(fill="both", expand=True, padx=8, pady=8)

    def fill_decisions(self, rows: list) -> None:
        self.dec_tree.delete(*self.dec_tree.get_children())
        for did, author, source, status, target, instruction, change, reason in rows:
            self.dec_tree.insert("", "end", values=(did, author, source, status, target, instruction + (f"  [{change}]" if change else ""), reason))
