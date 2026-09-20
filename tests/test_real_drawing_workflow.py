"""Real-world test of the interface workflow: the squash-court sheet set through the application service and the canvas (tests/test_real_drawing_workflow.py)

Protects:
    That what the Architectural Drawing workspace needs from a genuine 20-sheet, ~24,000-entity architectural DWG is really
    produced by the service: stage events for a real interpretation, read models for every tab (views, detected levels, observation
    groups including column and beam candidates, open questions with alternatives, issues and readiness), a drawing preview that
    builds and draws in reasonable time, overlays for a selected view and question, a saved project that reopens WITHOUT
    reinterpretation and with its decisions and linework, and an original drawing that is never changed.

Test type:
    Slow integration test (tier "slow", about 1 minute): run with `python -m tests slow`. Skipped when the sample drawing or the
    ODA File Converter is missing. It does not run in the default or "all but slow" selections, so the 65 MB conversion never
    burdens ordinary UI tests.

Dependencies:
    oracle.application, oracle.ui.preview_canvas, the ODA File Converter, input_dwgs/Sample Architectural Drawings - Proposed
    Squash Court Extension.dwg (never modified).
"""

import gc
import hashlib
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

from oracle.application import ArchitecturalSession
from oracle.ingestion import find_oda_converter
from tests.tiers import tier

REPO_ROOT = Path(__file__).resolve().parent.parent
DWG = REPO_ROOT / "input_dwgs" / "Sample Architectural Drawings - Proposed Squash Court Extension.dwg"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@tier("slow")
@unittest.skipUnless(DWG.exists() and find_oda_converter(), "sample drawing or ODA File Converter not available")
class RealDrawingThroughTheWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hash_before = sha(DWG)
        cls.listing_before = sorted(p.name for p in DWG.parent.iterdir())
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = []
        cls.events = []
        cls.session = ArchitecturalSession(engineer="Test Engineer", logger=lambda kind, message: cls.log.append((kind, message)))
        started = time.time()
        cls.session.interpret(DWG, progress=cls.events.append)
        cls.seconds = time.time() - started
        cls.sid = cls.session.source_id()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_original_drawing_is_untouched(self):
        self.assertEqual(sha(DWG), self.hash_before)
        self.assertEqual(sorted(p.name for p in DWG.parent.iterdir()), self.listing_before)

    def test_the_stages_were_reported_as_they_happened_and_none_failed(self):
        starts = [e.key for e in self.events if e.state == "start"]
        self.assertEqual([e.key for e in self.events if e.state == "done"], starts)
        self.assertEqual((starts[0], starts[1], starts[-1]), ("validate", "read", "review"))
        self.assertFalse([e for e in self.events if e.state == "failed"])
        self.assertEqual(self.session.summary().source.file, DWG.name)

    def test_every_tab_has_real_content_and_nothing_is_approved_yet(self):
        s = self.session
        views = s.view_rows()
        self.assertGreaterEqual(len(views), 15)
        self.assertEqual({v.review for v in views}, {"proposed"})
        detected, established = s.levels()
        self.assertGreaterEqual(len(detected), 2)
        self.assertEqual(established, [])
        groups = s.observation_groups()
        self.assertGreater(sum(len(rows) for rows in groups.values()), 100)
        self.assertTrue(all(row.kind and row.category for rows in groups.values() for row in rows))
        self.assertGreater(len(s.questions()), 0)                                   # a real drawing raises real questions
        self.assertGreater(len(s.issues()), 0)
        self.assertEqual(s.approved().views, [])
        self.assertNotEqual(s.readiness().level, "ready")

    def test_the_preview_builds_and_a_view_can_be_selected_on_it(self):
        started = time.time()
        preview = self.session.preview()
        self.assertGreater(len(preview), 5000)
        first = self.session.view_rows()[0].id
        overlay = self.session.overlay_for_object(first)
        self.assertIsNotNone(overlay.focus)
        self.assertLess(time.time() - started, 60)

    def test_the_canvas_draws_the_real_preview_in_reasonable_time(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        root.withdraw()
        try:
            from oracle.ui.preview_canvas import PreviewCanvas
            root.geometry("920x640+-3000+-3000")                                # shown, but off every screen: a withdrawn canvas has no size
            root.deiconify()
            canvas = PreviewCanvas(root, width=900, height=600)
            canvas.pack(fill="both", expand=True)
            root.update()
            canvas.set_preview(self.session.preview())
            canvas.set_overlays(base=self.session.view_overlays())
            started = time.time()
            canvas.redraw()
            self.assertLess(time.time() - started, 30)
            self.assertGreater(len(canvas.find_all()), 100)
            canvas.zoom(4.0)
            canvas.redraw()
            canvas.cancel_pending()
        finally:
            root.destroy()
            gc.collect()

    def test_saving_and_reopening_keeps_everything_without_reinterpreting(self):
        s = self.session
        first = s.view_rows()[0].id
        s.review_views([first], accept=True, reason="test")
        path = s.save_project(Path(self.tmp.name) / "squash.oracle.json")
        fresh = ArchitecturalSession()
        started = time.time()
        fresh.open_project(path)
        self.assertLess(time.time() - started, 30)                                   # nothing was interpreted
        self.assertEqual(fresh.project.to_dict(), s.project.to_dict())
        self.assertEqual([row[0] for row in fresh.approved().views], [first])
        self.assertIsNotNone(fresh.preview())
        self.assertEqual(sha(DWG), self.hash_before)
