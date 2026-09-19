"""Tests for the legacy GA adapter's provenance, value status and structural issues

Protects:
    That oracle.adapters.legacy_ga writes provenance and value statuses into the OracleProject (and so
    into the saved file): every object traceable to file, layer, entity type, member/joint number and
    coordinates; geometry SOURCE or INFERRED; sections and default slab thickness ASSUMED and
    distinguishable from engineer input; nothing invented (no material, no design basis); a column
    landing on a beam interior without a node reported as a BLOCKING issue without splitting the beam;
    readiness reflecting all of that; and an engineer's override afterwards keeping the evidence.

Test type:
    Integration and regression tests on the real GA fixture (input_dwgs/1st Flr, 2nd Flr and Roof
    GAs.dxf), plus hand-built parse results for the failure paths. Builds on the helpers of
    tests/test_adapter_legacy_ga.py.

Dependencies:
    oracle.core, oracle.adapters, and the legacy ga_dxf_parser (real parser, run for real).
"""

import re
import unittest

from oracle.adapters import adapt_legacy_ga
from oracle.core import (
    DecisionCategory, DecisionSource, DecisionStatus, ElementKind, EngineeringDecision, IssueCategory, IssueSeverity,
    OracleProject, Target, ValueStatus,
)
from oracle.core.readiness import BlockerKind
from tests.test_adapter_legacy_ga import ARCH_DXF, ELEV, GA_DXF, SIZES, adapt, crafted, gp, issues_where, parse

SOURCE_NAME = "1st Flr, 2nd Flr and Roof GAs.dxf"


def status_of(project, kind, object_id, field):
    target = {"level": Target.level, "node": Target.node}.get(kind, Target.element)(object_id)
    record = project.value_status_of(target, field)
    return record.status if record else None


class AdapterEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = parse(GA_DXF, ELEV)
        cls.res = adapt(cls.parsed, ELEV, project_id="ev-proj", source_file=SOURCE_NAME)
        cls.p = cls.res.project
        cls.b = cls.res.building

    # ---- provenance
    def test_every_object_has_a_whole_object_provenance_record_naming_its_source(self):
        p = self.p
        objects = [Target.level(lv.id) for lv in self.b.levels] + [Target.node(n.id) for n in self.b.nodes] + \
                  [Target.element(e.id) for e in self.b.elements]
        for target in objects:
            whole = [r for r in p.provenance_for(target) if r.field is None]
            self.assertEqual(len(whole), 1, target)
            self.assertEqual(whole[0].producer, "ga_dxf_parser")
            self.assertTrue(whole[0].method and whole[0].source.source_id)
            self.assertEqual(whole[0].source.file, SOURCE_NAME)

    def test_a_beam_is_traceable_to_layer_member_and_coordinates(self):
        beam = next(e for e in self.b.elements_of(ElementKind.BEAM) if e.id == "B12")
        record = next(r for r in self.p.provenance_for(Target.element("B12")) if r.field is None)
        self.assertEqual((record.source.layer, record.source.entity_type, record.source.context),
                         ("F.F BEAMS", "polyline segment", "level 1"))
        self.assertRegex(record.source.source_id, r"^member 12 \(joints \d+-\d+\)$")
        (x1, e1, z1), (x2, e2, z2) = record.source.coordinates
        self.assertIn("metres", record.source.coordinate_frame)
        self.assertEqual(e1, e2)
        self.assertAlmostEqual(e1 * 1000, self.b.get_level(beam.level_id).elevation_mm)
        start, end = self.b.get_node(beam.start_node_id).location, self.b.get_node(beam.end_node_id).location
        self.assertAlmostEqual(x1 * 1000, start.x_mm, places=2)
        self.assertAlmostEqual(z2 * 1000, end.y_mm, places=2)

    def test_columns_slabs_and_nodes_carry_their_own_kind_of_evidence(self):
        column = self.p.provenance_for(Target.element("C53"))[0]
        self.assertEqual((column.source.layer, column.source.entity_type), ("COLUMN G-1", "column outline"))
        slab = self.p.provenance_for(Target.element("S1-1"))[0]
        self.assertEqual((slab.source.layer, slab.source.entity_type), ("F.F BEAMS", "slab panel"))
        self.assertEqual(len(slab.source.coordinates), 4)
        self.assertIn("plan frame", slab.source.coordinate_frame)
        node = self.p.provenance_for(Target.node(self.b.nodes[0].id))[0]
        self.assertEqual(node.source.entity_type, "joint")

    def test_provenance_that_is_not_in_the_drawing_does_not_claim_the_drawing_file(self):
        section = self.p.provenance_for(Target.element("B12"), "section")
        self.assertEqual(len(section), 1)
        self.assertIsNone(section[0].source.file)
        self.assertEqual(section[0].source.source_id, "DEFAULT_SIZES[beam_mm]")
        thickness = self.p.provenance_for(Target.element("S1-1"), "thickness_mm")[0]
        self.assertIsNone(thickness.source.file)

    def test_provenance_ids_are_unique_and_one_import_shares_a_timestamp(self):
        ids = [r.id for r in self.p.provenance]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], "PV-00001")
        self.assertEqual(len({r.recorded_at for r in self.p.provenance}), 1)

    # ---- value status
    def test_geometry_is_source_or_inferred_as_appropriate(self):
        self.assertEqual(status_of(self.p, "element", "B12", "geometry"), ValueStatus.SOURCE)
        self.assertEqual(status_of(self.p, "node", self.b.nodes[0].id, "geometry"), ValueStatus.SOURCE)
        self.assertEqual(status_of(self.p, "element", "C53", "geometry"), ValueStatus.INFERRED)   # an outline centroid
        self.assertEqual(status_of(self.p, "element", "S1-1", "geometry"), ValueStatus.INFERRED)
        self.assertEqual(status_of(self.p, "element", "S1-1", "supported_by"), ValueStatus.INFERRED)
        self.assertEqual(status_of(self.p, "element", "B12", "level_id"), ValueStatus.INFERRED)
        self.assertEqual(status_of(self.p, "element", "C53", "lower_level_id"), ValueStatus.INFERRED)

    def test_sections_and_default_thickness_are_assumed_not_facts(self):
        for e in self.b.elements_of(ElementKind.BEAM) + self.b.elements_of(ElementKind.COLUMN):
            self.assertEqual(status_of(self.p, "element", e.id, "section"), ValueStatus.ASSUMED, e.id)
        for s in self.b.elements_of(ElementKind.SLAB):
            self.assertEqual(status_of(self.p, "element", s.id, "thickness_mm"), ValueStatus.ASSUMED, s.id)
        assumed = self.p.values_with_status(ValueStatus.ASSUMED)
        self.assertEqual(len(assumed), 77 + 52 + 36 + 4)   # + the four generated level names
        for record in assumed:
            if record.field == "section":
                self.assertTrue(record.provenance_ids)                                  # says where the placeholder came from

    def test_levels_carry_engineer_input_derived_and_inferred_values(self):
        self.assertEqual(status_of(self.p, "level", "1", "elevation_mm"), ValueStatus.ENGINEER_DEFINED)
        self.assertEqual(status_of(self.p, "level", "1", "storey_height_mm"), ValueStatus.DERIVED)
        self.assertIsNone(status_of(self.p, "level", "R", "storey_height_mm"))           # the top level has none
        self.assertEqual(status_of(self.p, "level", "1", "id"), ValueStatus.INFERRED)
        self.assertEqual(status_of(self.p, "level", "1", "name"), ValueStatus.ASSUMED)   # "Level 1" is generated
        decision = self.p.get_decision(self.p.value_status_of(Target.level("1"), "elevation_mm").decision_id)
        self.assertEqual((decision.source, decision.status, decision.author),
                         (DecisionSource.ENGINEER, DecisionStatus.ACCEPTED, "A. Engineer"))
        self.assertIn("G=0", decision.instruction)

    def test_engineer_supplied_thickness_and_names_are_engineer_defined_and_backed_by_a_decision(self):
        loading = {"1": {"slab_thickness_mm": 175}, "2": {"slab_thickness_mm": 175}, "R": {"slab_thickness_mm": 150}}
        names = {"1": "First Floor", "R": "Roof"}
        res = adapt(parse(GA_DXF, ELEV, per_level_loading=loading), ELEV, engineer_loading=loading, level_names=names,
                    source_file=SOURCE_NAME)
        p = res.project
        record = p.value_status_of(Target.element("S1-1"), "thickness_mm")
        self.assertEqual(record.status, ValueStatus.ENGINEER_DEFINED)
        decision = p.get_decision(record.decision_id)
        self.assertEqual((decision.source, decision.target), (DecisionSource.ENGINEER, Target.level("1")))
        self.assertIn("175", decision.instruction)
        self.assertEqual(p.value_status_of(Target.level("1"), "name").status, ValueStatus.ENGINEER_DEFINED)
        self.assertEqual(p.value_status_of(Target.level("2"), "name").status, ValueStatus.ASSUMED)   # not given
        self.assertEqual(p.building.get_level("1").name, "First Floor")
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())

    def test_nothing_is_invented_for_material_or_design_basis(self):
        self.assertIsNone(self.p.design_basis)
        for e in self.b.elements:
            self.assertIsNone(getattr(e, "material", None), e.id)
        self.assertEqual([r for r in self.p.value_statuses if r.field == "material"], [])
        self.assertTrue(issues_where(self.res, text="No design basis is attached"))

    # ---- structural geometry: no silent repair
    def test_a_column_ending_on_a_beam_interior_is_a_blocking_issue_and_the_beam_is_not_split(self):
        pair = [i for i in self.res.issues if "terminates on the interior of Beam" in i.message]
        self.assertGreaterEqual(len(pair), 16)
        for issue in pair:
            self.assertEqual((issue.severity, issue.category), (IssueSeverity.BLOCKING, IssueCategory.UNSUPPORTED_BEAM))
            match = re.match(r"Column (C\d+) terminates on the interior of Beam (B\d+) \(([\d.]+) m\), but the source "
                             r"beam geometry contains no corresponding structural node", issue.message)
            self.assertIsNotNone(match, issue.message)
            column_id, beam_id = match.group(1), match.group(2)
            self.assertEqual((issue.target, issue.related), (Target.element(beam_id), (Target.element(column_id),)))
            self.assertEqual(len(issue.evidence), 2)                                     # the beam's and the column's records
            cited = {self.p.get_provenance(e).target for e in issue.evidence}
            self.assertEqual(cited, {Target.element(beam_id), Target.element(column_id)})
            self.assertIn("The beam was not split", issue.message)
            self.assertIn("Engineer action: required", issue.message)
        self.assertEqual(len(self.b.elements_of(ElementKind.BEAM)), 52)                  # exactly as the parser gave them
        self.assertEqual(len(self.b.nodes), 130)

    def test_the_structural_finding_does_not_count_as_an_adapter_failure(self):
        self.assertTrue(self.res.complete)
        self.assertEqual(self.res.failures, 0)

    def test_a_beam_with_a_node_at_the_column_raises_no_such_issue(self):
        joints = {1: (0.0, 3.0, 0.0), 2: (5.0, 3.0, 0.0), 3: (10.0, 3.0, 0.0), 4: (5.0, 0.0, 0.0)}
        members = [(1, 1, 2, "beam", "1", "beam_mm"), (2, 2, 3, "beam", "1", "beam_mm"),
                   (3, 4, 2, "column", ("G", "1"), "column_mm")]
        res = adapt(crafted(joints, members), {"G": 0.0, "1": 3.0})
        self.assertEqual(issues_where(res, text="terminates on the interior"), [])

    def test_the_issue_message_for_a_crafted_case_matches_the_required_wording(self):
        joints = {1: (0.0, 3.0, 0.0), 2: (10.0, 3.0, 0.0), 3: (5.0, 0.0, 0.0), 4: (5.0, 3.0, 0.0)}
        members = [(23, 1, 2, "beam", "1", "beam_mm"), (16, 3, 4, "column", ("G", "1"), "column_mm")]
        res = adapt(crafted(joints, members), {"G": 0.0, "1": 3.0})
        [issue] = issues_where(res, text="terminates on the interior")
        self.assertTrue(issue.message.startswith(
            "Column C16 terminates on the interior of Beam B23 (10.0 m), but the source beam geometry contains no "
            "corresponding structural node"))
        self.assertEqual(issue.severity, IssueSeverity.BLOCKING)
        self.assertEqual(len(res.building.elements_of(ElementKind.BEAM)), 1)

    # ---- readiness
    def test_the_imported_project_is_not_ready_and_says_why(self):
        readiness = self.p.readiness()
        self.assertFalse(readiness.ready)
        kinds = {b.kind for b in readiness.blockers}
        self.assertEqual(kinds, {BlockerKind.BLOCKING_ISSUE, BlockerKind.ASSUMED_VALUE})
        self.assertEqual(len(readiness.by_kind(BlockerKind.BLOCKING_ISSUE)),
                         len([i for i in self.res.issues if i.severity == IssueSeverity.BLOCKING]))
        self.assertGreater(len(readiness.unconfirmed), 300)                              # inferred geometry is listed, not blocking
        self.assertEqual(self.res.readiness().summary(), readiness.summary())

    def test_an_engineer_can_override_an_assumed_section_and_the_evidence_stays(self):
        p = OracleProject.from_json(self.p.to_json())
        before = len(p.values_with_status(ValueStatus.ASSUMED))
        section = {"shape": "rectangular", "width_mm": 300, "depth_mm": 600}
        target = Target.element("B12")
        record = p.set_value(target, "section", section, EngineeringDecision(
            "D-B12", "A. Engineer", DecisionSource.ENGINEER, target, DecisionCategory.SECTION_SIZING,
            "B12 = 300x600 (transfer beam).", status=DecisionStatus.ACCEPTED, field="section", value=section))
        self.assertEqual((record.status, record.replaces), (ValueStatus.ENGINEER_OVERRIDE, ValueStatus.ASSUMED))
        self.assertEqual(p.building.get_element("B12").section.depth_mm, 600)
        self.assertEqual(len(p.values_with_status(ValueStatus.ASSUMED)), before - 1)
        self.assertEqual(p.get_decision("D-B12").previous_value["depth_mm"], SIZES["beam_mm"][1])
        self.assertEqual(len(p.provenance_for(target, "section")), 1)                    # the default-table evidence is kept
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())

    def test_an_engineer_can_accept_a_structural_issue_only_with_a_decision(self):
        p = OracleProject.from_json(self.p.to_json())
        issue = next(i for i in p.open_issues(IssueSeverity.BLOCKING))
        with self.assertRaises(Exception):
            p.accept_issue(issue.id, "Fine.")                                            # blocking: needs a decision
        p.add_decision(EngineeringDecision("D-ok", "A. Engineer", DecisionSource.ENGINEER, issue.target,
                                           DecisionCategory.ANALYSIS, "Beam is continuous over the column; not a support.",
                                           status=DecisionStatus.ACCEPTED))
        n = len(p.open_issues(IssueSeverity.BLOCKING))
        p.accept_issue(issue.id, "Engineer ruled it is not a support.", decision_id="D-ok")
        self.assertEqual(len(p.open_issues(IssueSeverity.BLOCKING)), n - 1)

    # ---- persistence
    def test_all_registries_survive_save_and_load(self):
        q = OracleProject.from_json(self.p.to_json())
        self.assertEqual(q.to_json(), self.p.to_json())
        for name in ("provenance", "value_statuses", "decisions", "issues"):
            self.assertEqual([x.to_dict() for x in getattr(q, name)], [x.to_dict() for x in getattr(self.p, name)], name)
        self.assertEqual(q.interpretation_sets, [])
        self.assertEqual(q.readiness().summary(), self.p.readiness().summary())
        self.assertEqual(q.next_provenance_id(), f"PV-{len(self.p.provenance) + 1:05d}")

    def test_the_same_input_gives_the_same_registries(self):
        again = adapt(self.parsed, ELEV, project_id="ev-proj", source_file=SOURCE_NAME).project
        strip = lambda rows: [{k: v for k, v in r.to_dict().items() if k != "recorded_at"} for r in rows]
        self.assertEqual(strip(again.provenance), strip(self.p.provenance))
        self.assertEqual([r.to_dict() for r in again.value_statuses], [r.to_dict() for r in self.p.value_statuses])
        self.assertEqual([i.to_dict() for i in again.issues], [i.to_dict() for i in self.p.issues])


class AdapterFailureEvidenceTests(unittest.TestCase):
    def test_a_failed_import_records_issues_but_no_invented_provenance_or_status(self):
        result = gp.parse_multilevel_ga(str(ARCH_DXF), storey_heights_m={"G": 0.0, "1": 3.0})
        res = adapt_legacy_ga(result, {"G": 0.0, "1": 3.0}, project_name="x", engineer="e", section_sizes_mm=SIZES,
                              source_file="test_floor.dxf")
        self.assertFalse(res.complete)
        self.assertGreater(res.failures, 0)
        self.assertEqual((res.project.provenance, res.project.value_statuses, res.project.decisions), ([], [], []))
        self.assertIsNone(res.building)
        self.assertFalse(res.readiness().ready)
        self.assertEqual([b.kind for b in res.readiness().blockers][0], BlockerKind.NO_BUILDING)
        OracleProject.from_json(res.project.to_json())

    def test_a_dropped_member_still_counts_as_a_failure(self):
        joints = {1: (0, 3.0, 0), 2: (5, 3.0, 0)}
        res = adapt(crafted(joints, [(1, 1, 2, "beam", "1", "beam_mm")]), {"G": 0.0, "1": 3.0}, section_sizes_mm={})
        self.assertFalse(res.complete)
        self.assertEqual(res.project.provenance_for(Target.element("B1")), [])          # nothing recorded for what was not built

    def test_single_floor_import_records_evidence_too(self):
        joints = {1: (0.0, 3.0, 0.0), 2: (5.0, 3.0, 0.0)}
        res = adapt(crafted(joints, [(1, 1, 2, "beam", "1", "beam_mm")]), {"G": 0.0, "1": 3.0})
        self.assertEqual(res.project.value_status_of(Target.element("B1"), "section").status, ValueStatus.ASSUMED)
        self.assertEqual(res.project.value_status_of(Target.element("B1"), "geometry").status, ValueStatus.SOURCE)
        self.assertIsNone(res.project.provenance_for(Target.element("B1"))[0].source.file)   # no source file was given


if __name__ == "__main__":
    unittest.main()
