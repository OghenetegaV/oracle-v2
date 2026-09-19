"""Tests for Oracle's decision authority, value-change history and issue evidence (oracle.core, schema 0.2.0)

Protects:
    The difference between an Oracle/AI recommendation, an engineer decision and an engineer
    override; that Oracle can never record its own inference as engineer-approved; that an engineer
    changing a value (C12 300x300 -> 350x350) keeps the whole decision chain, the old value and the
    provenance instead of overwriting them; supersession rules; and that issues can cite evidence,
    related objects, an interpretation and a decision, with unresolved blocking issues stopping the
    project being ready.

Test type:
    Unit tests.

Dependencies:
    oracle.core only.
"""

import unittest

from oracle.core import (
    DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision, EngineeringIssue, IssueCategory,
    IssueSeverity, IssueStatus, OracleProject, ProvenanceRecord, SourceReference, Target, ValidationError,
    ValueStatus, ValueStatusRecord,
)
from tests.fixtures import make_project

C5, B1 = Target.element("C5"), Target.element("B1")


def section(w, d):
    return {"shape": "rectangular", "width_mm": w, "depth_mm": d}


def engineer(did, target, **kw):
    kw.setdefault("instruction", "Engineer's ruling.")
    kw.setdefault("status", DecisionStatus.ACCEPTED)
    return EngineeringDecision(did, "A. Engineer", DecisionSource.ENGINEER, target, DecisionCategory.SECTION_SIZING, **kw)


def recommendation(did, target, source=DecisionSource.ORACLE, **kw):
    kw.setdefault("instruction", "Recommended by Oracle.")
    return EngineeringDecision(did, "Oracle", source, target, DecisionCategory.SECTION_SIZING, **kw)


def set_section(p, did, target, w, d):
    return p.set_value(target, "section", section(w, d), engineer(did, target, field="section", value=section(w, d),
                                                                  instruction=f"{target.id} = {w}x{d}"))


class RecommendationVersusDecisionTests(unittest.TestCase):
    def test_an_oracle_or_ai_recommendation_can_only_be_added_as_proposed(self):
        p = make_project()
        for source in (DecisionSource.ORACLE, DecisionSource.AI_ASSISTANT):
            with self.assertRaises(ValidationError):
                p.add_decision(recommendation("R-bad", B1, source, status=DecisionStatus.ACCEPTED))
            with self.assertRaises(ValidationError):
                p.add_decision(recommendation("R-bad", B1, source, status=DecisionStatus.OVERRIDDEN))
        p.add_decision(recommendation("R1", B1))
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.PROPOSED)

    def test_a_recommendation_cannot_be_approved_by_editing_its_status(self):
        p = make_project()
        p.add_decision(recommendation("R1", B1))
        for status in (DecisionStatus.ACCEPTED, DecisionStatus.OVERRIDDEN, DecisionStatus.REJECTED):
            with self.assertRaises(ValidationError):
                p.set_decision_status("R1", status)
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.PROPOSED)

    def test_engineer_accepts_a_recommendation(self):
        p = make_project()
        p.add_decision(recommendation("R1", B1))
        p.add_decision(engineer("D1", B1, responds_to="R1", instruction="Agreed."))
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.ACCEPTED)
        self.assertEqual(p.get_decision("R1").source, DecisionSource.ORACLE)   # it stays Oracle's, never the engineer's
        self.assertEqual(p.get_decision("D1").source, DecisionSource.ENGINEER)

    def test_engineer_overrides_a_recommendation(self):
        p = make_project()
        p.add_decision(recommendation("R1", B1, DecisionSource.AI_ASSISTANT))
        p.add_decision(engineer("D1", B1, responds_to="R1", overrides_recommendation=True, reason="Deeper beam needed."))
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.OVERRIDDEN)
        self.assertTrue(p.get_decision("D1").overrides_recommendation)

    def test_engineer_rejects_a_recommendation(self):
        p = make_project()
        p.add_decision(recommendation("R1", B1))
        p.add_decision(engineer("D1", B1, responds_to="R1", status=DecisionStatus.REJECTED))
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.REJECTED)

    def test_a_pending_response_leaves_the_recommendation_proposed_and_follows_later_changes(self):
        p = make_project()
        p.add_decision(recommendation("R1", B1))
        p.add_decision(engineer("D1", B1, responds_to="R1", status=DecisionStatus.PROPOSED))
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.PROPOSED)
        p.set_decision_status("D1", DecisionStatus.ACCEPTED)
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.ACCEPTED)
        p.set_decision_status("D1", DecisionStatus.PROPOSED)                 # the engineer takes it back
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.PROPOSED)

    def test_responses_must_come_from_an_engineer_to_a_real_recommendation(self):
        p = make_project()
        p.add_decision(engineer("D0", B1))
        p.add_decision(recommendation("R1", B1))
        with self.assertRaises(ValidationError):                              # unknown decision
            p.add_decision(engineer("D1", B1, responds_to="R404"))
        with self.assertRaises(ValidationError):                              # D0 is an engineer decision, not a recommendation
            p.add_decision(engineer("D1", B1, responds_to="D0"))
        with self.assertRaises(ValidationError):                              # Oracle cannot respond to anything
            recommendation("R2", B1, responds_to="R1")
        with self.assertRaises(ValidationError):                              # cannot respond to itself
            engineer("D1", B1, responds_to="D1")
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.PROPOSED)

    def test_responses_survive_save_and_load(self):
        p = make_project()
        p.add_decision(recommendation("R1", B1))
        p.add_decision(recommendation("R2", C5))
        p.add_decision(engineer("D1", B1, responds_to="R1", overrides_recommendation=True))
        q = OracleProject.from_json(p.to_json())
        self.assertEqual([q.get_decision(i).status for i in ("R1", "R2")],
                         [DecisionStatus.OVERRIDDEN, DecisionStatus.PROPOSED])
        self.assertEqual(q.get_decision("D1").responds_to, "R1")
        self.assertEqual(q.to_json(), p.to_json())

    def test_a_file_that_records_oracle_inference_as_approved_is_rejected_on_load(self):
        p = make_project()
        p.add_decision(recommendation("R1", B1))
        for tampered in (DecisionStatus.ACCEPTED, DecisionStatus.OVERRIDDEN, DecisionStatus.REJECTED):
            data = OracleProject.from_json(p.to_json()).to_dict()
            data["decisions"][0]["status"] = tampered.value
            with self.assertRaises(ValidationError, msg=tampered.value):
                OracleProject.from_dict(data)
        p.add_decision(engineer("D1", B1, responds_to="R1"))                  # accepted, not overridden
        data = OracleProject.from_json(p.to_json()).to_dict()
        data["decisions"][0]["status"] = "overridden"                          # the response does not say override
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_imported_decisions_are_not_treated_as_oracle_recommendations(self):
        p = make_project()
        p.add_decision(EngineeringDecision("I1", "legacy wizard", DecisionSource.IMPORTED, B1, DecisionCategory.OTHER,
                                           "Note carried over from a legacy project.", status=DecisionStatus.ACCEPTED))
        self.assertEqual(OracleProject.from_json(p.to_json()).get_decision("I1").status, DecisionStatus.ACCEPTED)

    def test_only_an_engineer_decision_can_accept_an_issue(self):
        p = make_project()
        p.add_issue(EngineeringIssue("I1", IssueSeverity.BLOCKING, IssueCategory.MISSING_SUPPORT, "B3 unsupported.",
                                     Target.element("B3"), "test"))
        p.add_decision(recommendation("R1", Target.element("B3")))
        with self.assertRaises(ValidationError):
            p.accept_issue("I1", "Oracle says fine.", decision_id="R1")
        self.assertEqual(p.get_issue("I1").status, IssueStatus.OPEN)
        p.add_decision(engineer("D1", Target.element("B3")))
        p.accept_issue("I1", "Engineer accepts.", decision_id="D1")
        self.assertEqual(p.get_issue("I1").status, IssueStatus.ACCEPTED)


class ValueChangeHistoryTests(unittest.TestCase):
    def test_engineer_changes_a_column_and_the_history_is_kept(self):
        p = make_project()
        p.set_value_status(ValueStatusRecord(C5, "section", ValueStatus.ASSUMED, note="placeholder"))
        p.add_provenance(ProvenanceRecord("PV-00001", C5, SourceReference(source_id="DEFAULT_SIZES[column_mm]"),
                                          "legacy default size table", "ga_dxf_parser", field="section"))
        first = set_section(p, "D1", C5, 300, 300)
        self.assertEqual(p.building.get_element("C5").section.width_mm, 300)
        self.assertEqual((first.status, first.replaces, first.decision_id),
                         (ValueStatus.ENGINEER_OVERRIDE, ValueStatus.ASSUMED, "D1"))     # overrode Oracle's assumption
        second = set_section(p, "D2", C5, 350, 350)
        self.assertEqual(p.building.get_element("C5").section.width_mm, 350)
        self.assertEqual((second.status, second.replaces, second.decision_id),
                         (ValueStatus.ENGINEER_OVERRIDE, ValueStatus.ENGINEER_OVERRIDE, "D2"))
        d1, d2 = p.get_decision("D1"), p.get_decision("D2")
        self.assertEqual((d1.status, d1.superseded_by), (DecisionStatus.SUPERSEDED, "D2"))
        self.assertEqual((d2.status, d2.superseded_by), (DecisionStatus.ACCEPTED, None))
        self.assertEqual(d1.previous_value, section(225, 225))                 # what the source/legacy value was
        self.assertEqual(d2.previous_value, section(300, 300))                 # what the engineer had set before
        self.assertEqual(d1.value, section(300, 300))                          # nothing was overwritten
        self.assertEqual([d.id for d in p.decision_history(C5, "section")], ["D1", "D2"])
        self.assertEqual(len(p.provenance_for(C5, "section")), 1)              # the original evidence is still there

    def test_history_and_status_survive_save_and_load(self):
        p = make_project()
        p.set_value_status(ValueStatusRecord(C5, "section", ValueStatus.ASSUMED))
        set_section(p, "D1", C5, 300, 300)
        set_section(p, "D2", C5, 350, 350)
        q = OracleProject.from_json(p.to_json())
        self.assertEqual(q.building.get_element("C5").section.depth_mm, 350)
        self.assertEqual([(d.id, d.status.value, d.previous_value) for d in q.decision_history(C5, "section")],
                         [("D1", "superseded", section(225, 225)), ("D2", "accepted", section(300, 300))])
        self.assertEqual(q.value_status_of(C5, "section").status, ValueStatus.ENGINEER_OVERRIDE)
        self.assertEqual(q.to_json(), p.to_json())

    def test_a_field_with_no_prior_status_becomes_engineer_defined(self):
        p = make_project()
        record = p.set_value(B1, "material", "C30/37", engineer("D1", B1, field="material", value="C30/37"))
        self.assertEqual((record.status, record.replaces), (ValueStatus.ENGINEER_DEFINED, None))
        self.assertEqual(p.building.get_element("B1").material, "C30/37")
        self.assertEqual(p.get_decision("D1").previous_value, "C25/30")

    def test_a_rejected_change_changes_nothing(self):
        p = make_project()
        before = p.to_json()
        cases = [
            (C5, "section", section(-5, 300), engineer("D1", C5, field="section", value=section(-5, 300))),   # invalid value
            (C5, "section", {"shape": "triangle"}, engineer("D1", C5, field="section", value={"shape": "triangle"})),
            (C5, "colour", "red", engineer("D1", C5, field="colour", value="red")),                             # no such field
            (C5, "id", "C99", engineer("D1", C5, field="id", value="C99")),                                     # identity is fixed
            (C5, "section.width_mm", 300, engineer("D1", C5, field="section.width_mm", value=300)),            # top-level only
            (Target.element("C404"), "section", section(300, 300),
             engineer("D1", Target.element("C404"), field="section", value=section(300, 300))),                # no such element
            (C5, "section", section(300, 300), engineer("D1", B1, field="section", value=section(300, 300))),  # wrong target
            (C5, "section", section(300, 300), engineer("D1", C5, field="material", value=section(300, 300))), # wrong field
            (C5, "section", section(300, 300), engineer("D1", C5, field="section", value=section(350, 350))), # wrong value
            (C5, "section", section(300, 300), engineer("D1", C5, field="section", value=section(300, 300),
                                                        status=DecisionStatus.PROPOSED)),                        # not accepted
            (C5, "section", section(300, 300), recommendation("D1", C5, field="section", value=section(300, 300))),  # not an engineer
        ]
        for target, field, value, decision in cases:
            with self.assertRaises(ValidationError, msg=f"{target.id}.{field}={value}"):
                p.set_value(target, field, value, decision)
            self.assertEqual(p.to_json(), before)
        self.assertEqual(p.decisions, [])

    def test_duplicate_decision_id_and_unsupported_targets(self):
        p = make_project()
        set_section(p, "D1", C5, 300, 300)
        with self.assertRaises(ValidationError):
            set_section(p, "D1", C5, 350, 350)
        self.assertEqual(p.building.get_element("C5").section.width_mm, 300)
        with self.assertRaises(ValidationError):
            p.set_value(Target.level("FF"), "name", "First", engineer("D9", Target.level("FF"), field="name", value="First"))
        with self.assertRaises(ValidationError):
            OracleProject.create("No building", "E").set_value(C5, "section", section(300, 300),
                                                               engineer("D9", C5, field="section", value=section(300, 300)))

    def test_an_accepted_field_change_cannot_be_recorded_without_applying_it(self):
        p = make_project()
        with self.assertRaises(ValidationError):
            p.add_decision(engineer("D1", C5, field="section", value=section(300, 300)))
        p.add_decision(engineer("D2", C5, field="section", value=section(300, 300), status=DecisionStatus.PROPOSED))
        with self.assertRaises(ValidationError):
            p.set_decision_status("D2", DecisionStatus.ACCEPTED)
        self.assertEqual(p.building.get_element("C5").section.width_mm, 225)

    def test_a_file_whose_model_disagrees_with_its_latest_decision_is_rejected(self):
        p = make_project()
        set_section(p, "D1", C5, 300, 300)
        data = OracleProject.from_json(p.to_json()).to_dict()
        column = next(c for c in data["building"]["elements"]["columns"] if c["id"] == "C5")
        column["section"]["width_mm"] = 999
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_a_change_that_answers_a_recommendation_records_the_outcome(self):
        p = make_project()
        p.add_decision(recommendation("R1", C5, field="section", value=section(300, 300)))
        p.set_value(C5, "section", section(350, 350),
                    engineer("D1", C5, field="section", value=section(350, 350), responds_to="R1",
                             overrides_recommendation=True))
        self.assertEqual(p.get_decision("R1").status, DecisionStatus.OVERRIDDEN)
        self.assertEqual(p.building.get_element("C5").section.width_mm, 350)
        self.assertEqual(p.get_decision("R1").value, section(300, 300))        # Oracle's proposal is preserved as proposed

    def test_element_replacement_that_breaks_a_relationship_is_refused(self):
        p = make_project()
        with self.assertRaises(ValidationError):
            p.building.replace_element(type(p.building.get_element("B1"))(
                "B1", "FF", "N1", "N404", p.building.get_element("B1").section))
        with self.assertRaises(ValidationError):
            p.building.replace_element(type(p.building.get_element("B1"))(
                "B404", "FF", "N1", "N2", p.building.get_element("B1").section))


class SupersessionTests(unittest.TestCase):
    def test_supersession_needs_the_same_target_and_field_and_no_loops(self):
        p = make_project()
        p.add_decision(engineer("D1", B1))
        p.add_decision(engineer("D2", C5))
        p.add_decision(engineer("D3", B1))
        with self.assertRaises(ValidationError):
            p.supersede_decision("D1", "D2")                                   # different target
        self.assertEqual(p.get_decision("D1").status, DecisionStatus.ACCEPTED)
        with self.assertRaises(ValidationError):
            p.supersede_decision("D1", "D1")
        self.assertEqual((p.get_decision("D1").status, p.get_decision("D1").superseded_by), (DecisionStatus.ACCEPTED, None))
        p.supersede_decision("D1", "D3")
        with self.assertRaises(ValidationError):
            p.supersede_decision("D3", "D1")                                   # would close a loop
        self.assertEqual(p.get_decision("D3").status, DecisionStatus.ACCEPTED)
        self.assertEqual(p.get_decision("D1").superseded_by, "D3")

    def test_a_loop_in_a_file_is_rejected(self):
        p = make_project()
        p.add_decision(engineer("D1", B1))
        p.add_decision(engineer("D2", B1))
        data = p.to_dict()
        data["decisions"][0].update(status="superseded", superseded_by="D2")
        data["decisions"][1].update(status="superseded", superseded_by="D1")
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_a_superseded_engineer_decision_still_backs_history(self):
        p = make_project()
        p.add_decision(engineer("D1", B1))
        p.set_value_status(ValueStatusRecord(B1, "material", ValueStatus.ENGINEER_DEFINED, decision_id="D1"))
        p.add_decision(engineer("D2", B1))
        p.supersede_decision("D1", "D2")
        OracleProject.from_json(p.to_json())                                   # still valid


class IssueEvidenceAndReadinessTests(unittest.TestCase):
    def setUp(self):
        self.p = make_project()
        self.p.add_provenance(ProvenanceRecord("PV-00001", B1, SourceReference(layer="F.F BEAMS", source_id="member 1"),
                                               "beam segment", "ga_dxf_parser"))
        self.p.add_provenance(ProvenanceRecord("PV-00002", C5, SourceReference(layer="COLUMN G-1", source_id="member 5"),
                                               "outline centroid", "ga_dxf_parser"))

    def blocking(self, iid="I1", **kw):
        return EngineeringIssue(iid, IssueSeverity.BLOCKING, IssueCategory.UNSUPPORTED_BEAM,
                                "Column C5 terminates on the interior of Beam B1, but the source beam geometry contains "
                                "no corresponding structural node.", B1, "test", **kw)

    def test_an_issue_can_cite_evidence_related_objects_and_a_decision(self):
        p = self.p
        p.add_decision(engineer("D1", B1))
        issue = self.blocking(evidence=["PV-00001", "PV-00002"], related=[C5])
        p.add_issue(issue)
        p.resolve_issue("I1", "Engineer confirmed the beam is continuous over the column.", decision_id="D1")
        q = OracleProject.from_json(p.to_json())
        got = q.get_issue("I1")
        self.assertEqual((got.evidence, got.related, got.decision_id, got.status),
                         (("PV-00001", "PV-00002"), (C5,), "D1", IssueStatus.RESOLVED))
        self.assertEqual(q.to_json(), p.to_json())

    def test_issue_references_are_checked(self):
        p = self.p
        for bad in (self.blocking(evidence=["PV-99999"]), self.blocking(related=[Target.element("C404")]),
                    self.blocking(interpretation_id="INT-404"), self.blocking(decision_id="D404")):
            with self.assertRaises(ValidationError):
                p.add_issue(bad)
        with self.assertRaises(ValidationError):
            self.blocking(related=["C5"])                                       # must be Targets
        self.assertEqual(p.issues, [])

    def test_unresolved_blocking_issue_stops_the_project_being_ready(self):
        p = self.p
        self.assertTrue(p.readiness().ready)
        p.add_issue(self.blocking())
        p.add_issue(EngineeringIssue("I2", IssueSeverity.WARNING, IssueCategory.SUSPICIOUS_SPAN, "Long.", B1, "test"))
        p.add_issue(EngineeringIssue("I3", IssueSeverity.ERROR, IssueCategory.OTHER, "An error.", B1, "test"))
        p.add_issue(EngineeringIssue("I4", IssueSeverity.INFO, IssueCategory.OTHER, "Note.", B1, "test"))
        readiness = p.readiness()
        self.assertFalse(readiness.ready)
        self.assertEqual([b.reference for b in readiness.blockers], ["I1"])   # only the blocking issue blocks
        self.assertIn("1 blocking issue", readiness.summary())
        p.add_decision(engineer("D1", B1))
        p.accept_issue("I1", "Engineer accepts this as drawn.", decision_id="D1")
        self.assertTrue(p.readiness().ready)

    def test_resolving_a_blocking_issue_makes_it_ready_again(self):
        p = self.p
        p.add_issue(self.blocking())
        self.assertFalse(p.readiness().ready)
        p.resolve_issue("I1", "Added a node under C5.")
        self.assertTrue(p.readiness().ready)


if __name__ == "__main__":
    unittest.main()
