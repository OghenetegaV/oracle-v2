"""Tests for the application layer's small pure pieces (tests/test_application_unit.py)

Protects:
    The checks the architectural workflow does BEFORE any interpretation starts: what a chosen file is (name, type, size),
    whether Oracle can read it and, if not, a plain-words reason (missing, folder, empty, wrong extension, damaged header, DWG
    without a converter); the geometry-cache file name beside a project; the overlay model that says what a selection means on
    the drawing; and the shape of the errors shown to the engineer (no traceback in the message).

Test type:
    Unit (tier "unit"): tiny temporary files only; no drawing is interpreted and no window is opened.

Dependencies:
    oracle.application.
"""

import tempfile
import unittest
from pathlib import Path

from oracle.application import Overlay, SourceChoiceRequired, StageEvent, WORKFLOW_STAGES, WorkflowError, inspect_drawing_file
from oracle.application.files import format_size
from oracle.application.preview import union_box
from oracle.application.session import ArchitecturalSession, STAGE_LABELS, cache_path_for
from tests.tiers import tier

NO_CONVERTER = lambda: None                                             # noqa: E731
HAS_CONVERTER = lambda: "C:/fake/ODAFileConverter.exe"                  # noqa: E731


@tier("unit")
class FileInspection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_a_dxf_is_described_by_name_type_and_size(self):
        p = self.write("plan.dxf", b"  0\nSECTION\n  2\nHEADER\n" + b"x" * 3000)
        info = inspect_drawing_file(p, converter_finder=NO_CONVERTER)
        self.assertTrue(info.ok)
        self.assertEqual((info.name, info.kind), ("plan.dxf", "DXF"))
        self.assertIn("KB", info.size_text)
        self.assertEqual(info.path, p)

    def test_a_missing_file_is_reported_with_its_name_and_never_raises(self):
        info = inspect_drawing_file(self.dir / "gone.dxf", converter_finder=NO_CONVERTER)
        self.assertFalse(info.ok)
        self.assertIn("gone.dxf", info.problem)

    def test_a_folder_an_empty_file_and_the_wrong_type_are_each_refused_in_words(self):
        self.assertIn("folder", inspect_drawing_file(self.dir, converter_finder=NO_CONVERTER).problem)
        self.assertIn("empty", inspect_drawing_file(self.write("e.dxf", b""), converter_finder=NO_CONVERTER).problem)
        wrong = inspect_drawing_file(self.write("notes.pdf", b"%PDF-1.4"), converter_finder=NO_CONVERTER)
        self.assertFalse(wrong.ok)
        self.assertIn(".dwg and .dxf", wrong.problem)

    def test_a_damaged_or_misnamed_file_is_noticed_by_its_header(self):
        self.assertIn("does not look like a DXF", inspect_drawing_file(self.write("a.dxf", b"hello world" * 10), converter_finder=NO_CONVERTER).problem)
        self.assertIn("does not look like a DWG", inspect_drawing_file(self.write("a.dwg", b"hello world" * 10), converter_finder=HAS_CONVERTER).problem)

    def test_a_dxf_saved_with_a_dwg_name_is_refused_with_advice(self):
        info = inspect_drawing_file(self.write("misnamed.dwg", b"  0" + bytes([13, 10]) + b"SECTION" + bytes([13, 10]) + b"  2"), converter_finder=HAS_CONVERTER)
        self.assertFalse(info.ok)
        self.assertIn("named .dwg but its content is a DXF", info.problem)

    def test_a_dwg_needs_the_converter_and_says_so(self):
        p = self.write("a.dwg", b"AC1032" + b"\0" * 100)
        refused = inspect_drawing_file(p, converter_finder=NO_CONVERTER)
        self.assertFalse(refused.ok)
        self.assertIn("ODA File Converter", refused.problem)
        accepted = inspect_drawing_file(p, converter_finder=HAS_CONVERTER)
        self.assertTrue(accepted.ok)
        self.assertIn("original is never changed", accepted.note)

    def test_sizes_are_written_for_people(self):
        self.assertEqual(format_size(12), "12 bytes")
        self.assertEqual(format_size(2048), "2.0 KB")
        self.assertEqual(format_size(65 * 1024 * 1024), "65.0 MB")


@tier("unit")
class SmallModels(unittest.TestCase):
    def test_the_geometry_cache_sits_beside_the_project_and_never_replaces_it(self):
        self.assertEqual(cache_path_for("C:/x/site.oracle.json"), Path("C:/x/site.oracle.geometry.json.gz"))
        self.assertNotEqual(cache_path_for("a.json"), Path("a.json"))

    def test_overlays_merge_and_union_boxes(self):
        a, b = Overlay(), Overlay()
        a.rects.append(((0, 0, 10, 10), "selected", "A", "VIEW-01"))
        a.focus = (0, 0, 10, 10)
        b.rects.append(((20, 5, 30, 40), "unresolved", "B", "VIEW-02"))
        b.focus = (20, 5, 30, 40)
        a.merge(b)
        self.assertEqual(len(a.rects), 2)
        self.assertEqual(a.focus, (0, 0, 30, 40))
        self.assertIsNone(union_box([]))
        self.assertIsNone(union_box([None]))

    def test_every_workflow_stage_has_a_label_and_the_last_one_is_review(self):
        self.assertTrue(all(key in STAGE_LABELS for key in WORKFLOW_STAGES))
        self.assertEqual((WORKFLOW_STAGES[0], WORKFLOW_STAGES[-1]), ("validate", "review"))
        self.assertEqual(len(set(WORKFLOW_STAGES)), len(WORKFLOW_STAGES))

    def test_workflow_errors_carry_words_not_tracebacks(self):
        err = WorkflowError("Oracle could not read this drawing", "The file is damaged.", file="a.dxf", logged=True)
        self.assertNotIn("Traceback", err.message)
        self.assertEqual((err.title, err.file, err.logged), ("Oracle could not read this drawing", "a.dxf", True))

    def test_stage_events_are_plain_values(self):
        event = StageEvent("units", "Detecting drawing units", "done", 3, 11)
        self.assertEqual((event.key, event.state), ("units", "done"))

    def test_a_session_with_no_project_refuses_rather_than_guessing(self):
        session = ArchitecturalSession(engineer="E")
        self.assertFalse(session.has_project)
        self.assertEqual(session.sources(), [])
        with self.assertRaises(Exception):
            session.source_id()
        self.assertTrue(issubclass(SourceChoiceRequired, Exception))
