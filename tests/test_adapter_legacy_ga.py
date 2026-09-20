"""Tests for Oracle's legacy GA parser adapter (oracle.adapters.legacy_ga)

Protects:
    The translation of ga_dxf_parser output into oracle.core: levels, elevations and storey
    heights, column and beam level relationships, node and element references, duplicate
    prevention, ID and unit/coordinate fidelity, slab support inference, provenance, and that bad
    or missing source data becomes EngineeringIssues instead of exceptions or silent corruption.
    Also the persistence round trip: legacy DXF -> adapter -> OracleProject -> JSON -> OracleProject.

Test type:
    Integration and regression tests on real fixtures (input_dwgs/1st Flr, 2nd Flr and Roof
    GAs.dxf, and input_dwgs/test_floor.dxf). The single-floor and void cases are derived from the
    real GA in a temporary directory; nothing under input_dwgs/ is written. Unit tests of hand-built
    parse results cover failure paths the fixtures cannot reach.

Dependencies:
    oracle.core, oracle.adapters, and the legacy ga_dxf_parser (real parser, run for real).
"""

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import ezdxf

from oracle.adapters import Basis, adapt_legacy_ga
from oracle.core import (
    DesignBasis, ElementKind, IssueCategory, IssueSeverity, OracleProject, Target,
)
from tests.tiers import tier

REPO_ROOT = Path(__file__).resolve().parent.parent
GA_DXF = REPO_ROOT / "input_dwgs" / "1st Flr, 2nd Flr and Roof GAs.dxf"
ARCH_DXF = REPO_ROOT / "input_dwgs" / "test_floor.dxf"
ELEV = {"G": 0.0, "1": 3.3, "2": 6.3, "R": 9.3}
ELEV_ONE = {"G": 0.0, "1": 3.3}

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
with contextlib.redirect_stdout(io.StringIO()):
    import ga_dxf_parser as gp

SIZES = gp.DEFAULT_SIZES


def parse(path, elevations, **kw):
    return gp.parse_multilevel_ga(str(path), storey_heights_m=elevations, **kw)


def adapt(result, elevations, **kw):
    kw.setdefault("project_name", "Fixture")
    kw.setdefault("engineer", "A. Engineer")
    kw.setdefault("section_sizes_mm", SIZES)
    return adapt_legacy_ga(result, elevations, **kw)


def issues_where(res, **checks):
    out = list(res.issues)
    if "severity" in checks:
        out = [i for i in out if i.severity == checks["severity"]]
    if "text" in checks:
        out = [i for i in out if checks["text"] in i.message]
    if "target" in checks:
        out = [i for i in out if i.target == checks["target"]]
    return out


class Joints:
    """Stand-in for the parser's JointRegistry: only coordinates() is used by the adapter."""

    def __init__(self, coords):
        self._coords = coords

    def coordinates(self):
        return self._coords


class Legacy:
    def __init__(self, kind, message):
        self.kind, self.message = kind, message


def crafted(joints, members, levels=("G", "1"), panels=None, loading=None, issues=(), voids=None):
    return {
        "levels": list(levels), "beam_layer_by_level": {levels[1]: "F.F BEAMS"},
        "column_layer_by_boundary": {(levels[0], levels[1]): "COLUMN G-1"}, "issues": list(issues),
        "model": {"joints": Joints(joints), "members": members, "slab_panels": panels or {}, "issues": []},
        "loading": loading if loading is not None else {levels[1]: {"slab_thickness_mm": 150}},
        "void_centroids": voids or {},
    }


@tier("integration")
class RealFixtureTests(unittest.TestCase):
    """Real multi-floor GA (G, 1, 2, R) and a single-floor case cut from the same drawing."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.multi_parse = parse(GA_DXF, ELEV)
        cls.multi = adapt(cls.multi_parse, ELEV, project_id="proj-multi", source_file=GA_DXF.name)

        doc = ezdxf.readfile(str(GA_DXF))
        msp = doc.modelspace()
        for name in ("COLUMN 1-2", "COLUMN 2-R", "S.F BEAMS", "R. BEAMS"):
            for e in [e for e in msp if e.dxf.layer == name]:
                msp.delete_entity(e)
            doc.layers.remove(name)
        cls.single_path = cls.tmp / "single_floor_ga.dxf"
        doc.saveas(str(cls.single_path))
        cls.single_parse = parse(cls.single_path, ELEV_ONE)
        cls.single = adapt(cls.single_parse, ELEV_ONE, project_id="proj-single")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- A. single floor
    def test_a_single_floor_conversion(self):
        res, b = self.single, self.single.building
        self.assertTrue(res.complete, [i.message for i in issues_where(res, severity=IssueSeverity.ERROR) + issues_where(res, severity=IssueSeverity.BLOCKING)])
        self.assertEqual([lv.id for lv in b.levels], ["G", "1"])
        self.assertEqual(len(b.elements_of(ElementKind.COLUMN)), 28)
        self.assertEqual(len(b.elements_of(ElementKind.BEAM)), 17)
        self.assertEqual(len(b.elements_of(ElementKind.SLAB)), 11)
        self.assertTrue(all((c.lower_level_id, c.upper_level_id) == ("G", "1") for c in b.elements_of(ElementKind.COLUMN)))
        self.assertTrue(all(bm.level_id == "1" for bm in b.elements_of(ElementKind.BEAM)))

    # ---- B. multi floor
    def test_b_multi_floor_conversion_matches_the_parser_counts(self):
        m = self.multi_parse["model"]
        beams = [x for x in m["members"] if x[3] == "beam"]
        cols = [x for x in m["members"] if x[3] == "column"]
        b = self.multi.building
        self.assertTrue(self.multi.complete)
        self.assertEqual(len(b.elements_of(ElementKind.BEAM)), len(beams))
        self.assertEqual(len(b.elements_of(ElementKind.COLUMN)), len(cols))
        self.assertEqual(len(b.elements_of(ElementKind.SLAB)), sum(len(p) for p in m["slab_panels"].values()))
        used = {j for x in m["members"] for j in (x[1], x[2])}
        self.assertEqual(len(b.nodes), len(used))  # slab-panel corner joints are not nodes
        self.assertLess(len(used), len(m["joints"].coordinates()))

    # ---- C. level ordering
    def test_c_level_ordering(self):
        b = self.multi.building
        self.assertEqual([lv.id for lv in b.levels], ["G", "1", "2", "R"])
        self.assertEqual([lv.index for lv in b.levels], [0, 1, 2, 3])
        self.assertEqual([lv.id for lv in b.levels], self.multi_parse["levels"])

    # ---- D. elevations and storey heights
    def test_d_elevations_and_storey_heights(self):
        b = self.multi.building
        self.assertEqual([lv.elevation_mm for lv in b.levels], [0.0, 3300.0, 6300.0, 9300.0])
        self.assertEqual([lv.storey_height_mm for lv in b.levels], [3300.0, 3000.0, 3000.0, None])
        for lv in b.levels[:-1]:
            self.assertEqual(lv.storey_height_mm, b.storey_height_mm(lv.id))

    # ---- E. column level relationships
    def test_e_column_level_relationships(self):
        b = self.multi.building
        pairs = {}
        for c in b.elements_of(ElementKind.COLUMN):
            pairs[(c.lower_level_id, c.upper_level_id)] = pairs.get((c.lower_level_id, c.upper_level_id), 0) + 1
            self.assertLess(b.get_level(c.lower_level_id).elevation_mm, b.get_level(c.upper_level_id).elevation_mm)
        self.assertEqual(pairs, {("G", "1"): 28, ("1", "2"): 26, ("2", "R"): 23})
        for c in b.elements_of(ElementKind.COLUMN):
            layer = self.multi.provenance_for("column", c.id).source_layer
            self.assertEqual(layer, f"COLUMN {c.lower_level_id}-{c.upper_level_id}")

    # ---- F. beam level relationships
    def test_f_beam_level_relationships(self):
        b = self.multi.building
        per_level = {}
        for bm in b.elements_of(ElementKind.BEAM):
            per_level[bm.level_id] = per_level.get(bm.level_id, 0) + 1
            self.assertEqual(b.get_node(bm.start_node_id).level_id, bm.level_id)
            self.assertEqual(b.get_node(bm.end_node_id).level_id, bm.level_id)
        self.assertEqual(per_level, {"1": 17, "2": 19, "R": 16})
        for bm in b.elements_of(ElementKind.BEAM):
            self.assertEqual(self.multi.provenance_for("beam", bm.id).source_layer,
                             self.multi_parse["beam_layer_by_level"][bm.level_id])

    # ---- G. node and element references
    def test_g_node_and_element_references(self):
        b = self.multi.building
        b.validate()
        self.multi.project.validate()
        ids = {n.id for n in b.nodes}
        for bm in b.elements_of(ElementKind.BEAM):
            self.assertIn(bm.start_node_id, ids)
            self.assertIn(bm.end_node_id, ids)
        beam_ids = {bm.id for bm in b.elements_of(ElementKind.BEAM)}
        for s in b.elements_of(ElementKind.SLAB):
            self.assertTrue(set(s.supported_by) <= beam_ids)
            self.assertEqual(s.opening_ids, [])

    # ---- H. duplicate prevention
    def test_h_no_duplicate_elements(self):
        b = self.multi.building
        keys = [(bm.level_id, frozenset((bm.start_node_id, bm.end_node_id))) for bm in b.elements_of(ElementKind.BEAM)]
        self.assertEqual(len(keys), len(set(keys)))
        cols = [(c.lower_level_id, c.upper_level_id, c.location.x_mm, c.location.y_mm) for c in b.elements_of(ElementKind.COLUMN)]
        self.assertEqual(len(cols), len(set(cols)))
        slabs = [(s.level_id, frozenset((v.x_mm, v.y_mm) for v in s.boundary.vertices))
                 for s in b.elements_of(ElementKind.SLAB)]  # floors legitimately share a footprint
        self.assertEqual(len(slabs), len(set(slabs)))
        ids = [e.id for e in b.elements]
        self.assertEqual(len(ids), len(set(ids)))

    # ---- J. IDs and traceability
    def test_j_ids_trace_back_to_legacy_numbers(self):
        b = self.multi.building
        for no, j1, j2, kind, where, size_key in self.multi_parse["model"]["members"]:
            eid = ("B" if kind == "beam" else "C") + str(no)
            self.assertTrue(b.has_element(eid), eid)
            self.assertIn(f"member {no} ", self.multi.provenance_for(kind, eid).source_id)
        for n in b.nodes:
            self.assertEqual(n.id, f"N{int(n.id[1:])}")
            self.assertIn(f"joint {int(n.id[1:])}", self.multi.provenance_for("node", n.id).source_id)
        self.assertTrue(all(b.has_element(f"S{lv}-1") for lv in ("1", "2", "R")))

    def test_j_every_object_has_provenance_with_the_right_basis(self):
        res, b = self.multi, self.multi.building
        for e in b.elements:
            self.assertIsNotNone(res.provenance_for(e.kind.value, e.id), e.id)
        for n in b.nodes:
            self.assertEqual(res.provenance_for("node", n.id).basis, Basis.EXTRACTED)
        self.assertEqual(res.provenance_for("beam", "B1").basis, Basis.EXTRACTED)
        self.assertEqual(res.provenance_for("slab", "S1-1").basis, Basis.INFERRED)
        self.assertEqual(res.provenance_for("level", "1").basis, Basis.INFERRED)
        self.assertIn("ASSUMED", res.provenance_for("beam", "B1").note)
        records = res.provenance_records()
        self.assertEqual([r["key"] for r in records], sorted(r["key"] for r in records))
        json.dumps(records)

    # ---- K. units and coordinates
    def test_k_units_and_coordinates(self):
        b = self.multi.building
        coords = self.multi_parse["model"]["joints"].coordinates()
        for no, j1, j2, kind, where, size_key in self.multi_parse["model"]["members"]:
            if kind != "beam":
                continue
            (x1, y1, z1), (x2, y2, z2) = coords[j1], coords[j2]
            expected = ((x2 - x1) ** 2 + (z2 - z1) ** 2) ** 0.5 * 1000.0
            self.assertAlmostEqual(b.member_length_mm(f"B{no}"), expected, places=2)
            self.assertAlmostEqual(b.get_node(f"N{j1}").location.x_mm, x1 * 1000.0, places=2)   # x -> x
            self.assertAlmostEqual(b.get_node(f"N{j1}").location.y_mm, z1 * 1000.0, places=2)   # legacy z -> plan y
            self.assertAlmostEqual(b.get_level(where).elevation_mm, y1 * 1000.0, places=2)      # legacy y is elevation
        longest = max(b.member_length_mm(e.id) for e in b.elements_of(ElementKind.BEAM))
        self.assertTrue(1000 < longest < 30000, longest)  # metres-scale runs (24.3 m is the full plan width)
        self.assertEqual(issues_where(self.multi, text="longest beam"), [])

    def test_floors_share_one_footprint_after_the_parsers_alignment(self):
        b = self.multi.building
        def bbox(level):
            xs = [n.location.x_mm for n in b.nodes if n.level_id == level]
            ys = [n.location.y_mm for n in b.nodes if n.level_id == level]
            return min(xs), min(ys)
        self.assertEqual(bbox("1"), bbox("2"))
        self.assertEqual(bbox("2"), bbox("R"))

    # ---- assumptions are kept apart from facts
    def test_sections_are_reported_as_assumed(self):
        res, b = self.multi, self.multi.building
        w, d = SIZES["beam_mm"]
        beam = b.get_element("B1")
        self.assertEqual((beam.section.width_mm, beam.section.depth_mm), (w, d))
        self.assertTrue(issues_where(res, text="legacy default size table", target=Target.level("1")))
        roof_beam = next(e for e in b.elements_of(ElementKind.BEAM) if e.level_id == "R")
        self.assertEqual((roof_beam.section.width_mm, roof_beam.section.depth_mm), SIZES["roof_beam_mm"])
        for i in issues_where(res, text="legacy default size table"):
            self.assertEqual(i.status.value, "open")
            self.assertIn("Engineer action: required", i.message)
        self.assertIsNone(beam.material)

    def test_default_slab_thickness_is_an_open_assumption_but_engineer_input_is_not(self):
        self.assertTrue(issues_where(self.multi, text="the engineer did not supply one"))
        loading = {"1": {"slab_thickness_mm": 175}, "2": {"slab_thickness_mm": 175}, "R": {"slab_thickness_mm": 150}}
        given = adapt(parse(GA_DXF, ELEV, per_level_loading=loading), ELEV, engineer_loading=loading)
        self.assertEqual({s.thickness_mm for s in given.building.elements_of(ElementKind.SLAB) if s.level_id == "1"}, {175})
        self.assertEqual(issues_where(given, text="the engineer did not supply one"), [])
        self.assertIn("ENGINEER_INPUT", given.provenance_for("slab", "S1-1").note)

    def test_design_basis_is_never_invented(self):
        self.assertIsNone(self.multi.project.design_basis)
        self.assertTrue(issues_where(self.multi, text="No design basis is attached"))
        basis = DesignBasis("BS 8110-1:1997", "C25/30", "Y (high-yield)")
        with_basis = adapt(self.multi_parse, ELEV, design_basis=basis)
        self.assertIs(with_basis.project.design_basis, basis)
        self.assertEqual(issues_where(with_basis, text="No design basis is attached"), [])
        self.assertTrue(issues_where(with_basis, text="supplied design basis is incomplete"))

    def test_legacy_parser_issues_are_carried_over_verbatim(self):
        legacy = [i.message for i in self.multi_parse["issues"]]
        carried = [i.message for i in issues_where(self.multi) if i.source == "ga_dxf_parser" and i.message in legacy]
        self.assertEqual(sorted(carried), sorted(legacy))
        self.assertTrue(all(i.severity == IssueSeverity.WARNING for i in issues_where(self.multi, text="drawn offset")))

    def test_information_the_parser_never_read_is_declared_lost(self):
        self.assertTrue(issues_where(self.multi, text="does not extract grid lines"))
        self.assertTrue(issues_where(self.multi, text="structural grid origin"))

    # ---- beams that run over columns without a node
    def test_beams_running_over_columns_are_reported_and_the_report_is_true(self):
        b, res = self.multi.building, self.multi
        reported = issues_where(res, text="runs over")
        self.assertGreaterEqual(len(reported), 1)
        pairs = 0
        for issue in reported:
            self.assertEqual(issue.target.scope.value, "element")
            self.assertEqual(issue.category, IssueCategory.UNSUPPORTED_BEAM)
            self.assertIn("Engineer action: required", issue.message)
            beam = b.get_element(issue.target.id)
            a, c = b.get_node(beam.start_node_id).location, b.get_node(beam.end_node_id).location
            named = [col.id for col in b.elements_of(ElementKind.COLUMN)
                     if beam.level_id in col.level_ids and issue.message.count(col.id) and _lies_on(col.location, a, c)]
            self.assertTrue(named, issue.message)
            pairs += len(named)
        self.assertGreaterEqual(pairs, 16)  # 16 columns of the real drawing are affected (independently counted)

    # ---- slab support inference
    def test_slab_supports_are_found_on_every_side(self):
        b = self.multi.building
        beams = {e.id: e for e in b.elements_of(ElementKind.BEAM)}
        for s in b.elements_of(ElementKind.SLAB):
            self.assertGreaterEqual(len(s.supported_by), 4, s.id)
            self.assertTrue(all(beams[x].level_id == s.level_id for x in s.supported_by))

    def test_adapter_does_not_mutate_the_parse_result(self):
        r = self.multi_parse
        before = (repr(r["model"]["members"]), repr(sorted(r["model"]["joints"].coordinates().items())),
                  repr(r["model"]["slab_panels"]), repr([(i.kind, i.message) for i in r["issues"]]), repr(r["loading"]))
        adapt(r, ELEV)
        after = (repr(r["model"]["members"]), repr(sorted(r["model"]["joints"].coordinates().items())),
                 repr(r["model"]["slab_panels"]), repr([(i.kind, i.message) for i in r["issues"]]), repr(r["loading"]))
        self.assertEqual(before, after)

    # ---- determinism and persistence
    def test_adaptation_is_deterministic(self):
        again = adapt(self.multi_parse, ELEV, project_id="proj-multi", source_file=GA_DXF.name)
        self.assertEqual(again.building.to_dict(), self.multi.building.to_dict())
        self.assertEqual([(i.id, i.severity, i.message) for i in again.issues],
                         [(i.id, i.severity, i.message) for i in self.multi.issues])
        self.assertEqual(again.provenance_records(), self.multi.provenance_records())

    def test_round_trip_through_project_json(self):
        original = self.multi.project
        text = original.to_json()
        restored = OracleProject.from_json(text)
        self.assertEqual(restored.to_json(), text)
        self.assertEqual(restored.building.to_dict(), original.building.to_dict())
        self.assertEqual(restored.project_id, "proj-multi")
        self.assertEqual([lv.elevation_mm for lv in restored.building.levels], [0.0, 3300.0, 6300.0, 9300.0])
        self.assertEqual({k.value: len(restored.building.elements_of(k)) for k in ElementKind if restored.building.elements_of(k)},
                         {"column": 77, "beam": 52, "slab": 36})
        self.assertEqual(restored.get_issue("IMP-0001").target, Target.project())
        self.assertEqual([i.to_dict() for i in restored.issues], [i.to_dict() for i in original.issues])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "imported.oracle.json"
            original.save(path)
            self.assertEqual(OracleProject.load(path).to_json(), text)
        for e in original.building.elements:  # geometry survives exactly, element by element
            self.assertEqual(restored.building.get_element(e.id).to_dict(), e.to_dict())

    def test_round_trip_for_the_single_floor_case(self):
        text = self.single.project.to_json()
        self.assertEqual(OracleProject.from_json(text).building.to_dict(), self.single.building.to_dict())


def _lies_on(pt, a, c, across=30.0, margin=80.0):
    """Independent check that pt is on segment a-c's interior (does not call adapter code)."""
    dx, dy = c.x_mm - a.x_mm, c.y_mm - a.y_mm
    length = (dx * dx + dy * dy) ** 0.5
    along = ((pt.x_mm - a.x_mm) * dx + (pt.y_mm - a.y_mm) * dy) / length
    off = abs(dx * (pt.y_mm - a.y_mm) - dy * (pt.x_mm - a.x_mm)) / length
    return off <= across and margin < along < length - margin


@tier("integration")
class VoidTests(unittest.TestCase):
    """A VOID marker over one real slab panel, added to the single-floor cut of the real GA."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        doc = ezdxf.readfile(str(GA_DXF))
        msp = doc.modelspace()
        for name in ("COLUMN 1-2", "COLUMN 2-R", "S.F BEAMS", "R. BEAMS"):
            for e in [e for e in msp if e.dxf.layer == name]:
                msp.delete_entity(e)
            doc.layers.remove(name)
        base = cls.tmp / "base.dxf"
        doc.saveas(str(base))
        panel = parse(base, ELEV_ONE)["model"]["slab_panels"]["1"][0]
        cx, cy = panel["centroid"][0] * 1000, panel["centroid"][1] * 1000
        cls.without = adapt(parse(base, ELEV_ONE), ELEV_ONE)
        doc.layers.add("VOID")
        msp.add_lwpolyline([(cx - 300, cy - 300), (cx + 300, cy - 300), (cx + 300, cy + 300), (cx - 300, cy + 300)],
                           close=True, dxfattribs={"layer": "VOID"})
        path = cls.tmp / "void.dxf"
        doc.saveas(str(path))
        cls.void_parse = parse(path, ELEV_ONE)
        cls.with_void = adapt(cls.void_parse, ELEV_ONE)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_void_removes_a_slab_and_is_reported_not_forgotten(self):
        slabs = lambda r: len(r.building.elements_of(ElementKind.SLAB))
        self.assertEqual(slabs(self.without) - slabs(self.with_void), 1)
        void_issues = issues_where(self.with_void, text="A VOID marker at plan")
        self.assertEqual(len(void_issues), 1)
        self.assertEqual(void_issues[0].target, Target.level("1"))
        self.assertIn("No Opening was created", void_issues[0].message)
        self.assertIn("Engineer action: required", void_issues[0].message)
        self.assertEqual(self.with_void.building.elements_of(ElementKind.OPENING), [])

    def test_the_parsers_own_void_note_is_kept(self):
        self.assertTrue(issues_where(self.with_void, text="excluded due to VOID markers"))


@tier("integration")
class FailureTests(unittest.TestCase):
    """L. Bad or missing source data produces EngineeringIssues, never an exception or a half model."""

    def test_architectural_drawing_is_not_a_structural_ga(self):
        result = gp.parse_multilevel_ga(str(ARCH_DXF), storey_heights_m=ELEV_ONE)
        res = adapt(result, ELEV_ONE)
        self.assertIsNone(res.building)
        self.assertFalse(res.complete)
        blocking = issues_where(res, severity=IssueSeverity.BLOCKING)
        self.assertGreaterEqual(len(blocking), 2)  # the parser's own, plus the adapter's "nothing imported"
        self.assertTrue(any("nothing was imported" in i.message for i in blocking))
        self.assertTrue(any("COLUMN" in i.message for i in blocking))
        OracleProject.from_json(res.project.to_json())  # a failed import still yields a valid, savable project

    def test_empty_parse_result_does_not_raise(self):
        res = adapt({}, {})
        self.assertFalse(res.complete)
        self.assertEqual(len(issues_where(res, severity=IssueSeverity.BLOCKING)), 1)

    def test_missing_elevation(self):
        res = adapt(crafted({}, [], levels=("G", "1", "2")), {"G": 0.0})
        self.assertIsNone(res.building)
        self.assertTrue(issues_where(res, text="No elevation was given"))

    def test_contradictory_elevations(self):
        res = adapt(crafted({}, []), {"G": 3.0, "1": 3.0})
        self.assertIsNone(res.building)
        self.assertTrue(issues_where(res, text="not strictly increasing"))
        res = adapt(crafted({}, []), {"G": 0.0, "1": float("nan")})
        self.assertIsNone(res.building)

    def test_unknown_legacy_issue_kind_is_kept_as_an_error(self):
        joints = {1: (0, 0.0, 0), 2: (5, 0.0, 0)}
        res = adapt(crafted(joints, [], issues=[Legacy("mystery", "something odd")]), {"G": 0.0, "1": 3.0})
        odd = issues_where(res, text="something odd")
        self.assertEqual(len(odd), 1)
        self.assertEqual(odd[0].severity, IssueSeverity.ERROR)
        self.assertIn("unrecognised legacy issue kind", odd[0].message)

    def test_joint_at_no_level_drops_the_member_with_an_issue(self):
        joints = {1: (0, 3.0, 0), 2: (5, 3.0, 0), 3: (5, 4.4, 5)}
        members = [(1, 1, 2, "beam", "1", "beam_mm"), (2, 2, 3, "beam", "1", "beam_mm")]
        res = adapt(crafted(joints, members), {"G": 0.0, "1": 3.0})
        self.assertFalse(res.complete)
        self.assertTrue(res.building.has_element("B1"))
        self.assertFalse(res.building.has_element("B2"))
        errs = issues_where(res, severity=IssueSeverity.ERROR)
        self.assertTrue(any("matches no level" in i.message for i in errs))
        self.assertTrue(any("could not be turned into a node" in i.message and "member 2" in i.message for i in errs))

    def test_member_on_the_wrong_level_is_rejected_not_placed(self):
        joints = {1: (0, 3.0, 0), 2: (5, 3.0, 0)}
        res = adapt(crafted(joints, [(1, 1, 2, "beam", "G", "beam_mm")], levels=("G", "1")), {"G": 0.0, "1": 3.0})
        self.assertEqual(res.building.elements_of(ElementKind.BEAM), [])
        self.assertTrue(any("is not at the elevation of level" in i.message
                            for i in issues_where(res, severity=IssueSeverity.ERROR)))

    def test_missing_section_size_is_an_error_not_a_guess(self):
        joints = {1: (0, 3.0, 0), 2: (5, 3.0, 0)}
        res = adapt(crafted(joints, [(1, 1, 2, "beam", "1", "beam_mm")]), {"G": 0.0, "1": 3.0}, section_sizes_mm={})
        self.assertEqual(res.building.elements_of(ElementKind.BEAM), [])
        self.assertFalse(res.complete)
        self.assertTrue(issues_where(res, text="section size key"))

    def test_unsupported_member_kind(self):
        joints = {1: (0, 3.0, 0), 2: (5, 3.0, 0)}
        res = adapt(crafted(joints, [(1, 1, 2, "brace", "1", "beam_mm")]), {"G": 0.0, "1": 3.0})
        self.assertFalse(res.complete)
        self.assertTrue(issues_where(res, text="neither beam nor column"))

    def test_slabs_without_thickness_are_not_invented(self):
        joints = {1: (0, 3.0, 0), 2: (5, 3.0, 0)}
        panel = {"vertices": [(0, 0), (5, 0), (5, 5), (0, 5)], "centroid": (2.5, 2.5)}
        res = adapt(crafted(joints, [(1, 1, 2, "beam", "1", "beam_mm")], panels={"1": [panel]}, loading={}),
                    {"G": 0.0, "1": 3.0})
        self.assertEqual(res.building.elements_of(ElementKind.SLAB), [])
        self.assertTrue(issues_where(res, text="no slab thickness"))

    def test_invalid_slab_boundary_is_reported(self):
        joints = {1: (0, 3.0, 0), 2: (5, 3.0, 0)}
        bowtie = {"vertices": [(0, 0), (5, 5), (5, 0), (0, 5)], "centroid": (2.5, 2.5)}
        res = adapt(crafted(joints, [(1, 1, 2, "beam", "1", "beam_mm")], panels={"1": [bowtie]}), {"G": 0.0, "1": 3.0})
        self.assertEqual(res.building.elements_of(ElementKind.SLAB), [])
        self.assertTrue(issues_where(res, text="rejected by the core model", severity=IssueSeverity.ERROR))

    def test_near_coincident_joints_merge_and_duplicate_beams_are_not_repeated(self):
        joints = {1: (0.0, 3.0, 0.0), 2: (5.0, 3.0, 0.0), 3: (0.0006, 3.0, 0.0)}  # joint 3 is 0.6 mm from joint 1
        members = [(1, 1, 2, "beam", "1", "beam_mm"), (2, 3, 2, "beam", "1", "beam_mm")]
        res = adapt(crafted(joints, members), {"G": 0.0, "1": 3.0})
        self.assertEqual(len(res.building.elements_of(ElementKind.BEAM)), 1)
        self.assertEqual(len(res.building.nodes), 2)
        self.assertTrue(issues_where(res, text="were merged into it"))
        self.assertTrue(issues_where(res, text="duplicates or collapses"))

    def test_column_on_a_beam_interior_is_reported_but_not_if_the_beam_is_split_there(self):
        joints = {1: (0.0, 3.0, 0.0), 2: (10.0, 3.0, 0.0), 3: (5.0, 0.0, 0.0), 4: (5.0, 3.0, 0.0)}
        column = (2, 3, 4, "column", ("G", "1"), "column_mm")
        unsplit = adapt(crafted(joints, [(1, 1, 2, "beam", "1", "beam_mm"), column]), {"G": 0.0, "1": 3.0})
        found = issues_where(unsplit, text="runs over")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].target, Target.element("B1"))
        self.assertIn("C2", found[0].message)
        # a beam split at the column: a beam end node sits at the crossing (joint 4)
        split = adapt(crafted(joints, [(1, 1, 4, "beam", "1", "beam_mm"), (3, 4, 2, "beam", "1", "beam_mm"), column]),
                      {"G": 0.0, "1": 3.0})
        self.assertEqual(issues_where(split, text="runs over"), [])

    def test_column_whose_joints_disagree_with_its_levels_is_rejected(self):
        joints = {1: (0, 0.0, 0), 2: (0, 3.0, 0)}
        res = adapt(crafted(joints, [(1, 2, 1, "column", ("G", "1"), "column_mm")]), {"G": 0.0, "1": 3.0})
        self.assertEqual(res.building.elements_of(ElementKind.COLUMN), [])
        self.assertTrue(any("not at the elevations" in i.message for i in res.issues))

    def test_suspicious_units_are_flagged(self):
        joints = {1: (0.0, 3.0, 0.0), 2: (0.2, 3.0, 0.0)}  # 0.2 m read as metres: a 200 mm "beam"
        res = adapt(crafted(joints, [(1, 1, 2, "beam", "1", "beam_mm")]), {"G": 0.0, "1": 3.0})
        flagged = issues_where(res, text="longest beam")
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0].category, IssueCategory.SUSPICIOUS_SPAN)

    def test_level_ids_are_made_valid_and_collisions_reported(self):
        joints = {1: (0, 0.0, 0)}
        res = adapt(crafted(joints, [], levels=("G", "F/F")), {"G": 0.0, "F/F": 3.0})
        self.assertEqual([lv.id for lv in res.building.levels], ["G", "F_F"])
        res = adapt(crafted(joints, [], levels=("A/1", "A_1")), {"A/1": 0.0, "A_1": 3.0})
        self.assertIsNone(res.building)
        self.assertTrue(issues_where(res, text="collide"))


@tier("integration")
class BoundaryTests(unittest.TestCase):
    """The adapter and the core stay independent of the legacy scripts and of each other."""

    def _run(self, code):
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(REPO_ROOT), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_adapter_does_not_import_legacy_scripts(self):
        self.assertEqual(self._run(
            "import sys, oracle.adapters;"
            "print(sorted(m for m in ('ga_dxf_parser','config','oracle_wizard','ezdxf','anthropic') if m in sys.modules))"),
            "[]")

    def test_core_does_not_import_adapters(self):
        self.assertEqual(self._run("import sys, oracle.core; print('oracle.adapters' in sys.modules)"), "False")


if __name__ == "__main__":
    unittest.main()
