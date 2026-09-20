"""Tests for the architectural workflow service (tests/test_application_workflow.py)

Protects:
    That the application layer drives the REAL interpretation pipeline and reports it honestly: stage events arrive in order and
    only real stages are named; a bad file fails with words, a logged technical trace and no half-built project; every engineer
    action is a recorded engineer decision that the domain accepts or refuses; a saved project reopens with its decisions, its
    sources and its linework and WITHOUT being reinterpreted; two sources are never silently chosen between; and the layers
    stay clean (the interface and application layers import no CAD library, nothing below imports the interface).

Test type:
    Integration (tier "integration"): synthetic DXFs built in memory and written to a temporary folder; no window is opened.

Dependencies:
    oracle.application, oracle.interpretation, tests.drawing_factory.
"""

import ast
import tempfile
import unittest
from pathlib import Path

from oracle.application import ActionRefused, ArchitecturalSession, SourceChoiceRequired, WorkflowError
from oracle.core import DecisionSource, ReviewStatus
from oracle.interpretation import STAGES, interpret_document
from tests.drawing_factory import Sheet, three_storey_sheet
from tests.tiers import tier

REPO = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.log = []
        self.session = ArchitecturalSession(engineer="Test Engineer", logger=lambda kind, message: self.log.append((kind, message)))

    def drawing(self, name="three.dxf", sheet=None) -> Path:
        path = self.dir / name
        (sheet or three_storey_sheet()).save(path)
        return path


@tier("integration")
class ProgressIsHonest(Base):
    def test_the_pipeline_reports_each_real_stage_once_started_and_once_done_in_order(self):
        events = []
        doc = three_storey_sheet().document()
        interpret_document(doc, project_name="p", engineer="E", progress=lambda stage, event: events.append((stage, event)))
        self.assertEqual([s for s, e in events if e == "start"], list(STAGES))
        self.assertEqual([s for s, e in events if e == "done"], list(STAGES))
        for stage in STAGES:                                             # a stage is never done before it started
            self.assertLess(events.index((stage, "start")), events.index((stage, "done")))

    def test_the_pipeline_needs_no_progress_callback(self):
        self.assertTrue(interpret_document(three_storey_sheet().document(), project_name="p", engineer="E").architectures)

    def test_the_session_reports_validate_and_read_before_the_pipeline_and_review_after(self):
        events = []
        self.session.interpret(self.drawing(), progress=events.append)
        starts = [e.key for e in events if e.state == "start"]
        self.assertEqual(starts[:2], ["validate", "read"])
        self.assertEqual(starts[-1], "review")
        self.assertEqual([e.key for e in events if e.state == "done"], starts)
        self.assertFalse([e for e in events if e.state == "failed"])


@tier("integration")
class Failures(Base):
    def test_a_missing_file_fails_at_validation_with_the_file_named(self):
        events = []
        with self.assertRaises(WorkflowError) as caught:
            self.session.interpret(self.dir / "nope.dxf", progress=events.append)
        self.assertIn("nope.dxf", caught.exception.message)
        self.assertFalse(self.session.has_project)
        self.assertEqual((events[-1].state, events[-1].key), ("failed", "validate"))

    def test_a_damaged_drawing_fails_in_words_and_is_logged_with_a_trace(self):
        bad = self.dir / "damaged.dxf"
        bad.write_bytes(b"  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1032\n  0\nGARBAGE\nnot a drawing\n")
        with self.assertRaises(WorkflowError) as caught:
            self.session.interpret(bad)
        exc = caught.exception
        self.assertNotIn("Traceback", exc.message)
        self.assertNotIn("Traceback", exc.title)
        self.assertEqual(exc.file, str(bad))
        self.assertTrue(exc.logged)
        self.assertTrue(any("Traceback" in message for _kind, message in self.log))       # the technical detail went to the log
        self.assertFalse(self.session.has_project)

    def test_a_failed_attempt_leaves_an_existing_project_alone(self):
        self.session.interpret(self.drawing())
        before = self.session.project.to_dict()
        with self.assertRaises(WorkflowError):
            self.session.interpret(self.dir / "nope.dxf", add_source=True)
        self.assertEqual(self.session.project.to_dict(), before)


@tier("integration")
class Reading(Base):
    def test_the_read_models_come_from_the_interpretation_and_carry_no_invention(self):
        self.session.interpret(self.drawing())
        rows = self.session.view_rows()
        self.assertEqual([r.title for r in rows], ["GROUND FLOOR PLAN", "FIRST FLOOR PLAN", "SECOND FLOOR PLAN"])
        self.assertEqual({r.review for r in rows}, {"proposed"})                # nothing is approved by Oracle itself
        detected, established = self.session.levels()
        self.assertEqual(len(detected), 3)
        self.assertEqual(established, [])                                       # detected levels are not established levels
        self.assertEqual(self.session.approved().views, [])
        self.assertEqual(self.session.readiness().level, "review")

    def test_a_selection_becomes_an_overlay_on_the_drawing(self):
        self.session.interpret(self.drawing())
        overlay = self.session.overlay_for_object("VIEW-02")
        self.assertEqual(len(overlay.rects), 1)
        self.assertIsNotNone(overlay.focus)
        self.assertGreater(len(self.session.preview()), 0)


@tier("integration")
class EngineerDecisions(Base):
    def setUp(self):
        super().setUp()
        self.session.interpret(self.drawing())

    def view(self, view_id):
        return self.session.project.architecture.get(view_id)

    def test_approving_a_view_is_a_recorded_engineer_decision(self):
        decision = self.session.review_views(["VIEW-01"], accept=True, reason="checked against the title block")
        self.assertEqual(decision.source, DecisionSource.ENGINEER)
        self.assertEqual(decision.author, "Test Engineer")
        self.assertEqual(decision.reason, "checked against the title block")
        self.assertEqual(self.view("VIEW-01").review, ReviewStatus.ACCEPTED)
        self.assertEqual([row[0] for row in self.session.approved().views], ["VIEW-01"])
        self.assertTrue(self.session.dirty)

    def test_establishing_levels_records_one_decision_and_makes_established_levels(self):
        detected, _ = self.session.levels()
        elevations = {lv.key: i * 3000.0 for i, lv in enumerate(detected)}
        self.session.establish_levels(elevations, elevation_type="finished_floor", reason="from the section")
        detected, established = self.session.levels()
        self.assertEqual(len(established), 3)
        self.assertEqual(sorted(e.elevation_mm for e in established), [0.0, 3000.0, 6000.0])
        self.assertEqual({e.elevation_type for e in established}, {"finished_floor"})
        self.assertTrue(all(e.structural_elevation_mm is None for e in established))       # a floor finish is never called structural
        self.assertTrue(all(lv.established_id for lv in detected))

    def test_a_refused_action_changes_nothing(self):
        before = self.session.project.to_dict()
        with self.assertRaises(ActionRefused):
            self.session.establish_levels({})
        with self.assertRaises(ActionRefused):
            self.session.set_view_field("VIEW-01", "bbox", 1)
        with self.assertRaises(ActionRefused):
            self.session.accept_issue("NO-SUCH-ISSUE", "because")
        self.assertEqual(self.session.project.to_dict(), before)

    def test_renaming_a_view_goes_through_the_decision_system(self):
        self.session.set_view_field("VIEW-01", "title", "GROUND FLOOR (engineer)", "clearer")
        self.assertEqual(self.view("VIEW-01").title, "GROUND FLOOR (engineer)")
        self.assertEqual(self.session.decision_rows()[-1][2], "engineer")

    def test_splitting_a_view_keeps_the_original_on_record(self):
        made = self.session.split_view("VIEW-01", "x", 10000.0, "two things")
        self.assertGreaterEqual(len(made), 1)
        self.assertEqual(self.view("VIEW-01").review, ReviewStatus.SUPERSEDED)


@tier("integration")
class SaveAndReopen(Base):
    def test_a_reopened_project_keeps_decisions_and_linework_and_is_not_reinterpreted(self):
        self.session.interpret(self.drawing())
        self.session.review_views(["VIEW-01"], accept=True, reason="ok")
        saved = self.session.save_project(self.dir / "site.oracle.json")
        self.assertFalse(self.session.dirty)
        before = self.session.project.to_dict()

        calls = []
        import oracle.application.session as module
        original = module.interpret_document
        module.interpret_document = lambda *a, **k: calls.append(1) or original(*a, **k)
        self.addCleanup(setattr, module, "interpret_document", original)

        fresh = ArchitecturalSession()
        fresh.open_project(saved)
        self.assertEqual(calls, [])                                             # opening never interprets
        self.assertEqual(fresh.project.to_dict(), before)
        self.assertEqual([row[0] for row in fresh.approved().views], ["VIEW-01"])
        self.assertIsNotNone(fresh.preview())                                   # linework came from the geometry cache
        self.assertEqual(len(fresh.decision_rows()), 1)
        self.assertFalse(fresh.dirty)

    def test_without_the_cache_the_preview_says_so_and_reload_needs_the_same_drawing(self):
        path = self.drawing()
        self.session.interpret(path)
        saved = self.session.save_project(self.dir / "p.oracle.json")
        (self.dir / "p.oracle.geometry.json.gz").unlink()
        fresh = ArchitecturalSession()
        fresh.open_project(saved)
        self.assertIsNone(fresh.preview())
        self.assertIn("Reload linework", fresh.geometry_note[fresh.source_id()])
        other = self.drawing("other.dxf", sheet=Sheet())
        with self.assertRaises(ActionRefused):
            fresh.reload_geometry(path=other)                                   # a different drawing is refused
        self.assertIsNone(fresh.preview())
        fresh.reload_geometry(path=path)
        self.assertGreater(len(fresh.preview()), 0)

    def test_a_project_file_that_is_not_a_project_is_reported_in_words(self):
        junk = self.dir / "junk.oracle.json"
        junk.write_text("{not json")
        with self.assertRaises(WorkflowError) as caught:
            ArchitecturalSession().open_project(junk)
        self.assertNotIn("Traceback", caught.exception.message)
        with self.assertRaises(WorkflowError):
            ArchitecturalSession().open_project(self.dir / "missing.oracle.json")


@tier("integration")
class SeveralSources(Base):
    def test_two_drawings_are_two_sources_and_none_is_chosen_for_the_engineer(self):
        self.session.interpret(self.drawing("a.dxf"), revision="A")
        self.session.interpret(self.drawing("b.dxf"), add_source=True, revision="B")
        sources = self.session.sources()
        self.assertEqual([s.id for s in sources], ["SRC-1", "SRC-2"])
        self.assertEqual([s.revision for s in sources], ["A", "B"])
        self.assertIn("revision A", sources[0].label)
        saved = self.session.save_project(self.dir / "two.oracle.json")
        fresh = ArchitecturalSession()
        fresh.open_project(saved)
        with self.assertRaises(SourceChoiceRequired):
            fresh.summary()
        with self.assertRaises(SourceChoiceRequired):
            fresh.view_rows()
        fresh.set_active_source("SRC-2")
        self.assertEqual(fresh.summary().source.file, "b.dxf")
        self.assertEqual(fresh.summary("SRC-1").source.file, "a.dxf")

    def test_view_ids_of_the_two_sources_do_not_collide(self):
        self.session.interpret(self.drawing("a.dxf"))
        self.session.interpret(self.drawing("b.dxf"), add_source=True)
        ids = [v.id for a in self.session.project.architectures for v in a.views]
        self.assertEqual(len(ids), len(set(ids)))


@tier("integration")
class LayerBoundaries(unittest.TestCase):
    @staticmethod
    def imports(path: Path) -> set:
        found = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(".".join(a.name.split(".")[:2]) for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(".".join(node.module.split(".")[:2]))
        return found

    def test_the_interface_and_application_layers_import_no_cad_library(self):
        for folder in ("ui", "application"):
            for path in (REPO / "oracle" / folder).glob("*.py"):
                self.assertFalse({"ezdxf"} & {m.split(".")[0] for m in self.imports(path)}, path.name)

    def test_the_interface_reaches_the_backend_only_through_the_application_layer(self):
        for path in (REPO / "oracle" / "ui").glob("*.py"):
            found = self.imports(path)
            self.assertFalse({"oracle.core", "oracle.interpretation", "oracle.ingestion"} & found, f"{path.name}: {found}")

    def test_nothing_below_the_interface_imports_it(self):
        for folder in ("core", "ingestion", "interpretation", "application", "adapters"):
            for path in (REPO / "oracle" / folder).rglob("*.py"):
                self.assertNotIn("oracle.ui", self.imports(path), path.name)
