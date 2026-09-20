"""Tests for the architectural interpretation model in oracle.core (schema 0.3.0)

Protects:
    The validation rules of the new core objects (DrawingSource, CoordinateFrame, LayerClassification,
    DrawingView, ArchitecturalObservation, HeightEvidence, CrossViewFinding), coordinate frames that
    compose source -> view -> building (translation, rotation, scale, mirroring, exact inverses), strict
    JSON round trips, the project's registry of architectural targets (provenance, value status and
    decisions may point at them; nothing else may), the readiness blocker for unreviewed views, and that
    oracle.core still imports no CAD library, AI client or plotting library.

Test type:
    Unit tests of oracle.core.architecture and its integration in OracleProject.

Dependencies:
    oracle.core only (plus a subprocess for the import-independence check).
"""

import subprocess
import sys
import unittest
from pathlib import Path
from dataclasses import replace

from oracle.core import (
    ArchitecturalInterpretation, ArchitecturalObservation, BlockerKind, CoordinateFrame, CrossViewFinding, DrawingSource,
    DrawingView, HeightEvidence, HintKind, LayerClassification, OracleProject, ProvenanceRecord, ReviewStatus, SourceReference,
    Target, TargetScope, UnitEstimate, ValidationError, ValueStatus, ValueStatusRecord, ViewType,
)
from tests.tiers import tier

SHA = "a" * 64
UNITS = UnitEstimate("mm", 1.0, 0.9, "header_metadata")


def source(**kw):
    return DrawingSource("SRC-1", "plan.dwg", SHA, UNITS, **kw)


def interpretation() -> ArchitecturalInterpretation:
    arch = ArchitecturalInterpretation(source(declared_unit="mm", source_metadata={"format_version": "X1", "layouts": ["A", "B"]}, extents=(0, 0, 10, 10), entity_count=3))
    arch.add(CoordinateFrame("FRM-0", "source"))
    arch.add(CoordinateFrame("FRM-1", "view", "FRM-0", (100.0, 200.0), 30.0))
    arch.add(CoordinateFrame("FRM-2", "building", "FRM-1", (-5.0, 7.0)))
    arch.add(LayerClassification("LAY-001", "A-WALL-EXT", "wall_external", 0.95, "token_match", None, 3))
    arch.add(DrawingView("VIEW-01", ViewType.FLOOR_PLAN, (0, 0, 10, 10), 0.9, "GROUND FLOOR PLAN", "GROUND", frame_id="FRM-1",
                         alignment_frame_id="FRM-2", entity_ids=("A1", "A2")))
    arch.add(DrawingView("VIEW-02", ViewType.SECTION, (20, 0, 30, 10), 0.8, "SECTION A-A", section_label="A-A"))
    arch.add(ArchitecturalObservation("OBS-00001", "column_symbol", "VIEW-01", ValueStatus.INFERRED, 0.8, "A-COLS", ("A1",),
                                      ((1, 1), (2, 1), (2, 2), (1, 2)), True, None, 1, HintKind.COLUMN_CANDIDATE))
    arch.add(HeightEvidence("HGT-001", "GROUND", "FLOOR:1", 3300.0, "level_tags", ValueStatus.SOURCE, 0.85, "VIEW-02"))
    arch.add(CrossViewFinding("FND-001", "How high?", "agree", "3300", ("VIEW-01", "VIEW-02")))
    return arch


@tier("unit")
class Validation(unittest.TestCase):
    def rejects(self, factory):
        with self.assertRaises(ValidationError):
            factory()

    def test_ids_must_have_their_kind_prefix(self):
        for bad in ("VIEW1", "view-01", "OBS-1", "", 5):
            self.rejects(lambda: DrawingView(bad, ViewType.FLOOR_PLAN, (0, 0, 1, 1), 0.5))
        self.rejects(lambda: CoordinateFrame("FRM-x", "f"))
        self.rejects(lambda: DrawingSource("DWG", "f", SHA, UNITS))

    def test_a_bbox_must_be_ordered_and_numeric(self):
        for bad in ((1, 1, 0, 0), (0, 0, 1), (0, 0, 1, "x"), None):
            self.rejects(lambda: DrawingView("VIEW-01", ViewType.FLOOR_PLAN, bad, 0.5))

    def test_confidence_is_between_zero_and_one(self):
        for bad in (-0.1, 1.1, "high", None):
            self.rejects(lambda: DrawingView("VIEW-01", ViewType.FLOOR_PLAN, (0, 0, 1, 1), bad))

    def test_only_a_floor_plan_carries_a_level_and_the_key_must_be_well_formed(self):
        DrawingView("VIEW-01", ViewType.FLOOR_PLAN, (0, 0, 1, 1), 0.5, level_key="FLOOR:12")
        self.rejects(lambda: DrawingView("VIEW-01", ViewType.SECTION, (0, 0, 1, 1), 0.5, level_key="GROUND"))
        for bad in ("Ground", "FLOOR:", "FLOOR:x", "LEVEL:1", "BASEMENT"):
            self.rejects(lambda: DrawingView("VIEW-01", ViewType.FLOOR_PLAN, (0, 0, 1, 1), 0.5, level_key=bad))

    def test_superseded_status_and_superseded_by_go_together(self):
        self.rejects(lambda: DrawingView("VIEW-01", ViewType.FLOOR_PLAN, (0, 0, 1, 1), 0.5, review=ReviewStatus.SUPERSEDED))
        self.rejects(lambda: DrawingView("VIEW-01", ViewType.FLOOR_PLAN, (0, 0, 1, 1), 0.5, superseded_by=("VIEW-02",)))
        DrawingView("VIEW-01", ViewType.FLOOR_PLAN, (0, 0, 1, 1), 0.5, review=ReviewStatus.SUPERSEDED, superseded_by=("VIEW-02",))

    def test_an_observation_is_a_source_or_inferred_reading_never_an_engineer_value(self):
        ok = dict(id="OBS-00001", kind="door", view_id="VIEW-01", confidence=0.5)
        ArchitecturalObservation(basis=ValueStatus.SOURCE, **ok)
        ArchitecturalObservation(basis=ValueStatus.INFERRED, **ok)
        for bad in (ValueStatus.ENGINEER_DEFINED, ValueStatus.CALCULATED):
            self.rejects(lambda: ArchitecturalObservation(basis=bad, **ok))
        self.rejects(lambda: ArchitecturalObservation(id="OBS-00001", kind="Not A Word", view_id="VIEW-01", basis=ValueStatus.SOURCE, confidence=0.5))

    def test_a_height_needs_two_different_valid_levels_and_a_positive_value(self):
        good = dict(id="HGT-001", from_level="GROUND", to_level="FLOOR:1", height_mm=3300.0, source="level_tags",
                    basis=ValueStatus.SOURCE, confidence=0.8)
        HeightEvidence(**good)
        self.rejects(lambda: HeightEvidence(**{**good, "height_mm": 0.0}))
        self.rejects(lambda: HeightEvidence(**{**good, "height_mm": -5.0}))
        self.rejects(lambda: HeightEvidence(**{**good, "to_level": "First"}))

    def test_a_finding_agrees_disagrees_or_is_insufficient(self):
        CrossViewFinding("FND-001", "q", "insufficient", "s")
        self.rejects(lambda: CrossViewFinding("FND-001", "q", "maybe", "s"))

    def test_units_are_a_known_unit_with_a_positive_factor(self):
        self.rejects(lambda: UnitEstimate("furlong", 201168.0, 0.5, "x"))
        self.rejects(lambda: UnitEstimate("mm", 0.0, 0.5, "x"))
        self.rejects(lambda: DrawingSource("SRC-1", "f", "not-a-hash", UNITS))

    def test_the_container_refuses_duplicates_and_dangling_references(self):
        arch = interpretation()
        self.rejects(lambda: arch.add(DrawingView("VIEW-01", ViewType.NOTES, (0, 0, 1, 1), 0.5)))
        self.rejects(lambda: arch.add(DrawingView("VIEW-09", ViewType.NOTES, (0, 0, 1, 1), 0.5, frame_id="FRM-9")))
        self.rejects(lambda: arch.add(ArchitecturalObservation("OBS-00002", "door", "VIEW-99", ValueStatus.SOURCE, 0.5)))
        self.rejects(lambda: arch.add(LayerClassification("LAY-002", "A-WALL-EXT", "wall", 0.5, "x")))       # same layer twice
        self.rejects(lambda: arch.add(CrossViewFinding("FND-002", "q", "agree", "s", ("VIEW-99",))))
        self.rejects(lambda: arch.get("VIEW-99"))

    def test_frames_cannot_form_a_loop(self):
        arch = ArchitecturalInterpretation(source())
        arch.add(CoordinateFrame("FRM-0", "root"))
        arch.add(CoordinateFrame("FRM-1", "a", "FRM-0"))
        self.rejects(lambda: arch.replace(CoordinateFrame("FRM-0", "root", "FRM-1")))

    def test_replace_keeps_the_type_and_the_id(self):
        arch = interpretation()
        view = arch.get("VIEW-01")
        arch.replace(replace(view, title="renamed"))
        self.assertEqual(arch.get("VIEW-01").title, "renamed")
        self.rejects(lambda: arch.replace(CoordinateFrame("VIEW-01".replace("VIEW", "FRM"), "x")))


@tier("unit")
class Frames(unittest.TestCase):
    def test_translation_rotation_scale_and_mirroring_compose(self):
        f = CoordinateFrame("FRM-1", "f", "FRM-0", (10.0, 20.0), 90.0, 2.0)
        x, y = f.to_parent((3.0, 1.0))
        self.assertAlmostEqual(x, 10.0 - 2.0, places=9)
        self.assertAlmostEqual(y, 20.0 + 6.0, places=9)
        m = CoordinateFrame("FRM-1", "f", "FRM-0", (0.0, 0.0), 0.0, 1.0, mirrored=True)
        self.assertEqual(m.to_parent((5.0, 2.0)), (-5.0, 2.0))

    def test_from_parent_is_the_exact_inverse(self):
        for f in (CoordinateFrame("FRM-1", "f", "FRM-0", (1_250_000.5, 4_800_000.25), 33.3, 1.0),
                  CoordinateFrame("FRM-1", "f", "FRM-0", (-3.0, 8.0), -120.0, 0.25, mirrored=True)):
            for p in ((0.0, 0.0), (12.5, -7.25), (1e5, 1e5)):
                q = f.from_parent(f.to_parent(p))
                self.assertAlmostEqual(q[0], p[0], places=6)
                self.assertAlmostEqual(q[1], p[1], places=6)

    def test_chains_run_from_a_view_all_the_way_to_source_coordinates(self):
        arch = interpretation()
        p = (12.0, 34.0)
        step1 = arch.get("FRM-2").to_parent(p)
        step2 = arch.get("FRM-1").to_parent(step1)
        self.assertEqual(arch.to_source("FRM-2", p), step2)
        back = arch.from_source("FRM-2", step2)
        self.assertAlmostEqual(back[0], p[0], places=9)
        self.assertAlmostEqual(back[1], p[1], places=9)
        self.assertEqual(arch.to_source("FRM-0", (5.0, 5.0)), (5.0, 5.0))

    def test_a_frame_cannot_be_zero_or_negative_scale(self):
        for scale in (0.0, -1.0):
            with self.assertRaises(ValidationError):
                CoordinateFrame("FRM-1", "f", "FRM-0", scale=scale)

    def test_source_units_convert_to_millimetres_by_the_drawing_unit(self):
        arch = ArchitecturalInterpretation(DrawingSource("SRC-1", "f", SHA, UnitEstimate("m", 1000.0, 0.9, "x")))
        self.assertEqual(arch.source_to_millimetres(3.3), 3300.0)


@tier("unit")
class RoundTrips(unittest.TestCase):
    def test_the_interpretation_round_trips_exactly(self):
        arch = interpretation()
        again = ArchitecturalInterpretation.from_dict(arch.to_dict())
        self.assertEqual(again.to_dict(), arch.to_dict())
        self.assertEqual(again.get("OBS-00001").hint, HintKind.COLUMN_CANDIDATE)
        self.assertEqual(again.get("VIEW-01").view_type, ViewType.FLOOR_PLAN)

    def test_unknown_keys_are_refused_at_every_level(self):
        for path in (("drawing",), ("views", 0), ("observations", 0), ("heights", 0), ("frames", 1), ("layers", 0), ("findings", 0)):
            d = interpretation().to_dict()
            node = d
            for step in path:
                node = node[step]
            node["surprise"] = 1
            with self.assertRaises(ValidationError, msg=str(path)):
                ArchitecturalInterpretation.from_dict(d)

    def test_a_broken_reference_in_a_file_is_refused_on_load(self):
        d = interpretation().to_dict()
        d["observations"][0]["view_id"] = "VIEW-77"
        with self.assertRaises(ValidationError):
            ArchitecturalInterpretation.from_dict(d)


@tier("unit")
class InsideAProject(unittest.TestCase):
    def project(self) -> OracleProject:
        p = OracleProject.create("Core", "Test Engineer")
        p.set_architecture(interpretation())
        return p

    def test_one_interpretation_per_project(self):
        p = self.project()
        with self.assertRaises(ValidationError):
            p.set_architecture(interpretation())            # the same source again: refused
        self.assertIsNone(OracleProject.create("x", "y").architecture)

    def test_provenance_and_status_may_only_name_objects_that_exist(self):
        p = self.project()
        rec = ProvenanceRecord("PV-00001", Target.architectural("VIEW-01"), SourceReference(source_id="x"), "spatial_cluster", "test")
        p.add_provenance(rec)
        p.set_value_status(ValueStatusRecord(Target.architectural("VIEW-01"), "view_type", ValueStatus.INFERRED, provenance_ids=("PV-00001",)))
        with self.assertRaises(ValidationError):
            p.add_provenance(ProvenanceRecord("PV-00002", Target.architectural("VIEW-99"), SourceReference(source_id="x"), "m", "t"))
        with self.assertRaises(ValidationError):
            p.set_value_status(ValueStatusRecord(Target.architectural("VIEW-99"), "view_type", ValueStatus.INFERRED))
        with self.assertRaises(ValidationError):
            p.set_value_status(ValueStatusRecord(Target.architectural("VIEW-01"), "no_such_field", ValueStatus.INFERRED))

    def test_a_project_without_an_interpretation_refuses_architectural_targets(self):
        p = OracleProject.create("x", "y")
        with self.assertRaises(ValidationError):
            p.add_provenance(ProvenanceRecord("PV-00001", Target.architectural("VIEW-01"), SourceReference(source_id="x"), "m", "t"))

    def test_unreviewed_plans_sections_and_elevations_block_readiness_but_notes_do_not(self):
        p = self.project()
        blockers = [b for b in p.readiness().blockers if b.kind == BlockerKind.UNREVIEWED_VIEW]
        self.assertEqual(sorted(b.reference for b in blockers), ["VIEW-01", "VIEW-02"])
        p.architecture.add(DrawingView("VIEW-03", ViewType.NOTES, (40, 0, 50, 10), 0.5))
        again = [b for b in p.readiness().blockers if b.kind == BlockerKind.UNREVIEWED_VIEW]
        self.assertEqual(len(again), 2)

    def test_the_project_round_trips_with_and_without_an_interpretation(self):
        p = self.project()
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())
        bare = OracleProject.create("bare", "e")
        self.assertEqual(bare.to_dict()["architectures"], [])
        self.assertEqual(OracleProject.from_json(bare.to_json()).to_json(), bare.to_json())

    def test_the_scope_exists_and_the_target_helper_uses_it(self):
        t = Target.architectural("VIEW-01")
        self.assertEqual((t.scope, t.id), (TargetScope.ARCHITECTURAL, "VIEW-01"))


@tier("integration")
class CoreStaysIndependent(unittest.TestCase):
    def test_core_imports_no_cad_ai_or_plotting_library(self):
        code = ("import sys, oracle.core; bad = [m for m in sys.modules if m.split('.')[0] in "
                "('ezdxf', 'shapely', 'matplotlib', 'anthropic', 'numpy')]; print(bad)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "[]")

    def test_the_architecture_module_names_no_cad_layer_conventions(self):
        import oracle.core.architecture as mod
        text = Path(mod.__file__).read_text(encoding="utf-8").lower()
        for word in ("ezdxf", "autocad", "a-wall", "staad"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
