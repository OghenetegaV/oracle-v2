"""Tests for the calmer review workflow at the service level (tests/test_review_workflow.py)

Protects:
    What an engineer relies on when reviewing an unfamiliar drawing: rejecting a view is an auditable engineer decision with a reason (nothing is
    deleted, the evidence stays traceable, the rejection survives save and load and is not resurrected by anything Oracle does later, and only
    another engineer decision can take it back); "Ask Engineer" keeps the engineer's own words as identified, timestamped, verbatim input, settles a
    question with an interpretation Oracle never proposed, changes no model value from prose, and is flagged as guidance for the next stage;
    Oracle's suggestions stay suggestions until the engineer acts; the review queue and cards are plain language (no internal ids) and follow the
    project's own state; several sources are never chosen between silently.

Test type:
    Integration (tier "integration"): synthetic DXFs interpreted through the real pipeline and read through the application layer; no window.

Dependencies:
    oracle.application, oracle.core, tests.drawing_factory.
"""

import re
import tempfile
import unittest
from pathlib import Path

from oracle.application import ActionRefused, ArchitecturalSession, SourceChoiceRequired
from oracle.core import (
    DecisionCategory, DecisionSource, DecisionStatus, Effect, EngineeringDecision, EngineeringIssue, Interpretation, InterpretationSet,
    InterpretationStatus, IssueCategory, IssueSeverity, IssueStatus, ReviewStatus, SetStatus, Target, ValidationError,
)
from tests.drawing_factory import three_storey_sheet
from tests.tiers import tier

INTERNAL = re.compile(r"\b(VIEW-\d+|IS-\d+|ARC-\d+|SRC-\d+|PV-\d+|ENG-\d+|CLR-\d+|OBS-\d+)\b|[0-9a-f]{32,}")


class Base(unittest.TestCase):
    inch = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.s = ArchitecturalSession(engineer="Test Engineer")
        self.path = self.dir / "plan.dxf"
        three_storey_sheet(insunits=1 if self.inch else None).save(self.path)
        self.s.interpret(self.path)
        self.p = self.s.project
        self.arch = self.p.architecture

    def reopen(self) -> ArchitecturalSession:
        saved = self.s.save_project(self.dir / "p.oracle.json")
        fresh = ArchitecturalSession()
        fresh.open_project(saved)
        return fresh


@tier("integration")
class RejectingAView(Base):
    def test_rejection_is_an_engineer_decision_that_carries_a_reason_code_and_an_explanation(self):
        d = self.s.reject_views(["VIEW-03"], "duplicate", "The same sheet appears twice.")
        self.assertEqual((d.source, d.status, d.author), (DecisionSource.ENGINEER, DecisionStatus.ACCEPTED, "Test Engineer"))
        self.assertEqual((d.reason_code, d.reason), ("duplicate", "The same sheet appears twice."))
        self.assertEqual(d.target, Target.architectural("VIEW-03"))
        self.assertEqual(self.arch.get("VIEW-03").review, ReviewStatus.REJECTED)
        self.assertEqual(self.p.get_decision(d.id).reason_code, "duplicate")

    def test_a_reason_is_required_and_other_needs_words(self):
        for code, text in (("", ""), ("nonsense", ""), ("other", ""), ("other", "   ")):
            with self.assertRaises(ActionRefused, msg=(code, text)):
                self.s.reject_views(["VIEW-03"], code, text)
        self.assertEqual(self.arch.get("VIEW-03").review, ReviewStatus.PROPOSED)
        self.s.reject_views(["VIEW-03"], "other", "It is a legend, not a view.")

    def test_nothing_is_deleted_and_the_view_stays_traceable(self):
        views_before = len(self.arch.views)
        observations_before = [o.id for o in self.arch.observations if o.view_id == "VIEW-03"]
        provenance_before = len(self.p.provenance_for(Target.architectural("VIEW-03")))
        d = self.s.reject_views(["VIEW-03"], "irrelevant")
        self.assertEqual(len(self.arch.views), views_before)
        self.assertTrue(observations_before)
        self.assertEqual([o.id for o in self.arch.observations if o.view_id == "VIEW-03"], observations_before)
        self.assertEqual(len(self.p.provenance_for(Target.architectural("VIEW-03"))), provenance_before)
        self.assertIn(d.id, [x.id for x in self.p.decisions_for(Target.architectural("VIEW-03"))])
        trace = self.p.trace(Target.architectural("VIEW-03"))
        self.assertIn("VIEW-03", str(trace.to_dict()))
        self.assertEqual(self.p.value_status_of(Target.architectural("VIEW-03"), "review").decision_id, d.id)

    def test_the_rejection_and_its_reason_survive_save_and_load(self):
        self.s.reject_views(["VIEW-03"], "outside_scope", "Site plan is out of scope.")
        fresh = self.reopen()
        self.assertEqual(fresh.project.architecture.get("VIEW-03").review, ReviewStatus.REJECTED)
        row = fresh.rejected_views()[0]
        self.assertEqual((row.name, row.reason, row.explanation, row.engineer), ("Second Floor Plan", "Outside current scope", "Site plan is out of scope.", "Test Engineer"))
        self.assertTrue(row.when)
        self.assertEqual(fresh.project.to_dict(), self.s.project.to_dict())

    def test_a_rejected_view_is_not_active_anywhere(self):
        self.s.review_views(["VIEW-01", "VIEW-02"], accept=True)
        self.s.reject_views(["VIEW-03"], "duplicate")
        self.assertNotIn("VIEW-03", [r[3] for r in self.s.view_overlays().rects])
        self.assertNotIn("VIEW-03", [row[0] for row in self.s.approved().views])
        self.assertNotIn("VIEW-03", [b for _k, ref, _m in self.s.readiness().blockers for b in [ref]])
        self.assertNotIn("views:confirm", [i.key for i in self.s.queue()[0]])                      # nothing left to confirm
        self.assertEqual(self.s.overview().lines[1][1], "3 views found")
        self.assertEqual([e.state for e in self.s.view_entries() if e.id == "VIEW-03"], ["rejected"])
        self.assertNotIn("VIEW-03", self.p.approved_architecture().to_dict().__str__())

    def test_nothing_oracle_does_later_resurrects_it(self):
        self.s.reject_views(["VIEW-03"], "duplicate")
        self.s.interpret(self.dir / "plan.dxf", add_source=True)                                  # the same drawing again, as a second source
        self.assertEqual(self.s.project.architecture_of("SRC-1").get("VIEW-03").review, ReviewStatus.REJECTED)
        fresh = self.reopen()
        self.assertEqual(fresh.project.architecture_of("SRC-1").get("VIEW-03").review, ReviewStatus.REJECTED)

    def test_only_another_engineer_decision_takes_it_back_and_the_history_is_kept(self):
        first = self.s.reject_views(["VIEW-03"], "duplicate")
        second = self.s.reconsider_views(["VIEW-03"], "It is needed after all.")
        self.assertEqual(self.arch.get("VIEW-03").review, ReviewStatus.PROPOSED)
        self.assertEqual({first.id, second.id} <= {d.id for d in self.p.decisions}, True)
        self.assertEqual(self.s.rejected_views(), [])
        with self.assertRaises(ActionRefused):
            self.s.reconsider_views(["VIEW-03"])                                                  # it is not accepted or rejected any more

    def test_rejecting_closes_only_the_questions_about_the_rejected_view(self):
        s = InterpretationSet("IS-900", "How does the second floor plan line up?", [
            Interpretation("INT-900", "corner", 0.5, effects=(Effect.align_view("VIEW-03", (0, 0)),)),
            Interpretation("INT-901", "centre", 0.3, effects=(Effect.align_view("VIEW-03", (10, 10)),))], subject=Target.architectural("VIEW-03"))
        other = InterpretationSet("IS-901", "How does the first floor plan line up?", [
            Interpretation("INT-902", "corner", 0.5, effects=(Effect.align_view("VIEW-02", (0, 0)),))], subject=Target.architectural("VIEW-02"))
        self.p.add_interpretation_set(s)
        self.p.add_interpretation_set(other)
        self.p.add_issue(EngineeringIssue("ARC-900", IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY, "Cannot line VIEW-03 up.",
                                          Target.architectural("VIEW-03"), "test", interpretation_set_id="IS-900"))
        self.assertEqual(self.s.rejection_consequences(["VIEW-03"]), {"questions": 1, "issues": 1})
        frame_before = self.arch.get("VIEW-03").alignment_frame_id
        self.s.reject_views(["VIEW-03"], "incorrectly_detected")
        self.assertEqual(self.p.get_interpretation_set("IS-900").status, SetStatus.NONE_APPLY)     # nothing chosen, nothing applied
        self.assertEqual(self.p.get_interpretation_set("IS-901").status, SetStatus.OPEN)           # a question about another view is untouched
        self.assertEqual(self.p.get_issue("ARC-900").status, IssueStatus.ACCEPTED)
        self.assertEqual(self.arch.get("VIEW-03").alignment_frame_id, frame_before)                # none of the readings was applied
        self.assertEqual(self.reopen().project.get_interpretation_set("IS-900").status, SetStatus.NONE_APPLY)


@tier("integration")
class AskEngineer(Base):
    inch = True                                                                                     # the file says inches, the geometry is millimetres

    def test_the_engineers_words_answer_a_question_oracle_had_only_three_readings_for(self):
        card = self.s.card("set:IS-001")
        self.assertEqual(card.kind, "question")
        c = self.s.ask_engineer(card.ask_target, "The drawing is in millimetres except the door schedule, which is in inches.", "see title block", set_id="IS-001")
        oracle_meanings = {a.meaning for a in self.p.get_interpretation_set("IS-001").alternatives if a.origin == "oracle"}
        self.assertNotIn(c.statement, oracle_meanings)
        self.assertEqual(self.p.get_interpretation_set("IS-001").status, SetStatus.RESOLVED)
        self.assertEqual(self.p.get_interpretation_set("IS-001").accepted.origin, "engineer")
        self.assertNotIn("set:IS-001", [i.key for i in self.s.queue()[0]])
        self.assertIn("set:IS-001", [i.key for i in self.s.queue()[1]])

    def test_the_input_is_recorded_with_identity_time_target_status_and_decision(self):
        c = self.s.ask_engineer("SRC-1", "These columns are existing and retained.", "from the survey")
        self.assertEqual((c.author, c.statement, c.notes, c.target, c.disposition), ("Test Engineer", "These columns are existing and retained.", "from the survey",
                                                                                   Target.architectural("SRC-1"), "guidance"))
        self.assertTrue(c.created_at)
        d = self.p.get_decision(c.decision_id)
        self.assertEqual((d.source, d.status, d.category, d.instruction), (DecisionSource.ENGINEER, DecisionStatus.ACCEPTED,
                                                                          DecisionCategory.ARCHITECTURAL_COORDINATION, "These columns are existing and retained."))
        self.assertIsNotNone(c.provenance_id)
        self.assertEqual(self.p.get_provenance(c.provenance_id).method, "engineer_clarification")
        row = self.s.clarification_rows()[0]
        self.assertEqual((row[2], row[4], row[5]), ("Test Engineer", "These columns are existing and retained.", "kept as guidance for the next stage"))

    def test_the_input_survives_save_and_load_and_is_offered_to_the_next_stage_as_guidance(self):
        self.s.ask_engineer("SRC-1", "Treat this as an existing reinforced concrete balcony.", set_id="IS-001")
        fresh = self.reopen()
        self.assertEqual([c.statement for c in fresh.project.clarifications], ["Treat this as an existing reinforced concrete balcony."])
        guidance = fresh.project.approved_architecture().engineer_guidance
        self.assertEqual([(g.statement, g.disposition, g.author) for g in guidance], [("Treat this as an existing reinforced concrete balcony.", "guidance", "Test Engineer")])
        self.assertEqual(fresh.project.to_dict(), self.s.project.to_dict())

    def test_prose_never_becomes_a_model_value_or_structure_and_never_confirms_the_unit(self):
        building_before = self.p.building
        unit_before = self.arch.drawing.units.to_dict() if hasattr(self.arch.drawing.units, "to_dict") else self.arch.drawing.units
        self.s.ask_engineer("SRC-1", "Make everything 300 mm thick concrete and add columns at every grid intersection.", set_id="IS-001")
        self.assertIs(self.p.building, building_before)
        self.assertEqual(self.arch.drawing.units.to_dict() if hasattr(self.arch.drawing.units, "to_dict") else self.arch.drawing.units, unit_before)
        approved = self.s.approved()
        self.assertFalse(approved.unit_confirmed)                                                   # words do not confirm a unit
        self.assertTrue(approved.blockers)

    def test_an_unclear_observation_can_be_routed_to_ask_engineer(self):
        obs = next(o for o in self.arch.observations if o.kind in ("column_symbol", "door", "window", "wall_external"))
        hint, review = obs.hint, obs.review
        c = self.s.ask_engineer(obs.id, "This symbol is a retained column stub, not a new column.")
        self.assertEqual(c.target, Target.architectural(obs.id))
        self.assertIn("engineer_clarification", [r.method for r in self.p.provenance_for(Target.architectural(obs.id))])
        self.assertEqual((self.arch.get(obs.id).hint, self.arch.get(obs.id).review), (hint, review))      # the observation itself is not approved or changed

    def test_words_are_required(self):
        with self.assertRaises(ActionRefused):
            self.s.ask_engineer("SRC-1", "   ")
        self.assertEqual(self.p.clarifications, [])

    def test_a_height_value_needs_a_height_question(self):
        with self.assertRaises(ActionRefused):
            self.s.answer_height("IS-001", 3200)


@tier("integration")
class SuggestionsStaySuggestions(Base):
    inch = True

    def test_nothing_is_accepted_until_the_engineer_acts_even_after_save_and_load(self):
        for session in (self.s, self.reopen()):
            s = session.project.get_interpretation_set("IS-001")
            self.assertEqual(s.status, SetStatus.OPEN)
            self.assertEqual({a.status for a in s.alternatives}, {InterpretationStatus.PROPOSED})
            card = session.card("set:IS-001")
            self.assertEqual(card.engineer_status, "")
            self.assertTrue(card.suggestions and all(x.confidence in ("High", "Medium", "Low") for x in card.suggestions))
            self.assertIn("accept_suggestion", card.actions)

    def test_a_non_engineer_decision_cannot_accept_a_suggestion(self):
        oracle = EngineeringDecision("D-X", "Oracle", DecisionSource.ORACLE, Target.architectural("SRC-1"), DecisionCategory.OTHER, "auto", status=DecisionStatus.PROPOSED)
        self.p.add_decision(oracle)
        with self.assertRaises(ValidationError):
            self.p.accept_interpretation("IS-001", self.p.get_interpretation_set("IS-001").alternatives[0].id, "D-X")
        self.assertEqual(self.p.get_interpretation_set("IS-001").status, SetStatus.OPEN)

    def test_after_the_engineer_accepts_it_reads_as_an_engineer_decision(self):
        alt = self.s.card("set:IS-001").suggestions[0]
        self.s.accept_alternative("IS-001", alt.id, "checked the title block")
        card = self.s.card("set:IS-001")
        self.assertEqual(card.kind, "answered")
        self.assertTrue(card.engineer_status.startswith("Engineer decision:"))


@tier("integration")
class QueueAndCards(Base):
    inch = True

    def test_the_queue_lists_what_needs_the_engineer_in_plain_words_and_is_derived_from_the_project(self):
        todo, done = self.s.queue()
        self.assertEqual([i.key for i in todo], ["set:IS-001", "views:confirm", "levels:establish"])
        self.assertEqual(done, [])
        self.assertEqual(todo[0].label, "Unit of the drawing")
        self.assertIn("inch", todo[0].detail)
        ov = self.s.overview()
        self.assertEqual([t for _i, t, _tone in ov.lines], ["Drawing interpreted", "3 views found", "3 items need your review"])
        self.assertFalse(ov.complete)

    def test_items_leave_the_queue_when_resolved_and_the_review_completes(self):
        self.s.accept_alternative("IS-001", self.s.card("set:IS-001").suggestions[0].id)
        self.s.review_views(["VIEW-01", "VIEW-02", "VIEW-03"], accept=True)
        detected, _ = self.s.levels()
        self.s.establish_levels({lv.key: i * 3000.0 for i, lv in enumerate(detected)}, elevation_type="finished_floor")
        todo, done = self.s.queue()
        self.assertEqual(todo, [])
        self.assertEqual({i.key for i in done}, {"set:IS-001", "views:accepted", "levels:done"})
        ov = self.s.overview()
        self.assertTrue(ov.complete)
        self.assertEqual(ov.lines[-1][1], "Architectural review complete")
        self.assertEqual(self.s.card("complete").kind, "complete")

    def test_cards_offer_only_the_actions_that_fit_the_item(self):
        self.assertEqual(self.s.card("set:IS-001").actions[0], "accept_suggestion")
        self.assertIn("ask_engineer", self.s.card("set:IS-001").actions)
        self.assertEqual(self.s.card("views:confirm").actions, ["confirm_all", "review_views"])
        self.assertEqual(self.s.card("levels:establish").actions[0], "set_levels")
        needs = self.s.card("view:VIEW-01")
        self.assertEqual((needs.state, needs.actions), ("needs_review", ["accept_view", "view_actions"]))       # the rest lives in the View Actions menu
        self.assertIn("reject_view", needs.menu)
        self.s.review_views(["VIEW-01"], accept=True)
        self.assertEqual(self.s.card("view:VIEW-01").state, "accepted")
        self.s.reject_views(["VIEW-02"], "duplicate", "twice")
        rejected = self.s.card("view:VIEW-02")
        self.assertEqual((rejected.state, rejected.actions), ("rejected", ["reconsider", "review_evidence"]))
        self.assertEqual((rejected.rejection.reason, rejected.rejection.explanation), ("Duplicate", "twice"))

    def test_the_default_models_carry_no_internal_ids_hashes_or_confidence_numbers(self):
        self.s.ask_engineer("VIEW-01", "This is the ground floor.")
        texts = []
        todo, done = self.s.queue()
        for item in todo + done:
            texts += [item.title, item.detail, item.label]
        for key in ["set:IS-001", "views:confirm", "levels:establish", "view:VIEW-01", "complete"]:
            card = self.s.card(key)
            texts += [card.title, card.body, card.engineer_status, *card.facts]
            for x in card.suggestions:
                texts += [x.label, x.confidence, x.consequence]
            for who, when, statement, fate in card.engineer_notes:
                texts += [who, when, statement, fate]
        ov = self.s.overview()
        texts += [t for _i, t, _tone in ov.lines] + ov.needs_lines + ov.resolved_lines + [ov.drawing]
        texts += [e.label for e in self.s.view_entries()] + [label for _sid, label in self.s.source_choices()]
        offenders = [t for t in texts if t and INTERNAL.search(t)]
        self.assertEqual(offenders, [])
        self.assertFalse([t for t in texts if re.search(r"\b0\.\d\d\b", t or "")])                  # no raw confidence numbers


@tier("integration")
class SourcesAreNeverPickedSilently(Base):
    def test_two_sources_have_readable_distinct_labels_and_need_an_explicit_choice(self):
        self.s.interpret(self.path, add_source=True, revision="B")
        labels = [label for _sid, label in self.s.source_choices()]
        self.assertEqual(len(set(labels)), 2)
        self.assertTrue(all("SRC-" not in label for label in labels))
        self.assertIn("Revision B", labels[1])
        fresh = self.reopen()
        for call in (fresh.overview, fresh.queue, fresh.view_entries):
            with self.assertRaises(SourceChoiceRequired):
                call()
        fresh.set_active_source("SRC-2")
        self.assertTrue(fresh.view_entries())
