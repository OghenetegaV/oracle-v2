"""Tests for CAD ingestion (oracle.ingestion) and unit detection (oracle.interpretation.units)

Protects:
    That a DXF (and a DWG, through the ODA converter, when it is installed) becomes a neutral, JSON-cacheable
    DrawingDocument with every entity kept at its source coordinates, that reading never modifies or
    writes beside the original, that arcs are boxed by the part actually drawn, that unreadable input fails
    with a clear IngestionError, and that units are taken from the file only when the drawing corroborates
    them: metadata that the geometry contradicts, or no metadata at all, is ASSUMED, lowers confidence,
    and asks the engineer instead of guessing silently.

Test type:
    Unit and integration tests; the DWG test is skipped when the converter or the sample is missing.

Dependencies:
    oracle.ingestion, oracle.interpretation.units, ezdxf, tests.drawing_factory.
"""

import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

import ezdxf

from oracle.core import ValueStatus
from oracle.ingestion import DrawingDocument, IngestionError, find_oda_converter, read_drawing, read_ezdxf_document
from oracle.ingestion.dxf_reader import _arc_box
from oracle.interpretation.units import detect_units
from tests.drawing_factory import Sheet
from tests.tiers import tier

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DWG = REPO_ROOT / "input_dwgs" / "test_floor.dwg"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@tier("integration")
class ReadingDxf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "plan.dxf"
        sheet = Sheet()
        sheet.plan((1000.0, 2000.0), "GROUND FLOOR PLAN", labels=("STAIR",))
        sheet.section((0.0, -16000.0), "SECTION A-A", dims=True)
        sheet.save(self.path)
        self.doc = read_drawing(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_document_records_the_file_and_its_hash(self):
        self.assertEqual(self.doc.source_file, "plan.dxf")
        self.assertEqual(self.doc.sha256, sha(self.path))
        self.assertEqual(self.doc.unit_code, 4)
        self.assertTrue(self.doc.format_version.startswith("AC"))

    def test_reading_neither_modifies_the_file_nor_writes_beside_it(self):
        before = sha(self.path)
        read_drawing(self.path)
        self.assertEqual(sha(self.path), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["plan.dxf"])

    def test_every_kind_the_drawing_holds_is_read(self):
        kinds = {e.kind for e in self.doc.entities}
        self.assertTrue({"LINE", "POLYLINE", "TEXT", "INSERT", "DIMENSION"} <= kinds, kinds)
        self.assertEqual(self.doc.skipped, {})

    def test_coordinates_are_kept_exactly_as_drawn(self):
        lines = [e for e in self.doc.entities if e.kind == "LINE" and e.layer == "A-GRID"]
        xs = sorted({round(e.points[0][0], 6) for e in lines if abs(e.points[0][0] - e.points[1][0]) < 1e-9})
        self.assertEqual(xs, [1000.0, 7000.0, 13000.0])                       # the vertical grid lines A, B, C

    def test_text_and_dimensions_keep_their_content(self):
        texts = {e.text for e in self.doc.entities if e.kind == "TEXT"}
        self.assertIn("GROUND FLOOR PLAN", texts)
        self.assertIn("STAIR", texts)
        dims = [e for e in self.doc.entities if e.kind == "DIMENSION"]
        self.assertTrue(dims)
        self.assertTrue(all(d.value == 3300.0 and d.rotation == 90.0 for d in dims))

    def test_blocks_are_kept_as_a_placed_footprint_with_their_name(self):
        doors = [e for e in self.doc.entities if e.kind == "INSERT" and e.block == "DOOR-900"]
        self.assertEqual(len(doors), 3)
        for d in doors:
            self.assertAlmostEqual(d.width, 900.0, delta=1.0)

    def test_the_document_round_trips_through_json(self):
        again = DrawingDocument.from_dict(json.loads(json.dumps(self.doc.to_dict())))
        self.assertEqual(again.to_dict(), self.doc.to_dict())
        self.assertEqual(len(again.entities), len(self.doc.entities))

    def test_entity_ids_are_unique_and_stable_between_reads(self):
        ids = [e.id for e in self.doc.entities]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [e.id for e in read_drawing(self.path).entities])

    def test_geometry_box_is_the_drawn_extent(self):
        box = self.doc.geometry_box()
        self.assertLessEqual(box[0], 1000.0 - 600.0 + 1e-6)
        self.assertLess(box[1], 2000.0)
        self.assertGreater(box[2], 13000.0)


@tier("integration")
class Arcs(unittest.TestCase):
    def test_an_arc_is_boxed_by_the_part_drawn_not_the_whole_circle(self):
        x0, y0, x1, y1 = _arc_box((0.0, 0.0), 1000.0, 0.0, 90.0)
        self.assertAlmostEqual(x0, 0.0, places=6)
        self.assertAlmostEqual(y0, 0.0, places=6)
        self.assertAlmostEqual(x1, 1000.0, places=6)
        self.assertAlmostEqual(y1, 1000.0, places=6)

    def test_an_arc_that_crosses_zero_degrees_includes_the_rightmost_point(self):
        x0, _y0, x1, _y1 = _arc_box((0.0, 0.0), 500.0, 350.0, 10.0)
        self.assertAlmostEqual(x1, 500.0, places=6)
        self.assertGreater(x0, 400.0)

    def test_a_dxf_arc_goes_through_the_reader(self):
        doc = ezdxf.new("R2018")
        doc.modelspace().add_arc((5000.0, 5000.0), 1000.0, 0.0, 90.0)
        (arc,) = read_ezdxf_document(doc).entities
        self.assertEqual(arc.kind, "ARC")
        self.assertAlmostEqual(arc.box[2] - arc.box[0], 1000.0, places=3)


@tier("integration")
class Refusals(unittest.TestCase):
    def test_missing_unsupported_and_garbled_files_raise_ingestion_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "a.txt").write_text("x", encoding="utf-8")
            (tmp / "b.dxf").write_text("not a drawing at all", encoding="utf-8")
            for name in ("missing.dxf", "a.txt", "b.dxf"):
                with self.assertRaises(IngestionError, msg=name):
                    read_drawing(tmp / name)

    def test_a_dwg_without_a_converter_is_refused_clearly(self):
        with tempfile.TemporaryDirectory() as tmp:
            dwg = Path(tmp) / "x.dwg"
            dwg.write_bytes(b"AC1032" + b"\0" * 100)
            with self.assertRaises(IngestionError):
                read_drawing(dwg, oda_converter=str(Path(tmp) / "no_such_converter.exe"))


@tier("release")
@unittest.skipUnless(find_oda_converter() and SAMPLE_DWG.exists(), "ODA File Converter or input_dwgs/test_floor.dwg not available")
class ReadingDwg(unittest.TestCase):
    def test_a_real_dwg_is_converted_read_and_left_untouched(self):
        before = sha(SAMPLE_DWG)
        listing = sorted(p.name for p in SAMPLE_DWG.parent.iterdir())
        doc = read_drawing(SAMPLE_DWG)
        self.assertEqual(doc.sha256, before, "the hash is of the original DWG, not of the converted file")
        self.assertEqual(sha(SAMPLE_DWG), before)
        self.assertEqual(sorted(p.name for p in SAMPLE_DWG.parent.iterdir()), listing, "no converted file is left beside the drawing")
        self.assertTrue(doc.entities)
        self.assertEqual(doc.source_file, SAMPLE_DWG.name)


@tier("integration")
class UnitDetection(unittest.TestCase):
    def verdict(self, **kwargs):
        s = Sheet(**kwargs)
        s.plan()
        s.section((0, -16000), "SECTION A-A", dims=True)
        return detect_units(s.document())

    def test_metadata_the_drawing_corroborates_is_source(self):
        for units, expected, factor in (("mm", "mm", 1.0), ("m", "m", 1000.0), ("inch", "inch", 25.4)):
            v = self.verdict(units=units)
            self.assertEqual((v.estimate.unit, v.estimate.factor_to_mm), (expected, factor), units)
            self.assertEqual(v.basis, ValueStatus.SOURCE, units)
            self.assertGreaterEqual(v.estimate.confidence, 0.9, units)
            self.assertFalse(v.needs_engineer, units)

    def test_no_metadata_is_assumed_never_source_and_is_capped(self):
        v = self.verdict(units="unitless")
        self.assertEqual(v.basis, ValueStatus.ASSUMED)
        self.assertLessEqual(v.estimate.confidence, 0.75)
        self.assertTrue(v.needs_engineer)

    def test_metadata_the_geometry_contradicts_is_a_conflict(self):
        v = self.verdict(units="mm", insunits=6)                       # says metres; 12000-unit walls are not 12 km
        self.assertEqual(v.estimate.unit, "mm")
        self.assertEqual(v.basis, ValueStatus.ASSUMED)
        self.assertEqual(len(v.conflicts), 1)
        self.assertIn("metadata says m", v.conflicts[0])
        self.assertLessEqual(v.estimate.confidence, 0.6)
        self.assertTrue(v.needs_engineer)

    def test_other_readings_are_kept_as_alternatives_when_not_negligible(self):
        v = self.verdict(units="mm", insunits=1)
        self.assertIn("inch", [a.unit for a in v.alternatives])

    def test_the_evidence_is_explained_for_provenance(self):
        v = self.verdict(units="mm")
        text = " ".join(d for d, _w in v.evidence)
        self.assertIn("median drawn line", text)
        self.assertIn("metadata says mm", text)

    def test_an_empty_drawing_gets_a_low_confidence_answer_not_a_crash(self):
        v = detect_units(Sheet().document())
        self.assertLessEqual(v.estimate.confidence, 0.75)
        self.assertTrue(v.needs_engineer)
        self.assertTrue(math.isfinite(v.estimate.factor_to_mm))


if __name__ == "__main__":
    unittest.main()
