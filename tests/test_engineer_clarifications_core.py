"""Tests for engineer clarifications, reason codes and the 0.4.0 -> 0.5.0 migration (tests/test_engineer_clarifications_core.py)

Protects:
    The domain side of "Ask Engineer" and of rejecting with a reason. An engineer can answer an open question in their OWN words instead of
    choosing one of Oracle's readings: the words are kept verbatim with the engineer's identity, the time, the target and the decision that
    recorded them; the Oracle readings are rejected under the same decision (never silently resurrected); linked issues are ACCEPTED, not fixed;
    nothing in the model changes from prose; a VALUE the engineer supplies is applied only through validated structured effects and only all-or-
    nothing. Free-form input is only ever recorded under an accepted engineer decision, and a project that holds it round-trips and validates.
    A decision may carry a reason code. A 0.4.0 project file (a genuine one) migrates to 0.5.0 without inventing anything.

Test type:
    Unit (tier "unit"): hand-built projects, no drawing and no window.

Dependencies:
    oracle.core, tests.fixtures.
"""

import json
import unittest
from pathlib import Path

from oracle.core import (
    DecisionCategory, DecisionSource, DecisionStatus, Effect, EngineerClarification, EngineeringDecision, EngineeringIssue, InterpretationSet,
    Interpretation, InterpretationStatus, IssueCategory, IssueSeverity, IssueStatus, OracleProject, SetStatus, Target, ValidationError,
)
from tests.fixtures import make_project
from tests.tiers import tier

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEVEL = Target.level("FF")


def engineer(did, target=LEVEL, instruction="I say so.", **kw):
    return EngineeringDecision(did, "A. Engineer", DecisionSource.ENGINEER, target, DecisionCategory.ARCHITECTURAL_COORDINATION, instruction,
                               status=DecisionStatus.ACCEPTED, **kw)


def question(project, *, with_issue=True, effects_on_first=()):
    s = InterpretationSet("IS-1", "What is the region above the first floor?", [
        Interpretation("INT-A", "slab panel", 0.7, effects=effects_on_first), Interpretation("INT-B", "void", 0.3)], subject=LEVEL)
    project.add_interpretation_set(s)
    if with_issue:
        project.add_issue(EngineeringIssue("ISS-1", IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY, "Region unclear.", LEVEL, "test",
                                           interpretation_set_id="IS-1"))
    return s


@tier("unit")
class AskEngineerOnAQuestion(unittest.TestCase):
    def setUp(self):
        self.p = make_project()
        question(self.p)

    def test_the_engineers_own_words_settle_the_question_and_are_kept_verbatim(self):
        words = "Treat this as an existing reinforced concrete balcony.\nDo not model it as a slab."
        c = self.p.answer_with_engineer_input("IS-1", engineer("D-1", instruction=words), words, "from the site visit", target=LEVEL)
        self.assertEqual((c.id, c.statement, c.notes, c.author, c.disposition), ("CLR-001", words, "from the site visit", "A. Engineer", "guidance"))
        self.assertEqual(c.decision_id, "D-1")
        self.assertTrue(c.created_at)                                              # a timestamp
        s = self.p.get_interpretation_set("IS-1")
        self.assertEqual(s.status, SetStatus.RESOLVED)
        self.assertEqual(s.accepted.origin, "engineer")
        self.assertEqual(s.accepted.decision_id, "D-1")
        self.assertEqual({a.status for a in s.alternatives if a.origin == "oracle"}, {InterpretationStatus.REJECTED})   # Oracle's readings set aside
        self.assertEqual(self.p.get_decision("D-1").source, DecisionSource.ENGINEER)

    def test_an_interpretation_oracle_never_listed_is_allowed(self):
        self.p.answer_with_engineer_input("IS-1", engineer("D-1"), "A roof overhang; not a slab.", target=LEVEL)
        self.assertNotIn("roof overhang", " ".join(a.meaning for a in self.p.get_interpretation_set("IS-1").alternatives if a.origin == "oracle"))
        self.assertEqual(self.p.get_interpretation_set("IS-1").accepted.meaning, "A roof overhang; not a slab.")

    def test_prose_changes_nothing_in_the_model(self):
        before = self.p.building.to_dict()
        self.p.answer_with_engineer_input("IS-1", engineer("D-1"), "Make it 300 mm thick and structural.", target=LEVEL)
        self.assertEqual(self.p.building.to_dict(), before)
        self.assertEqual(self.p.clarifications[0].disposition, "guidance")           # flagged as guidance, not applied

    def test_issues_on_the_question_are_accepted_not_fixed_and_quote_the_statement(self):
        self.p.answer_with_engineer_input("IS-1", engineer("D-1"), "It is a balcony.", target=LEVEL)
        issue = self.p.get_issue("ISS-1")
        self.assertEqual(issue.status, IssueStatus.ACCEPTED)
        self.assertIn("It is a balcony.", issue.resolution)

    def test_the_clarification_and_its_provenance_survive_save_and_load(self):
        self.p.answer_with_engineer_input("IS-1", engineer("D-1"), "Existing and retained.", "notes here", target=LEVEL)
        back = OracleProject.from_json(self.p.to_json())
        self.assertEqual([c.to_dict() for c in back.clarifications], [c.to_dict() for c in self.p.clarifications])
        self.assertEqual(back.to_json(), self.p.to_json())
        self.assertEqual(back.get_interpretation_set("IS-1").accepted.origin, "engineer")
        self.assertEqual([r.method for r in back.provenance_for(LEVEL)][-1], "engineer_clarification")

    def test_an_answered_question_cannot_be_answered_again(self):
        self.p.answer_with_engineer_input("IS-1", engineer("D-1"), "One.", target=LEVEL)
        with self.assertRaises(ValidationError):
            self.p.answer_with_engineer_input("IS-1", engineer("D-2"), "Two.", target=LEVEL)
        self.assertEqual(len(self.p.clarifications), 1)

    def test_only_an_accepted_engineer_decision_can_carry_engineer_input(self):
        oracle = EngineeringDecision("D-1", "Oracle", DecisionSource.ORACLE, LEVEL, DecisionCategory.LAYOUT, "x")
        proposed = EngineeringDecision("D-2", "A. Engineer", DecisionSource.ENGINEER, LEVEL, DecisionCategory.LAYOUT, "x", status=DecisionStatus.PROPOSED)
        for bad in (oracle, proposed):
            with self.assertRaises(ValidationError):
                self.p.answer_with_engineer_input("IS-1", bad, "words", target=LEVEL)
        self.assertEqual((self.p.clarifications, self.p.get_interpretation_set("IS-1").status), ([], SetStatus.OPEN))

    def test_empty_words_and_unknown_targets_are_refused_and_change_nothing(self):
        for words in ("", "   "):
            with self.assertRaises(ValidationError):
                self.p.answer_with_engineer_input("IS-1", engineer("D-1"), words, target=LEVEL)
        with self.assertRaises(ValidationError):
            self.p.answer_with_engineer_input("IS-1", engineer("D-1"), "words", target=Target.level("NOPE"))
        self.assertEqual((self.p.clarifications, self.p.decisions, self.p.get_interpretation_set("IS-1").status), ([], [], SetStatus.OPEN))

    def test_a_meaning_that_repeats_an_oracle_reading_is_kept_distinguishable(self):
        self.p.answer_with_engineer_input("IS-1", engineer("D-1"), "void", target=LEVEL)
        meanings = [a.meaning for a in self.p.get_interpretation_set("IS-1").alternatives]
        self.assertEqual(len(meanings), len(set(m.lower() for m in meanings)))


@tier("unit")
class GuidanceWithoutAQuestion(unittest.TestCase):
    def test_guidance_about_a_target_is_recorded_with_provenance_and_changes_nothing(self):
        p = make_project()
        before = p.building.to_dict()
        c = p.record_clarification(engineer("D-1"), LEVEL, "These columns are existing and retained.")
        self.assertEqual((c.interpretation_set_id, c.provenance_id is not None, c.disposition), (None, True, "guidance"))
        self.assertEqual(p.building.to_dict(), before)
        self.assertEqual(p.clarifications_for(LEVEL)[0].statement, "These columns are existing and retained.")
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())

    def test_guidance_about_the_whole_project_needs_no_provenance(self):
        p = make_project()
        c = p.record_clarification(engineer("D-1", target=Target.project()), Target.project(), "All existing structure is retained.")
        self.assertIsNone(c.provenance_id)
        self.assertEqual(OracleProject.from_json(p.to_json()).clarifications[0].statement, "All existing structure is retained.")

    def test_the_clarification_must_rest_on_a_recorded_engineer_decision_when_loaded(self):
        p = make_project()
        p.record_clarification(engineer("D-1"), LEVEL, "Words.")
        data = p.to_dict()
        data["decisions"][0]["source"] = "oracle"
        data["decisions"][0]["status"] = "proposed"
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_an_engineer_answer_without_its_clarification_is_refused_when_loaded(self):
        p = make_project()
        question(p, with_issue=False)
        p.answer_with_engineer_input("IS-1", engineer("D-1"), "Words.", target=LEVEL)
        data = p.to_dict()
        data["clarifications"] = []
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_clarification_values_are_validated(self):
        with self.assertRaises(ValidationError):
            EngineerClarification("CLR-001", "D-1", LEVEL, "  ", "A")
        with self.assertRaises(ValidationError):
            EngineerClarification("CLR-001", "D-1", LEVEL, "words", "A", disposition="weird")
        with self.assertRaises(ValidationError):
            EngineerClarification("CLR-001", "D-1", LEVEL, "words", "A", interpretation_set_id="IS-1")


@tier("unit")
class AValueTheEngineerSuppliesIsApplied(unittest.TestCase):
    def test_a_supplied_value_is_applied_through_structured_effects_and_marked_applied(self):
        p = make_project()
        effect = Effect.set_value(Target.level("FF"), "name", "Podium")
        question(p, with_issue=True, effects_on_first=(effect,))
        c = p.answer_with_engineer_input("IS-1", engineer("D-1"), "Call the first floor the podium.", target=LEVEL,
                                         effects=(Effect.set_value(Target.level("FF"), "name", "Podium"),))
        self.assertEqual(c.disposition, "applied")
        self.assertEqual(p.building.get_level("FF").name, "Podium")
        self.assertEqual(p.get_issue("ISS-1").status, IssueStatus.RESOLVED)
        self.assertEqual(OracleProject.from_json(p.to_json()).building.get_level("FF").name, "Podium")

    def test_a_refused_effect_leaves_everything_untouched(self):
        p = make_project()
        question(p)
        before = p.to_json()
        with self.assertRaises(ValidationError):
            p.answer_with_engineer_input("IS-1", engineer("D-1"), "Rename a level that does not exist.", target=LEVEL,
                                         effects=(Effect.set_value(Target.level("NOPE"), "name", "X"),))
        self.assertEqual(p.to_json(), before)


@tier("unit")
class ReasonCodeAndReconsider(unittest.TestCase):
    def test_a_decision_may_carry_a_reason_code_and_it_round_trips(self):
        d = engineer("D-1", reason_code="duplicate")
        self.assertEqual(EngineeringDecision.from_dict(d.to_dict()).reason_code, "duplicate")
        self.assertIsNone(engineer("D-2").reason_code)
        self.assertNotIn("Duplicate", json.dumps(engineer("D-3").to_dict()))

    def test_a_reason_code_is_a_short_lowercase_token(self):
        for bad in ("Duplicate", "with space", "", "9x", "x" * 41):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                engineer("D-1", reason_code=bad)


@tier("unit")
class MigrationFrom040(unittest.TestCase):
    def data(self):
        return json.loads((FIXTURES / "schema_0_4_0_project.json").read_text(encoding="utf-8"))

    def test_a_genuine_0_4_0_project_loads_and_gains_an_empty_clarification_registry(self):
        old = self.data()
        self.assertEqual(old["schema_version"], "0.4.0")
        self.assertNotIn("clarifications", old)
        p = OracleProject.from_dict(old)
        self.assertEqual((p.schema_version, p.clarifications), ("0.5.0", []))
        self.assertEqual(len(p.decisions), 2)                                         # nothing was lost or invented
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())

    def test_migration_does_not_touch_the_original_and_refuses_a_field_it_cannot_have(self):
        old = self.data()
        snapshot = json.dumps(old, sort_keys=True)
        OracleProject.from_dict(old)
        self.assertEqual(json.dumps(old, sort_keys=True), snapshot)
        bad = self.data()
        bad["clarifications"] = []
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(bad)
