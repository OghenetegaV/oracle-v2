"""Tests for structured interpretation resolution (Phase 3.5)

Protects:
    That accepting an alternative of an interpretation set actually DOES what its machine-readable effects say: an
    accepted height becomes engineer-defined height evidence that the level suggestion uses, an accepted unit becomes the
    drawing's effective unit and clears the assumption, an accepted alignment creates the frame and sets the view, an
    accepted level or view type changes the view, merges and splits are carried out; that the engineer decision, the
    resolved set, the rejected siblings, the model change, its value status and history, and the linked issue all agree
    afterwards; that rejecting an alternative mutates nothing; that a refused effect leaves the project exactly as it was;
    that everything survives save and load; and that a forged or inconsistent payload is rejected on load.

Test type:
    Integration tests on synthetic drawings run through the real pipeline, plus hand-built sets for the generic mechanism.

Dependencies:
    oracle.core, oracle.interpretation, tests.support35, tests.drawing_factory.
"""

import copy
import json
import unittest

from oracle.core import (
    DecisionSource, DecisionStatus, Effect, EffectKind, Interpretation, InterpretationSet, IssueSeverity, OracleProject,
    SetStatus, Target, ValidationError, ValueStatus, ViewType,
)
from oracle.interpretation import align_view, interpret_document, suggest_elevations
from tests.drawing_factory import Sheet
from tests.support35 import decision, plans
from tests.tiers import tier


def conflict_project():
    s = Sheet()
    s.plan((0, 0), "GROUND FLOOR PLAN", ffl="FFL 0")
    s.plan((20000, 0), "FIRST FLOOR PLAN", ffl="FFL 3300")
    s.section((0, -16000), "SECTION A-A", (("GROUND FLOOR", 0), ("FIRST FLOOR", 3600)))
    return interpret_document(s.document(), project_name="Conflict", engineer="Test Engineer")


def the_set(project, text):
    (found,) = [s for s in project.interpretation_sets if text in s.question]
    return found


def linked_issues(project, s):
    return [i for i in project.issues if i.interpretation_set_id == s.id]


def accept(project, s, meaning, note="the engineer decides"):
    alt = next(a for a in s.alternatives if a.meaning == meaning)
    d = decision(Target.architectural(project.architecture.drawing.id), note)
    project.add_decision(d)
    project.accept_interpretation(s.id, alt.id, d.id)
    return alt, d


@tier("integration")
class AcceptedHeight(unittest.TestCase):
    def setUp(self):
        self.p = conflict_project()
        self.s = the_set(self.p, "height from GROUND to FLOOR:1")

    def test_the_alternatives_carry_their_meaning_as_data(self):
        for alt in self.s.alternatives:
            (effect,) = alt.effects
            self.assertEqual(effect.kind, EffectKind.ACCEPT_HEIGHT)
        heights = sorted(a.effects[0].params["height_mm"] for a in self.s.alternatives)
        self.assertEqual(heights, [3300.0, 3600.0])

    def test_accepting_a_height_produces_the_accepted_height(self):
        self.assertIsNone(suggest_elevations(self.p), "unresolved: nothing is suggested")
        alt, d = accept(self.p, self.s, "3600_mm")
        accepted = [h for h in self.p.architecture.heights if h.basis == ValueStatus.ENGINEER_DEFINED]
        self.assertEqual([(h.from_level, h.to_level, h.height_mm, h.source) for h in accepted],
                         [("GROUND", "FLOOR:1", 3600.0, "engineer_resolution")])
        self.assertEqual(self.p.value_status_of(Target.architectural(accepted[0].id), "height_mm").decision_id, d.id)
        self.assertEqual(alt.applied, ({"kind": "accept_height", "created": [accepted[0].id]},))

    def test_the_choice_is_what_the_level_suggestion_then_uses(self):
        accept(self.p, self.s, "3300_mm")
        self.assertEqual(suggest_elevations(self.p), {"GROUND": 0.0, "FLOOR:1": 3300.0})

    def test_the_set_is_resolved_and_the_siblings_are_rejected_under_the_same_decision(self):
        alt, d = accept(self.p, self.s, "3600_mm")
        now = self.p.get_interpretation_set(self.s.id)
        self.assertEqual(now.status, SetStatus.RESOLVED)
        self.assertEqual(now.accepted.id, alt.id)
        self.assertEqual({a.decision_id for a in now.alternatives}, {d.id})
        self.assertEqual([a.status.value for a in now.alternatives if a.id != alt.id], ["rejected"])
        self.assertEqual(self.p.open_interpretation_sets(), [])

    def test_the_linked_issue_is_no_longer_open_and_says_why(self):
        (issue,) = linked_issues(self.p, self.s)
        self.assertEqual((issue.severity, issue.is_open), (IssueSeverity.ERROR, True))
        alt, d = accept(self.p, self.s, "3600_mm")
        (issue,) = linked_issues(self.p, self.s)
        self.assertFalse(issue.is_open)
        self.assertEqual(issue.decision_id, d.id)
        self.assertIn(alt.id, issue.resolution)

    def test_the_conflicting_evidence_is_kept_not_deleted(self):
        accept(self.p, self.s, "3600_mm")
        sources = {(h.source, h.height_mm) for h in self.p.architecture.heights if (h.from_level, h.to_level) == ("GROUND", "FLOOR:1")}
        self.assertEqual(sources, {("plan_text", 3300.0), ("level_tags", 3600.0), ("engineer_resolution", 3600.0)})

    def test_an_unrelated_set_and_issue_are_untouched(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((20000, 0), "FIRST & SECOND FLOOR PLAN")
        s.plan((40000, 0), "ROOF PLAN", ffl="FFL 9000")
        p = interpret_document(s.document(), project_name="Two", engineer="E")
        others_before = [(x.id, x.status.value) for x in p.interpretation_sets if "Which level" not in x.question]
        level_set = the_set(p, "Which level")
        accept(p, level_set, "floor_2")
        self.assertEqual([(x.id, x.status.value) for x in p.interpretation_sets if "Which level" not in x.question], others_before)
        self.assertEqual([i.is_open for i in p.issues if i.interpretation_set_id != level_set.id and i.severity == IssueSeverity.WARNING][:1], [True])

    def test_it_survives_save_and_load(self):
        alt, _d = accept(self.p, self.s, "3600_mm")
        text = self.p.to_json()
        again = OracleProject.from_json(text)
        self.assertEqual(again.to_json(), text)
        self.assertEqual(again.get_interpretation_set(self.s.id).accepted.applied, alt.applied)
        self.assertEqual(suggest_elevations(again), {"GROUND": 0.0, "FLOOR:1": 3600.0})
        self.assertFalse(linked_issues(again, self.s)[0].is_open)


@tier("integration")
class RejectingChangesNothing(unittest.TestCase):
    def test_rejecting_an_alternative_does_not_touch_the_model_or_the_issue(self):
        p = conflict_project()
        s = the_set(p, "height from GROUND to FLOOR:1")
        before = copy.deepcopy(p.to_dict())
        alt = next(a for a in s.alternatives if a.meaning == "3300_mm")
        d = decision(Target.architectural("SRC-1"), "not this one")
        p.add_decision(d)
        p.reject_interpretation(s.id, alt.id, d.id)
        after = p.to_dict()
        self.assertEqual(after["architectures"], before["architectures"])
        self.assertEqual(after["issues"], before["issues"])
        self.assertEqual(after["value_status"], before["value_status"])
        self.assertEqual(p.get_interpretation_set(s.id).status, SetStatus.OPEN)
        self.assertTrue(linked_issues(p, s)[0].is_open)
        self.assertIsNone(suggest_elevations(p))

    def test_rejecting_every_alternative_still_changes_nothing(self):
        p = conflict_project()
        s = the_set(p, "height from GROUND to FLOOR:1")
        d = decision(Target.architectural("SRC-1"), "neither")
        p.add_decision(d)
        for a in s.alternatives:
            p.reject_interpretation(s.id, a.id, d.id)
        self.assertEqual(p.get_interpretation_set(s.id).status, SetStatus.NONE_APPLY)
        self.assertEqual([h for h in p.architecture.heights if h.basis == ValueStatus.ENGINEER_DEFINED], [])
        self.assertTrue(linked_issues(p, s)[0].is_open, "no answer was given, so the issue stands")

    def test_oracle_cannot_resolve_anything(self):
        p = conflict_project()
        s = the_set(p, "height from GROUND to FLOOR:1")
        rec = decision(Target.architectural("SRC-1"), source=DecisionSource.ORACLE, status=DecisionStatus.PROPOSED)
        p.add_decision(rec)
        with self.assertRaises(ValidationError):
            p.accept_interpretation(s.id, s.alternatives[0].id, rec.id)
        self.assertEqual(p.get_interpretation_set(s.id).status, SetStatus.OPEN)


@tier("integration")
class AcceptedUnit(unittest.TestCase):
    def project(self, insunits):
        s = Sheet(units="mm", insunits=insunits)
        s.plan((0, 0), "GROUND FLOOR PLAN")
        return interpret_document(s.document(), project_name="Units", engineer="E")

    def test_accepting_a_unit_changes_the_effective_unit_and_clears_the_assumption(self):
        p = self.project(6)                                        # says metres; drawn in millimetres
        s = the_set(p, "What unit is the drawing in?")
        src = Target.architectural("SRC-1")
        self.assertEqual(p.value_status_of(src, "units").status, ValueStatus.ASSUMED)
        self.assertIn("assumed_value", [b.kind.value for b in p.readiness().blockers])
        accept(p, s, "mm")
        units = p.architecture.drawing.units
        self.assertEqual((units.unit, units.factor_to_mm, units.confidence, units.method), ("mm", 1.0, 1.0, "engineer_decision"))
        record = p.value_status_of(src, "units")
        self.assertEqual((record.status, record.replaces), (ValueStatus.ENGINEER_OVERRIDE, ValueStatus.ASSUMED))
        self.assertNotIn("assumed_value", [b.kind.value for b in p.readiness().blockers])
        self.assertFalse(linked_issues(p, s)[0].is_open)

    def test_accepting_another_unit_changes_what_source_numbers_mean(self):
        p = self.project(6)
        s = the_set(p, "What unit is the drawing in?")
        before = p.architecture.source_to_millimetres(10.0)
        other = next(a for a in s.alternatives if a.meaning != "mm")
        accept(p, s, other.meaning)
        self.assertNotEqual(p.architecture.source_to_millimetres(10.0), before)
        self.assertEqual(p.architecture.drawing.units.unit, other.meaning)

    def test_the_unit_decision_is_in_the_value_history_and_survives_reload(self):
        p = self.project(6)
        accept(p, the_set(p, "What unit is the drawing in?"), "mm")
        history = p.decision_history(Target.architectural("SRC-1"), "units")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].previous_value["unit"], "mm")
        again = OracleProject.from_json(p.to_json())
        self.assertEqual(again.architecture.drawing.units.method, "engineer_decision")
        self.assertEqual(again.to_json(), p.to_json())


@tier("integration")
class AcceptedAlignmentAndLevelAndViewType(unittest.TestCase):
    def mirrored(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((34000, 0), "FIRST FLOOR PLAN", mirror=True)
        return interpret_document(s.document(), project_name="Mirror", engineer="E")

    def test_accepting_an_alignment_creates_the_frame_and_sets_the_view(self):
        p = self.mirrored()
        s = the_set(p, "line up")
        first = next(v for v in plans(p) if v.level_key == "FLOOR:1")
        self.assertIsNone(first.alignment_frame_id)
        alt = s.ranked()[0]
        d = decision(Target.architectural("SRC-1"), "align them")
        p.add_decision(d)
        p.accept_interpretation(s.id, alt.id, d.id)
        now = p.architecture.get(first.id)
        self.assertIsNotNone(now.alignment_frame_id)
        frame = next(f for f in p.architecture.frames if f.id == now.alignment_frame_id)
        expected = alt.effects[0].params["translation"]
        self.assertEqual(frame.translation, (-expected[0], -expected[1]))
        self.assertEqual(p.value_status_of(Target.architectural(first.id), "alignment_frame_id").status, ValueStatus.ENGINEER_DEFINED)
        self.assertFalse(linked_issues(p, s)[0].is_open)
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())

    def test_an_engineers_own_translation_is_not_overwritten_by_the_alternatives(self):
        p = self.mirrored()
        s = the_set(p, "line up")
        first = next(v for v in plans(p) if v.level_key == "FLOOR:1")
        align_view(p, first.id, (123.0, 456.0), engineer="E", reason="measured", set_id=s.id, interpretation_id=s.ranked()[0].id)
        frame = next(f for f in p.architecture.frames if f.id == p.architecture.get(first.id).alignment_frame_id)
        self.assertEqual(frame.translation, (-123.0, -456.0))
        self.assertEqual(p.get_interpretation_set(s.id).accepted.applied[0]["kind"], "skipped")
        self.assertFalse(linked_issues(p, s)[0].is_open)
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())

    def test_accepting_which_level_a_plan_shows_sets_the_level(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((20000, 0), "FIRST & SECOND FLOOR PLAN")
        p = interpret_document(s.document(), project_name="Both", engineer="E")
        both = next(v for v in plans(p) if v.title == "FIRST & SECOND FLOOR PLAN")
        q = the_set(p, "Which level")
        accept(p, q, "floor_2")
        self.assertEqual(p.architecture.get(both.id).level_key, "FLOOR:2")
        self.assertEqual(p.value_status_of(Target.architectural(both.id), "level_key").status, ValueStatus.ENGINEER_DEFINED)
        self.assertFalse(linked_issues(p, q)[0].is_open)

    def hand_set(self, p, effects_by_meaning, subject=None, set_id="IS-900"):
        alts = [Interpretation(f"INT-{int(set_id[3:]) * 10 + i}", m, 0.5 - i / 100, effects=tuple(e)) for i, (m, e) in enumerate(effects_by_meaning.items())]
        p.add_interpretation_set(InterpretationSet(set_id, "A hand-built question?", alts, (), subject))
        return p.get_interpretation_set(set_id)

    def test_accepting_a_view_type_changes_the_view(self):
        p = conflict_project()
        section = p.architecture.views_of(ViewType.SECTION)[0]
        t = Target.architectural(section.id)
        s = self.hand_set(p, {"elevation": [Effect.set_value(t, "view_type", "elevation")], "section": [Effect.set_value(t, "view_type", "section")]}, t)
        accept(p, s, "elevation")
        self.assertEqual(p.architecture.get(section.id).view_type, ViewType.ELEVATION)

    def test_a_merge_and_a_split_can_be_the_meaning_of_an_alternative(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((30000, 0), None)
        p = interpret_document(s.document(), project_name="Merge", engineer="E")
        a, b = sorted(plans(p), key=lambda v: v.bbox[0])
        q = self.hand_set(p, {"one_plan": [Effect.merge_views([a.id, b.id], "VIEW-90")], "two_plans": [Effect.acknowledge()]})
        accept(p, q, "one_plan")
        self.assertEqual(p.architecture.get(a.id).superseded_by, ("VIEW-90",))
        self.assertEqual(p.architecture.get("VIEW-90").level_key, "GROUND")
        merged = p.architecture.get("VIEW-90")
        half = len(merged.entity_ids) // 2
        parts = {"VIEW-91": {"bbox": list(merged.bbox), "entity_ids": list(merged.entity_ids[:half])},
                 "VIEW-92": {"bbox": list(merged.bbox), "entity_ids": list(merged.entity_ids[half:])}}
        q2 = self.hand_set(p, {"split_it": [Effect.split_view("VIEW-90", parts)], "leave_it": [Effect.acknowledge()]}, set_id="IS-901")
        accept(p, q2, "split_it")
        self.assertEqual(p.architecture.get("VIEW-90").superseded_by, ("VIEW-91", "VIEW-92"))
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())

    def test_an_acknowledgement_changes_no_model_state_but_records_the_answer_and_resolves_the_issue(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((20000, 0), "GROUND FLOOR PLAN COPY")
        s.plan((40000, 0), "GROUND FLOOR PLAN REVISED")
        p = interpret_document(s.document(), project_name="Dup", engineer="E")
        q = the_set(p, "each claim to be the GROUND plan")
        before = copy.deepcopy(p.to_dict())
        accept(p, q, next(a.meaning for a in q.alternatives if a.meaning.startswith("all_are")))
        after = p.to_dict()
        self.assertEqual(after["architectures"], before["architectures"])
        self.assertFalse(linked_issues(p, q)[0].is_open)


@tier("integration")
class RefusedResolutionsChangeNothing(unittest.TestCase):
    def test_if_any_effect_is_refused_nothing_at_all_changes(self):
        p = conflict_project()
        section = p.architecture.views_of(ViewType.SECTION)[0]
        t = Target.architectural(section.id)
        s = InterpretationSet("IS-910", "Two effects, the second impossible?", [
            Interpretation("INT-9100", "bad", 0.6, effects=(Effect.set_value(t, "title", "RENAMED"),
                                                             Effect.set_value(t, "level_key", "GROUND"))),   # a section cannot carry a level
            Interpretation("INT-9101", "fine", 0.4, effects=(Effect.acknowledge(),))], (), t)
        p.add_interpretation_set(s)
        d = decision(t, "try it")
        p.add_decision(d)
        before = p.to_json()
        with self.assertRaises(ValidationError) as caught:
            p.accept_interpretation("IS-910", "INT-9100", d.id)
        self.assertIn("nothing was changed", str(caught.exception))
        self.assertEqual(p.to_json(), before, "not even the first effect (the rename) stayed applied")
        self.assertEqual(p.get_interpretation_set("IS-910").status, SetStatus.OPEN)

    def test_an_alternative_that_promises_to_change_something_that_does_not_exist_is_refused_up_front(self):
        p = conflict_project()
        ghost = Target.architectural("VIEW-99")
        with self.assertRaises(ValidationError):
            p.add_interpretation_set(InterpretationSet("IS-911", "Ghost?", [
                Interpretation("INT-9110", "x", 0.5, effects=(Effect.set_value(ghost, "title", "T"),)),
                Interpretation("INT-9111", "y", 0.5)], (), None))

    def test_an_alternative_without_effects_still_only_records_the_choice(self):
        p = conflict_project()
        s = InterpretationSet("IS-912", "Legacy style?", [Interpretation("INT-9120", "a", 0.5), Interpretation("INT-9121", "b", 0.5)], (), None)
        p.add_interpretation_set(s)
        before_issues = [(i.id, i.status.value) for i in p.issues]
        accept(p, s, "a")
        self.assertEqual(p.get_interpretation_set("IS-912").status, SetStatus.RESOLVED)
        self.assertEqual([(i.id, i.status.value) for i in p.issues], before_issues)


@tier("unit")
class EffectsAreStrictlyValidated(unittest.TestCase):
    def test_unknown_kinds_missing_and_extra_parameters_are_rejected(self):
        bad = [
            {"kind": "delete_everything", "params": {}},
            {"kind": "accept_height", "params": {"from_level": "GROUND", "to_level": "FLOOR:1"}},
            {"kind": "accept_height", "params": {"from_level": "GROUND", "to_level": "FLOOR:1", "height_mm": 3000, "surprise": 1}},
            {"kind": "accept_height", "params": {"from_level": "GROUND", "to_level": "GROUND", "height_mm": 3000}},
            {"kind": "accept_height", "params": {"from_level": "Ground", "to_level": "FLOOR:1", "height_mm": 3000}},
            {"kind": "accept_height", "params": {"from_level": "GROUND", "to_level": "FLOOR:1", "height_mm": -5}},
            {"kind": "accept_height", "params": {"from_level": "GROUND", "to_level": "FLOOR:1", "height_mm": True}},
            {"kind": "set_value", "params": {"target": {"scope": "project", "id": None}, "field": "x", "value": 1}},
            {"kind": "set_value", "params": {"target": {"scope": "level", "id": "GF"}, "field": "a.b", "value": 1}},
            {"kind": "align_view", "params": {"view_id": "VIEW-01", "translation": [1]}},
            {"kind": "merge_views", "params": {"view_ids": ["VIEW-01"], "new_id": "VIEW-09"}},
            {"kind": "split_view", "params": {"view_id": "VIEW-01", "parts": {"VIEW-02": {"bbox": [0, 0, 1, 1], "entity_ids": []}}}},
        ]
        for row in bad:
            with self.assertRaises(ValidationError, msg=str(row)):
                Effect.from_dict(row)

    def test_a_valid_effect_round_trips_exactly(self):
        for e in (Effect.accept_height("GROUND", "NAMED:PODIUM", 4200.0), Effect.align_view("VIEW-01", (1.5, -2.0)),
                  Effect.set_value(Target.level("GF"), "name", "Ground"), Effect.acknowledge("noted"), Effect.acknowledge()):
            self.assertEqual(Effect.from_dict(e.to_dict()), e)


@tier("integration")
class ForgedResolutionsAreRejectedOnLoad(unittest.TestCase):
    def resolved_data(self):
        p = conflict_project()
        accept(p, the_set(p, "height from GROUND to FLOOR:1"), "3600_mm")
        return json.loads(p.to_json())

    def alt(self, data):
        s = next(x for x in data["interpretations"] if "height from GROUND" in x["question"])
        return next(a for a in s["alternatives"] if a["status"] == "accepted"), s

    def test_the_untampered_file_loads(self):
        OracleProject.from_dict(self.resolved_data())

    def test_a_payload_edited_after_the_fact_is_rejected(self):
        data = self.resolved_data()
        alt, _ = self.alt(data)
        alt["effects"][0]["params"]["height_mm"] = 3000.0
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_missing_applied_records_are_rejected(self):
        data = self.resolved_data()
        alt, _ = self.alt(data)
        alt["applied"] = []
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_an_applied_record_that_names_a_height_that_is_not_there_is_rejected(self):
        data = self.resolved_data()
        alt, _ = self.alt(data)
        alt["applied"][0]["created"] = ["HGT-999"]
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_an_accepted_height_edited_in_the_model_is_rejected(self):
        data = self.resolved_data()
        for h in data["architectures"][0]["heights"]:
            if h["source"] == "engineer_resolution":
                h["height_mm"] = 3500.0
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_an_issue_reopened_behind_a_resolved_set_is_rejected(self):
        data = self.resolved_data()
        _alt, s = self.alt(data)
        for i in data["issues"]:
            if i.get("interpretation_set_id") == s["id"]:
                i["status"], i["resolution"], i["decision_id"] = "open", None, None
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_a_unit_resolution_whose_model_value_was_edited_is_rejected(self):
        s = Sheet(units="mm", insunits=6)
        s.plan((0, 0), "GROUND FLOOR PLAN")
        p = interpret_document(s.document(), project_name="U", engineer="E")
        accept(p, the_set(p, "What unit"), "mm")
        data = json.loads(p.to_json())
        data["architectures"][0]["drawing"]["units"]["unit"] = "inch"
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_a_derived_decision_that_disagrees_with_the_payload_is_rejected(self):
        s = Sheet(units="mm", insunits=6)
        s.plan((0, 0), "GROUND FLOOR PLAN")
        p = interpret_document(s.document(), project_name="U", engineer="E")
        accept(p, the_set(p, "What unit"), "mm")
        data = json.loads(p.to_json())
        for d in data["decisions"]:
            if d["id"].endswith(".E1"):
                d["value"] = {"unit": "cm", "factor_to_mm": 10.0, "confidence": 1.0, "method": "engineer_decision", "note": None}
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)


if __name__ == "__main__":
    unittest.main()
