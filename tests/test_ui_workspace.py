"""Tests for the Architectural Drawing workspace and its place in the wizard (tests/test_ui_workspace.py)

Protects:
    That the calm Tkinter workspace really drives the pipeline through the application layer: a welcoming first screen, a very simple import
    (technical file detail only behind "Details"), honest stage-by-stage processing that never freezes the window, and a review screen that shows
    the drawing, a short queue of what needs the engineer and ONE action card whose buttons fit the item. It protects that Oracle's suggestions are
    never worded or recorded as decisions, that rejecting a view asks for a reason and keeps the evidence, that Ask Engineer records the engineer's
    words, that no internal id, hash or confidence number is on the default screen while the same facts are one click away in Evidence & Details, that
    the layout stays usable at small and large window sizes, that several sources force an explicit choice, and that the existing wizard still starts,
    still offers its steps and gets back to them after the workspace closes.

Test type:
    Integration (tier "integration"): a real (withdrawn) Tk root, synthetic DXFs in a temporary folder, a scripted Prompts object instead of modal
    dialogs. Skipped when no display is available.

Dependencies:
    tkinter, oracle.ui, oracle_wizard (imported lazily), tests.drawing_factory.
"""

import gc
import re
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

from tests.drawing_factory import three_storey_sheet
from tests.tiers import tier

ROOT = None                                                             # one hidden Tk root for the whole module (each Tk costs time)
INTERNAL = re.compile(r"\b(VIEW-\d+|IS-\d+|ARC-\d+|SRC-\d+|PV-\d+|ENG-\d+|CLR-\d+|OBS-\d+|FRM-\d+)\b|[0-9a-f]{32,}")


def setUpModule():
    global ROOT
    try:
        ROOT = tk.Tk()
    except tk.TclError as exc:                                          # no display on this machine
        raise unittest.SkipTest(f"no display: {exc}")
    ROOT.withdraw()


def tearDownModule():
    global ROOT
    if ROOT is not None:
        ROOT.update()
        ROOT.destroy()
        gc.collect()
        ROOT = None


class ScriptedPrompts:
    """Stands in for the modal dialogs: records what the engineer would have been shown and answers as told."""

    def __init__(self):
        self.shown = []
        self.drawing = self.project = self.save_to = None
        self.reason, self.text = "checked", "Renamed"
        self.reject = ("duplicate", "The same sheet appears twice.")
        self.engineer_input = ("These columns are existing and retained.", "from the site visit")
        self.number, self.level, self.pair = "3200", "GROUND", ("0", "0")
        self.view_type = ("section", "It is the section through the stair core.")
        self.confirm_answer = True
        self.establish = None

    def error(self, title, message):
        self.shown.append(("error", title, message))

    def info(self, title, message):
        self.shown.append(("info", title, message))

    def confirm(self, title, message):
        self.shown.append(("confirm", title, message))
        return self.confirm_answer

    def ask_reason(self, title, prompt, required=False, ok_text="Record decision"):
        self.shown.append(("reason", title, prompt))
        return self.reason

    def ask_text(self, title, prompt, initial=""):
        return self.text

    def ask_pair(self, title, prompt, labels, initial=("0", "0")):
        return self.pair

    def ask_reject_view(self, names, consequences=None):
        self.shown.append(("reject", tuple(names), consequences))
        return self.reject

    def ask_engineer_input(self, subject, suggestions=None):
        self.shown.append(("ask_engineer", subject, suggestions))
        return self.engineer_input

    def ask_number(self, title, prompt, unit=""):
        self.shown.append(("number", title, prompt))
        return self.number

    def ask_view_type(self, view_name, current_label, choices, current_key):
        self.shown.append(("view_type", view_name, current_label, tuple(k for k, _l in choices), current_key))
        return self.view_type

    def ask_level(self, view_name, options):
        self.shown.append(("level", view_name))
        return self.level

    def ask_establish_levels(self, keys, suggestion, note):
        self.shown.append(("establish", keys, suggestion))
        return self.establish or {"elevations": {k: i * 3000.0 for i, (k, _l) in enumerate(keys)}, "elevation_type": "finished_floor", "reason": "r"}

    def ask_open_drawing(self, initial_dir=None):
        return self.drawing

    def ask_open_project(self, initial_dir=None):
        return self.project

    def ask_save_project(self, initial_dir=None, initial_name=""):
        return self.save_to


def all_text(widget) -> list:
    """Every piece of text the engineer can read in a widget tree (labels, buttons, list rows), excluding the drawing itself."""
    out = []
    for w in [widget, *_descendants(widget)]:
        if isinstance(w, ttk.Treeview):
            out += [w.item(i, "text") for i in _tree_items(w)]
        elif isinstance(w, (tk.Label, tk.Button, tk.Checkbutton, tk.Menubutton)):
            out.append(str(w.cget("text")))
    return [t for t in out if t]


def _descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from _descendants(child)


def _tree_items(tree, parent=""):
    for item in tree.get_children(parent):
        yield item
        yield from _tree_items(tree, item)


def buttons(widget) -> list:
    return [str(w.cget("text")) for w in _descendants(widget) if isinstance(w, tk.Button)]


class Base(unittest.TestCase):
    inch = True                                                         # the file says inches, the geometry is millimetres: one real question

    def setUp(self):
        from oracle.ui.architectural_workspace import ArchitecturalWorkspace
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.root = ROOT
        self.log = []
        self.exited = []
        self.ws = ArchitecturalWorkspace(self.root, on_exit=lambda: self.exited.append(1), logger=lambda kind, message: self.log.append((kind, message)))
        self.ws.pack(fill="both", expand=True)
        self.addCleanup(self.ws.destroy)
        self.addCleanup(gc.collect)                                     # Tk variables must be freed on this thread, not the worker's
        self.root.geometry("1200x760")
        self.root.update()
        self.prompts = ScriptedPrompts()
        self.ws.prompts = self.prompts

    def drawing(self, name="plan.dxf") -> Path:
        path = self.dir / name
        three_storey_sheet(insunits=1 if self.inch else None).save(path)
        return path

    def interpret(self, path=None):
        path = path or self.drawing()
        self.ws.show_import()
        self.ws.set_file(path)
        self.ws.start_interpretation()
        self.assertEqual(self.ws.screen, "processing")
        self.assertTrue(self.ws.wait_for_worker(120))
        return path


@tier("integration")
class WelcomeAndImport(Base):
    def test_the_first_screen_welcomes_and_offers_two_clear_choices(self):
        self.assertEqual(self.ws.screen, "welcome")
        text = " ".join(all_text(self.ws.welcome_frame))
        for phrase in ("Architectural Drawing Review", "Turn a DWG/DXF into a reviewed architectural model.", "identify drawing views", "ask you when it is uncertain",
                       "You remain in control of every engineering decision.", "Open Architectural Drawing", "Open Existing Project"):
            self.assertIn(phrase, text)
        self.assertFalse(self.ws.session.has_project)
        self.assertEqual(self.ws.continue_btn.winfo_manager(), "")                     # nothing to continue yet

    def test_open_takes_you_to_a_very_simple_import(self):
        self.ws.open_btn.invoke()
        self.assertEqual(self.ws.screen, "import")
        self.assertEqual(str(self.ws.interpret_btn["state"]), "disabled")
        self.assertIn("Choose a drawing to review.", " ".join(all_text(self.ws.import_frame)))

    def test_a_chosen_drawing_shows_name_type_and_status_while_paths_and_sizes_wait_behind_details(self):
        self.ws.show_import()
        path = self.drawing()
        self.ws.set_file(path)
        self.assertEqual((self.ws.info_vars["name"].get(), self.ws.info_vars["type"].get(), self.ws.info_vars["status"].get()), ("plan.dxf", "DXF", "Ready to interpret"))
        self.assertEqual(self.ws.import_details.winfo_manager(), "")                     # hidden by default
        self.ws.details_toggle.invoke()
        self.assertTrue(self.ws.import_details.winfo_manager())
        self.assertEqual(self.ws.info_vars["path"].get(), str(path))
        self.assertIn("KB", self.ws.info_vars["size"].get())
        self.assertEqual(str(self.ws.interpret_btn["state"]), "normal")
        self.assertFalse(self.ws.session.has_project)                                    # still not interpreted

    def test_an_unusable_file_is_refused_with_the_reason_and_the_details_open(self):
        self.ws.show_import()
        bad = self.dir / "notes.txt"
        bad.write_text("hello")
        self.ws.set_file(bad)
        self.assertEqual(str(self.ws.interpret_btn["state"]), "disabled")
        self.assertIn(".dwg and .dxf", self.ws.info_vars["status"].get())
        self.assertTrue(self.ws.import_details.winfo_manager())

    def test_the_browse_button_uses_the_chooser(self):
        self.ws.show_import()
        self.prompts.drawing = str(self.drawing())
        self.ws.choose_file()
        self.assertEqual(self.ws.info_vars["name"].get(), "plan.dxf")


@tier("integration")
class Processing(Base):
    def test_the_window_stays_alive_and_every_stage_ends_done(self):
        seen = []
        original = self.ws._on_stage
        self.ws._on_stage = lambda e: (seen.append((e.key, e.state)), original(e))
        self.ws.show_import()
        self.ws.set_file(self.drawing())
        self.ws.start_interpretation()
        self.assertIsNotNone(self.ws._worker)
        self.assertTrue(self.ws.wait_for_worker(120))
        self.assertEqual(self.ws.screen, "review")
        started = [k for k, s in seen if s == "start"]
        self.assertEqual([k for k, s in seen if s == "done"], started)
        self.assertEqual((started[0], started[-1]), ("validate", "review"))
        for key in started:
            self.assertEqual(self.ws._stage_rows[key][0]["text"], "✓")

    def test_a_failure_shows_words_offers_both_ways_out_and_logs_the_technical_trace(self):
        bad = self.dir / "damaged.dxf"
        bad.write_bytes(b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1032\n  0\nGARBAGE\nnot a drawing\n")
        self.ws.show_import()
        self.ws.set_file(bad)
        self.assertEqual(str(self.ws.interpret_btn["state"]), "normal")
        self.ws.start_interpretation()
        self.assertTrue(self.ws.wait_for_worker(60))
        self.assertEqual(self.ws.screen, "processing")
        self.assertEqual(self.ws.error_box.winfo_manager(), "pack")
        shown = " ".join(w["text"] for w in (self.ws.error_title, self.ws.error_file, self.ws.error_message, self.ws.error_note))
        self.assertNotIn("Traceback", shown)
        self.assertIn("damaged.dxf", shown)
        self.assertTrue(any("Traceback" in message for _kind, message in self.log))
        self.assertFalse(self.ws.session.has_project)
        self.ws.show_import()                                                            # "Choose another drawing"
        self.assertEqual(self.ws.screen, "import")

    def test_retry_runs_the_same_file_again(self):
        path = self.dir / "later.dxf"
        self.ws.show_import()
        self.ws.set_file(self.drawing("stand-in.dxf"))
        self.ws.pending_file = str(path)                                                 # the file will only exist on the retry
        self.ws.file_info = self.ws.session.inspect_file(self.dir / "stand-in.dxf")
        self.ws.start_interpretation()
        self.assertTrue(self.ws.wait_for_worker(60))
        self.assertEqual(self.ws.error_box.winfo_manager(), "pack")
        three_storey_sheet().save(path)
        self.ws.retry()
        self.assertTrue(self.ws.wait_for_worker(120))
        self.assertEqual(self.ws.screen, "review")


@tier("integration")
class TheReviewScreen(Base):
    def setUp(self):
        super().setUp()
        self.interpret()
        self.screen = self.ws.review

    def rows(self):
        return [self.screen.tree.item(i, "text") for i in _tree_items(self.screen.tree)]

    def test_it_says_where_things_stand_and_selects_the_next_thing_that_needs_the_engineer(self):
        text = " ".join(all_text(self.screen.header))
        self.assertIn("plan.dxf", text)
        self.assertIn("Drawing interpreted", text)
        self.assertIn("3 views found", text)
        self.assertIn("3 items need your review", text)
        self.assertIn("Needs your attention (3)", self.rows())
        self.assertIn("⚠  Unit of the drawing", self.rows())
        self.assertEqual(self.screen.selected_key(), "set:IS-001")                       # the first thing to do, already selected
        self.assertEqual(self.screen.current_key, "set:IS-001")

    def test_the_card_has_one_primary_action_and_only_actions_that_fit_the_question(self):
        self.assertEqual(buttons(self.screen.footer)[0], "Accept suggestion")
        self.assertEqual(sorted(buttons(self.screen.footer)[1:]), ["Ask Engineer", "Choose another…", "Review evidence"])
        primaries = [w for w in _descendants(self.screen.footer) if isinstance(w, tk.Button) and w["bg"] == "#2f5d9e"]
        self.assertEqual(len(primaries), 1)

    def test_oracles_suggestion_is_labelled_a_suggestion_and_never_a_decision_until_the_engineer_acts(self):
        card_text = " ".join(all_text(self.screen.card_scroll.body))
        self.assertIn("ORACLE SUGGESTS", card_text.upper())
        self.assertNotIn("ENGINEER DECISION", card_text.upper())
        self.assertIn("Confidence:", card_text)
        self.assertNotRegex(card_text, r"\b0\.\d\d\b")
        self.ws.run_action("accept_suggestion", self.ws.session.card("set:IS-001"))
        self.assertEqual(self.prompts.shown[-1][0], "reason")
        self.assertIn("Oracle suggests", self.prompts.shown[-1][2])
        self.ws.on_select("set:IS-001")
        answered = " ".join(all_text(self.screen.card_scroll.body)).upper()
        self.assertIn("ENGINEER DECISION", answered)
        self.assertNotIn("ORACLE SUGGESTS", answered)

    def test_accepting_a_suggestion_moves_it_to_completed_and_offers_the_next_item(self):
        self.ws.run_action("accept_suggestion", self.ws.session.card("set:IS-001"))
        self.assertIn("Needs your attention (2)", self.rows())
        self.assertIn("Completed (1)", self.rows())
        self.assertEqual(self.screen.current_key, "views:confirm")                       # the next thing, chosen for the engineer
        self.assertTrue(self.ws.session.dirty)
        self.assertEqual(self.screen.save_btn["text"], "Save •")

    def test_nothing_internal_is_on_the_default_screen(self):
        self.ws.session.ask_engineer("VIEW-01", "This is the ground floor.")
        self.ws.refresh()
        for mode in ("queue", "views"):
            self.screen.set_mode(mode)
            for key in ("set:IS-001", "views:confirm", "levels:establish", "view:VIEW-01"):
                if (mode == "views") != key.startswith("view:"):
                    continue
                self.ws.on_select(key)
                shown = all_text(self.screen)
                self.assertEqual([t for t in shown if INTERNAL.search(t)], [], (mode, key))
                self.assertFalse([t for t in shown if re.search(r"\b0\.\d\d\b", t)], (mode, key))
        self.assertFalse([t for t in all_text(self.ws.welcome_frame) + all_text(self.ws.import_frame) if INTERNAL.search(t)])

    def test_evidence_and_details_still_exposes_the_underlying_data_when_asked(self):
        self.assertIsNone(self.ws.panels)                                                # not open by default
        self.ws.run_action("review_evidence", self.ws.session.card("set:IS-001"))
        self.assertIsNotNone(self.ws.panels)
        self.assertEqual(self.ws.panels.tab_name(), "questions")
        self.assertIn("IS-001", self.ws.panels.q_tree.get_children())
        summary = self.ws.panels.summary_text.get("1.0", "end")
        self.assertIn("SRC-1", summary)
        self.assertIn("SHA-256", summary)
        self.ws.panels.show_tab("views")
        self.ws.panels.views_tree.selection_set("VIEW-01")
        self.ws.update()
        detail = self.ws.panels.view_detail.get("1.0", "end")
        self.assertIn("VIEW-01", detail)
        self.assertIn("Evidence Oracle recorded", detail)
        self.ws.session.ask_engineer("VIEW-01", "This is the ground floor.")
        self.ws.refresh()
        self.assertEqual([self.ws.panels.input_tree.item(i, "values")[4] for i in self.ws.panels.input_tree.get_children()], ["This is the ground floor."])
        self.assertTrue(self.ws.panels.dec_tree.get_children())
        self.ws.close_details()
        self.assertIsNone(self.ws.panels)

    def test_observations_are_still_grouped_by_the_real_vocabulary_in_the_details(self):
        self.ws.open_details("observations")
        tree = self.ws.panels.obs_tree
        groups = {i[4:] for i in tree.get_children()}
        self.assertTrue({"walls", "openings", "column_candidates", "grid"} <= groups)
        observation = tree.get_children("cat:column_candidates")[0]
        tree.selection_set(observation)
        self.ws.update()
        self.assertEqual(self.ws.selected_ref, observation)                              # selecting it highlights it on the drawing
        self.assertTrue(self.ws.canvas.selection.rects or self.ws.canvas.selection.entity_ids or self.ws.canvas.selection.polylines)

    def test_every_technical_tab_can_still_be_opened(self):
        self.ws.open_details()
        for tab in ("summary", "views", "levels", "observations", "questions", "issues", "approved", "input", "decisions"):
            self.ws.panels.show_tab(tab)
            self.ws.update()
            self.assertEqual(self.ws.panels.tab_name(), tab)

    def test_the_drawing_is_shown_and_a_selection_is_highlighted_on_it(self):
        self.assertGreater(len(self.ws.canvas.preview), 0)
        self.assertGreaterEqual(len(self.ws.canvas.base_overlay.rects), 3)
        self.ws.on_select("views:confirm")
        self.assertGreaterEqual(len(self.ws.canvas.selection.rects), 3)

    def test_the_canvas_can_fit_zoom_and_report_a_click(self):
        self.root.geometry("1200x760+-2600+0")                                           # shown, but off every screen: a hidden canvas has no size to fit
        self.root.deiconify()
        self.addCleanup(self.root.withdraw)
        for _ in range(6):
            self.root.update()
        self.ws.canvas.fit()
        self.ws.update()
        before = self.ws.canvas.scale
        self.ws.canvas.zoom(2.0)
        self.assertAlmostEqual(self.ws.canvas.scale, before * 2.0)
        self.ws.canvas.fit()
        self.assertAlmostEqual(self.ws.canvas.scale, before)
        rect = next(r for r in self.ws.canvas.base_overlay.rects if r[3] == "VIEW-01")
        x, y = (rect[0][0] + rect[0][2]) / 2, (rect[0][1] + rect[0][3]) / 2
        self.assertEqual(self.ws.canvas.pick_view(x, y), "VIEW-01")
        self.ws.on_canvas_pick("VIEW-01")                                                # a click selects the view in the Views list
        self.assertEqual((self.screen.mode, self.screen.current_key), ("views", "view:VIEW-01"))

    def test_select_view_mode_can_be_turned_off(self):
        self.screen.select_mode.set(False)
        self.screen._toggle_select()
        self.assertFalse(self.ws.canvas.pick_enabled)


@tier("integration")
class RejectingViewsInTheInterface(Base):
    def setUp(self):
        super().setUp()
        self.interpret()
        self.screen = self.ws.review
        self.screen.set_mode("views")

    def rows(self):
        return [self.screen.tree.item(i, "text") for i in _tree_items(self.screen.tree)]

    def test_the_views_are_a_compact_list_with_three_states_and_no_internal_ids(self):
        rows = self.rows()
        self.assertIn("Floor plans (3)  —  3 to review", rows)
        self.assertIn("⚠  Ground Floor Plan  — needs review", rows)
        self.ws.session.review_views(["VIEW-01"], accept=True)
        self.ws.refresh()
        self.assertIn("✓  Ground Floor Plan", self.rows())

    def test_reject_view_asks_why_records_the_decision_and_moves_it_under_rejected(self):
        self.ws.on_select("view:VIEW-03")
        self.assertIn("View Actions  \u25be", buttons(self.screen.footer))                 # reject lives in the View Actions menu
        self.assertIn("reject_view", self.ws.session.card("view:VIEW-03").menu)
        self.ws.run_action("reject_view", self.ws.session.card("view:VIEW-03"))
        self.assertEqual(self.prompts.shown[-1][:2], ("reject", ("Second Floor Plan",)))
        d = self.ws.session.decision_rows()[-1]
        self.assertEqual((d[2], d[3]), ("engineer", "accepted"))
        self.assertEqual(self.ws.session.project.decisions[-1].reason_code, "duplicate")
        rows = self.rows()
        self.assertIn("Rejected (1)", rows)
        self.assertIn("✕  Second Floor Plan  —  Duplicate", rows)
        self.assertIn("Floor plans (2)  —  2 to review", rows)                       # the rejected view is no longer active
        self.ws.on_select("view:VIEW-03")
        card = " ".join(all_text(self.screen.card_scroll.body))
        self.assertIn("Rejected — Duplicate", card)
        self.assertIn("The same sheet appears twice.", card)
        self.assertIn(self.ws.session.engineer, card)
        self.assertEqual(buttons(self.screen.footer)[0], "Reconsider")

    def test_cancelling_the_reject_dialog_records_nothing(self):
        self.prompts.reject = None
        self.ws.run_action("reject_view", self.ws.session.card("view:VIEW-03"))
        self.assertEqual(self.ws.session.decision_rows(), [])

    def test_reconsider_brings_it_back_to_needs_review(self):
        self.ws.run_action("reject_view", self.ws.session.card("view:VIEW-03"))
        self.ws.on_select("view:VIEW-03")
        self.ws.run_action("reconsider", self.ws.session.card("view:VIEW-03"))
        self.assertNotIn("Rejected (1)", self.rows())
        self.assertEqual(len(self.ws.session.decision_rows()), 2)                        # both decisions are on record

    def test_a_rejected_view_survives_save_and_reopen_in_a_fresh_workspace(self):
        from oracle.ui.architectural_workspace import ArchitecturalWorkspace
        self.ws.run_action("reject_view", self.ws.session.card("view:VIEW-03"))
        self.prompts.save_to = str(self.dir / "p.oracle.json")
        saved = self.ws.save_project()
        other = ArchitecturalWorkspace(self.root)
        other.prompts = ScriptedPrompts()
        other.pack(fill="both", expand=True)
        self.addCleanup(other.destroy)
        self.assertTrue(other.open_project(saved))
        other.review.set_mode("views")
        rows = [other.review.tree.item(i, "text") for i in _tree_items(other.review.tree)]
        self.assertIn("Rejected (1)", rows)
        self.assertEqual(other.session.project.to_dict(), self.ws.session.project.to_dict())

    def test_confirming_all_views_asks_first_and_is_one_recorded_decision(self):
        self.screen.set_mode("queue")
        self.ws.run_action("confirm_all", self.ws.session.card("views:confirm"))
        self.assertEqual(self.prompts.shown[-1][0], "confirm")
        self.assertIn("Ground Floor Plan", self.prompts.shown[-1][2])
        self.assertEqual({e.state for e in self.ws.session.view_entries()}, {"accepted"})
        self.assertEqual(len(self.ws.session.decision_rows()), 1)
        self.prompts.confirm_answer = False
        self.ws.session.reconsider_views(["VIEW-01"])
        self.ws.refresh()
        before = len(self.ws.session.decision_rows())
        self.ws.act_confirm_all(["VIEW-01"])
        self.assertEqual(len(self.ws.session.decision_rows()), before)                   # declined: nothing recorded


@tier("integration")
class AskEngineerInTheInterface(Base):
    def setUp(self):
        super().setUp()
        self.interpret()
        self.screen = self.ws.review

    def test_ask_engineer_is_offered_next_to_oracles_suggestions_and_takes_free_text(self):
        self.assertIn("Ask Engineer", buttons(self.screen.footer))
        self.ws.run_action("ask_engineer", self.ws.session.card("set:IS-001"))
        kind, subject, suggestions = self.prompts.shown[-1]
        self.assertEqual(kind, "ask_engineer")
        self.assertIn("What unit is the drawing in?", subject)
        self.assertTrue(suggestions)                                                     # Oracle's suggestions are shown for reference, not as the only choices
        clarification = self.ws.session.project.clarifications[0]
        self.assertEqual((clarification.statement, clarification.notes), ("These columns are existing and retained.", "from the site visit"))
        rows = [self.screen.tree.item(i, "text") for i in _tree_items(self.screen.tree)]
        self.assertIn("Completed (1)", rows)

    def test_the_engineers_input_is_shown_as_engineer_input_not_as_oracles_words(self):
        self.screen.set_mode("views")
        self.ws.on_select("view:VIEW-01")
        self.ws.run_action("ask_engineer", self.ws.session.card("view:VIEW-01"))
        shown = " ".join(all_text(self.screen.card_scroll.body))
        self.assertIn("ENGINEER INPUT", shown.upper())
        self.assertIn("These columns are existing and retained.", shown)
        self.assertIn("Kept as guidance for the next stage", shown)
        self.assertNotIn("ORACLE SUGGESTS", shown.upper())
        self.assertEqual(self.ws.session.project.clarifications[0].author, self.ws.session.engineer)

    def test_cancelling_and_empty_input_record_nothing(self):
        self.prompts.engineer_input = None
        self.ws.run_action("ask_engineer", self.ws.session.card("set:IS-001"))
        self.prompts.engineer_input = ("   ", "")
        self.ws.run_action("ask_engineer", self.ws.session.card("set:IS-001"))
        self.assertEqual(self.ws.session.project.clarifications, [])
        self.assertEqual(self.prompts.shown[-1][0], "error")

    def test_a_height_value_is_refused_in_words_where_the_question_is_not_about_a_height(self):
        self.ws.run_action("enter_value", self.ws.session.card("set:IS-001"))              # this question is about units
        self.assertEqual(self.prompts.shown[-1][0], "error")
        self.assertNotIn("Traceback", self.prompts.shown[-1][2])

    def test_a_refused_action_is_shown_in_words_and_changes_nothing(self):
        self.ws.act_accept_issue("NO-SUCH-ISSUE")
        self.assertEqual(self.prompts.shown[-1][0], "error")
        self.assertEqual(self.ws.session.decision_rows(), [])


@tier("integration")
class OtherActions(Base):
    def setUp(self):
        super().setUp()
        self.interpret()
        self.screen = self.ws.review

    def test_setting_the_building_levels_from_the_card(self):
        self.ws.run_action("set_levels", self.ws.session.card("levels:establish"))
        self.assertEqual(self.prompts.shown[-1][0], "establish")
        _detected, built = self.ws.session.levels()
        self.assertEqual(len(built), 3)
        self.assertTrue(all(b.structural_elevation_mm is None for b in built))            # a floor finish is never called structural
        self.assertIn("levels:done", [i.key for i in self.ws.session.queue()[1]])

    def test_the_whole_review_can_be_completed_and_says_so(self):
        self.ws.run_action("accept_suggestion", self.ws.session.card("set:IS-001"))
        self.ws.run_action("confirm_all", self.ws.session.card("views:confirm"))
        self.ws.run_action("set_levels", self.ws.session.card("levels:establish"))
        rows = [self.screen.tree.item(i, "text") for i in _tree_items(self.screen.tree)]
        self.assertIn("Nothing needs your attention", rows)
        self.assertEqual(self.screen.current_key, "complete")
        self.assertIn("Architectural review complete", " ".join(all_text(self.screen)))
        self.assertIn("Save project", buttons(self.screen.footer))

    def test_save_then_reopen_in_a_fresh_workspace_shows_the_same_review_without_reinterpreting(self):
        from oracle.ui.architectural_workspace import ArchitecturalWorkspace
        self.ws.run_action("accept_suggestion", self.ws.session.card("set:IS-001"))
        self.prompts.save_to = str(self.dir / "site.oracle.json")
        saved = self.ws.save_project()
        self.assertEqual(saved, Path(self.prompts.save_to))
        self.assertFalse(self.ws.session.dirty)
        self.assertEqual(self.screen.save_btn["text"], "Save")
        other = ArchitecturalWorkspace(self.root)
        other.prompts = ScriptedPrompts()
        other.pack(fill="both", expand=True)
        self.addCleanup(other.destroy)
        self.assertTrue(other.open_project(saved))
        self.assertEqual(other.screen, "review")
        self.assertEqual(other.session.project.to_dict(), self.ws.session.project.to_dict())
        self.assertGreater(len(other.canvas.preview), 0)
        self.assertIn("Opened", other.review.flash_label["text"])

    def test_leaving_with_unsaved_decisions_asks_first(self):
        self.ws.run_action("accept_suggestion", self.ws.session.card("set:IS-001"))
        self.prompts.confirm_answer = False
        self.ws.exit()
        self.assertEqual(self.exited, [])
        self.prompts.confirm_answer = True
        self.ws.exit()
        self.assertEqual(self.exited, [1])


@tier("integration")
class MultipleSources(Base):
    def test_the_engineer_must_choose_between_two_sources_and_sees_readable_names(self):
        from oracle.ui.architectural_workspace import ArchitecturalWorkspace
        self.interpret(self.drawing("a.dxf"))
        self.ws.session.interpret(self.drawing("b.dxf"), add_source=True, revision="B")
        saved = self.ws.session.save_project(self.dir / "two.oracle.json")
        other = ArchitecturalWorkspace(self.root)
        other.prompts = ScriptedPrompts()
        other.pack(fill="both", expand=True)
        self.addCleanup(other.destroy)
        self.assertTrue(other.open_project(saved))
        other.update()
        self.assertEqual(other.review.choice.winfo_manager(), "pack")                     # asked, not guessed
        self.assertEqual(other.review.vpaned.winfo_manager(), "")
        offered = buttons(other.review.choice)
        self.assertEqual(len(offered), 2)
        self.assertTrue(all("SRC-" not in b for b in offered))
        self.assertTrue(any("Revision B" in b for b in offered))
        other.on_source_chosen(next(label for _sid, label in other.session.source_choices() if "Revision B" in label))
        other.update()
        self.assertEqual(other.review.choice.winfo_manager(), "")
        self.assertEqual(other.review.vpaned.winfo_manager(), "pack")
        self.assertIn("Revision B", other.review.source_var.get())                        # the picker stays available in the header


@tier("integration")
class LayoutAtDifferentWindowSizes(Base):
    def setUp(self):
        super().setUp()
        self.interpret()

    def measure(self, size):
        self.root.geometry(f"{size}+-2600+0")
        self.root.deiconify()
        self.addCleanup(self.root.withdraw)
        for _ in range(8):
            self.root.update()
        return self.ws.review

    def test_the_drawing_and_the_action_buttons_stay_usable_from_small_to_large_windows(self):
        for size in ("1000x600", "1280x720", "1700x1000"):
            screen = self.measure(size)
            self.assertGreaterEqual(screen.canvas.winfo_width(), 380, size)                # the drawing is the centre of the screen
            self.assertGreaterEqual(screen.canvas.winfo_height(), 250, size)
            footer = screen.footer
            self.assertTrue(footer.winfo_ismapped(), size)
            bottom = footer.winfo_rooty() + footer.winfo_height()
            window_bottom = self.root.winfo_rooty() + self.root.winfo_height()
            self.assertLessEqual(bottom, window_bottom + 2, f"the action buttons are pushed below the window at {size}")
            self.assertGreaterEqual(screen.card_scroll.winfo_height(), 60, size)             # the card text keeps some room (it scrolls when tall)
            self.assertGreaterEqual(screen.tree.winfo_height(), 50, size)                   # the queue stays readable

    def test_a_long_engineer_input_scrolls_instead_of_pushing_the_actions_away(self):
        self.ws.session.ask_engineer("VIEW-01", "A very long note. " * 400)
        screen = self.measure("1100x640")
        screen.set_mode("views")
        self.ws.on_select("view:VIEW-01")
        for _ in range(6):
            self.root.update()
        self.assertTrue(screen.footer.winfo_ismapped())
        self.assertLessEqual(screen.footer.winfo_rooty() + screen.footer.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height() + 2)
        self.assertGreater(screen.card_scroll.body.winfo_reqheight(), screen.card_scroll.canvas.winfo_height())     # taller than its window: it scrolls


@tier("integration")
class WizardIntegration(unittest.TestCase):
    def setUp(self):
        import oracle_wizard
        patch = mock.patch.object(oracle_wizard.Wizard, "_maybe_ask_for_api_key", lambda self: None)     # would open a key dialog
        patch.start()
        self.addCleanup(patch.stop)
        try:
            self.wizard = oracle_wizard.Wizard()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"no display: {exc}")
        self.wizard.withdraw()
        self.addCleanup(gc.collect)
        self.addCleanup(self.wizard.destroy)
        self.addCleanup(self.cancel_timers)
        self.wizard.update()

    def cancel_timers(self):
        if self.wizard.arch_workspace is not None:                                            # the workspace cancels its own timers when it closes
            self.wizard.close_architectural_workflow()
        for after_id in self.wizard.tk.splitlist(self.wizard.tk.call("after", "info")):       # the wizard's own start-up timers
            self.wizard.after_cancel(after_id)

    def test_the_wizard_still_starts_on_its_welcome_step_and_offers_the_new_workflow(self):
        import oracle_wizard
        self.assertEqual(self.wizard.step_index, 0)
        self.assertIsNone(self.wizard.arch_workspace)
        labels = [w["text"] for w in self.wizard.content.winfo_children() if isinstance(w, tk.Button)]
        self.assertTrue(any(text.startswith("Architectural Drawing Review") for text in labels))
        self.assertEqual(self.wizard.next_btn["text"], "Get started →")           # the structural workflow's entry is unchanged
        self.assertEqual(oracle_wizard.STEP_TITLES[0], "Welcome")
        self.assertEqual(len(oracle_wizard.STEP_TITLES), 8)                              # the structural steps are as they were

    def test_the_workspace_replaces_the_step_area_and_closing_it_restores_the_wizard(self):
        ws = self.wizard.open_architectural_workflow()
        self.wizard.update()
        self.assertIs(self.wizard.arch_workspace, ws)
        self.assertEqual(ws.screen, "welcome")
        self.assertEqual(self.wizard.content_area.winfo_manager(), "")
        self.assertEqual(self.wizard.header.winfo_manager(), "")                          # the workspace has its own top bar
        self.assertIs(self.wizard.open_architectural_workflow(), ws)                      # opening twice does not stack another
        self.wizard.close_architectural_workflow()
        self.wizard.update()
        self.assertIsNone(self.wizard.arch_workspace)
        for part in (self.wizard.header, self.wizard.content_area, self.wizard.footer):
            self.assertEqual(part.winfo_manager(), "pack")
        self.assertEqual(self.wizard.step_index, 0)
        self.wizard.show_step(1)                                                          # the structural steps still work
        self.wizard.update()
        self.assertEqual(self.wizard.step_index, 1)

    def test_the_workspace_window_fits_the_screen(self):
        self.wizard.open_architectural_workflow()
        self.wizard.update()
        self.assertLessEqual(self.wizard.winfo_width(), self.wizard.winfo_screenwidth())
        self.assertLessEqual(self.wizard.winfo_height(), self.wizard.winfo_screenheight())

    def test_the_workflow_does_not_touch_the_structural_project_state(self):
        before = dict(self.wizard.data)
        self.wizard.open_architectural_workflow()
        self.wizard.close_architectural_workflow()
        self.assertEqual(self.wizard.data, before)


@tier("integration")
class GuidedDrawingTools(Base):
    """Align Plan and Split View as point-picking on the drawing, and Change View Type from the View Actions menu."""

    def setUp(self):
        super().setUp()
        self.interpret()
        self.screen = self.ws.review
        self.screen.set_mode("views")
        self.ws.on_select("view:VIEW-02")
        self.s = self.ws.session
        self.b1, self.b2 = self.s.view_box("VIEW-01"), self.s.view_box("VIEW-02")
        self.p1, self.q1 = (self.b2[0] + 2000, self.b2[1] + 1500), (self.b1[0] + 2000, self.b1[1] + 1500)

    def test_align_plan_asks_for_one_point_on_the_plan_and_the_matching_point_on_the_reference(self):
        self.assertTrue(self.ws.start_alignment("VIEW-02"))
        tool = self.ws.tool
        self.assertIsNotNone(self.ws.canvas.point_handler)                                    # the canvas waits for a click
        self.assertEqual(str(self.ws.canvas["cursor"]), "tcross")
        self.assertIn("Click a matching point", self.screen.banner["text"])
        self.assertIn("First Floor Plan", self.screen.banner["text"])
        self.assertEqual(self.screen.banner.winfo_manager(), "place")
        panel = " ".join(all_text(self.screen.card_scroll.body))
        self.assertIn("STEP 1 OF 2", panel.upper())
        self.assertIn("Align:  First Floor Plan", panel)
        self.assertIn("To:  Ground Floor Plan", panel)
        self.ws.canvas.point_handler(self.p1)                                                 # the engineer clicks on the plan being aligned
        self.assertEqual((tool.state, len(self.ws.canvas.markers)), ("reference", 1))
        self.assertIn("Now click the same point on Ground Floor Plan", self.screen.banner["text"])
        self.assertIn("STEP 2 OF 2", " ".join(all_text(self.screen.card_scroll.body)).upper())

    def test_clicking_on_the_wrong_plan_does_not_advance(self):
        self.ws.start_alignment("VIEW-02")
        self.ws.canvas.point_handler(self.q1)                                                 # a point on the reference, not on the plan
        self.assertEqual(self.ws.tool.state, "plan")
        self.assertIn("Click a point on First Floor Plan", self.screen.flash_label["text"])

    def test_the_preview_is_shown_before_anything_is_recorded_and_accepting_records_one_decision(self):
        before = self.s.project.to_json()
        self.ws.start_alignment("VIEW-02")
        self.ws.canvas.point_handler(self.p1)
        self.ws.canvas.point_handler(self.q1)
        tool = self.ws.tool
        self.assertEqual(tool.state, "preview")
        self.assertEqual(self.s.project.to_json(), before)                                     # nothing is committed by picking
        self.assertTrue(self.ws.canvas.selection.polylines)                                    # the plan is drawn where it would land
        self.assertEqual({m[3] for m in self.ws.canvas.markers}, {"reference"})
        panel = " ".join(all_text(self.screen.card_scroll.body) + buttons(self.screen.footer))
        for phrase in ("Preview alignment", "No rotation.", "Accept Alignment", "Add second point pair", "Pick Again", "Cancel"):
            self.assertIn(phrase, panel)
        self.assertIn("Nothing is recorded until you accept", panel)
        decisions = len(self.s.project.decisions)
        tool.accept()
        self.assertEqual(len(self.s.project.decisions), decisions + 1)
        self.assertIn("Point-picked alignment", self.s.project.decisions[-1].reason)
        self.assertIsNone(self.ws.tool)
        self.assertIsNone(self.ws.canvas.point_handler)
        self.assertEqual(self.screen.banner.winfo_manager(), "")

    def test_a_second_pair_turns_the_plan(self):
        self.ws.start_alignment("VIEW-02")
        self.ws.canvas.point_handler(self.p1)
        self.ws.canvas.point_handler(self.q1)
        self.ws.tool.add_second_pair()
        self.assertIn("STEP 3 OF 4", " ".join(all_text(self.screen.card_scroll.body)).upper())
        p2 = (self.b2[0] + 6000, self.b2[1] + 1500)
        q2 = (self.q1[0], self.q1[1] + 4000)                                                   # the reference line runs up, the plan's runs right
        self.ws.canvas.point_handler(p2)
        self.ws.canvas.point_handler(q2)
        self.assertAlmostEqual(abs(self.ws.tool.solution.rotation_deg), 90.0, places=3)
        self.ws.tool.accept()
        self.assertAlmostEqual(abs(self.s.project.architecture.frames[-1].rotation_deg), 90.0, places=3)

    def test_cancel_and_pick_again_leave_the_project_untouched(self):
        before = self.s.project.to_json()
        self.ws.start_alignment("VIEW-02")
        self.ws.canvas.point_handler(self.p1)
        self.ws.canvas.point_handler(self.q1)
        self.ws.tool.pick_again()
        self.assertEqual((self.ws.tool.state, self.ws.tool.pairs, self.ws.canvas.markers), ("plan", [], []))
        self.ws.tool.cancel()
        self.assertIsNone(self.ws.tool)
        self.assertEqual(self.s.project.to_json(), before)
        self.assertEqual(self.screen.banner.winfo_manager(), "")
        self.assertIn("View Actions  ▾", buttons(self.screen.footer))                     # back to the view's card

    def test_selecting_something_else_cancels_a_tool_in_progress(self):
        self.ws.start_alignment("VIEW-02")
        self.ws.on_select("view:VIEW-03")
        self.assertIsNone(self.ws.tool)
        self.assertIsNone(self.ws.canvas.point_handler)

    def test_align_plan_is_in_the_view_actions_menu_only_for_plans(self):
        self.assertIn("align_plan", self.s.card("view:VIEW-02").menu)
        self.ws.run_action("align_plan", self.s.card("view:VIEW-02"))
        self.assertIsNotNone(self.ws.tool)
        self.ws.cancel_tool()
        self.s.change_view_type("VIEW-02", "section")
        self.assertNotIn("align_plan", self.s.card("view:VIEW-02").menu)

    def test_split_view_explains_itself_previews_the_parts_and_then_splits(self):
        self.ws.run_action("split_view", self.s.card("view:VIEW-03"))
        panel = " ".join(all_text(self.screen.card_scroll.body))
        self.assertIn("Use Split View when Oracle has treated one region", panel)
        self.assertIn("ground floor plan and a first floor plan", panel)
        b = self.s.view_box("VIEW-03")
        before = self.s.project.to_json()
        self.ws.canvas.point_handler(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
        self.assertEqual(self.s.project.to_json(), before)                                     # a preview, not a split
        self.assertEqual(len(self.ws.canvas.selection.rects), 2)
        self.assertIn("Accept Split", buttons(self.screen.footer))
        self.assertIn("The drawing itself is not changed", " ".join(all_text(self.screen.card_scroll.body)))
        self.ws.tool.accept()
        self.assertEqual(self.s.project.architecture.get("VIEW-03").review.value, "superseded")

    def test_change_view_type_is_offered_apart_from_reject_and_keeps_the_view(self):
        menu = self.s.card("view:VIEW-02").menu
        self.assertLess(menu.index("change_type"), menu.index("reject_view"))
        self.ws.run_action("change_type", self.s.card("view:VIEW-02"))
        kind, name, current, choices, key = self.prompts.shown[-1]
        self.assertEqual((kind, name, current, key), ("view_type", "First Floor Plan", "Plan", "floor_plan"))
        self.assertEqual(choices, ("floor_plan", "section", "elevation", "detail", "unknown"))
        view = self.s.project.architecture.get("VIEW-02")
        self.assertEqual((view.view_type.value, view.review.value), ("section", "proposed"))
        self.assertEqual(self.s.rejected_views(), [])
        shown = " ".join(all_text(self.screen.card_scroll.body))
        self.assertIn("Section", shown)
        self.assertIn("Oracle read this view as: Plan. You corrected it to: Section.", shown)

    def test_cancelling_the_type_dialog_changes_nothing(self):
        self.prompts.view_type = None
        self.ws.run_action("change_type", self.s.card("view:VIEW-02"))
        self.assertEqual(self.s.project.architecture.get("VIEW-02").view_type.value, "floor_plan")
        self.assertEqual(self.s.decision_rows(), [])

    def test_the_structural_and_original_toggle_only_changes_what_is_drawn(self):
        before = self.s.project.to_json()
        self.screen.set_drawing_mode("original")
        self.assertEqual(self.ws.canvas.mode, "original")
        self.screen.set_drawing_mode("structural")
        self.assertEqual(self.ws.canvas.mode, "structural")
        self.assertEqual(self.s.project.to_json(), before)

    def test_the_view_card_shows_type_and_status_and_no_internals(self):
        shown = all_text(self.screen.card_scroll.body)
        for word in ("Type", "Plan", "Status", "Needs review"):
            self.assertIn(word, shown)
        self.assertEqual([t for t in all_text(self.screen) if INTERNAL.search(t)], [])
