"""Tests for Oracle's OracleProject

Protects:
    Project identity and metadata, JSON round-trip and determinism, schema-version and
    unknown-field rejection, load-time reference validation, modified_at behaviour and atomic
    save/load.

Test type:
    Unit tests.

Dependencies:
    oracle.core only; a temporary directory for the file test.
"""

import json
import tempfile
import unittest
from pathlib import Path

from oracle.core import (
    SCHEMA_VERSION, DecisionCategory, DecisionSource, EngineeringDecision, EngineeringIssue, IssueCategory,
    GridLine, IssueSeverity, OracleProject, Point2D, SchemaVersionError, Target, ValidationError,
)
from tests.fixtures import make_project


class ProjectTests(unittest.TestCase):
    def test_create_project_metadata(self):
        p = OracleProject.create("Job 1", "Engineer A", client="C", location="Abuja", description="d")
        self.assertEqual(p.name, "Job 1")
        self.assertEqual(p.schema_version, SCHEMA_VERSION)
        self.assertEqual(len(p.project_id), 32)
        self.assertEqual(p.created_at, p.modified_at)
        self.assertIsNone(p.building)
        self.assertIsNone(p.design_basis)

    def test_requires_name_and_engineer(self):
        with self.assertRaises(ValidationError):
            OracleProject.create("  ", "Engineer")
        with self.assertRaises(ValidationError):
            OracleProject.create("Job", "")

    def test_roundtrip_preserves_ids_and_metadata(self):
        p = make_project()
        p.add_decision(EngineeringDecision("D1", "A. Engineer", DecisionSource.ENGINEER, Target.element("C5"),
                                           DecisionCategory.LAYOUT, "Keep C5 aligned with the wall."))
        p.add_issue(EngineeringIssue("I1", IssueSeverity.WARNING, IssueCategory.SUSPICIOUS_SPAN, "Long span.",
                                     Target.element("B1"), "test"))
        q = OracleProject.from_json(p.to_json())
        self.assertEqual(q.project_id, p.project_id)
        self.assertEqual((q.name, q.client, q.location, q.engineer, q.description),
                         (p.name, p.client, p.location, p.engineer, p.description))
        self.assertEqual((q.created_at, q.modified_at), (p.created_at, p.modified_at))
        self.assertEqual([e.id for e in q.building.elements], [e.id for e in p.building.elements])
        self.assertEqual([lv.id for lv in q.building.levels], ["GF", "FF", "ROOF"])
        self.assertEqual(q.get_decision("D1").instruction, "Keep C5 aligned with the wall.")
        self.assertEqual(q.get_issue("I1").target, Target.element("B1"))
        self.assertEqual(q.design_basis.to_dict(), p.design_basis.to_dict())

    def test_serialisation_is_deterministic_and_idempotent(self):
        p = make_project()
        first = p.to_json()
        self.assertEqual(first, p.to_json())
        self.assertEqual(first, OracleProject.from_json(first).to_json())
        self.assertTrue(first.endswith("\n"))
        json.loads(first)  # plain JSON, nothing Python-specific

    def test_loading_does_not_bump_modified_at(self):
        p = make_project()
        p.modified_at = "2020-01-01T00:00:00Z"
        q = OracleProject.from_json(p.to_json())
        self.assertEqual(q.modified_at, "2020-01-01T00:00:00Z")

    def test_editing_after_load_updates_modified_at(self):
        p = make_project()
        p.modified_at = "2020-01-01T00:00:00Z"
        q = OracleProject.from_json(p.to_json())
        q.building.add_grid(GridLine("A", Point2D(0, 0), Point2D(0, 10000)))
        self.assertNotEqual(q.modified_at, "2020-01-01T00:00:00Z")

    def test_unsupported_schema_version_rejected(self):
        data = make_project().to_dict()
        data["schema_version"] = "9.0.0"
        with self.assertRaises(SchemaVersionError):
            OracleProject.from_dict(data)
        del data["schema_version"]
        with self.assertRaises(SchemaVersionError):
            OracleProject.from_dict(data)

    def test_unknown_field_rejected_rather_than_silently_dropped(self):
        data = make_project().to_dict()
        data["surprise"] = 1
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_invalid_json_rejected(self):
        with self.assertRaises(ValidationError):
            OracleProject.from_json("{not json")

    def test_corrupt_reference_rejected_on_load(self):
        data = make_project().to_dict()
        data["building"]["elements"]["beams"][0]["start_node_id"] = "NOPE"
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_save_and_load_file(self):
        p = make_project()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.oracle.json"
            p.save(path)
            self.assertFalse(Path(str(path) + ".tmp").exists())
            self.assertEqual(OracleProject.load(path).to_json(), p.to_json())


if __name__ == "__main__":
    unittest.main()
