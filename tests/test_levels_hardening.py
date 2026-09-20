"""Tests for the level model, the level value-status fix and engineer changes to levels (Phase 3.5)

Protects:
    That establishing levels records a value status ONLY for values that exist (storey heights are stored, derived, and
    absent for the top level); that an engineer can change a level's name, elevation, storey height and other fields
    through the ordinary decision path (previous value, supersession, value status, save/load, strict validation, and
    readiness all following), with the levels above moving together and every consequence recorded as its own
    decision; that a level's identity is not its label (an unfamiliar name keeps its own identity, and the drawing's
    label is preserved verbatim); and that finished-floor, structural and datum elevations are never converted into one
    another silently.

Test type:
    Unit and integration tests (synthetic drawings run through the real pipeline).

Dependencies:
    oracle.core, oracle.interpretation, tests.support35, tests.drawing_factory.
"""

import json
import unittest

from oracle.core import (
    BuildingModel, DecisionStatus, IssueSeverity, Level, OracleProject, Target, ValidationError, ValueStatus,
)
from oracle.interpretation import InterpretationConfig, establish_levels, interpret_document, suggest_elevations
from oracle.interpretation.naming import level_display_name, level_id_for
from tests.drawing_factory import Sheet
from tests.support35 import decision, drawing_sheet, plans, reviewed, with_levels
from tests.tiers import tier


def set_level(p, level_id, field, value, note="engineer decision"):
    t = Target.level(level_id)
    return p.set_value(t, field, value, decision(t, note, field=field, value=value))


def statuses(p, level_id):
    return {r.field: r for r in p.value_statuses if r.target == Target.level(level_id)}


@tier("integration")
class TheStatusBugStaysFixed(unittest.TestCase):
    """The old establish_levels() recorded storey_height_mm as DERIVED without ever storing a height."""

    def test_no_value_status_exists_for_a_value_that_does_not_exist(self):
        p = with_levels()
        for record in p.value_statuses:
            if record.target.scope.value != "level":
                continue
            value = p.building.get_level(record.target.id).to_full_dict()[record.field]
            self.assertIsNotNone(value, f"{record.target.id}.{record.field} has status {record.status.value} but no value")

    def test_storey_heights_are_stored_derived_and_real(self):
        p = with_levels()
        gf, l1, l2 = p.building.levels
        self.assertEqual((gf.storey_height_mm, l1.storey_height_mm, l2.storey_height_mm), (3300.0, 3300.0, None))
        self.assertEqual(statuses(p, "GF")["storey_height_mm"].status, ValueStatus.DERIVED)
        self.assertNotIn("storey_height_mm", statuses(p, "L2"), "the top level has no storey height, so no status either")

    def test_the_stored_height_is_the_gap_between_the_levels(self):
        p = with_levels()
        levels = p.building.levels
        for lo, hi in zip(levels, levels[1:]):
            self.assertAlmostEqual(lo.storey_height_mm, hi.elevation_mm - lo.elevation_mm, places=6)

    def test_the_result_survives_save_and_load_and_readiness_counts_only_real_assumptions(self):
        p = with_levels()
        text = p.to_json()
        again = OracleProject.from_json(text)
        self.assertEqual(again.to_json(), text)
        assumed = [r for r in again.values_with_status(ValueStatus.ASSUMED)]
        self.assertEqual(sorted((r.target.id, r.field) for r in assumed), [("GF", "name"), ("L1", "name"), ("L2", "name")])
        self.assertEqual(len(again.readiness().blockers), 3)

    def test_without_a_decision_elevations_are_only_derived(self):
        p = reviewed(drawing_project())
        establish_levels(p, suggest_elevations(p))
        self.assertEqual(statuses(p, "L1")["elevation_mm"].status, ValueStatus.DERIVED)
        self.assertEqual(p.evidence_links, [], "an evidence link needs an engineer decision, so none is created")


def drawing_project():
    return interpret_document(drawing_sheet().document(), project_name="L", engineer="Test Engineer")


@tier("integration")
class EngineerChangesToLevels(unittest.TestCase):
    def test_a_generated_name_is_confirmed_by_the_engineer_and_readiness_follows(self):
        p = with_levels()
        before = len(p.readiness().blockers)
        set_level(p, "L1", "name", "Level One")
        self.assertEqual(p.building.get_level("L1").name, "Level One")
        record = statuses(p, "L1")["name"]
        self.assertEqual((record.status, record.replaces), (ValueStatus.ENGINEER_OVERRIDE, ValueStatus.ASSUMED))
        self.assertEqual(len(p.readiness().blockers), before - 1)
        d = p.decision_history(Target.level("L1"), "name")[0]
        self.assertEqual((d.previous_value, d.value), ("First Floor", "Level One"))

    def test_the_project_becomes_ready_once_the_engineer_has_confirmed_everything(self):
        p = with_levels()
        for level in list(p.building.levels):
            set_level(p, level.id, "name", f"{level.name} (confirmed)")
        self.assertEqual(p.readiness().blockers, [])
        self.assertTrue(p.readiness().ready)

    def test_changing_an_elevation_moves_only_that_level_and_recomputes_derived_heights(self):
        p = with_levels()
        set_level(p, "L1", "elevation_mm", 3400.0)
        gf, l1, l2 = p.building.levels
        self.assertEqual((gf.elevation_mm, l1.elevation_mm, l2.elevation_mm), (0.0, 3400.0, 6600.0))
        self.assertEqual((gf.storey_height_mm, l1.storey_height_mm), (3400.0, 3200.0))
        self.assertEqual(statuses(p, "GF")["storey_height_mm"].status, ValueStatus.DERIVED)
        self.assertIn("was 3300", statuses(p, "GF")["storey_height_mm"].note)
        self.assertEqual(statuses(p, "L1")["elevation_mm"].status, ValueStatus.ENGINEER_OVERRIDE)

    def test_changing_a_storey_height_moves_the_levels_above_and_records_each_as_a_decision(self):
        p = with_levels()
        set_level(p, "GF", "storey_height_mm", 3600.0)
        self.assertEqual([(l.id, l.elevation_mm, l.storey_height_mm) for l in p.building.levels],
                         [("GF", 0.0, 3600.0), ("L1", 3600.0, 3300.0), ("L2", 6900.0, None)])
        moved = {d.target.id: d for d in p.decisions if d.id.endswith((".S1", ".S2"))}
        self.assertEqual({k: (v.previous_value, v.value) for k, v in moved.items()},
                         {"L1": (3300.0, 3600.0), "L2": (6600.0, 6900.0)})
        for d in moved.values():
            self.assertIn("Moved with", d.instruction)
            self.assertEqual(d.status, DecisionStatus.ACCEPTED)
        self.assertEqual(statuses(p, "L2")["elevation_mm"].status, ValueStatus.ENGINEER_OVERRIDE)

    def test_the_whole_history_survives_save_and_load(self):
        p = with_levels()
        set_level(p, "L1", "elevation_mm", 3400.0)
        set_level(p, "L1", "elevation_mm", 3350.0)
        set_level(p, "GF", "storey_height_mm", 3600.0)
        text = p.to_json()
        again = OracleProject.from_json(text)
        self.assertEqual(again.to_json(), text)
        history = [(d.id[-3:] if "." in d.id else "top", d.previous_value, d.value, d.status.value)
                   for d in again.decision_history(Target.level("L1"), "elevation_mm")]
        self.assertEqual([h[1:3] for h in history], [(3300.0, 3400.0), (3400.0, 3350.0), (3350.0, 3600.0)])
        self.assertEqual([h[3] for h in history], ["superseded", "superseded", "accepted"])
        self.assertEqual(again.building.get_level("L1").elevation_mm, 3600.0)

    def test_an_engineer_set_storey_height_is_never_recomputed_silently(self):
        p = with_levels()
        set_level(p, "GF", "storey_height_mm", 3300.0)              # now an engineer's own value
        before = p.to_json()
        with self.assertRaises(ValidationError) as caught:
            set_level(p, "L1", "elevation_mm", 3500.0)
        self.assertIn("engineer-set storey height", str(caught.exception))
        self.assertEqual(p.to_json(), before, "a refused change leaves the project exactly as it was")

    def test_invalid_level_changes_are_refused_and_change_nothing(self):
        p = with_levels()
        before = p.to_json()
        bad = [("L1", "elevation_mm", 0.0), ("L1", "name", "Ground Floor"), ("L1", "id", "X"), ("L1", "index", 5),
               ("L1", "nonsense", 1), ("L1", "elevation_type", "top_of_slab"), ("L1", "storey_height_mm", -1.0)]
        for level_id, field, value in bad:
            with self.assertRaises(ValidationError, msg=f"{field}={value!r}"):
                set_level(p, level_id, field, value)
        self.assertEqual(p.to_json(), before)

    def test_a_recommendation_cannot_change_a_level(self):
        from oracle.core import DecisionSource
        p = with_levels()
        t = Target.level("L1")
        rec = decision(t, field="name", value="X", source=DecisionSource.ORACLE, status=DecisionStatus.PROPOSED)
        with self.assertRaises(ValidationError):
            p.set_value(t, "name", "X", rec)

    def test_a_hand_edited_level_that_contradicts_its_decision_is_rejected_on_load(self):
        p = with_levels()
        set_level(p, "L1", "name", "Level One")
        data = json.loads(p.to_json())
        next(l for l in data["building"]["levels"] if l["id"] == "L1")["name"] = "Forged"
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_the_same_path_serves_the_elements_it_always_served(self):
        from tests.fixtures import make_project
        from tests.test_decision_authority import section
        p = make_project()
        t = Target.element("C5")
        p.set_value(t, "section", section(300, 300), decision(t, field="section", value=section(300, 300)))
        self.assertEqual(p.building.get_element("C5").section.width_mm, 300)


@tier("integration")
class LevelIdentityIsNotALabel(unittest.TestCase):
    def podium_project(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((20000, 0), "PODIUM PLAN")
        s.section((0, -16000), "SECTION A-A", (("GROUND FLOOR", 0), ("PODIUM", 4200)))
        config = InterpretationConfig(level_aliases={"PODIUM": "PODIUM"})
        return interpret_document(s.document(), project_name="Podium", engineer="Test Engineer", config=config)

    def test_an_unfamiliar_label_keeps_its_own_identity(self):
        p = self.podium_project()
        keys = sorted(v.level_key for v in plans(p))
        self.assertEqual(keys, ["GROUND", "NAMED:PODIUM"])
        self.assertNotIn("FLOOR:1", keys, "a podium is not silently the first floor")

    def test_height_evidence_and_suggested_levels_use_that_identity(self):
        p = self.podium_project()
        self.assertEqual([(h.from_level, h.to_level, h.height_mm) for h in p.architecture.heights
                          if h.source == "level_tags"], [("GROUND", "NAMED:PODIUM", 4200.0)])
        reviewed(p)
        self.assertEqual(suggest_elevations(p), {"GROUND": 0.0, "NAMED:PODIUM": 4200.0})

    def test_the_level_keeps_identity_engineer_label_and_the_drawings_own_label(self):
        p = self.podium_project()
        reviewed(p)
        establish_levels(p, suggest_elevations(p), decision(Target.project(), "levels"))
        podium = p.building.get_level("PODIUM")
        self.assertEqual((podium.key, podium.name, podium.source_label), ("NAMED:PODIUM", "Podium", "PODIUM PLAN"))
        set_level(p, "PODIUM", "name", "Podium Deck")
        again = p.building.get_level("PODIUM")
        self.assertEqual((again.id, again.name, again.source_label, again.key), ("PODIUM", "Podium Deck", "PODIUM PLAN", "NAMED:PODIUM"))

    def test_the_label_can_change_without_touching_identity_references(self):
        p = with_levels()
        from oracle.core import Node, Point2D
        p.building.add_node(Node("NX", "L1", Point2D(0, 0)))
        set_level(p, "L1", "name", "Mezzanine Deck")
        self.assertEqual(p.building.get_node("NX").level_id, "L1")

    def test_two_different_unfamiliar_labels_are_two_identities(self):
        self.assertEqual((level_id_for("NAMED:PODIUM"), level_id_for("NAMED:LOWER_TERRACE")), ("PODIUM", "LOWER_TERRACE"))
        self.assertEqual(level_display_name("NAMED:LOWER_TERRACE"), "Lower Terrace")

    def test_an_alias_may_still_name_a_standard_level(self):
        s = Sheet()
        s.plan((0, 0), "MAIN HALL PLAN")
        p = interpret_document(s.document(), project_name="Hall", engineer="E", config=InterpretationConfig(level_aliases={"MAIN HALL": "GROUND"}))
        self.assertEqual([v.level_key for v in plans(p)], ["GROUND"])

    def test_a_plan_with_an_unknown_label_can_be_given_a_named_level_by_the_engineer(self):
        s = Sheet()
        s.plan((0, 0), "TERRACE PLAN")
        p = interpret_document(s.document(), project_name="Terrace", engineer="E")
        (v,) = plans(p)
        self.assertIsNone(v.level_key)
        t = Target.architectural(v.id)
        p.set_value(t, "level_key", "NAMED:TERRACE", decision(t, field="level_key", value="NAMED:TERRACE"))
        self.assertEqual(p.architecture.get(v.id).level_key, "NAMED:TERRACE")
        with self.assertRaises(ValidationError):
            p.set_value(t, "level_key", "NAMED:", decision(t, field="level_key", value="NAMED:"))

    def test_the_level_model_still_reads_the_old_format(self):
        old = {"id": "GF", "name": "Ground Floor", "index": 0, "elevation_mm": 0.0, "storey_height_mm": 3000.0}
        level = Level.from_dict(old)
        self.assertEqual(level.to_dict(), old, "a level with no new fields serialises exactly as before")
        self.assertEqual((level.elevation_type, level.structural_elevation, level.key, level.source_label), ("unspecified", None, None, None))


@tier("integration")
class FinishedStructuralAndDatumElevations(unittest.TestCase):
    def test_a_finished_floor_elevation_is_not_a_structural_elevation(self):
        p = with_levels(elevation_type="finished_floor")
        for level in p.building.levels:
            self.assertEqual(level.elevation_type, "finished_floor")
            self.assertIsNone(level.structural_elevation, "nothing derives a structural level from a finished one")
        text = " ".join(i.message for i in p.issues)
        self.assertIn("structural elevation", text)
        warnings = [i for i in p.issues if "structural elevation" in i.message]
        self.assertEqual([i.severity for i in warnings], [IssueSeverity.WARNING])

    def test_an_unstated_type_claims_nothing(self):
        p = with_levels()
        self.assertEqual({l.elevation_type for l in p.building.levels}, {"unspecified"})
        self.assertNotIn("elevation_type", statuses(p, "L1"))
        self.assertIn("of unstated type", [i for i in p.issues if "structural elevation" in i.message][0].message)

    def test_claiming_a_type_without_a_decision_leaves_the_claim_assumed(self):
        p = reviewed(drawing_project())
        establish_levels(p, suggest_elevations(p), elevation_type="finished_floor")
        self.assertEqual(statuses(p, "GF")["elevation_type"].status, ValueStatus.ASSUMED)

    def test_the_engineer_establishes_a_structural_elevation_separately(self):
        p = with_levels(elevation_type="finished_floor")
        set_level(p, "L1", "structural_elevation_mm", 3150.0, "top of slab, from the structural drawing")
        level = p.building.get_level("L1")
        self.assertEqual((level.elevation_mm, level.elevation_type, level.structural_elevation), (3300.0, "finished_floor", 3150.0))
        self.assertEqual(statuses(p, "L1")["structural_elevation_mm"].status, ValueStatus.ENGINEER_DEFINED)
        self.assertIsNone(p.building.get_level("GF").structural_elevation)
        again = OracleProject.from_json(p.to_json())
        self.assertEqual(again.building.get_level("L1").structural_elevation, 3150.0)

    def test_a_structural_level_type_and_a_disagreeing_structural_value_cannot_coexist(self):
        with self.assertRaises(ValidationError):
            Level("X", "X", 100.0, elevation_type="structural", structural_elevation_mm=200.0)
        ok = Level("X", "X", 100.0, elevation_type="structural")
        self.assertEqual(ok.structural_elevation, 100.0)

    def test_a_datum_line_is_a_datum_not_a_floor(self):
        b = BuildingModel("B", "B")
        b.add_level(Level("NGL", "Natural Ground", -300.0, elevation_type="datum"))
        b.add_level(Level("GF", "Ground", 0.0, elevation_type="finished_floor"))
        self.assertEqual([(l.id, l.elevation_type, l.structural_elevation) for l in b.levels],
                         [("NGL", "datum", None), ("GF", "finished_floor", None)])

    def test_the_level_says_what_its_elevations_are_measured_from(self):
        p = with_levels()
        self.assertIn("relative to the lowest level", p.building.get_level("L1").datum)
        self.assertIn("no site datum applied", p.building.get_level("L1").datum)


if __name__ == "__main__":
    unittest.main()
