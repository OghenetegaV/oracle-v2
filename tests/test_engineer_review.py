"""Tests for the engineer's control over an architectural interpretation

Protects:
    That the engineer can accept, reject, modify, merge, split, rename, supply missing information and
    override what Oracle proposed, that EVERY such action is an accepted engineer decision in the existing
    decision system (so nothing is silently corrected), that Oracle and the AI assistant can only propose,
    that a refused action changes nothing, that the readiness gate follows the reviews, and that the
    reviewed project survives a save and reload.

Test type:
    Integration tests on synthetic drawings (tests/drawing_factory.py) run through the real pipeline.

Dependencies:
    oracle.core, oracle.interpretation, tests.drawing_factory.
"""

import unittest
from pathlib import Path

from oracle.core import (
    BlockerKind, DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision, OracleProject,
    ReviewStatus, Target, ValidationError, ValueStatus, ViewType,
)
from oracle.interpretation import (
    InterpretationConfig, align_view, establish_levels, interpret_document, suggest_elevations,
)
from oracle.interpretation.advisor import Advice
from tests.drawing_factory import UNKNOWN, Sheet, three_storey_sheet
from tests.tiers import tier

LEVELS3 = (("GROUND FLOOR", 0), ("FIRST FLOOR", 3300), ("SECOND FLOOR", 6600))
_counter = [0]


def decision(target, instruction="engineer decision", *, source=DecisionSource.ENGINEER, status=DecisionStatus.ACCEPTED,
             field=None, value=None, responds_to=None):
    _counter[0] += 1
    return EngineeringDecision(f"D-{_counter[0]:04d}", "Test Engineer", source, target, DecisionCategory.OTHER, instruction,
                               status=status, field=field, value=value, responds_to=responds_to)


def clean_project() -> OracleProject:
    s = three_storey_sheet()
    s.section((0, -16000), "SECTION A-A", LEVELS3)
    return interpret_document(s.document(), project_name="Review", engineer="Test Engineer")


def plans(p):
    return [v for v in p.architecture.views_of(ViewType.FLOOR_PLAN) if v.variant is None]


@tier("integration")
class AcceptAndReject(unittest.TestCase):
    def test_accepting_views_uses_one_engineer_decision_and_clears_their_blockers(self):
        p = clean_project()
        ids = [v.id for v in p.architecture.views if v.view_type in (ViewType.FLOOR_PLAN, ViewType.SECTION)]
        before = [b for b in p.readiness().blockers if b.kind == BlockerKind.UNREVIEWED_VIEW]
        self.assertEqual(len(before), len(ids))
        d = decision(Target.architectural(ids[0]), "These views are what the title says.")
        p.review_views(ids, d)
        self.assertEqual({p.architecture.get(i).review for i in ids}, {ReviewStatus.ACCEPTED})
        self.assertEqual(p.get_decision(d.id).source, DecisionSource.ENGINEER)
        self.assertEqual([b for b in p.readiness().blockers if b.kind == BlockerKind.UNREVIEWED_VIEW], [])
        status = p.value_status_of(Target.architectural(ids[0]), "review")
        self.assertEqual((status.status, status.decision_id), (ValueStatus.ENGINEER_DEFINED, d.id))

    def test_rejecting_a_view_keeps_it_and_records_why(self):
        p = clean_project()
        vid = plans(p)[0].id
        p.review_views([vid], decision(Target.architectural(vid), "Not a real plan."), accept=False)
        self.assertEqual(p.architecture.get(vid).review, ReviewStatus.REJECTED)
        self.assertIn(vid, [v.id for v in p.architecture.views])

    def test_oracle_or_ai_cannot_review_a_view(self):
        p = clean_project()
        vid = plans(p)[0].id
        for source in (DecisionSource.ORACLE, DecisionSource.AI_ASSISTANT):
            with self.assertRaises(ValidationError):
                p.review_views([vid], decision(Target.architectural(vid), source=source, status=DecisionStatus.PROPOSED))
        self.assertEqual(p.architecture.get(vid).review, ReviewStatus.PROPOSED)

    def test_a_review_with_one_bad_view_changes_nothing(self):
        p = clean_project()
        vid = plans(p)[0].id
        with self.assertRaises(ValidationError):
            p.review_views([vid, "VIEW-99"], decision(Target.architectural(vid)))
        self.assertEqual(p.architecture.get(vid).review, ReviewStatus.PROPOSED)


@tier("integration")
class ModifyRenameOverride(unittest.TestCase):
    def test_renaming_a_view_is_a_decision_with_history(self):
        p = clean_project()
        v = plans(p)[0]
        target = Target.architectural(v.id)
        d1 = decision(target, "Use the drawing register name.", field="title", value="GA-01 GROUND FLOOR")
        p.set_value(target, "title", "GA-01 GROUND FLOOR", d1)
        self.assertEqual(p.architecture.get(v.id).title, "GA-01 GROUND FLOOR")
        self.assertEqual(p.value_status_of(target, "title").status, ValueStatus.ENGINEER_DEFINED)
        d2 = decision(target, "Shorter.", field="title", value="GA-01")
        p.set_value(target, "title", "GA-01", d2)
        self.assertEqual(p.value_status_of(target, "title").status, ValueStatus.ENGINEER_OVERRIDE)
        self.assertEqual(p.get_decision(d1.id).status, DecisionStatus.SUPERSEDED)
        self.assertEqual(p.get_decision(d2.id).previous_value, "GA-01 GROUND FLOOR")

    def test_correcting_an_inferred_level_overrides_it_and_keeps_the_inference_in_the_record(self):
        p = clean_project()
        v = next(v for v in plans(p) if v.level_key == "FLOOR:2")
        target = Target.architectural(v.id)
        self.assertEqual(p.value_status_of(target, "level_key").status, ValueStatus.INFERRED)
        p.set_value(target, "level_key", "ROOF", decision(target, "This is the roof plan.", field="level_key", value="ROOF"))
        record = p.value_status_of(target, "level_key")
        self.assertEqual((record.status, record.replaces), (ValueStatus.ENGINEER_OVERRIDE, ValueStatus.INFERRED))
        self.assertEqual(p.architecture.get(v.id).level_key, "ROOF")
        self.assertTrue(p.provenance_for(target, "level_key"), "the original evidence is still on record")

    def test_supplying_a_missing_level_is_a_decision(self):
        s = Sheet()
        s.plan((0, 0), title=None)
        p = interpret_document(s.document(), project_name="Untitled", engineer="Test Engineer")
        (v,) = plans(p)
        self.assertIsNone(v.level_key)
        target = Target.architectural(v.id)
        self.assertIsNone(p.value_status_of(target, "level_key"))
        p.set_value(target, "level_key", "GROUND", decision(target, "It is the ground floor.", field="level_key", value="GROUND"))
        self.assertEqual(p.architecture.get(v.id).level_key, "GROUND")
        self.assertEqual(p.value_status_of(target, "level_key").status, ValueStatus.ENGINEER_DEFINED)

    def test_an_invalid_change_is_refused_and_nothing_changes(self):
        p = clean_project()
        section = p.architecture.views_of(ViewType.SECTION)[0]
        target = Target.architectural(section.id)
        with self.assertRaises(ValidationError):                                # a section cannot carry a level
            p.set_value(target, "level_key", "GROUND", decision(target, field="level_key", value="GROUND"))
        with self.assertRaises(ValidationError):                                # not a level key
            v = plans(p)[0]
            t = Target.architectural(v.id)
            p.set_value(t, "level_key", "Level Ground", decision(t, field="level_key", value="Level Ground"))
        self.assertIsNone(p.architecture.get(section.id).level_key)
        self.assertEqual(p.architecture.get(plans(p)[0].id).level_key, "GROUND")

    def test_a_recommendation_is_not_a_change(self):
        p = clean_project()
        v = plans(p)[0]
        target = Target.architectural(v.id)
        with self.assertRaises(ValidationError):
            p.set_value(target, "title", "X", decision(target, source=DecisionSource.ORACLE, status=DecisionStatus.PROPOSED,
                                                       field="title", value="X"))
        self.assertNotEqual(p.architecture.get(v.id).title, "X")


@tier("integration")
class MergeAndSplit(unittest.TestCase):
    def two_halves(self):
        """One ground plan whose right-hand part was drawn far enough away to be found as a separate, untitled view."""
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((30000, 0), None, labels=("STAIR",))
        return interpret_document(s.document(), project_name="Halves", engineer="Test Engineer")

    def test_merging_supersedes_the_originals_and_keeps_their_observations(self):
        p = self.two_halves()
        a, b = sorted(plans(p), key=lambda v: v.bbox[0])
        observations_before = len(p.architecture.observations)
        d = decision(Target.architectural(a.id), "These are one plan.")
        merged = p.merge_views([a.id, b.id], "VIEW-90", d)
        self.assertEqual(merged.level_key, "GROUND")
        self.assertEqual(merged.review, ReviewStatus.ACCEPTED)
        self.assertEqual(p.architecture.get(a.id).review, ReviewStatus.SUPERSEDED)
        self.assertEqual(p.architecture.get(a.id).superseded_by, ("VIEW-90",))
        self.assertEqual(set(merged.entity_ids), set(a.entity_ids) | set(b.entity_ids))
        self.assertEqual(len(p.architecture.observations), observations_before)
        self.assertTrue(all(o.view_id == "VIEW-90" for o in p.architecture.observations))
        self.assertIsNone(merged.frame_id)                                     # the interpreter must derive frames again
        p.validate()

    def test_views_of_different_levels_or_types_are_not_merged(self):
        s = three_storey_sheet()
        s.section((0, -16000), "SECTION A-A", LEVELS3)
        p = interpret_document(s.document(), project_name="No merge", engineer="Test Engineer")
        ground, first = sorted(plans(p), key=lambda v: v.bbox[0])[:2]
        section = p.architecture.views_of(ViewType.SECTION)[0]
        for ids in ([ground.id, first.id], [ground.id, section.id]):
            with self.assertRaises(ValidationError):
                p.merge_views(ids, "VIEW-91", decision(Target.architectural(ids[0])))
        self.assertEqual(p.architecture.get(ground.id).review, ReviewStatus.PROPOSED)

    def test_splitting_needs_the_parts_to_cover_the_original_exactly(self):
        p = self.two_halves()
        v = sorted(plans(p), key=lambda v: v.bbox[0])[0]
        half = len(v.entity_ids) // 2
        parts = {"VIEW-81": {"bbox": v.bbox, "entity_ids": list(v.entity_ids[:half])},
                 "VIEW-82": {"bbox": v.bbox, "entity_ids": list(v.entity_ids[half:])}}
        with self.assertRaises(ValidationError):                                    # drops an entity
            p.split_view(v.id, {"VIEW-81": parts["VIEW-81"], "VIEW-82": {"bbox": v.bbox, "entity_ids": list(v.entity_ids[half + 1:])}},
                         decision(Target.architectural(v.id)))
        self.assertEqual(p.architecture.get(v.id).review, ReviewStatus.PROPOSED)
        owned = [o.id for o in p.architecture.observations_in(v.id)]
        made = p.split_view(v.id, parts, decision(Target.architectural(v.id), "Two drawings on one spot."))
        self.assertEqual([m.id for m in made], ["VIEW-81", "VIEW-82"])
        self.assertEqual(p.architecture.get(v.id).review, ReviewStatus.SUPERSEDED)
        self.assertEqual(p.architecture.get(v.id).superseded_by, ("VIEW-81", "VIEW-82"))
        self.assertEqual(p.architecture.observations_in(v.id), [])
        self.assertTrue(all(p.architecture.get(i).view_id in ("VIEW-81", "VIEW-82") for i in owned))
        p.validate()


@tier("integration")
class SupplyingInformation(unittest.TestCase):
    def test_levels_are_not_created_until_an_engineer_decision_says_so(self):
        p = clean_project()
        elevations = suggest_elevations(p)
        self.assertEqual(elevations, {"GROUND": 0.0, "FLOOR:1": 3300.0, "FLOOR:2": 6600.0})
        self.assertIsNone(p.building)
        d = decision(Target.project(), "Take the levels from the section.")
        levels = establish_levels(p, elevations, d)
        self.assertEqual([(l.id, l.elevation_mm) for l in levels], [("GF", 0.0), ("L1", 3300.0), ("L2", 6600.0)])
        record = p.value_status_of(Target.level("L1"), "elevation_mm")
        self.assertEqual((record.status, record.decision_id), (ValueStatus.ENGINEER_DEFINED, d.id))
        self.assertEqual(p.value_status_of(Target.level("L1"), "name").status, ValueStatus.ASSUMED)   # a generated name is flagged
        p.validate()

    def test_a_recommendation_or_a_field_change_cannot_create_levels(self):
        p = clean_project()
        for d in (decision(Target.project(), source=DecisionSource.ORACLE, status=DecisionStatus.PROPOSED),
                  decision(Target.level("GF"), field="elevation_mm", value=1.0)):
            with self.assertRaises(ValidationError):
                establish_levels(p, {"GROUND": 0.0, "FLOOR:1": 3300.0}, d)
        self.assertIsNone(p.building)

    def test_an_engineer_can_align_plans_oracle_could_not_and_the_alternative_is_resolved(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((34000, 0), "FIRST FLOOR PLAN", mirror=True)
        p = interpret_document(s.document(), project_name="Mirror", engineer="Test Engineer")
        first = next(v for v in plans(p) if v.level_key == "FLOOR:1")
        self.assertIsNone(first.alignment_frame_id)
        (aset,) = [x for x in p.interpretation_sets if "line up" in x.question]
        chosen = aset.ranked()[0]
        d = align_view(p, first.id, (0.0, 0.0), engineer="Test Engineer", reason="Checked against the grid on the drawing.",
                       set_id=aset.id, interpretation_id=chosen.id)
        again = p.architecture.get(first.id)
        self.assertIsNotNone(again.alignment_frame_id)
        self.assertEqual(p.get_decision(d.id).source, DecisionSource.ENGINEER)
        self.assertEqual(p.get_interpretation_set(aset.id).status.value, "resolved")
        self.assertEqual(p.value_status_of(Target.architectural(first.id), "alignment_frame_id").status, ValueStatus.ENGINEER_DEFINED)
        frame = next(f for f in p.architecture.frames if f.id == again.alignment_frame_id)
        self.assertEqual(frame.translation, (-0.0, -0.0))
        p.validate()

    def test_a_refused_alignment_leaves_no_frame_behind(self):
        p = clean_project()
        section = p.architecture.views_of(ViewType.SECTION)[0]
        frames_before = len(p.architecture.frames)
        with self.assertRaises(ValidationError):
            align_view(p, section.id, (1.0, 1.0), engineer="Test Engineer", reason="x")
        with self.assertRaises(ValidationError):
            align_view(p, plans(p)[0].id, (1.0, 1.0), engineer="Test Engineer", reason="x", set_id="IS-999", interpretation_id="INT-1")
        self.assertEqual(len(p.architecture.frames), frames_before)


@tier("integration")
class ConflictsStayOpenUntilTheEngineerDecides(unittest.TestCase):
    def setUp(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN", ffl="FFL 0")
        s.plan((20000, 0), "FIRST FLOOR PLAN", ffl="FFL 3300")
        s.section((0, -16000), "SECTION A-A", (("GROUND FLOOR", 0), ("FIRST FLOOR", 3600)))
        self.p = interpret_document(s.document(), project_name="Conflict", engineer="Test Engineer")

    def test_the_conflicting_heights_block_readiness_until_resolved_by_a_decision(self):
        (s,) = [x for x in self.p.interpretation_sets if "height from GROUND to FLOOR:1" in x.question]
        self.assertIn(BlockerKind.OPEN_INTERPRETATION, {b.kind for b in self.p.readiness().blockers})
        chosen = next(a for a in s.alternatives if a.meaning == "3600_mm")
        d = decision(Target.architectural("SRC-1"), "The section governs; the plan FFL text is out of date.")
        self.p.add_decision(d)
        self.p.accept_interpretation(s.id, chosen.id, d.id)
        after = self.p.get_interpretation_set(s.id)
        self.assertEqual(after.status.value, "resolved")
        self.assertEqual([a.status.value for a in after.alternatives if a.id != chosen.id], ["rejected"])
        self.assertEqual(after.accepted.decision_id, d.id)

    def test_oracle_cannot_resolve_the_conflict(self):
        (s,) = [x for x in self.p.interpretation_sets if "height from GROUND to FLOOR:1" in x.question]
        rec = decision(Target.architectural("SRC-1"), source=DecisionSource.ORACLE, status=DecisionStatus.PROPOSED)
        self.p.add_decision(rec)
        with self.assertRaises(ValidationError):
            self.p.accept_interpretation(s.id, s.alternatives[0].id, rec.id)
        self.assertEqual(self.p.get_interpretation_set(s.id).status.value, "open")


@tier("integration")
class AiAssistanceIsBounded(unittest.TestCase):
    class Guesser:
        def __init__(self):
            self.asked = []

        def advise(self, layer_name, sample):
            self.asked.append(layer_name)
            return [Advice("wall_external", 0.7, "the entities are long parallel lines")]

    def test_advice_is_only_ever_a_proposed_ai_recommendation(self):
        s = Sheet(layers=UNKNOWN)
        s.plan()
        guesser = self.Guesser()
        p = interpret_document(s.document(), project_name="AI", engineer="Test Engineer",
                               config=InterpretationConfig(advisor=guesser))
        self.assertTrue(guesser.asked)
        self.assertNotIn("Defpoints", guesser.asked)
        advice = [d for d in p.decisions if d.source == DecisionSource.AI_ASSISTANT]
        self.assertTrue(advice)
        self.assertEqual({d.status for d in advice}, {DecisionStatus.PROPOSED})
        by_name = {l.name: l for l in p.architecture.layers}
        self.assertEqual(by_name[UNKNOWN["wall_ext"]].semantic_class, "unknown")      # the classification did not change
        self.assertEqual(sum(1 for o in p.architecture.observations if o.kind == "wall_external"), 0)

    def test_the_engineer_can_accept_the_advice_through_set_value(self):
        s = Sheet(layers=UNKNOWN)
        s.plan()
        p = interpret_document(s.document(), project_name="AI", engineer="Test Engineer",
                               config=InterpretationConfig(advisor=self.Guesser()))
        layer = next(l for l in p.architecture.layers if l.name == UNKNOWN["wall_ext"])
        ai = next(d for d in p.decisions if d.source == DecisionSource.AI_ASSISTANT and d.target == Target.architectural(layer.id))
        target = Target.architectural(layer.id)
        p.set_value(target, "semantic_class", "wall_external",
                    decision(target, "Confirmed: these are the external walls.", field="semantic_class", value="wall_external",
                             responds_to=ai.id))
        self.assertEqual(p.architecture.get(layer.id).semantic_class, "wall_external")
        self.assertNotEqual(p.get_decision(ai.id).status, DecisionStatus.PROPOSED)       # the recommendation was answered
        self.assertEqual(p.value_status_of(target, "semantic_class").status, ValueStatus.ENGINEER_OVERRIDE)

    def test_the_advisor_module_makes_no_network_call_and_needs_no_ai_client(self):
        import oracle.interpretation.advisor as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        for word in ("anthropic", "requests", "urllib", "socket", "http"):
            self.assertNotIn(f"import {word}", source)


@tier("integration")
class Persistence(unittest.TestCase):
    def test_a_reviewed_project_reloads_exactly(self):
        p = clean_project()
        vid = plans(p)[0].id
        p.review_views([vid], decision(Target.architectural(vid), "OK."))
        t = Target.architectural(vid)
        p.set_value(t, "title", "GA-01", decision(t, field="title", value="GA-01"))
        text = p.to_json()
        again = OracleProject.from_json(text)
        self.assertEqual(again.to_json(), text)
        self.assertEqual(again.architecture.get(vid).title, "GA-01")
        self.assertEqual(again.architecture.get(vid).review, ReviewStatus.ACCEPTED)
        self.assertEqual(again.readiness().summary(), p.readiness().summary())


if __name__ == "__main__":
    unittest.main()
