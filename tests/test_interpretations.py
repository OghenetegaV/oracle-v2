"""Tests for Oracle's alternative interpretations (oracle.core, schema 0.2.0)

Protects:
    InterpretationSet / Interpretation: holding several confidence-ranked readings of the same source
    evidence, the engineer accepting or rejecting them through a recorded engineer decision, derived
    set status, unresolved ambiguity blocking readiness, cross-reference checks, persistence, and
    rejection of malformed interpretation data.

Test type:
    Unit tests.

Dependencies:
    oracle.core only.
"""

import unittest

from oracle.core import (
    DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision, EngineeringIssue, Interpretation,
    InterpretationSet, InterpretationStatus, IssueCategory, IssueSeverity, OracleProject, ProvenanceRecord, SetStatus,
    SourceReference, Target, ValidationError,
)
from tests.fixtures import make_project
from tests.tiers import tier

S1 = Target.element("S1")


def region_set(set_id="IS-1", **kw):
    """The user's example: one rectangular region, three readings."""
    kw.setdefault("question", "What is the rectangular region at (0-5000, 0-5000)?")
    kw.setdefault("evidence", ("PV-00001",))
    return InterpretationSet(set_id, alternatives=[
        Interpretation("INT-A", "slab_panel", 0.82, "Bounded by four beams, no hatch."),
        Interpretation("INT-B", "void", 0.14, "Could be an unshaded opening."),
        Interpretation("INT-C", "balcony", 0.04),
    ], **kw)


def engineer_decision(did, **kw):
    kw.setdefault("instruction", "It is a slab panel.")
    return EngineeringDecision(did, "A. Engineer", DecisionSource.ENGINEER, kw.pop("target", Target.project()),
                               DecisionCategory.ARCHITECTURAL_COORDINATION, status=DecisionStatus.ACCEPTED, **kw)


def project_with_evidence():
    p = make_project()
    p.add_provenance(ProvenanceRecord("PV-00001", S1, SourceReference(layer="F.F BEAMS", entity_type="closed region"),
                                      "region enclosed by beams", "interpreter"))
    return p


@tier("unit")
class InterpretationModelTests(unittest.TestCase):
    def test_a_set_holds_ranked_alternatives_for_the_same_evidence(self):
        s = region_set()
        self.assertEqual([a.meaning for a in s.ranked()], ["slab_panel", "void", "balcony"])
        self.assertEqual([a.confidence for a in s.ranked()], [0.82, 0.14, 0.04])
        self.assertEqual(s.status, SetStatus.OPEN)
        self.assertIsNone(s.accepted)
        self.assertTrue(all(a.status == InterpretationStatus.PROPOSED and a.decision_id is None for a in s.alternatives))
        self.assertEqual(s.evidence, ("PV-00001",))

    def test_ranking_follows_confidence_not_listed_order(self):
        s = InterpretationSet("IS-2", "?", [Interpretation("X", "void", 0.1), Interpretation("Y", "slab_panel", 0.9)])
        self.assertEqual([a.id for a in s.ranked()], ["Y", "X"])

    def test_accepting_one_rejects_the_rest_under_the_same_decision(self):
        s = region_set()
        s.accept("INT-A", "D1")
        self.assertEqual([(a.id, a.status, a.decision_id) for a in s.alternatives],
                         [("INT-A", InterpretationStatus.ACCEPTED, "D1"), ("INT-B", InterpretationStatus.REJECTED, "D1"),
                          ("INT-C", InterpretationStatus.REJECTED, "D1")])
        self.assertEqual((s.status, s.accepted.meaning), (SetStatus.RESOLVED, "slab_panel"))

    def test_the_engineer_may_prefer_a_low_confidence_reading(self):
        s = region_set()
        s.accept("INT-B", "D1")
        self.assertEqual(s.accepted.meaning, "void")

    def test_rejecting_alternatives_one_by_one(self):
        s = region_set()
        s.reject("INT-B", "D1")
        s.reject("INT-C", "D2")
        self.assertEqual(s.status, SetStatus.OPEN)                             # A is still proposed
        s.accept("INT-A", "D3")
        self.assertEqual([a.decision_id for a in s.alternatives], ["D3", "D1", "D2"])   # earlier rejections keep their own decision
        t = region_set("IS-3")
        for a, d in (("INT-A", "D1"), ("INT-B", "D2"), ("INT-C", "D3")):
            t.reject(a, d)
        self.assertEqual((t.status, t.accepted), (SetStatus.NONE_APPLY, None))

    def test_bad_transitions_change_nothing(self):
        s = region_set()
        s.accept("INT-A", "D1")
        with self.assertRaises(ValidationError):
            s.accept("INT-B", "D2")                                            # already settled
        with self.assertRaises(ValidationError):
            s.reject("INT-A", "D2")                                            # already accepted
        with self.assertRaises(ValidationError):
            region_set().accept("INT-Z", "D1")                                 # unknown alternative
        with self.assertRaises(ValidationError):
            region_set().accept("INT-A", "not valid!")
        self.assertEqual(s.accepted.id, "INT-A")

    def test_validation(self):
        for bad in (-0.01, 1.01, float("nan"), "0.5", None):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                Interpretation("X", "void", bad)
        with self.assertRaises(ValidationError):
            Interpretation("X", "  ", 0.5)
        with self.assertRaises(ValidationError):
            Interpretation("X", "void", 0.5, status="accepted")                # accepted needs a decision
        with self.assertRaises(ValidationError):
            Interpretation("X", "void", 0.5, decision_id="D1")                 # proposed has none yet
        with self.assertRaises(ValidationError):
            InterpretationSet("IS-9", "?", [])
        with self.assertRaises(ValidationError):
            InterpretationSet("IS-9", "?", [Interpretation("X", "void", 0.5), Interpretation("X", "slab", 0.4)])
        with self.assertRaises(ValidationError):
            InterpretationSet("IS-9", "?", [Interpretation("X", "void", 0.5), Interpretation("Y", "Void", 0.4)])
        with self.assertRaises(ValidationError):
            InterpretationSet("IS-9", "  ", [Interpretation("X", "void", 0.5)])
        with self.assertRaises(ValidationError):
            InterpretationSet("IS-9", "?", ["void"])
        with self.assertRaises(ValidationError):
            InterpretationSet("IS-9", "?", [Interpretation("X", "void", 0.5, "r", status="accepted", decision_id="D1"),
                                            Interpretation("Y", "slab", 0.4, "r", status="accepted", decision_id="D1")])


@tier("unit")
class InterpretationInProjectTests(unittest.TestCase):
    def test_add_query_and_readiness(self):
        p = project_with_evidence()
        self.assertTrue(p.readiness().ready)
        p.add_interpretation_set(region_set(subject=S1))
        self.assertEqual([s.id for s in p.open_interpretation_sets()], ["IS-1"])
        readiness = p.readiness()
        self.assertFalse(readiness.ready)
        self.assertEqual([(b.kind.value, b.reference) for b in readiness.blockers], [("open_interpretation", "IS-1")])
        self.assertIn("unresolved interpretation", readiness.summary())

    def test_the_engineer_selects_a_reading_through_a_decision(self):
        p = project_with_evidence()
        p.add_interpretation_set(region_set(subject=S1))
        p.add_decision(engineer_decision("D1", target=S1))
        p.accept_interpretation("IS-1", "INT-A", "D1")
        s = p.get_interpretation_set("IS-1")
        self.assertEqual((s.status, s.accepted.meaning), (SetStatus.RESOLVED, "slab_panel"))
        self.assertEqual([a.status.value for a in s.alternatives], ["accepted", "rejected", "rejected"])
        self.assertEqual(p.open_interpretation_sets(), [])
        self.assertTrue(p.readiness().ready)

    def test_selection_needs_a_real_accepted_engineer_decision(self):
        p = project_with_evidence()
        p.add_interpretation_set(region_set())
        with self.assertRaises(ValidationError):
            p.accept_interpretation("IS-1", "INT-A", "D404")
        p.add_decision(EngineeringDecision("R1", "Oracle", DecisionSource.ORACLE, Target.project(),
                                           DecisionCategory.OTHER, "Oracle thinks it is a slab."))
        with self.assertRaises(ValidationError):                               # Oracle cannot choose for the engineer
            p.accept_interpretation("IS-1", "INT-A", "R1")
        p.add_decision(EngineeringDecision("D2", "A. Engineer", DecisionSource.ENGINEER, Target.project(),
                                           DecisionCategory.OTHER, "Thinking about it."))  # proposed, not accepted
        with self.assertRaises(ValidationError):                               # not yet accepted
            p.accept_interpretation("IS-1", "INT-A", "D2")
        with self.assertRaises(ValidationError):
            p.accept_interpretation("IS-404", "INT-A", "D2")
        self.assertEqual(p.get_interpretation_set("IS-1").status, SetStatus.OPEN)

    def test_engineer_rejects_readings_one_at_a_time(self):
        p = project_with_evidence()
        p.add_interpretation_set(region_set())
        p.add_decision(engineer_decision("D1", instruction="Not a balcony."))
        p.reject_interpretation("IS-1", "INT-C", "D1")
        s = p.get_interpretation_set("IS-1")
        self.assertEqual(s.get("INT-C").status, InterpretationStatus.REJECTED)
        self.assertEqual(s.status, SetStatus.OPEN)
        with self.assertRaises(ValidationError):
            p.reject_interpretation("IS-1", "INT-C", "D1")

    def test_ids_subjects_and_evidence_are_checked(self):
        p = project_with_evidence()
        p.add_interpretation_set(region_set())
        with self.assertRaises(ValidationError):
            p.add_interpretation_set(region_set())                             # duplicate set id
        other = InterpretationSet("IS-2", "?", [Interpretation("INT-A", "wall", 0.5)])
        with self.assertRaises(ValidationError):
            p.add_interpretation_set(other)                                    # alternative ids are project-wide
        with self.assertRaises(ValidationError):
            p.add_interpretation_set(region_set("IS-3", subject=Target.element("S404")))
        with self.assertRaises(ValidationError):
            p.add_interpretation_set(InterpretationSet("IS-4", "?", [Interpretation("Z", "void", 0.5)],
                                                       evidence=("PV-99999",)))
        with self.assertRaises(ValidationError):
            p.add_interpretation_set(InterpretationSet("IS-5", "?", [Interpretation("Z", "void", 0.5, evidence=("PV-99999",))]))
        self.assertEqual([s.id for s in p.interpretation_sets], ["IS-1"])

    def test_an_issue_can_point_at_an_interpretation(self):
        p = project_with_evidence()
        p.add_interpretation_set(region_set(subject=S1))
        p.add_issue(EngineeringIssue("I1", IssueSeverity.WARNING, IssueCategory.AMBIGUOUS_GEOMETRY,
                                     "Region could be a slab or a void.", S1, "interpreter", interpretation_id="INT-B",
                                     evidence=["PV-00001"]))
        self.assertEqual(OracleProject.from_json(p.to_json()).get_issue("I1").interpretation_id, "INT-B")

    def test_open_and_resolved_sets_survive_save_and_load(self):
        p = project_with_evidence()
        p.add_interpretation_set(region_set(subject=S1))
        p.add_interpretation_set(InterpretationSet("IS-2", "Balcony or projection?", [
            Interpretation("INT-P", "balcony", 0.6, "Outside the grid."), Interpretation("INT-Q", "projection", 0.3)]))
        p.add_decision(engineer_decision("D1", target=S1))
        p.accept_interpretation("IS-1", "INT-A", "D1")
        q = OracleProject.from_json(p.to_json())
        self.assertEqual(q.to_json(), p.to_json())
        self.assertEqual(q.get_interpretation_set("IS-1").status, SetStatus.RESOLVED)
        self.assertEqual(q.get_interpretation_set("IS-1").accepted.decision_id, "D1")
        self.assertEqual([a.confidence for a in q.get_interpretation_set("IS-1").alternatives], [0.82, 0.14, 0.04])
        self.assertEqual(q.get_interpretation_set("IS-1").subject, S1)
        self.assertEqual([s.id for s in q.open_interpretation_sets()], ["IS-2"])
        self.assertEqual(q.get_interpretation_set("IS-2").alternatives[0].rationale, "Outside the grid.")

    def test_malformed_interpretation_data_is_rejected_on_load(self):
        p = project_with_evidence()
        p.add_interpretation_set(region_set(subject=S1))
        p.add_decision(engineer_decision("D1"))
        def broken(mutate):
            data = OracleProject.from_json(p.to_json()).to_dict()
            mutate(data["interpretations"][0])
            with self.assertRaises(ValidationError):
                OracleProject.from_dict(data)
        broken(lambda s: s.update(alternatives=[]))
        broken(lambda s: s.update(alternatives="slab"))
        broken(lambda s: s["alternatives"][0].update(confidence=3))
        broken(lambda s: s["alternatives"][0].update(status="maybe"))
        broken(lambda s: s["alternatives"][0].update(status="accepted"))          # no decision
        broken(lambda s: s["alternatives"][1].update(decision_id="D1"))            # proposed but has a decision
        broken(lambda s: s["alternatives"][0].update(status="accepted", decision_id="D404"))
        broken(lambda s: [a.update(status="accepted", decision_id="D1") for a in s["alternatives"][:2]])
        broken(lambda s: s.update(subject={"scope": "element", "id": "S404"}))
        broken(lambda s: s.update(evidence=["PV-404"]))
        broken(lambda s: s.pop("question"))
        broken(lambda s: s.update(unexpected=True))
        broken(lambda s: s["alternatives"][0].update(id="INT-B"))                  # duplicate alternative id
        dup = OracleProject.from_json(p.to_json()).to_dict()
        dup["interpretations"].append(dict(dup["interpretations"][0]))
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(dup)

    def test_a_loaded_accepted_reading_must_be_backed_by_an_engineer_decision(self):
        p = project_with_evidence()
        p.add_interpretation_set(region_set())
        p.add_decision(engineer_decision("D1"))
        p.accept_interpretation("IS-1", "INT-A", "D1")
        data = OracleProject.from_json(p.to_json()).to_dict()
        data["decisions"][0]["source"] = "oracle"
        data["decisions"][0]["status"] = "proposed"
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)


if __name__ == "__main__":
    unittest.main()
