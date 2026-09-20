"""Tests for the Architectural Drawing workspace and its place in the wizard (tests/test_ui_workspace.py)

Protects:
    That the Tkinter workspace really drives the pipeline through the application layer: choosing a file shows what it is and
    whether Oracle can read it; interpretation runs on a worker thread and the window stays alive; stages are marked done only
    when they finished; a failure shows words (and offers retry / another drawing) and never a traceback; the review screen shows
    what Oracle understood and only what it understood; an engineer action makes a recorded decision and refreshes what is shown;
    a saved project reopens without reinterpretation; a project with two sources makes the engineer choose one; and the
    existing wizard still starts, still shows its eight steps, and gets back to them after the workspace closes.

Test type:
    Integration (tier "integration"): a real (withdrawn) Tk root, synthetic DXFs in a temporary folder, a scripted Prompts object
    instead of modal dialogs. Skipped when no display is available.

Dependencies:
    tkinter, oracle.ui, oracle_wizard (imported lazily), tests.drawing_factory.
"""

import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

from tests.drawing_factory import three_storey_sheet
from tests.tiers import tier


ROOT = None                                                             # one hidden Tk root for the whole module (each Tk costs time)


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

    def __init__(self, drawing=None, project=None, save_to=None):
        self.shown, self.drawing, self.project, self.save_to = [], drawing, project, save_to
        self.reason, self.text, self.pair = "checked", "Renamed", ("0", "0")
        self.establish = None
        self.confirm_answer = True

    def error(self, title, message):
        self.shown.append(("error", title, message))

    def info(self, title, message):
        self.shown.append(("info", title, message))

    def confirm(self, title, message):
        self.shown.append(("confirm", title, message))
        return self.confirm_answer

    def ask_reason(self, title, prompt, required=False):
        self.shown.append(("reason", title, prompt))
        return self.reason

    def ask_text(self, title, prompt, initial=""):
        return self.text

    def ask_pair(self, title, prompt, labels, initial=("0", "0")):
        return self.pair

    def ask_establish_levels(self, keys, suggestion, note):
        self.shown.append(("establish", keys, suggestion))
        return self.establish or {"elevations": {k: i * 3000.0 for i, (k, _l) in enumerate(keys)}, "elevation_type": "finished_floor", "reason": "r"}

    def ask_open_drawing(self, initial_dir=None):
        return self.drawing

    def ask_open_project(self, initial_dir=None):
        return self.project

    def ask_save_project(self, initial_dir=None, initial_name=""):
        return self.save_to


class Base(unittest.TestCase):
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

    def drawing(self, name="three.dxf") -> Path:
        path = self.dir / name
        three_storey_sheet().save(path)
        return path

    def interpret(self, path=None):
        path = path or self.drawing()
        self.ws.set_file(path)
        self.ws.start_interpretation()
        self.assertEqual(self.ws.screen, "processing")
        self.assertTrue(self.ws.wait_for_worker(120))
        return path


@tier("integration")
class ChoosingAndProcessing(Base):
    def test_the_first_screen_asks_for_a_drawing_and_nothing_is_interpreted_yet(self):
        self.assertEqual(self.ws.screen, "start")
        self.assertEqual(str(self.ws.interpret_btn["state"]), "disabled")
        self.assertFalse(self.ws.session.has_project)

    def test_choosing_a_file_shows_its_name_type_size_and_path(self):
        path = self.drawing()
        self.ws.set_file(path)
        self.assertEqual(self.ws.info_vars["name"].get(), "three.dxf")
        self.assertEqual(self.ws.info_vars["type"].get(), "DXF")
        self.assertIn("KB", self.ws.info_vars["size"].get())
        self.assertEqual(self.ws.info_vars["path"].get(), str(path))
        self.assertEqual(str(self.ws.interpret_btn["state"]), "normal")
        self.assertFalse(self.ws.session.has_project)                    # still not interpreted

    def test_an_unusable_file_is_refused_on_the_first_screen_with_the_reason(self):
        bad = self.dir / "notes.txt"
        bad.write_text("hello")
        self.ws.set_file(bad)
        self.assertEqual(str(self.ws.interpret_btn["state"]), "disabled")
        self.assertIn(".dwg and .dxf", self.ws.file_status["text"])

    def test_the_browse_button_uses_the_chooser_and_selects_that_file(self):
        self.prompts.drawing = str(self.drawing())
        self.ws.choose_file()
        self.assertEqual(self.ws.info_vars["name"].get(), "three.dxf")

    def test_the_window_stays_alive_while_a_drawing_is_interpreted_and_every_stage_ends_done(self):
        seen = []
        original = self.ws._on_stage
        self.ws._on_stage = lambda e: (seen.append((e.key, e.state)), original(e))
        self.ws.set_file(self.drawing())
        self.ws.start_interpretation()
        self.assertIsNotNone(self.ws._worker)
        self.assertTrue(self.ws.wait_for_worker(120))
        self.assertEqual(self.ws.screen, "review")
        started = [k for k, s in seen if s == "start"]
        self.assertEqual([k for k, s in seen if s == "done"], started)
        self.assertEqual(started[0], "validate")
        self.assertEqual(started[-1], "review")
        for key in started:
            self.assertEqual(self.ws._stage_rows[key][0]["text"], "✓")

    def test_a_failure_shows_words_offers_both_ways_out_and_logs_the_technical_trace(self):
        bad = self.dir / "damaged.dxf"
        bad.write_bytes(b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1032\n  0\nGARBAGE\nnot a drawing\n")
        self.ws.set_file(bad)
        self.assertEqual(str(self.ws.interpret_btn["state"]), "normal")
        self.ws.start_interpretation()
        self.assertTrue(self.ws.wait_for_worker(60))
        self.assertEqual(self.ws.screen, "processing")
        self.assertEqual(self.ws.error_box.winfo_manager(), "pack")           # shown (the root is withdrawn, so ismapped would be false)
        shown = " ".join(w["text"] for w in (self.ws.error_title, self.ws.error_file, self.ws.error_message, self.ws.error_note))
        self.assertNotIn("Traceback", shown)
        self.assertIn("damaged.dxf", shown)
        self.assertTrue(any("Traceback" in message for _kind, message in self.log))
        self.assertFalse(self.ws.session.has_project)
        self.ws.show_start()                                              # "Choose another drawing"
        self.assertEqual(self.ws.screen, "start")

    def test_retry_runs_the_same_file_again(self):
        path = self.dir / "later.dxf"
        self.ws.set_file(self.drawing("stand-in.dxf"))
        self.ws.pending_file = str(path)                                  # the file will only exist on the retry
        self.ws.file_info = self.ws.session.inspect_file(self.dir / "stand-in.dxf")
        self.ws.start_interpretation()
        self.assertTrue(self.ws.wait_for_worker(60))
        self.assertEqual(self.ws.error_box.winfo_manager(), "pack")           # shown (the root is withdrawn, so ismapped would be false)
        three_storey_sheet().save(path)
        self.ws.retry()
        self.assertTrue(self.ws.wait_for_worker(120))
        self.assertEqual(self.ws.screen, "review")


@tier("integration")
class Reviewing(Base):
    def setUp(self):
        super().setUp()
        self.path = self.interpret()

    def test_the_review_screen_shows_what_oracle_understood_and_that_review_is_required(self):
        self.assertEqual(self.ws.screen, "review")
        self.assertIn("Engineer review required", self.ws.banner["text"])
        titles = [self.ws.panels.views_tree.set(i, "title") for i in self.ws.panels.views_tree.get_children()]
        self.assertEqual(titles, ["GROUND FLOOR PLAN", "FIRST FLOOR PLAN", "SECOND FLOOR PLAN"])
        self.assertEqual(len(self.ws.panels.detected_tree.get_children()), 3)
        self.assertEqual(len(self.ws.panels.built_tree.get_children()), 0)          # nothing established yet
        self.assertGreater(len(self.ws.canvas.preview), 0)                          # the real linework is drawn
        self.assertGreaterEqual(len(self.ws.canvas.base_overlay.rects), 3)          # and the views outlined

    def test_observations_are_grouped_by_the_real_vocabulary_and_each_carries_its_own_evidence(self):
        tree = self.ws.panels.obs_tree
        groups = {i[4:] for i in tree.get_children()}
        self.assertTrue({"walls", "openings", "column_candidates", "grid"} <= groups)
        observation = tree.get_children("cat:column_candidates")[0]
        self.ws.panels.show_tab("observations")
        tree.selection_set(observation)
        self.ws.update()
        self.assertEqual(self.ws.selected_ref, observation)                          # selecting it highlights it on the drawing
        self.assertTrue(self.ws.canvas.selection.rects or self.ws.canvas.selection.entity_ids or self.ws.canvas.selection.polylines)

    def test_every_tab_can_be_opened(self):
        for tab in ("summary", "views", "levels", "observations", "questions", "issues", "approved", "decisions"):
            self.ws.panels.show_tab(tab)
            self.ws.update()
            self.assertEqual(self.ws.panels.tab_name(), tab)

    def test_selecting_a_view_highlights_it_on_the_drawing(self):
        self.ws.panels.show_tab("views")
        self.ws.panels.views_tree.selection_set("VIEW-02")
        self.ws.update()
        self.assertEqual([r[3] for r in self.ws.canvas.selection.rects], ["VIEW-02"])

    def test_the_canvas_can_fit_zoom_and_report_a_click(self):
        self.ws.update()
        before = self.ws.canvas.scale
        self.ws.canvas.zoom(2.0)
        self.assertAlmostEqual(self.ws.canvas.scale, before * 2.0)
        self.ws.canvas.fit()
        self.assertAlmostEqual(self.ws.canvas.scale, before)
        rect = next(r for r in self.ws.canvas.base_overlay.rects if r[3] == "VIEW-01")
        x, y = (rect[0][0] + rect[0][2]) / 2, (rect[0][1] + rect[0][3]) / 2
        self.assertEqual(self.ws.canvas.pick_view(x, y), "VIEW-01")

    def test_approving_a_view_records_an_engineer_decision_and_updates_what_is_shown(self):
        self.ws.act_review_views(["VIEW-01"], True)
        self.assertEqual(self.prompts.shown[-1][0], "reason")
        rows = {r.id: r.review for r in self.ws.session.view_rows()}
        self.assertEqual(rows["VIEW-01"], "accepted")
        self.assertEqual(self.ws.panels.views_tree.item("VIEW-01", "tags") and "approved" in self.ws.panels.views_tree.item("VIEW-01", "tags"), True)
        self.assertEqual(len(self.ws.panels.dec_tree.get_children()), 1)
        self.assertTrue(self.ws.session.dirty)

    def test_cancelling_the_reason_dialog_records_nothing(self):
        self.prompts.reason = None
        self.ws.act_review_views(["VIEW-01"], True)
        self.assertEqual(len(self.ws.session.decision_rows()), 0)

    def test_an_action_with_nothing_selected_explains_itself_and_changes_nothing(self):
        self.ws.act_review_views([], True)
        self.assertEqual(self.prompts.shown[-1][0], "info")
        self.assertEqual(len(self.ws.session.decision_rows()), 0)

    def test_a_refused_action_is_shown_in_words_and_changes_nothing(self):
        self.ws.act_accept_issue("NO-SUCH-ISSUE")
        self.assertEqual(self.prompts.shown[-1][0], "error")
        self.assertNotIn("Traceback", self.prompts.shown[-1][2])
        self.assertEqual(len(self.ws.session.decision_rows()), 0)

    def test_establishing_levels_shows_them_as_engineer_established_and_still_not_structural(self):
        self.ws.act_establish_levels()
        self.assertEqual(len(self.ws.panels.built_tree.get_children()), 3)
        _detected, built = self.ws.session.levels()
        self.assertTrue(all(b.structural_elevation_mm is None for b in built))

    def test_renaming_a_view_and_a_level_go_through_the_session(self):
        self.ws.act_view_title(["VIEW-01"])
        self.assertEqual(self.ws.session.project.architecture.get("VIEW-01").title, "Renamed")
        self.ws.act_establish_levels()
        level_id = self.ws.session.levels()[1][0].id
        self.ws.act_level_rename([level_id])
        self.assertEqual(self.ws.session.levels()[1][0].name, "Renamed")

    def test_save_then_reopen_in_a_fresh_workspace_shows_the_same_review_without_reinterpreting(self):
        self.ws.act_review_views(["VIEW-01"], True)
        self.prompts.save_to = str(self.dir / "site.oracle.json")
        saved = self.ws.save_project()
        self.assertEqual(saved, Path(self.prompts.save_to))
        self.assertFalse(self.ws.session.dirty)

        from oracle.ui.architectural_workspace import ArchitecturalWorkspace
        other = ArchitecturalWorkspace(self.root)
        other.prompts = ScriptedPrompts()
        other.pack(fill="both", expand=True)
        self.addCleanup(other.destroy)
        self.assertTrue(other.open_project(saved))
        self.assertEqual(other.screen, "review")
        self.assertEqual(other.session.project.to_dict(), self.ws.session.project.to_dict())
        self.assertEqual(other.banner["text"], self.ws.banner["text"])
        self.assertGreater(len(other.canvas.preview), 0)
        self.assertEqual(len(other.panels.dec_tree.get_children()), 1)

    def test_leaving_with_unsaved_decisions_asks_first(self):
        self.ws.act_review_views(["VIEW-01"], True)
        self.prompts.confirm_answer = False
        self.ws.exit()
        self.assertEqual(self.exited, [])
        self.prompts.confirm_answer = True
        self.ws.exit()
        self.assertEqual(self.exited, [1])


@tier("integration")
class MultipleSources(Base):
    def test_the_engineer_must_choose_between_two_sources_and_can_see_both_revisions(self):
        first = self.interpret(self.drawing("a.dxf"))
        self.ws.session.interpret(self.drawing("b.dxf"), add_source=True, revision="B")
        saved = self.ws.session.save_project(self.dir / "two.oracle.json")
        from oracle.ui.architectural_workspace import ArchitecturalWorkspace
        other = ArchitecturalWorkspace(self.root)
        other.prompts = ScriptedPrompts()
        other.pack(fill="both", expand=True)
        self.addCleanup(other.destroy)
        self.assertTrue(other.open_project(saved))
        other.update()
        self.assertEqual(other.choice_notice.winfo_manager(), "pack")                # asked, not guessed
        self.assertEqual(len(other.panels.views_tree.get_children()), 0)
        self.assertEqual(len(other.source_box["values"]), 2)
        self.assertIn("revision B", other.source_box["values"][1])
        other.source_var.set(other.source_box["values"][1])
        other._on_source_chosen()
        other.update()
        self.assertEqual(other.choice_notice.winfo_manager(), "")
        self.assertEqual(len(other.panels.views_tree.get_children()), 3)
        self.assertEqual(first.name, "a.dxf")


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
        for after_id in self.wizard.tk.splitlist(self.wizard.tk.call("after", "info")):       # the wizard's own start-up timers
            self.wizard.after_cancel(after_id)

    def test_the_wizard_still_starts_on_its_welcome_step_and_offers_the_new_workflow(self):
        self.assertEqual(self.wizard.step_index, 0)
        self.assertIsNone(self.wizard.arch_workspace)
        labels = [w["text"] for w in self.wizard.content.winfo_children() if isinstance(w, tk.Button)]
        self.assertTrue(any(text.startswith("Architectural Drawing") for text in labels))
        self.assertEqual(self.wizard.next_btn["text"], "Get started →")           # the structural workflow's entry is unchanged

    def test_the_workspace_replaces_the_step_area_and_closing_it_restores_the_wizard(self):
        ws = self.wizard.open_architectural_workflow()
        self.wizard.update()
        self.assertIs(self.wizard.arch_workspace, ws)
        self.assertEqual(ws.screen, "start")
        self.assertEqual(self.wizard.content_area.winfo_manager(), "")
        self.assertIs(self.wizard.open_architectural_workflow(), ws)                  # opening twice does not stack another
        self.wizard.close_architectural_workflow()
        self.wizard.update()
        self.assertIsNone(self.wizard.arch_workspace)
        self.assertEqual(self.wizard.content_area.winfo_manager(), "pack")
        self.assertEqual(self.wizard.footer.winfo_manager(), "pack")
        self.assertEqual(self.wizard.step_index, 0)
        self.wizard.show_step(1)                                                       # the structural steps still work
        self.wizard.update()
        self.assertEqual(self.wizard.step_index, 1)

    def test_the_workflow_does_not_touch_the_structural_project_state(self):
        before = dict(self.wizard.data)
        self.wizard.open_architectural_workflow()
        self.wizard.close_architectural_workflow()
        self.assertEqual(self.wizard.data, before)
