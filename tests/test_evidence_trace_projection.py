"""Tests for evidence links, trace() and the approved architecture projection (Phase 3.5)

Protects:
    That a structural object can say, in domain-neutral and validated terms, which approved architectural evidence it was
    derived from, under which engineer decision (typed EvidenceLinks, no free-text convention); that one observation can
    support several structural readings while nothing is decided without an engineer; that trace() walks the chain
    backwards from a final object to the source drawing and its entity identifiers, reports every missing link as a
    gap and never fabricates one; that the approved architecture projection exposes only approved, domain-level,
    millimetre data with no CAD vocabulary and can be consumed without importing any CAD or interpretation code; and that
    all of it survives save and load.

Test type:
    Integration tests on synthetic drawings; the projection's isolation is checked in a fresh Python process.

Dependencies:
    oracle.core, oracle.interpretation, tests.support35, tests.drawing_factory, tests.fixtures.
"""

import dataclasses
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from oracle.core import (
    DecisionSource, DecisionStatus, EvidenceLink, EvidenceRelation, Interpretation, InterpretationSet, OracleProject,
    ReviewStatus, Target, ValidationError, ValueStatus, ViewType,
)
from oracle.interpretation import interpret_document, interpret_into
from tests.drawing_factory import Sheet
from tests.support35 import decision, drawing_sheet, interpreted, plans, structural_project, with_levels
from tests.tiers import tier

REPO_ROOT = Path(__file__).resolve().parent.parent


def column_symbols(p):
    return [o for o in p.architecture.observations if o.kind == "column_symbol"]


def approve(p, observation, view=True):
    if view:
        p.review_views([observation.view_id], decision(Target.architectural(observation.view_id), "view checked"))
    p.review_observations([observation.id], decision(Target.architectural(observation.id), "observation checked"))


@tier("integration")
class TypedLinks(unittest.TestCase):
    def setUp(self):
        self.p = structural_project()
        self.obs = column_symbols(self.p)[0]

    def test_a_column_can_rest_on_an_approved_observation(self):
        approve(self.p, self.obs)
        d = decision(Target.element("C5"), "C5 is the column shown here")
        link = self.p.link_evidence(Target.element("C5"), Target.architectural(self.obs.id), EvidenceRelation.DERIVED_FROM, d,
                                    note="retained column at the drawing's grid")
        self.assertEqual((link.subject, link.evidence, link.relation), (Target.element("C5"), Target.architectural(self.obs.id),
                                                                        EvidenceRelation.DERIVED_FROM))
        self.assertEqual(self.p.get_decision(link.decision_id).source, DecisionSource.ENGINEER)
        self.assertEqual(self.p.links_for(Target.element("C5")), [link])
        self.assertEqual(self.p.links_from(Target.architectural(self.obs.id)), [link])

    def test_unapproved_evidence_cannot_support_anything(self):
        d = decision(Target.element("C5"), "premature")
        with self.assertRaises(ValidationError) as caught:
            self.p.link_evidence(Target.element("C5"), Target.architectural(self.obs.id), EvidenceRelation.DERIVED_FROM, d)
        self.assertIn("has not been accepted", str(caught.exception))
        self.assertEqual(self.p.evidence_links, [])
        self.assertNotIn(d.id, [x.id for x in self.p.decisions], "a refused link leaves no decision behind")

    def test_an_approved_observation_of_an_unapproved_view_is_still_not_enough(self):
        approve(self.p, self.obs, view=False)
        with self.assertRaises(ValidationError):
            self.p.link_evidence(Target.element("C5"), Target.architectural(self.obs.id), EvidenceRelation.SUPPORTED_BY,
                                 decision(Target.element("C5")))

    def test_a_rejected_observation_cannot_be_used(self):
        self.p.review_views([self.obs.view_id], decision(Target.architectural(self.obs.view_id)))
        self.p.review_observations([self.obs.id], decision(Target.architectural(self.obs.id), "not a column"), accept=False)
        with self.assertRaises(ValidationError):
            self.p.link_evidence(Target.element("C5"), Target.architectural(self.obs.id), EvidenceRelation.DERIVED_FROM,
                                 decision(Target.element("C5")))

    def test_only_an_engineer_can_make_a_link(self):
        approve(self.p, self.obs)
        for source in (DecisionSource.ORACLE, DecisionSource.AI_ASSISTANT):
            with self.assertRaises(ValidationError):
                self.p.link_evidence(Target.element("C5"), Target.architectural(self.obs.id), EvidenceRelation.DERIVED_FROM,
                                     decision(Target.element("C5"), source=source, status=DecisionStatus.PROPOSED))

    def test_the_subject_and_the_evidence_must_exist_and_be_the_right_kind(self):
        approve(self.p, self.obs)
        ev = Target.architectural(self.obs.id)
        with self.assertRaises(ValidationError):
            self.p.link_evidence(Target.element("C99"), ev, EvidenceRelation.DERIVED_FROM, decision(Target.project()))
        with self.assertRaises(ValidationError):
            self.p.link_evidence(Target.element("C5"), Target.architectural("OBS-99999"), EvidenceRelation.DERIVED_FROM, decision(Target.project()))
        layer = self.p.architecture.layers[0]
        with self.assertRaises(ValidationError):                       # a layer classification is not something a decision rests on
            self.p.link_evidence(Target.element("C5"), Target.architectural(layer.id), EvidenceRelation.DERIVED_FROM, decision(Target.project()))
        with self.assertRaises(ValidationError):
            EvidenceLink("EV-0001", Target.element("C5"), "derived_from", "D1", Target.element("C5"))     # evidence must be architectural-scope
        with self.assertRaises(ValidationError):
            EvidenceLink("EV-0001", ev, "derived_from", "D1")                                            # no subject at all
        with self.assertRaises(ValidationError):
            EvidenceLink("EV-0001", ev, "invented_relation", "D1", Target.element("C5"))

    def test_the_same_link_is_not_made_twice(self):
        approve(self.p, self.obs)
        ev = Target.architectural(self.obs.id)
        self.p.link_evidence(Target.element("C5"), ev, EvidenceRelation.DERIVED_FROM, decision(Target.element("C5")))
        with self.assertRaises(ValidationError):
            self.p.link_evidence(Target.element("C5"), ev, EvidenceRelation.DERIVED_FROM, decision(Target.element("C5")))

    def test_links_are_serialised_and_reloaded_and_forged_ones_are_rejected(self):
        approve(self.p, self.obs)
        self.p.link_evidence(Target.element("C5"), Target.architectural(self.obs.id), EvidenceRelation.DERIVED_FROM, decision(Target.element("C5")))
        text = self.p.to_json()
        self.assertEqual(OracleProject.from_json(text).to_json(), text)
        for mutate in (lambda d: d["evidence_links"][0]["evidence"].update(id="OBS-99999"),
                       lambda d: d["evidence_links"][0].update(decision_id="NOPE"),
                       lambda d: d["evidence_links"][0]["subject"].update(id="C99"),
                       lambda d: d["evidence_links"][0].update(surprise=1),
                       lambda d: d["evidence_links"][0].update(relation="proves")):
            data = json.loads(text)
            mutate(data)
            with self.assertRaises(ValidationError):
                OracleProject.from_dict(data)

    def test_a_link_can_rest_on_a_decision_instead_of_an_object(self):
        approve(self.p, self.obs)
        d = decision(Target.element("C5"), "keep C5 aligned with the wall")
        link = self.p.link_evidence(None, Target.architectural(self.obs.id), EvidenceRelation.CONSTRAINED_BY, d, subject_decision_id=d.id)
        self.assertEqual(link.subject_key, ("decision", d.id))
        self.assertEqual(self.p.links_for(d.id), [link])


@tier("integration")
class OneObservationSeveralInterpretations(unittest.TestCase):
    def test_the_same_symbol_can_be_read_several_ways_and_only_an_engineer_decides(self):
        p = structural_project()
        obs = column_symbols(p)[0]
        self.assertEqual((obs.kind, obs.hint.value, obs.review), ("column_symbol", "column_candidate", ReviewStatus.PROPOSED))
        target = Target.architectural(obs.id)
        alts = [Interpretation("INT-8001", "retained_column", 0.5), Interpretation("INT-8002", "demolished_column", 0.3),
                Interpretation("INT-8003", "new_column_at_same_position", 0.2)]
        p.add_interpretation_set(InterpretationSet("IS-800", "What structural role does this symbol have?", alts, (), target))
        self.assertEqual(len(p.get_interpretation_set("IS-800").alternatives), 3)
        self.assertEqual(p.open_interpretation_sets()[-1].id, "IS-800")
        self.assertIn("open_interpretation", [b.kind.value for b in p.readiness().blockers])

    def test_several_structural_objects_may_rest_on_one_observation_and_one_object_on_several(self):
        p = structural_project()
        a, b = column_symbols(p)[:2]
        for o in (a, b):
            approve(p, o, view=(o is a))
        p.link_evidence(Target.element("C1"), Target.architectural(a.id), EvidenceRelation.DERIVED_FROM, decision(Target.element("C1")))
        p.link_evidence(Target.element("C2"), Target.architectural(a.id), EvidenceRelation.SUPPORTED_BY, decision(Target.element("C2")))
        p.link_evidence(Target.element("C1"), Target.architectural(b.id), EvidenceRelation.SUPPORTED_BY, decision(Target.element("C1")))
        self.assertEqual(len(p.links_from(Target.architectural(a.id))), 2)
        self.assertEqual(len(p.links_for(Target.element("C1"))), 2)

    def test_three_tiers_observation_then_proposed_interpretation_then_engineer_decision(self):
        p = structural_project()
        obs = column_symbols(p)[0]
        approve(p, obs)
        approved = p.approved_architecture()
        (hint,) = [h for h in approved.hints if h.observation_id == obs.id]
        self.assertEqual((hint.hint, hint.approved), ("column_candidate", False), "approving the symbol does not approve a meaning")
        t = Target.architectural(obs.id)
        p.set_value(t, "hint", "column_candidate", decision(t, "yes, a column candidate", field="hint", value="column_candidate"))
        (hint,) = [h for h in p.approved_architecture().hints if h.observation_id == obs.id]
        self.assertTrue(hint.approved)
        self.assertEqual(p.value_status_of(t, "hint").status, ValueStatus.ENGINEER_DEFINED)


@tier("integration")
class Trace(unittest.TestCase):
    def setUp(self):
        self.p = structural_project()
        self.doc_entities = {}
        self.obs = column_symbols(self.p)[0]
        approve(self.p, self.obs)
        self.link = self.p.link_evidence(Target.element("C5"), Target.architectural(self.obs.id), EvidenceRelation.DERIVED_FROM,
                                         decision(Target.element("C5"), "C5 rests on this symbol"))

    def test_a_structural_element_traces_back_to_the_drawing_entity(self):
        tr = self.p.trace("C5")
        self.assertTrue(tr.complete, tr.gaps)
        kinds = [s.kind for s in tr.steps]
        for kind in ("target", "evidence_link", "decision", "evidence", "provenance", "source"):
            self.assertIn(kind, kinds)
        (source,) = tr.sources
        self.assertEqual((source["id"], source["file"], source["interpretation_id"]), ("SRC-1", "synthetic.dxf", "AINT-1"))
        self.assertEqual(len(source["sha256"]), 64)
        self.assertIn(self.obs.entity_ids[0], [e["entity_id"] for e in tr.entities])
        self.assertIsNotNone(tr.confidence)

    def test_the_entity_identifier_is_a_real_entity_of_the_original_drawing(self):
        doc = drawing_sheet().document()
        real = {e.id: e for e in doc.entities}
        tr = self.p.trace("C5")
        handles = [e["entity_id"] for e in tr.entities if e["via"].startswith("DrawingView") or e["via"].startswith("ArchitecturalObservation")]
        self.assertTrue(handles)
        observed = [e for e in tr.entities if e["via"].startswith("ArchitecturalObservation")]
        for row in observed:
            self.assertIn(row["entity_id"], real)
            self.assertEqual(real[row["entity_id"]].layer, self.obs.layer, "the traced entity sits where the observation says")

    def test_the_chain_carries_the_engineer_decisions_and_the_provenance_details(self):
        tr = self.p.trace(Target.element("C5"))
        decisions = tr.steps_of("decision")
        self.assertTrue(all(d.data["source"] == "engineer" for d in decisions))
        prov = tr.steps_of("provenance")
        self.assertTrue(prov)
        first = prov[0].data
        self.assertEqual((first["producer"], first["file"]), ("oracle.interpretation", "synthetic.dxf"))
        self.assertEqual(first["entity_handle"], self.obs.entity_ids[0])
        self.assertIsNotNone(first["recorded_at"])
        json.dumps(tr.to_dict())                                     # machine-readable: it is plain data

    def test_a_level_traces_to_its_height_evidence_and_plans(self):
        p = with_levels()
        tr = p.trace("L1")
        self.assertTrue(tr.complete, tr.gaps)
        evidence = [s.ref for s in tr.steps_of("evidence")]
        self.assertTrue(any(r.startswith("HGT-") for r in evidence))
        self.assertTrue(any(r.startswith("VIEW-") for r in evidence))
        self.assertTrue(tr.entities)

    def test_a_decision_traces_too(self):
        tr = self.p.trace(self.link.decision_id)
        self.assertTrue(tr.complete, tr.gaps)
        self.assertEqual(tr.subject, {"decision": self.link.decision_id})

    def test_an_interpretation_object_traces_to_its_provenance_and_source(self):
        tr = self.p.trace(self.obs.id)
        self.assertTrue(tr.complete, tr.gaps)
        self.assertIn(self.obs.entity_ids[0], [e["entity_id"] for e in tr.entities])
        self.assertEqual(tr.sources[0]["id"], "SRC-1")

    def test_an_accepted_height_traces_through_the_resolution_to_the_evidence_of_the_question(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN", ffl="FFL 0")
        s.plan((20000, 0), "FIRST FLOOR PLAN", ffl="FFL 3300")
        s.section((0, -16000), "SECTION A-A", (("GROUND FLOOR", 0), ("FIRST FLOOR", 3600)))
        p = interpret_document(s.document(), project_name="R", engineer="E")
        q = next(x for x in p.interpretation_sets if "height from GROUND to FLOOR:1" in x.question)
        alt = next(a for a in q.alternatives if a.meaning == "3600_mm")
        d = decision(Target.architectural("SRC-1"), "3600")
        p.add_decision(d)
        p.accept_interpretation(q.id, alt.id, d.id)
        hid = alt.applied[0]["created"][0]
        tr = p.trace(hid)
        self.assertEqual([s.kind for s in tr.steps_of("resolution")], ["resolution"])
        self.assertEqual(tr.steps_of("decision")[0].ref, d.id)
        self.assertTrue(tr.entities, "the question's own evidence leads to drawing entities")

    def test_missing_links_are_reported_and_not_invented(self):
        tr = self.p.trace("C3")                                        # no evidence link at all
        self.assertFalse(tr.complete)
        self.assertIn("no evidence link", tr.gaps[0])
        self.assertEqual((tr.sources, tr.entities), ((), ()))
        self.assertEqual(tr.steps_of("evidence"), [])

    def test_evidence_that_was_later_rejected_is_a_gap(self):
        self.p.review_observations([self.obs.id], decision(Target.architectural(self.obs.id), "on reflection, no"), accept=False)
        tr = self.p.trace("C5")
        self.assertFalse(tr.complete)
        self.assertTrue(any("rejected" in g for g in tr.gaps))

    def test_evidence_without_provenance_is_a_gap(self):
        data = json.loads(self.p.to_json())
        data["provenance"] = [r for r in data["provenance"] if not (r["target"]["scope"] == "architectural" and r["target"]["id"] == self.obs.id)]
        data["value_status"] = [r for r in data["value_status"] if not (r["target"]["scope"] == "architectural" and r["target"]["id"] == self.obs.id
                                                                       and r["field"] != "review")]
        for issue in data["issues"]:
            issue["evidence"] = []
        for s in data["interpretations"]:
            s["evidence"] = []
            for a in s["alternatives"]:
                a["evidence"] = []
        again = OracleProject.from_dict(data)
        tr = again.trace("C5")
        self.assertTrue(any("no provenance record" in g for g in tr.gaps), tr.gaps)

    def test_evidence_with_no_source_entity_is_a_gap(self):
        arch = self.p.architecture
        bare = dataclasses.replace(arch.get(self.obs.id), entity_ids=())
        arch.replace(bare)
        data = json.loads(self.p.to_json())
        for r in data["provenance"]:
            if r["target"]["id"] == self.obs.id:
                r["source"]["entity_handle"] = None
                r["source"]["layer"] = r["source"]["layer"] or "x"
        tr = OracleProject.from_dict(data).trace("C5")
        self.assertTrue(any("No source entity identifier" in g for g in tr.gaps), tr.gaps)

    def test_a_link_pointed_at_a_recommendation_instead_of_an_engineer_decision_is_rejected_on_load(self):
        rec = decision(Target.element("C5"), "Oracle suggests it", source=DecisionSource.ORACLE, status=DecisionStatus.PROPOSED)
        self.p.add_decision(rec)
        data = json.loads(self.p.to_json())
        data["evidence_links"][0]["decision_id"] = rec.id
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(data)

    def test_unknown_and_ambiguous_names_are_refused_not_guessed(self):
        with self.assertRaises(ValidationError):
            self.p.trace("NOPE-1")
        with self.assertRaises(ValidationError):
            self.p.trace(42)
        p = with_levels()
        from oracle.core import Node, Point2D
        p.building.add_node(Node("GF", "L1", Point2D(0, 0)))         # a node with the same id as a level
        with self.assertRaises(ValidationError):
            p.trace("GF")
        self.assertEqual(p.trace(Target.level("GF")).subject, {"scope": "level", "id": "GF"})

    def test_a_trace_survives_save_and_load(self):
        again = OracleProject.from_json(self.p.to_json())
        self.assertEqual(again.trace("C5").to_dict(), self.p.trace("C5").to_dict())


@tier("integration")
class ApprovedProjection(unittest.TestCase):
    def setUp(self):
        self.p = with_levels()
        arch = self.p.architecture
        self.p.review_observations([o.id for o in arch.observations], decision(Target.architectural(arch.observations[0].id), "all observations checked"))

    def test_only_approved_things_are_in_it(self):
        p = interpreted()                                            # nothing approved yet
        a = p.approved_architecture()
        self.assertEqual((a.views, a.observations, a.hints, a.levels), ((), (), (), ()))
        self.assertFalse(a.ready)
        b = self.p.approved_architecture()
        self.assertEqual({v.kind for v in b.views}, {"floor_plan", "section"})
        self.assertTrue(b.observations)
        self.assertTrue(all(o.view_id in {v.id for v in b.views} for o in b.observations))

    def test_levels_carry_identity_label_elevation_type_and_an_unestablished_structural_elevation(self):
        b = self.p.approved_architecture()
        levels = {l.id: l for l in b.levels}
        self.assertEqual([levels[k].elevation_mm for k in ("GF", "L1", "L2")], [0.0, 3300.0, 6600.0])
        self.assertEqual({l.structural_elevation_mm for l in b.levels}, {None})
        self.assertEqual(levels["L1"].key, "FLOOR:1")
        self.assertEqual(levels["L1"].source_label, "FIRST FLOOR PLAN")
        plan = next(v for v in b.views if v.level_key == "FLOOR:1")
        self.assertEqual(plan.level_id, "L1", "a plan is tied to the building level it realises")

    def test_geometry_is_in_millimetres_in_the_view_frame_or_the_building_frame_and_says_which(self):
        b = self.p.approved_architecture()
        plan_views = [v for v in b.views if v.kind == "floor_plan"]
        self.assertTrue(all(v.frame == "building" for v in plan_views), "the three equal plans aligned by their grid")
        ground = next(v for v in plan_views if v.level_key == "GROUND")
        width = ground.bbox_mm[2] - ground.bbox_mm[0]
        self.assertAlmostEqual(width, 13900.0, delta=1.0)
        columns = [o for o in b.observations if o.kind == "column_symbol" and o.view_id == ground.id]
        xs = sorted({round(o.geometry_mm[0][0] + 150) for o in columns})
        self.assertEqual(xs, [xs[0], xs[0] + 6000, xs[0] + 12000], "columns sit on the 6 m grid, in millimetres")
        same_place = {round(min(p[0] for p in o.geometry_mm)) for o in b.observations if o.kind == "column_symbol"}
        self.assertEqual(len(same_place), 3, "all three floors' columns land on the same three x positions: one building frame")

    def test_a_drawing_in_metres_is_projected_in_millimetres(self):
        s = Sheet(units="m")
        s.plan((0, 0), "GROUND FLOOR PLAN")
        p = interpret_document(s.document(), project_name="M", engineer="E")
        v = p.architecture.views_of(ViewType.FLOOR_PLAN)[0]
        p.review_views([v.id], decision(Target.architectural(v.id)))
        (view,) = p.approved_architecture().views
        self.assertAlmostEqual(view.bbox_mm[2] - view.bbox_mm[0], 13900.0, delta=1.0)
        self.assertEqual(p.approved_architecture().unit, "mm")

    def test_an_unaligned_plan_is_reported_as_such_never_silently_placed(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((34000, 0), "FIRST FLOOR PLAN", mirror=True)
        p = interpret_document(s.document(), project_name="Mirror", engineer="E")
        ids = [v.id for v in plans(p)]
        p.review_views(ids, decision(Target.architectural(ids[0])))
        a = p.approved_architecture()
        frames = {v.level_key: v.frame for v in a.views}
        self.assertEqual((frames["GROUND"], frames["FLOOR:1"]), ("building", "view"))
        self.assertTrue(any("not aligned" in b for b in a.blockers))

    def test_blockers_are_listed_and_clear_when_the_engineer_has_resolved_them(self):
        s = Sheet(units="mm", insunits=6)
        s.plan((0, 0), "GROUND FLOOR PLAN")
        p = interpret_document(s.document(), project_name="B", engineer="E")
        p.review_views([plans(p)[0].id], decision(Target.architectural(plans(p)[0].id)))
        a = p.approved_architecture()
        self.assertFalse(a.unit_confirmed)
        self.assertTrue(any("unit" in b for b in a.blockers))
        self.assertTrue(any("interpretation question" in b for b in a.blockers))
        q = next(x for x in p.interpretation_sets if "What unit" in x.question)
        d = decision(Target.architectural("SRC-1"), "millimetres")
        p.add_decision(d)
        p.accept_interpretation(q.id, next(x for x in q.alternatives if x.meaning == "mm").id, d.id)
        self.assertTrue(p.approved_architecture().unit_confirmed)

    def test_it_is_ready_only_when_nothing_stands_in_the_way(self):
        b = self.p.approved_architecture()
        self.assertTrue(b.ready, b.blockers)

    def test_it_is_read_only(self):
        b = self.p.approved_architecture()
        self.assertIsInstance(b.views, tuple)
        self.assertIsInstance(b.observations, tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            b.unit = "m"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            b.levels[0].elevation_mm = 5.0
        before = self.p.to_json()
        self.p.approved_architecture().to_dict()
        self.assertEqual(self.p.to_json(), before, "building the projection changes nothing")

    def test_it_exposes_no_cad_vocabulary(self):
        text = json.dumps(self.p.approved_architecture().to_dict()).lower()
        for word in ("layer", "handle", "dxf", "dwg", "ezdxf", "insunits", "unit_code", "layout", "paper", "a-wall", "entity_ids",
                     "sha256", "format_version", "source_metadata"):
            self.assertNotIn(word, text, word)

    def test_several_sources_must_be_named(self):
        p = interpreted()
        interpret_into(p, drawing_sheet().document(), revision="B")
        with self.assertRaises(ValidationError):
            p.approved_architecture()
        self.assertEqual(p.approved_architecture("SRC-2").revision, "B")
        self.assertEqual(p.approved_architecture("SRC-1").revision, None)
        with self.assertRaises(ValidationError):
            p.approved_architecture("SRC-9")
        with self.assertRaises(ValidationError):
            OracleProject.create("empty", "E").approved_architecture()

    def test_it_can_be_consumed_from_a_saved_project_without_any_cad_or_interpretation_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved.oracle.json"
            self.p.save(path)
            code = (
                "import sys, json\n"
                "from oracle.core import OracleProject\n"
                f"p = OracleProject.load(r'{path}')\n"
                "a = p.approved_architecture()\n"
                "assert a.ready and a.levels and a.observations and a.views\n"
                "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('ezdxf', 'anthropic', 'shapely', 'matplotlib', 'numpy')\n"
                "             or m.startswith('oracle.ingestion') or m.startswith('oracle.interpretation') or m.startswith('oracle.adapters'))\n"
                "print(json.dumps(bad))\n")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(REPO_ROOT))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]), [])


if __name__ == "__main__":
    unittest.main()
