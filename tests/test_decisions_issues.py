"""Tests for Oracle's decisions and issues

Protects:
    Decision creation, attachment to project, level and element targets, override and
    supersession rules, and issue severity/status handling, including that accepting an error or
    blocking issue needs a recorded engineer decision.

Test type:
    Unit tests.

Dependencies:
    oracle.core only.
"""

import unittest

from oracle.core import (
    DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision, EngineeringIssue, IssueCategory,
    IssueSeverity, IssueStatus, OracleProject, Target, ValidationError,
)
from tests.fixtures import make_project


def decision(did="D1", target=None, **kw):
    kw.setdefault("author", "A. Engineer")
    kw.setdefault("source", DecisionSource.ENGINEER)
    kw.setdefault("category", DecisionCategory.LAYOUT)
    kw.setdefault("instruction", "Keep column C12 aligned with the architectural wall.")
    return EngineeringDecision(did, target=target or Target.project(), **kw)


def issue(iid="I1", target=None, **kw):
    kw.setdefault("severity", IssueSeverity.WARNING)
    kw.setdefault("category", IssueCategory.SUSPICIOUS_SPAN)
    kw.setdefault("message", "Beam span looks unusually long.")
    kw.setdefault("source", "test")
    return EngineeringIssue(iid, target=target or Target.project(), **kw)


class DecisionTests(unittest.TestCase):
    def test_create_decision_defaults(self):
        d = decision(target=Target.element("C5"), reason="Wall above is load-bearing.")
        self.assertEqual(d.status, DecisionStatus.PROPOSED)
        self.assertFalse(d.overrides_recommendation)
        self.assertRegex(d.created_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(d.reason, "Wall above is load-bearing.")

    def test_validation(self):
        with self.assertRaises(ValidationError):
            decision(instruction="   ")
        with self.assertRaises(ValidationError):
            decision(author="")
        with self.assertRaises(ValidationError):
            decision(category="vibes")
        with self.assertRaises(ValidationError):
            decision(created_at="yesterday")
        with self.assertRaises(ValidationError):
            Target.element("")

    def test_attach_to_project_and_element(self):
        p = make_project()
        p.add_decision(decision("D1"))
        p.add_decision(decision("D2", Target.element("C5")))
        p.add_decision(decision("D3", Target.level("FF")))
        self.assertEqual([d.id for d in p.decisions], ["D1", "D2", "D3"])
        self.assertEqual([d.id for d in p.decisions_for(Target.element("C5"))], ["D2"])
        self.assertEqual([d.id for d in p.decisions_for(Target.project())], ["D1"])

    def test_target_must_exist_and_ids_must_be_unique(self):
        p = make_project()
        with self.assertRaises(ValidationError):
            p.add_decision(decision("D1", Target.element("C404")))
        with self.assertRaises(ValidationError):
            p.add_decision(decision("D1", Target.level("L404")))
        p.add_decision(decision("D1"))
        with self.assertRaises(ValidationError):
            p.add_decision(decision("D1"))
        empty = OracleProject.create("No building", "E")
        with self.assertRaises(ValidationError):
            empty.add_decision(decision("D1", Target.element("C1")))
        empty.add_decision(decision("D1"))  # project-wide is fine without a building

    def test_only_engineer_can_override_a_recommendation(self):
        d = decision(overrides_recommendation=True, oracle_recommendation="Use 225x225 columns.",
                     status=DecisionStatus.OVERRIDDEN)
        self.assertTrue(d.overrides_recommendation)
        with self.assertRaises(ValidationError):
            decision(source=DecisionSource.AI_ASSISTANT, overrides_recommendation=True)
        with self.assertRaises(ValidationError):
            decision(oracle_recommendation="text without the flag")

    def test_status_changes_and_supersession(self):
        p = make_project()
        p.add_decision(decision("D1"))
        p.add_decision(decision("D2", instruction="Actually use 300x300."))
        p.set_decision_status("D1", DecisionStatus.ACCEPTED)
        self.assertEqual(p.get_decision("D1").status, DecisionStatus.ACCEPTED)
        with self.assertRaises(ValidationError):
            p.set_decision_status("D1", DecisionStatus.SUPERSEDED)
        p.supersede_decision("D1", "D2")
        self.assertEqual(p.get_decision("D1").status, DecisionStatus.SUPERSEDED)
        self.assertEqual(p.get_decision("D1").superseded_by, "D2")
        with self.assertRaises(ValidationError):
            p.supersede_decision("D2", "D404")
        with self.assertRaises(ValidationError):
            p.supersede_decision("D2", "D2")

    def test_superseded_status_requires_superseder(self):
        with self.assertRaises(ValidationError):
            decision(status=DecisionStatus.SUPERSEDED)
        with self.assertRaises(ValidationError):
            decision(superseded_by="D2")

    def test_roundtrip(self):
        d = decision(target=Target.element("C12"), reason="r", overrides_recommendation=True,
                     oracle_recommendation="x", status=DecisionStatus.ACCEPTED)
        self.assertEqual(EngineeringDecision.from_dict(d.to_dict()).to_dict(), d.to_dict())


class IssueTests(unittest.TestCase):
    def test_create_issue(self):
        i = issue(severity=IssueSeverity.BLOCKING, category=IssueCategory.MISSING_SUPPORT,
                  target=Target.element("B3"))
        self.assertEqual(i.status, IssueStatus.OPEN)
        self.assertTrue(i.is_open)
        self.assertEqual(i.severity, IssueSeverity.BLOCKING)
        self.assertEqual([s.value for s in IssueSeverity], ["info", "warning", "error", "blocking"])

    def test_bad_values(self):
        with self.assertRaises(ValidationError):
            issue(severity="catastrophic")
        with self.assertRaises(ValidationError):
            issue(message="")
        with self.assertRaises(ValidationError):
            issue(source=" ")

    def test_resolve(self):
        i = issue()
        i.resolve("Added a trimmer beam at grid B.")
        self.assertEqual(i.status, IssueStatus.RESOLVED)
        self.assertEqual(i.resolution, "Added a trimmer beam at grid B.")
        self.assertFalse(i.is_open)

    def test_closing_needs_a_note(self):
        i = issue()
        with self.assertRaises(ValidationError):
            i.resolve("")
        self.assertEqual(i.status, IssueStatus.OPEN)  # a rejected close leaves the issue untouched
        self.assertIsNone(i.resolution)
        with self.assertRaises(ValidationError):
            issue(status=IssueStatus.RESOLVED)

    def test_accepting_error_or_blocking_needs_an_engineer_decision(self):
        for sev in (IssueSeverity.ERROR, IssueSeverity.BLOCKING):
            i = issue(severity=sev)
            with self.assertRaises(ValidationError):
                i.accept("Fine as is.")
            self.assertEqual(i.status, IssueStatus.OPEN)
            i.accept("Fine as is.", decision_id="D1")
            self.assertEqual(i.status, IssueStatus.ACCEPTED)
        w = issue(severity=IssueSeverity.WARNING)
        w.accept("Known and acceptable.")
        self.assertEqual(w.status, IssueStatus.ACCEPTED)

    def test_project_issue_workflow(self):
        p = make_project()
        p.add_issue(issue("I1", Target.element("B1"), severity=IssueSeverity.BLOCKING))
        p.add_issue(issue("I2", Target.level("FF")))
        p.add_issue(issue("I3", Target.project(), severity=IssueSeverity.INFO))
        self.assertTrue(p.has_blocking_issues())
        self.assertEqual({i.id for i in p.open_issues()}, {"I1", "I2", "I3"})
        self.assertEqual([i.id for i in p.issues_for(Target.element("B1"))], ["I1"])

        with self.assertRaises(ValidationError):  # accepting a blocking issue needs a recorded decision
            p.accept_issue("I1", "Engineer is happy.")
        with self.assertRaises(ValidationError):  # ...that actually exists
            p.accept_issue("I1", "Engineer is happy.", decision_id="D404")
        p.add_decision(decision("D1", Target.element("B1")))
        p.accept_issue("I1", "Engineer is happy.", decision_id="D1")
        self.assertFalse(p.has_blocking_issues())
        p.resolve_issue("I2", "Fixed.")
        self.assertEqual({i.id for i in p.open_issues()}, {"I3"})
        self.assertEqual(p.get_issue("I1").decision_id, "D1")

    def test_issue_target_and_decision_reference_validated(self):
        p = make_project()
        with self.assertRaises(ValidationError):
            p.add_issue(issue("I1", Target.element("C404")))
        with self.assertRaises(ValidationError):
            p.add_issue(issue("I1", status=IssueStatus.ACCEPTED, resolution="ok", decision_id="D404"))
        p.add_issue(issue("I1"))
        with self.assertRaises(ValidationError):
            p.add_issue(issue("I1"))

    def test_roundtrip(self):
        i = issue(severity=IssueSeverity.ERROR, status=IssueStatus.ACCEPTED, resolution="ok", decision_id="D1")
        self.assertEqual(EngineeringIssue.from_dict(i.to_dict()).to_dict(), i.to_dict())


if __name__ == "__main__":
    unittest.main()
