"""Tests for Oracle's schema migration, readiness gate and core independence (oracle.core)

Protects:
    Loading a genuine schema-0.1.0 project (tests/fixtures/schema_0_1_0_project.json, written by the
    Phase 1 code at tag v2.0.0-phase1) into the current schema without losing or inventing anything, refusal
    of unknown versions, unchanged strict-keys policy, the readiness rules (blocking issues,
    unresolved interpretations, assumed values, inferred-but-unconfirmed values), and that oracle.core
    imports nothing from the legacy scripts, adapters or CAD libraries.

Test type:
    Unit and regression tests (the 0.1.0 fixture is a real old-format file).

Dependencies:
    oracle.core only; a subprocess for the import-independence checks.
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from oracle.core import (
    SCHEMA_VERSION, BlockerKind, DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision,
    EngineeringIssue, Interpretation, InterpretationSet, IssueCategory, IssueSeverity, OracleProject, ProvenanceRecord,
    SchemaVersionError, SourceReference, Target, ValidationError, ValueStatus, ValueStatusRecord,
)
from oracle.core.migrations import MIGRATIONS, migrate
from tests.fixtures import make_project
from tests.tiers import tier

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "schema_0_1_0_project.json"
FIXTURE_0_2_0 = REPO_ROOT / "tests" / "fixtures" / "schema_0_2_0_project.json"
B1, C5 = Target.element("B1"), Target.element("C5")


def old_file() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@tier("unit")
class MigrationTests(unittest.TestCase):
    def test_the_fixture_really_is_an_old_format_file(self):
        data = old_file()
        self.assertEqual(data["schema_version"], "0.1.0")
        for key in ("provenance", "value_status", "interpretations"):
            self.assertNotIn(key, data)
        self.assertEqual(SCHEMA_VERSION, "0.4.0")

    def test_a_0_1_0_project_loads_with_nothing_lost_and_nothing_invented(self):
        old = old_file()
        p = OracleProject.from_dict(old)
        self.assertEqual(p.schema_version, "0.4.0")
        self.assertEqual((p.project_id, p.name, p.engineer, p.client, p.location, p.created_at, p.modified_at),
                         (old["project_id"], old["name"], old["engineer"], old["client"], old["location"],
                          old["created_at"], old["modified_at"]))
        self.assertEqual(p.building.to_dict(), old["building"])
        self.assertEqual(p.design_basis.to_dict(), old["design_basis"])
        self.assertEqual([d.id for d in p.decisions], [d["id"] for d in old["decisions"]])
        self.assertEqual([i.id for i in p.issues], ["I1", "I2"])
        self.assertEqual(p.get_decision("D1").status, DecisionStatus.ACCEPTED)
        self.assertEqual(p.get_decision("D2").status, DecisionStatus.PROPOSED)   # the Oracle recommendation stays a proposal
        self.assertEqual((p.provenance, p.value_statuses, p.interpretation_sets), ([], [], []))
        self.assertEqual(p.get_issue("I2").severity, IssueSeverity.BLOCKING)

    def test_a_migrated_project_is_saved_as_the_current_schema_and_reloads(self):
        p = OracleProject.from_dict(old_file())
        text = p.to_json()
        self.assertEqual(json.loads(text)["schema_version"], "0.4.0")
        for key in ("provenance", "value_status", "interpretations"):
            self.assertEqual(json.loads(text)[key], [])
        self.assertEqual(OracleProject.from_json(text).to_json(), text)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migrated.oracle.json"
            path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            OracleProject.load(path).save(path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], "0.4.0")

    def test_a_migrated_project_can_take_the_new_records(self):
        p = OracleProject.from_dict(old_file())
        self.assertEqual(p.next_provenance_id(), "PV-00001")
        p.add_provenance(ProvenanceRecord("PV-00001", B1, SourceReference(source_id="hand entry"), "manual", "engineer"))
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ASSUMED))
        self.assertFalse(p.readiness().ready)                                     # I2 (blocking) and the assumed section
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())

    def test_migration_does_not_modify_its_input(self):
        old = old_file()
        snapshot = copy.deepcopy(old)
        result = migrate(old)
        self.assertEqual(old, snapshot)
        self.assertEqual(result["schema_version"], "0.4.0")
        self.assertIsNot(result["decisions"], old["decisions"])

    def test_current_schema_needs_no_migration(self):
        data = make_project().to_dict()
        self.assertEqual(migrate(data), data)

    def test_unknown_and_future_versions_are_refused(self):
        for version in ("9.0.0", "0.5.0", "0.0.1", "0.1", None, 2, ""):
            data = old_file()
            if version is None:
                del data["schema_version"]
            else:
                data["schema_version"] = version
            with self.assertRaises(SchemaVersionError, msg=repr(version)):
                OracleProject.from_dict(data)
        self.assertEqual(sorted(MIGRATIONS), ["0.1.0", "0.2.0", "0.3.0"])

    def test_non_objects_are_refused(self):
        for bad in ([], "0.1.0", None, 3):
            with self.assertRaises(ValidationError):
                OracleProject.from_dict(bad)

    def test_unknown_fields_are_still_rejected_after_migration(self):
        for mutate in (lambda d: d.update(surprise=1), lambda d: d["decisions"][0].update(surprise=1),
                       lambda d: d["issues"][0].update(surprise=1), lambda d: d["building"].update(surprise=1)):
            data = old_file()
            mutate(data)
            with self.assertRaises(ValidationError):
                OracleProject.from_dict(data)

    def test_a_current_file_must_carry_the_new_registries(self):
        for key in ("provenance", "value_status", "interpretations"):
            data = make_project().to_dict()
            del data[key]
            with self.assertRaises(ValidationError, msg=key):
                OracleProject.from_dict(data)

    def test_new_registries_in_an_old_file_are_refused_as_unknown_fields(self):
        data = old_file()
        data["provenance"] = [{"nonsense": True}]                                   # 0.1.0 never had this key
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_old_files_that_were_already_invalid_stay_invalid(self):
        data = old_file()
        data["building"]["elements"]["beams"][0]["start_node_id"] = "NOPE"
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)


@tier("unit")
class ReadinessTests(unittest.TestCase):
    def test_a_project_without_a_building_is_not_ready(self):
        readiness = OracleProject.create("Empty", "E").readiness()
        self.assertFalse(readiness.ready)
        self.assertEqual([b.kind for b in readiness.blockers], [BlockerKind.NO_BUILDING])

    def test_a_clean_project_is_ready(self):
        readiness = make_project().readiness()
        self.assertTrue(readiness.ready)
        self.assertEqual((readiness.blockers, readiness.unconfirmed), ([], []))
        self.assertTrue(readiness.summary().startswith("Ready"))

    def test_assumed_values_block_but_inferred_ones_are_only_listed(self):
        p = make_project()
        p.set_value_status(ValueStatusRecord(C5, "geometry", ValueStatus.INFERRED))
        p.set_value_status(ValueStatusRecord(B1, "geometry", ValueStatus.SOURCE))
        readiness = p.readiness()
        self.assertTrue(readiness.ready)
        self.assertEqual(readiness.unconfirmed, ["element C5.geometry"])
        self.assertIn("1 inferred value(s) not engineer-confirmed", readiness.summary())
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ASSUMED))
        readiness = p.readiness()
        self.assertFalse(readiness.ready)
        self.assertEqual([(b.kind, b.reference) for b in readiness.blockers],
                         [(BlockerKind.ASSUMED_VALUE, "element B1.section")])

    def test_confirming_an_assumed_value_lifts_the_block(self):
        p = make_project()
        p.set_value_status(ValueStatusRecord(C5, "section", ValueStatus.ASSUMED))
        self.assertFalse(p.readiness().ready)
        section = {"shape": "rectangular", "width_mm": 300, "depth_mm": 300}
        p.set_value(C5, "section", section, EngineeringDecision(
            "D1", "A. Engineer", DecisionSource.ENGINEER, C5, DecisionCategory.SECTION_SIZING, "C5 = 300x300",
            status=DecisionStatus.ACCEPTED, field="section", value=section))
        self.assertTrue(p.readiness().ready)

    def test_every_kind_of_blocker_is_reported_together_and_deterministically(self):
        p = make_project()
        p.add_provenance(ProvenanceRecord("PV-00001", B1, SourceReference(source_id="x"), "m", "p"))
        p.add_issue(EngineeringIssue("I1", IssueSeverity.BLOCKING, IssueCategory.MISSING_SUPPORT, "Unsupported.", B1, "t"))
        p.add_interpretation_set(InterpretationSet("IS-1", "Slab or void?", [Interpretation("A", "slab", 0.6),
                                                                              Interpretation("B", "void", 0.4)],
                                                   evidence=["PV-00001"]))
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ASSUMED))
        readiness = p.readiness()
        self.assertEqual([b.kind.value for b in readiness.blockers],
                         ["blocking_issue", "open_interpretation", "assumed_value"])
        self.assertEqual([b.reference for b in readiness.blockers], ["I1", "IS-1", "element B1.section"])
        self.assertEqual(readiness.summary(), "NOT ready for final engineering output: 1 blocking issue(s), "
                                              "1 unresolved interpretation(s), 1 assumed value(s).")
        self.assertEqual(len(readiness.by_kind(BlockerKind.ASSUMED_VALUE)), 1)
        again = OracleProject.from_json(p.to_json()).readiness()
        self.assertEqual([(b.kind, b.reference) for b in again.blockers], [(b.kind, b.reference) for b in readiness.blockers])


@tier("integration")
class CoreIndependenceTests(unittest.TestCase):
    def _imported(self, code):
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(REPO_ROOT), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_core_imports_no_legacy_module_adapter_or_cad_or_ai_library(self):
        forbidden = ("ga_dxf_parser", "dxf_parser", "config", "oracle_wizard", "oracle_pipeline", "staad_mock",
                     "staad_v8i_integration", "design_module", "claude_ga_generator", "lisp_detail_generator",
                     "dwg_detail_generator", "oracle.adapters", "ezdxf", "anthropic", "shapely", "matplotlib",
                     "tkinter", "win32com")
        result = self._imported(f"import sys, oracle.core; print(sorted(m for m in {forbidden!r} if m in sys.modules))")
        self.assertEqual(result, "[]")

    def test_core_source_names_no_cad_layer_conventions(self):
        for path in (REPO_ROOT / "oracle" / "core").glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for word in ("ezdxf", "import anthropic", "openstaad", "column g-1", "f.f beams"):
                self.assertNotIn(word, text, f"{path.name} mentions {word!r}")


if __name__ == "__main__":
    unittest.main()


@tier("unit")
class MigrationFrom020Tests(unittest.TestCase):
    """0.2.0 -> 0.4.0 (through 0.3.0): the architectural interpretation section and the 0.4.0 registries arrive empty;
    nothing else changes."""

    def data(self) -> dict:
        return json.loads(FIXTURE_0_2_0.read_text(encoding="utf-8"))

    def test_the_fixture_is_a_genuine_0_2_0_file(self):
        data = self.data()
        self.assertEqual(data["schema_version"], "0.2.0")
        self.assertNotIn("architecture", data)

    def test_a_0_2_0_project_loads_with_nothing_lost_and_no_architecture_invented(self):
        old = self.data()
        p = OracleProject.from_dict(old)
        self.assertEqual(p.schema_version, "0.4.0")
        self.assertIsNone(p.architecture)
        self.assertEqual(p.building.to_dict(), old["building"])
        self.assertEqual([d.id for d in p.decisions], [d["id"] for d in old["decisions"]])
        self.assertEqual([i.id for i in p.issues], [i["id"] for i in old["issues"]])
        self.assertEqual(len(p.provenance), len(old["provenance"]))
        self.assertEqual(len(p.value_statuses), len(old["value_status"]))

    def test_migration_does_not_modify_its_input_and_round_trips(self):
        old = self.data()
        snapshot = copy.deepcopy(old)
        result = migrate(old)
        self.assertEqual(old, snapshot)
        self.assertEqual(result["schema_version"], "0.4.0")
        self.assertEqual(result["architectures"], [])
        text = OracleProject.from_dict(old).to_json()
        self.assertEqual(OracleProject.from_json(text).to_json(), text)

    def test_an_architecture_key_in_a_0_2_0_file_is_refused(self):
        data = self.data()
        data["architecture"] = {}
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_a_current_file_must_carry_the_architecture_key(self):
        data = make_project().to_dict()
        del data["architectures"]
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)
