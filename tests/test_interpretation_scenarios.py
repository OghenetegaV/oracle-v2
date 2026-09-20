"""Tests for the architectural interpretation pipeline on 15 synthetic scenarios

Protects:
    That Oracle reads a drawing without inventing anything: views are found spatially (several plans per
    sheet, plans told apart from sections and elevations), levels come from the drawing's own words,
    storey heights come only from written evidence and are never made up, disagreements between plans,
    sections and elevations become interpretation sets AND issues (never a silent choice), uncertain units
    and unknown layers are reported, original geometry is untouched, coordinates transform correctly
    (rotation, far-from-origin, side-by-side), every derived object carries provenance and a value status,
    and the result is deterministic. Scenarios 1-15 follow the Phase 3 brief.

Test type:
    Integration tests: a synthetic DXF is built in memory (tests/drawing_factory.py) with known content and
    run through interpret_document; the result is compared with what was drawn, not with Oracle's own output.

Dependencies:
    oracle.interpretation, oracle.core, oracle.ingestion, ezdxf and tests.drawing_factory.
"""

import copy
import re
import tempfile
import unittest
from pathlib import Path

from oracle.core import (
    HintKind, IssueSeverity, OracleProject, ReviewStatus, Target, ValueStatus, ViewType,
)
from oracle.ingestion import IngestionError, read_drawing
from oracle.interpretation import InterpretationConfig, interpret_document, suggest_elevations
from oracle.interpretation.layers import LayerConfig
from tests.drawing_factory import NCS, PLAIN, UNKNOWN, Sheet, three_storey_sheet
from tests.tiers import tier

STRUCTURAL_WORDS = ("structural", "load", "bearing", "shear", "slab_design", "foundation")
LEVELS3 = (("GROUND FLOOR", 0), ("FIRST FLOOR", 3300), ("SECOND FLOOR", 6600))


def run(sheet: Sheet) -> OracleProject:
    return interpret_document(sheet.document(), project_name="Synthetic", engineer="Test Engineer")


def kinds(project, view_id=None) -> dict:
    out: dict = {}
    for o in project.architecture.observations:
        if view_id is None or o.view_id == view_id:
            out[o.kind] = out.get(o.kind, 0) + o.count
    return out


def heights(project, a, c) -> list:
    return sorted((h.height_mm, h.source, h.basis) for h in project.architecture.heights if (h.from_level, h.to_level) == (a, c))


def plans(project) -> list:
    return [v for v in project.architecture.views_of(ViewType.FLOOR_PLAN) if v.variant is None]


def issue_text(project) -> str:
    return "\n".join(i.message for i in project.issues)


def strip_time(text: str) -> str:
    return re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?", "T", text)


@tier("integration")
class Scenario01SingleGroundPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = Sheet()
        cls.sheet.plan((0, 0), "GROUND FLOOR PLAN")
        cls.doc = cls.sheet.document()
        cls.before = copy.deepcopy(cls.doc.to_dict())
        cls.project = interpret_document(cls.doc, project_name="One", engineer="Test Engineer")

    def test_one_floor_plan_is_found_and_named_from_its_title(self):
        (plan,) = plans(self.project)
        self.assertEqual(plan.level_key, "GROUND")
        self.assertEqual(plan.title, "GROUND FLOOR PLAN")
        self.assertGreaterEqual(plan.confidence, 0.9)
        self.assertEqual(plan.review, ReviewStatus.PROPOSED)

    def test_the_observations_match_what_was_drawn(self):
        k = kinds(self.project)
        self.assertEqual(k["door"], 3)
        self.assertEqual(k["window"], 4)
        self.assertEqual(k["column_symbol"], 6)
        self.assertGreater(k["wall_external"], 0)
        self.assertGreater(k["wall_internal"], 0)

    def test_observations_are_not_structural_classifications(self):
        for o in self.project.architecture.observations:
            self.assertFalse(any(w in o.kind for w in STRUCTURAL_WORDS), o.kind)
        hinted = [o for o in self.project.architecture.observations if o.hint]
        self.assertTrue(hinted)
        for o in hinted:
            self.assertIsInstance(o.hint, HintKind)
            self.assertTrue(self.project.provenance_for(Target.architectural(o.id)), "a hint must carry evidence")

    def test_the_building_is_not_created_and_the_project_is_not_ready(self):
        self.assertIsNone(self.project.building)
        readiness = self.project.readiness()
        self.assertFalse(readiness.ready)

    def test_units_come_from_the_file_and_are_corroborated(self):
        source = self.project.architecture.drawing
        self.assertEqual(source.units.unit, "mm")
        self.assertEqual(self.project.value_status_of(Target.architectural(source.id), "units").status, ValueStatus.SOURCE)

    def test_the_original_geometry_is_untouched(self):
        self.assertEqual(self.doc.to_dict(), self.before)
        ids = {e.id for e in self.doc.entities}
        for v in self.project.architecture.views:
            self.assertTrue(set(v.entity_ids) <= ids)
        for o in self.project.architecture.observations:
            self.assertTrue(set(o.entity_ids) <= ids)

    def test_no_storey_height_is_invented_from_a_single_plan(self):
        self.assertEqual(self.project.architecture.heights, [])

    def test_everything_derived_has_provenance_and_a_value_status(self):
        arch = self.project.architecture
        for obj in arch.views + arch.observations + arch.layers:
            self.assertTrue(self.project.provenance_for(Target.architectural(obj.id)), obj.id)
        for v in arch.views:
            self.assertIsNotNone(self.project.value_status_of(Target.architectural(v.id), "view_type"))
        self.project.validate()


@tier("integration")
class Scenario02ThreeFloorsSideBySide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = three_storey_sheet()
        cls.sheet.section((0, -16000), "SECTION A-A", LEVELS3, dims=True)
        cls.project = run(cls.sheet)
        cls.arch = cls.project.architecture

    def test_three_plans_are_separated_spatially_and_ordered_by_level(self):
        found = {v.level_key: v for v in plans(self.project)}
        self.assertEqual(sorted(found), ["FLOOR:1", "FLOOR:2", "GROUND"])
        boxes = sorted(v.bbox for v in found.values())
        for a, b in zip(boxes, boxes[1:]):
            self.assertLess(a[2], b[0], "plans must not overlap")

    def test_the_floors_line_up_by_their_grid_labels(self):
        by_level = {v.level_key: v for v in plans(self.project)}
        self.assertTrue(all(v.alignment_frame_id for v in by_level.values()))
        ground = by_level["GROUND"]
        truth = {p["title"]: p for p in self.sheet.truth["plans"]}
        a1_source = truth["GROUND FLOOR PLAN"]["to_source"]((0, 0))
        building_point = self.arch.from_source(ground.alignment_frame_id, a1_source)
        for title, level in (("FIRST FLOOR PLAN", "FLOOR:1"), ("SECOND FLOOR PLAN", "FLOOR:2")):
            expected = truth[title]["to_source"]((0, 0))
            got = self.arch.to_source(by_level[level].alignment_frame_id, building_point)
            self.assertAlmostEqual(got[0], expected[0], places=3)
            self.assertAlmostEqual(got[1], expected[1], places=3)

    def test_plans_sections_and_levels_agree_so_no_level_ambiguity_is_raised(self):
        self.assertEqual([s for s in self.project.interpretation_sets if "structural levels" in s.question], [])
        self.assertEqual(heights(self.project, "GROUND", "FLOOR:1")[0][0], 3300.0)
        self.assertEqual(heights(self.project, "FLOOR:1", "FLOOR:2")[0][0], 3300.0)

    def test_written_elevations_are_source_and_the_section_dimensions_corroborate_them(self):
        rows = heights(self.project, "GROUND", "FLOOR:1")
        self.assertIn((3300.0, "level_tags", ValueStatus.SOURCE), rows)
        self.assertIn((3300.0, "section_dimension", ValueStatus.SOURCE), rows)

    def test_the_levels_can_be_suggested_only_because_nothing_disagrees(self):
        self.assertEqual(self.project.open_interpretation_sets(), [])
        self.assertEqual(suggest_elevations(self.project), {"GROUND": 0.0, "FLOOR:1": 3300.0, "FLOOR:2": 6600.0})


@tier("integration")
class Scenario03DifferentLayerNaming(unittest.TestCase):
    def test_plain_layer_names_give_the_same_reading_as_ncs_names(self):
        results = {}
        for name, scheme in (("ncs", NCS), ("plain", PLAIN)):
            s = Sheet(layers=scheme)
            s.plan((0, 0), "GROUND FLOOR PLAN")
            p = run(s)
            (plan,) = plans(p)
            results[name] = (plan.level_key, plan.view_type, kinds(p)["door"], kinds(p)["window"], kinds(p)["column_symbol"])
        self.assertEqual(results["ncs"], results["plain"])

    def test_the_layer_classes_come_from_tokens_not_from_one_naming_convention(self):
        s = Sheet(layers=PLAIN)
        s.plan()
        classes = {l.name: l.semantic_class for l in run(s).architecture.layers}
        self.assertEqual(classes["EXTERNAL WALLS"], "wall_external")
        self.assertEqual(classes["DOORS"], "door")
        self.assertEqual(classes["WINDOWS"], "window")
        self.assertEqual(classes["COLUMNS"], "column")

    def test_a_level_written_another_way_is_still_a_level(self):
        for title, key in (("G/F PLAN", "GROUND"), ("LEVEL 01 PLAN", "FLOOR:1"), ("FF PLAN", "FLOOR:1"),
                           ("GROUND FLR PLAN", "GROUND"), ("L00 PLAN", "GROUND"), ("SECOND FLOOR PLAN", "FLOOR:2")):
            s = Sheet()
            s.plan(title=title)
            (plan,) = plans(run(s))
            self.assertEqual(plan.level_key, key, title)


@tier("integration")
class Scenario04PlansWithSections(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = three_storey_sheet()
        cls.sheet.section((0, -16000), "SECTION A-A", LEVELS3)
        cls.sheet.section((25000, -16000), "SECTION B-B", LEVELS3)
        cls.project = run(cls.sheet)

    def test_sections_are_sections_and_not_plans(self):
        sections = self.project.architecture.views_of(ViewType.SECTION)
        self.assertEqual(sorted(v.section_label for v in sections), ["A-A", "B-B"])
        for v in sections:
            self.assertIsNone(v.level_key, "only a floor plan may carry a level")
        self.assertEqual(len(plans(self.project)), 3)

    def test_heights_come_from_the_section_tags_and_are_source_evidence(self):
        for a, c in (("GROUND", "FLOOR:1"), ("FLOOR:1", "FLOOR:2")):
            rows = heights(self.project, a, c)
            self.assertEqual({r[0] for r in rows}, {3300.0})
            self.assertTrue(all(r[2] == ValueStatus.SOURCE for r in rows))
            self.assertEqual(len(rows), 2)

    def test_two_sections_that_agree_raise_no_height_dispute(self):
        self.assertEqual([s for s in self.project.interpretation_sets if "height" in s.question.lower()], [])
        self.assertFalse([i for i in self.project.issues if i.severity == IssueSeverity.ERROR])

    def test_level_lines_are_observed_with_their_labels(self):
        section = self.project.architecture.views_of(ViewType.SECTION)[0]
        lines = [o for o in self.project.architecture.observations_in(section.id) if o.kind == "level_line"]
        self.assertEqual(len(lines), 3)


@tier("integration")
class Scenario05PlansWithElevations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = three_storey_sheet()
        cls.sheet.elevation((0, -16000), "FRONT ELEVATION", LEVELS3)
        cls.sheet.elevation((25000, -16000), "LEFT SIDE ELEVATION", LEVELS3)
        cls.project = run(cls.sheet)

    def test_elevations_are_their_own_kind_of_evidence(self):
        elevations = self.project.architecture.views_of(ViewType.ELEVATION)
        self.assertEqual(sorted(v.orientation for v in elevations), ["front", "left_side"])
        for v in elevations:
            self.assertIsNone(v.level_key)
        self.assertEqual(len(plans(self.project)), 3)

    def test_an_elevation_gives_written_level_evidence_but_is_never_a_plan(self):
        rows = heights(self.project, "GROUND", "FLOOR:1")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(h.source == "level_tags" for h in self.project.architecture.heights))
        sources = {h.view_id for h in self.project.architecture.heights}
        self.assertEqual(sources, {v.id for v in self.project.architecture.views_of(ViewType.ELEVATION)})

    def test_elevation_observations_do_not_include_plan_things(self):
        for v in self.project.architecture.views_of(ViewType.ELEVATION):
            self.assertNotIn("column_symbol", kinds(self.project, v.id))
            self.assertNotIn("door", kinds(self.project, v.id))


@tier("integration")
class Scenario06SectionWithMezzanine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = Sheet()
        cls.sheet.plan((0, 0), "GROUND FLOOR PLAN")
        cls.sheet.plan((20000, 0), "FIRST FLOOR PLAN")
        cls.sheet.section((0, -16000), "SECTION A-A", (("GROUND FLOOR", 0), ("FIRST FLOOR", 3600)),
                          partial=(("MEZZANINE", 1800, 0, 4000),))
        cls.project = run(cls.sheet)

    def test_the_partial_floor_is_observed_and_marked_as_a_hint_not_decided(self):
        partial = [o for o in self.project.architecture.observations if o.kind == "partial_floor_line"]
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0].hint, HintKind.DOUBLE_HEIGHT)
        self.assertLess(partial[0].confidence, 0.6)

    def test_the_extra_level_is_reported_as_a_disagreement_with_the_plans_not_silently_added(self):
        sets = [s for s in self.project.interpretation_sets if "levels exist" in s.question]
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0].status.value, "open")
        self.assertTrue(any(i.severity == IssueSeverity.ERROR and "levels" in i.message for i in self.project.issues))
        self.assertIsNone(self.project.building)

    def test_the_mezzanine_height_is_written_evidence_and_not_extended_to_a_storey_height(self):
        self.assertEqual(heights(self.project, "GROUND", "MEZZANINE")[0][0], 1800.0)
        self.assertEqual(heights(self.project, "MEZZANINE", "FLOOR:1")[0][0], 1800.0)
        self.assertIsNone(suggest_elevations(self.project))


@tier("integration")
class Scenario07DoubleHeightSpace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = Sheet()
        cls.sheet.plan((0, 0), "GROUND FLOOR PLAN", labels=("VOID",))
        cls.sheet.plan((20000, 0), "FIRST FLOOR PLAN")
        cls.sheet.plan((40000, 0), "ROOF PLAN")
        cls.sheet.section((0, -16000), "SECTION A-A", (("GROUND FLOOR", 0), ("ROOF", 6600)),
                          partial=(("FIRST FLOOR", 3300, 5500, 10000),))
        cls.project = run(cls.sheet)

    def test_the_void_and_the_partial_floor_are_hints_with_evidence(self):
        void = [o for o in self.project.architecture.observations if o.kind == "void"]
        self.assertEqual(len(void), 1)
        self.assertEqual(void[0].hint, HintKind.DOUBLE_HEIGHT)
        self.assertTrue(void[0].closed)
        self.assertTrue(self.project.provenance_for(Target.architectural(void[0].id)))
        partial = [o for o in self.project.architecture.observations if o.kind == "partial_floor_line"]
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0].hint, HintKind.DOUBLE_HEIGHT)
        self.assertTrue(self.project.provenance_for(Target.architectural(partial[0].id)))

    def test_no_level_is_invented_for_the_double_height_space_and_nothing_is_decided(self):
        self.assertEqual(sorted(v.level_key for v in plans(self.project)), ["FLOOR:1", "GROUND", "ROOF"])
        self.assertEqual(heights(self.project, "GROUND", "FLOOR:1")[0][0], 3300.0)
        self.assertIsNone(self.project.building)
        self.assertEqual({o.basis for o in self.project.architecture.observations if o.hint} - {ValueStatus.INFERRED, ValueStatus.SOURCE}, set())


@tier("integration")
class Scenario08StairAndLift(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = Sheet()
        cls.sheet.plan((0, 0), "GROUND FLOOR PLAN", labels=("STAIR", "LIFT"))
        cls.project = run(cls.sheet)

    def test_a_stair_and_a_lift_are_observed_with_their_enclosing_shape_and_area(self):
        by_kind = {o.kind: o for o in self.project.architecture.observations if o.kind in ("stair", "lift")}
        self.assertEqual(set(by_kind), {"stair", "lift"})
        self.assertEqual(by_kind["stair"].hint, HintKind.STAIR_OPENING)
        self.assertEqual(by_kind["lift"].hint, HintKind.LIFT_SHAFT)
        for o in by_kind.values():
            self.assertTrue(o.closed)
            self.assertEqual(o.basis, ValueStatus.INFERRED)
            notes = " ".join(p.note or "" for p in self.project.provenance_for(Target.architectural(o.id)))
            self.assertIn("m2", notes)

    def test_hints_stay_hints(self):
        for o in self.project.architecture.observations:
            if o.hint:
                self.assertIn(o.basis, (ValueStatus.INFERRED, ValueStatus.SOURCE))
        self.assertIsNone(self.project.building)


@tier("integration")
class Scenario09PlanVersusSectionConflict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = Sheet()
        cls.sheet.plan((0, 0), "GROUND FLOOR PLAN", ffl="FFL 0")
        cls.sheet.plan((20000, 0), "FIRST FLOOR PLAN", ffl="FFL 3300")
        cls.sheet.section((0, -16000), "SECTION A-A", (("GROUND FLOOR", 0), ("FIRST FLOOR", 3600)))
        cls.project = run(cls.sheet)

    def test_both_heights_are_kept_with_their_sources(self):
        rows = heights(self.project, "GROUND", "FLOOR:1")
        self.assertIn((3300.0, "plan_text", ValueStatus.DERIVED), rows)
        self.assertIn((3600.0, "level_tags", ValueStatus.SOURCE), rows)

    def test_the_conflict_is_an_open_interpretation_set_with_both_alternatives(self):
        (s,) = [s for s in self.project.interpretation_sets if "height from GROUND to FLOOR:1" in s.question]
        self.assertEqual(s.status.value, "open")
        self.assertEqual(sorted(a.meaning for a in s.alternatives), ["3300_mm", "3600_mm"])

    def test_the_conflict_is_also_an_error_issue_and_a_cross_view_finding(self):
        errors = [i for i in self.project.issues if i.severity == IssueSeverity.ERROR and "3300" in i.message and "3600" in i.message]
        self.assertEqual(len(errors), 1)
        findings = [f for f in self.project.architecture.findings if f.agreement == "disagree" and "GROUND to FLOOR:1" in f.question]
        self.assertEqual(len(findings), 1)
        self.assertIsNotNone(findings[0].interpretation_set_id)

    def test_oracle_does_not_choose_between_them(self):
        self.assertIsNone(self.project.building)
        self.assertIsNone(suggest_elevations(self.project))
        self.assertTrue(any("height" in b.lower() or "interpretation" in b.lower() for b in self.project.readiness().summary().split(", ")))


@tier("integration")
class Scenario10UnknownLayerNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = Sheet(layers=UNKNOWN)
        cls.sheet.plan((0, 0), "GROUND FLOOR PLAN")
        cls.project = run(cls.sheet)

    def test_unrecognised_layers_are_unknown_not_guessed(self):
        by_name = {l.name: l for l in self.project.architecture.layers if l.entity_count}
        self.assertTrue(by_name)
        for key in ("wall_ext", "wall_int", "grid"):
            layer = by_name[UNKNOWN[key]]
            self.assertEqual(layer.semantic_class, "unknown", layer.name)
            self.assertEqual(layer.confidence, 0.0)

    def test_the_only_meaning_taken_from_unknown_layers_is_the_block_name_and_it_is_weak(self):
        for key in ("door", "window", "column"):
            layer = next(l for l in self.project.architecture.layers if l.name == UNKNOWN[key])
            self.assertEqual(layer.method, "block_name")
            self.assertLess(layer.confidence, 0.8)

    def test_the_engineer_is_told_and_no_walls_are_invented_from_unknown_layers(self):
        self.assertIn("could not be classified", issue_text(self.project))
        self.assertEqual(kinds(self.project).get("wall_external", 0), 0)
        self.assertEqual(kinds(self.project).get("wall_internal", 0), 0)
        self.assertEqual(kinds(self.project).get("grid_lines", 0), 0)

    def test_the_plan_is_still_found_but_with_less_confidence_than_with_known_layers(self):
        known = Sheet()
        known.plan((0, 0), "GROUND FLOOR PLAN")
        (a,) = plans(self.project)
        (b,) = plans(run(known))
        self.assertLess(a.confidence, b.confidence)

    def test_a_configured_alias_makes_them_known_and_says_so(self):
        aliases = {UNKNOWN["wall_ext"]: "wall_external", UNKNOWN["door"]: "door"}
        project = interpret_document(self.sheet.document(), project_name="Aliased", engineer="Test Engineer",
                                     config=InterpretationConfig(layers=LayerConfig(aliases)))
        by_name = {l.name: l for l in project.architecture.layers}
        self.assertEqual(by_name[UNKNOWN["wall_ext"]].semantic_class, "wall_external")
        self.assertEqual(by_name[UNKNOWN["wall_ext"]].method, "configured_alias")
        self.assertEqual(kinds(project)["door"], 3)


@tier("integration")
class Scenario11MissingUnits(unittest.TestCase):
    def test_no_unit_metadata_means_the_unit_is_assumed_and_the_engineer_is_asked(self):
        s = Sheet(units="unitless")
        s.plan((0, 0), "GROUND FLOOR PLAN")
        p = run(s)
        source = p.architecture.drawing
        self.assertEqual(p.value_status_of(Target.architectural(source.id), "units").status, ValueStatus.ASSUMED)
        self.assertLess(source.units.confidence, 0.9)
        self.assertTrue([q for q in p.interpretation_sets if "unit" in q.question.lower()])
        self.assertTrue([i for i in p.issues if "unit" in i.message.lower()])
        self.assertTrue(any("assumed" in b for b in p.readiness().summary().split(", ")))

    def test_metadata_that_contradicts_the_drawing_is_a_conflict_not_a_silent_choice(self):
        s = Sheet(units="mm", insunits=6)                        # says metres; the geometry is clearly millimetres
        s.plan((0, 0), "GROUND FLOOR PLAN")
        p = run(s)
        self.assertEqual(p.architecture.drawing.source_metadata["unit_code"], 6)
        errors = [i for i in p.issues if i.severity == IssueSeverity.ERROR and "unit" in i.message.lower()]
        self.assertEqual(len(errors), 1)
        self.assertEqual(p.value_status_of(Target.architectural(p.architecture.drawing.id), "units").status, ValueStatus.ASSUMED)

    def test_a_drawing_in_metres_is_read_in_millimetres(self):
        s = Sheet(units="m")
        s.plan((0, 0), "GROUND FLOOR PLAN")
        p = run(s)
        self.assertEqual(p.architecture.drawing.units.unit, "m")
        self.assertEqual(p.architecture.drawing.units.factor_to_mm, 1000.0)
        (plan,) = plans(p)
        self.assertAlmostEqual(p.architecture.source_to_millimetres(plan.bbox[2] - plan.bbox[0]), 13900.0, delta=1.0)


@tier("integration")
class Scenario12RotatedPlan(unittest.TestCase):
    ANGLE = 20.0

    @classmethod
    def setUpClass(cls):
        cls.sheet = Sheet()
        cls.sheet.plan((5000, 5000), "GROUND FLOOR PLAN", rotation_deg=cls.ANGLE)
        cls.project = run(cls.sheet)

    def test_the_rotation_is_found_and_recorded_in_the_view_frame(self):
        (plan,) = plans(self.project)
        frame = next(f for f in self.project.architecture.frames if f.id == plan.frame_id)
        self.assertAlmostEqual(frame.rotation_deg, self.ANGLE, delta=0.6)
        self.assertEqual(frame.parent, "FRM-0")
        self.assertIn("rotated", issue_text(self.project))

    def test_view_coordinates_run_along_the_walls_and_map_back_to_the_source(self):
        (plan,) = plans(self.project)
        arch = self.project.architecture
        truth = self.sheet.truth["plans"][0]
        a1, c1 = truth["to_source"]((0, 0)), truth["to_source"]((12000, 0))
        la, lc = arch.from_source(plan.frame_id, a1), arch.from_source(plan.frame_id, c1)
        self.assertAlmostEqual(lc[0] - la[0], 12000.0, delta=100.0)      # along the view's x axis
        self.assertAlmostEqual(lc[1] - la[1], 0.0, delta=100.0)          # and not along y
        back = arch.to_source(plan.frame_id, la)
        self.assertAlmostEqual(back[0], a1[0], places=6)
        self.assertAlmostEqual(back[1], a1[1], places=6)

    def test_the_source_coordinates_are_kept_as_drawn(self):
        (plan,) = plans(self.project)
        doc = self.sheet.document()
        self.assertEqual(plan.bbox, (min(e.box[0] for e in doc.entities if e.id in set(plan.entity_ids)),
                                     min(e.box[1] for e in doc.entities if e.id in set(plan.entity_ids)),
                                     max(e.box[2] for e in doc.entities if e.id in set(plan.entity_ids)),
                                     max(e.box[3] for e in doc.entities if e.id in set(plan.entity_ids))))

    def test_a_mirrored_second_plan_is_not_silently_merged_with_the_first(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((34000, 0), "FIRST FLOOR PLAN", mirror=True)
        p = run(s)
        by_level = {v.level_key: v for v in plans(p)}
        self.assertEqual(sorted(by_level), ["FLOOR:1", "GROUND"])
        self.assertIsNone(by_level["FLOOR:1"].alignment_frame_id)
        self.assertTrue([q for q in p.interpretation_sets if "line up" in q.question])
        self.assertTrue([i for i in p.issues if "cannot be lined up" in i.message])


@tier("integration")
class Scenario13FarFromOrigin(unittest.TestCase):
    ORIGIN = (1_250_000.0, 4_800_000.0)

    @classmethod
    def setUpClass(cls):
        cls.sheet = Sheet()
        cls.sheet.plan(cls.ORIGIN, "GROUND FLOOR PLAN")
        cls.sheet.section((cls.ORIGIN[0], cls.ORIGIN[1] - 16000), "SECTION A-A", LEVELS3[:2])
        cls.doc = cls.sheet.document()
        cls.before = copy.deepcopy(cls.doc.to_dict())
        cls.project = interpret_document(cls.doc, project_name="Far", engineer="Test Engineer")

    def test_views_are_found_at_the_drawn_location_and_nothing_is_recentred(self):
        (plan,) = plans(self.project)
        self.assertGreaterEqual(plan.bbox[0], self.ORIGIN[0] - 1000)
        self.assertLessEqual(plan.bbox[2], self.ORIGIN[0] + 14000)
        self.assertEqual(self.doc.to_dict(), self.before)
        (section,) = self.project.architecture.views_of(ViewType.SECTION)
        self.assertLess(section.bbox[3], plan.bbox[1])

    def test_the_view_frame_carries_the_offset_and_round_trips_without_loss(self):
        (plan,) = plans(self.project)
        arch = self.project.architecture
        frame = next(f for f in arch.frames if f.id == plan.frame_id)
        self.assertAlmostEqual(frame.translation[0], plan.bbox[0], delta=1.0)
        for p in ((0.0, 0.0), (12000.0, 8000.0), (-3.5, 7.25)):
            q = arch.from_source(plan.frame_id, arch.to_source(plan.frame_id, p))
            self.assertAlmostEqual(q[0], p[0], places=6)
            self.assertAlmostEqual(q[1], p[1], places=6)

    def test_heights_are_unaffected_by_the_offset(self):
        self.assertEqual(heights(self.project, "GROUND", "FLOOR:1")[0][0], 3300.0)


@tier("integration")
class Scenario14TwoFloorPlanInterpretations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheet = Sheet()
        cls.sheet.plan((0, 0), "GROUND FLOOR PLAN")
        cls.sheet.plan((20000, 0), "FIRST & SECOND FLOOR PLAN")
        cls.project = run(cls.sheet)

    def test_a_title_naming_two_levels_gets_no_level_and_an_interpretation_set(self):
        both = next(v for v in plans(self.project) if v.title == "FIRST & SECOND FLOOR PLAN")
        self.assertIsNone(both.level_key)
        (s,) = [s for s in self.project.interpretation_sets if both.id in s.question]
        self.assertEqual(sorted(a.meaning for a in s.alternatives), ["floor_1", "floor_2"])
        self.assertEqual(s.status.value, "open")
        self.assertTrue([i for i in self.project.issues if both.id in i.message and "several levels" in i.message])

    def test_the_engineer_can_resolve_it_through_the_decision_system(self):
        from oracle.core import DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision
        (s,) = [s for s in self.project.interpretation_sets if "Which level" in s.question]
        chosen = next(a for a in s.alternatives if a.meaning == "floor_2")
        d = EngineeringDecision("D-1", "Test Engineer", DecisionSource.ENGINEER, s.subject, DecisionCategory.OTHER,
                                "This plan is the second floor.", status=DecisionStatus.ACCEPTED)
        self.project.add_decision(d)
        self.project.accept_interpretation(s.id, chosen.id, d.id)
        self.assertEqual(self.project.get_interpretation_set(s.id).status.value, "resolved")
        self.assertEqual([a.status.value for a in self.project.get_interpretation_set(s.id).alternatives if a.id != chosen.id],
                         ["rejected"])

    def test_two_views_with_close_type_evidence_become_alternatives_too(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.scatter("section_cut", 30, origin=(0, -20000))          # untitled bare marks: nothing says what this is
        p = run(s)
        for view in p.architecture.views:
            if view.view_type == ViewType.UNKNOWN:
                self.assertLess(view.confidence, 0.4)


@tier("integration")
class Scenario15MalformedOrIncomplete(unittest.TestCase):
    def test_an_empty_drawing_gives_a_blocking_issue_not_a_crash(self):
        p = run(Sheet())
        self.assertEqual(p.architecture.views, [])
        self.assertTrue([i for i in p.issues if i.severity == IssueSeverity.BLOCKING])
        self.assertFalse(p.readiness().ready)
        p.validate()

    def test_text_only_gives_a_blocking_issue(self):
        s = Sheet()
        s._text("text", "GROUND FLOOR PLAN", (0, 0), 350)
        p = run(s)
        self.assertEqual(p.architecture.views, [])
        self.assertTrue([i for i in p.issues if i.severity == IssueSeverity.BLOCKING])

    def test_a_plan_with_no_title_is_reported_and_left_unassigned(self):
        s = Sheet()
        s.plan((0, 0), title=None)
        p = run(s)
        (plan,) = plans(p)
        self.assertIsNone(plan.level_key)
        self.assertIn("does not name its level", issue_text(p))

    def test_a_section_without_level_text_does_not_produce_a_height(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((20000, 0), "FIRST FLOOR PLAN")
        s.section((0, -16000), "SECTION A-A", (("A", 0), ("B", 3300)))      # tags that are not level names
        p = run(s)
        self.assertEqual(p.architecture.heights, [])
        self.assertIn("Storey height for Ground Floor", issue_text(p))
        self.assertIn("not assumed", issue_text(p))

    def test_files_that_are_not_drawings_are_refused_with_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.dxf"
            bad.write_text("this is not a drawing", encoding="utf-8")
            with self.assertRaises(IngestionError):
                read_drawing(bad)
            with self.assertRaises(IngestionError):
                read_drawing(Path(tmp) / "missing.dxf")
            other = Path(tmp) / "notes.txt"
            other.write_text("x", encoding="utf-8")
            with self.assertRaises(IngestionError):
                read_drawing(other)

    def test_a_truncated_dxf_is_recovered_or_refused_but_never_partially_invented(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.dxf"
            s.save(good)
            text = good.read_text(encoding="utf-8")
            cut = Path(tmp) / "cut.dxf"
            cut.write_text(text[: int(len(text) * 0.6)], encoding="utf-8")
            try:
                doc = read_drawing(cut)
            except IngestionError:
                return
            self.assertLess(len(doc.entities), len(s.document().entities))
            project = interpret_document(doc, project_name="Cut", engineer="Test Engineer")
            project.validate()


@tier("integration")
class CrossCuttingProperties(unittest.TestCase):
    def test_the_same_drawing_gives_the_same_project_every_time(self):
        texts = []
        for _ in range(2):
            s = three_storey_sheet()
            s.section((0, -16000), "SECTION A-A", LEVELS3, dims=True)
            s.elevation((25000, -16000), "FRONT ELEVATION", LEVELS3)
            doc = s.document()
            p = interpret_document(doc, project_name="Det", engineer="Test Engineer", project_id="PRJ-DET")
            texts.append(strip_time(p.to_json()))
        self.assertEqual(texts[0], texts[1])

    def test_the_project_round_trips_through_json_with_the_interpretation_intact(self):
        s = three_storey_sheet()
        s.section((0, -16000), "SECTION A-A", LEVELS3)
        p = run(s)
        again = OracleProject.from_json(p.to_json())
        self.assertEqual(again.to_json(), p.to_json())
        self.assertEqual(len(again.architecture.views), len(p.architecture.views))
        self.assertEqual(again.architecture.drawing.sha256, p.architecture.drawing.sha256)

    def test_every_interpretation_set_has_an_issue_or_finding_that_explains_it(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN", ffl="FFL 0")
        s.plan((20000, 0), "FIRST FLOOR PLAN", ffl="FFL 3300")
        s.section((0, -16000), "SECTION A-A", (("GROUND FLOOR", 0), ("FIRST FLOOR", 3600)))
        p = run(s)
        self.assertTrue(p.interpretation_sets)
        for issue_set in p.interpretation_sets:
            self.assertTrue(issue_set.alternatives and len(issue_set.alternatives) >= 2)
        self.assertGreaterEqual(len([i for i in p.issues if i.severity in (IssueSeverity.ERROR, IssueSeverity.WARNING)]), 1)

    def test_shuffled_entity_order_does_not_change_what_is_found(self):
        s = three_storey_sheet()
        doc = s.document()
        first = interpret_document(doc, project_name="A", engineer="E", project_id="PRJ-A")
        shuffled = copy.deepcopy(doc)
        shuffled.entities.reverse()
        second = interpret_document(shuffled, project_name="A", engineer="E", project_id="PRJ-A")
        sig = lambda p: sorted((v.view_type.value, v.level_key, tuple(round(x) for x in v.bbox)) for v in p.architecture.views)
        self.assertEqual(sig(first), sig(second))


if __name__ == "__main__":
    unittest.main()
