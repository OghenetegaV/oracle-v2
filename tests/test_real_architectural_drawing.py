"""Real-world test: a Revit-exported architectural sheet set (Proposed Squash Court Extension)

Protects:
    That the whole Phase 3 pipeline copes with a genuine 20-sheet, ~24,000-entity architectural DWG that no
    synthetic drawing resembles: it is converted and read without loss, split into the views it really
    contains (ground / first / roof plans with blow-ups and furniture plans, three sections, six elevations,
    a sheet list), levels and heights are taken from its written level tags, and the things a careful
    engineer would question are RAISED and not decided: the file claims inches while the drawing is clearly
    millimetres, plan and section level counts differ, two levels are tagged with two elevations each, and
    plans cannot be lined up with confidence. The original DWG must remain byte-for-byte untouched.

Test type:
    Slow integration test (about 35 s: the ODA converter and the pure-Python reader dominate). Tier "slow": it is NOT
    part of the default run; run it with `python -m tests slow` (see tests/tiers.py and docs/TESTING.md). Skipped when
    the sample drawing or the ODA File Converter is missing.
    Assertions are deliberately about what is robustly there, not about every detail: Oracle does not claim
    100% accuracy on real drawings and this test does not either.

Dependencies:
    oracle.interpretation, oracle.ingestion, the ODA File Converter, input_dwgs/Sample Architectural Drawings -
    Proposed Squash Court Extension.dwg (never modified).
"""

import hashlib
import json
import time
import unittest
from collections import Counter
from pathlib import Path

from oracle.core import (
    BlockerKind, DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision, IssueSeverity, OracleProject, Target,
    ValueStatus, ViewType,
)
from oracle.ingestion import find_oda_converter
from oracle.interpretation import interpret_file, render_report
from tests.tiers import tier

REPO_ROOT = Path(__file__).resolve().parent.parent
DWG = REPO_ROOT / "input_dwgs" / "Sample Architectural Drawings - Proposed Squash Court Extension.dwg"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@tier("slow")
@unittest.skipUnless(DWG.exists() and find_oda_converter(), "sample drawing or ODA File Converter not available")
class RealSquashCourtDrawing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hash_before = sha(DWG)
        cls.listing_before = sorted(p.name for p in DWG.parent.iterdir())
        started = time.time()
        cls.project = interpret_file(DWG, project_name="Squash Court Extension", engineer="Test Engineer")
        cls.seconds = time.time() - started
        cls.arch = cls.project.architecture

    def test_the_original_drawing_is_untouched_and_nothing_is_left_beside_it(self):
        self.assertEqual(sha(DWG), self.hash_before)
        self.assertEqual(self.arch.drawing.sha256, self.hash_before)
        self.assertEqual(sorted(p.name for p in DWG.parent.iterdir()), self.listing_before)

    def test_the_drawing_is_read_completely(self):
        self.assertGreater(self.arch.drawing.entity_count, 20000)
        problems = [w for w in self.arch.drawing.warnings if "could not be read" in w or "needed recovery" in w]
        self.assertEqual(problems, [])                              # the DWG-conversion note is informational; these would not be

    def test_the_views_it_really_contains_are_found_and_typed(self):
        counts = Counter(v.view_type for v in self.arch.views)
        self.assertGreaterEqual(counts[ViewType.SECTION], 3)
        self.assertGreaterEqual(counts[ViewType.ELEVATION], 6)
        self.assertGreaterEqual(counts[ViewType.FLOOR_PLAN], 3)
        self.assertEqual(sorted({v.section_label for v in self.arch.views_of(ViewType.SECTION)} - {None}), ["A-A", "B-B", "C-C"])
        plan_levels = {v.level_key for v in self.arch.views_of(ViewType.FLOOR_PLAN) if v.variant is None and v.level_key}
        self.assertEqual(plan_levels, {"GROUND", "FLOOR:1", "ROOF"})

    def test_plans_are_told_apart_from_sections_and_elevations(self):
        for v in self.arch.views:
            if v.view_type != ViewType.FLOOR_PLAN:
                self.assertIsNone(v.level_key, v.id)
        blowups = [v for v in self.arch.views if v.variant == "blowup"]
        self.assertTrue(blowups, "blow-ups are recognised as variants, not as rival plans")

    def test_storey_heights_come_from_written_level_tags_as_source(self):
        rows = [(h.height_mm, h.source, h.basis) for h in self.arch.heights if (h.from_level, h.to_level) == ("GROUND", "FLOOR:1")]
        self.assertTrue(rows)
        self.assertEqual({r[0] for r in rows}, {3150.0})
        self.assertEqual({r[2] for r in rows}, {ValueStatus.SOURCE})
        self.assertGreaterEqual(len(rows), 6, "sections and elevations both contribute")

    def test_the_unit_conflict_is_raised_not_hidden(self):
        source = self.arch.drawing
        self.assertEqual(source.source_metadata["unit_code"], 1, "the file says inches")
        self.assertEqual(source.declared_unit, "inch")
        self.assertEqual(source.units.unit, "mm")
        self.assertEqual(self.project.value_status_of(Target.architectural(source.id), "units").status, ValueStatus.ASSUMED)
        self.assertTrue([i for i in self.project.issues if i.severity == IssueSeverity.ERROR and "metadata says inch" in i.message])
        self.assertTrue([s for s in self.project.interpretation_sets if s.question == "What unit is the drawing in?"])

    def test_disagreements_are_interpretation_sets_and_issues_never_a_silent_choice(self):
        questions = [s.question for s in self.project.interpretation_sets]
        self.assertTrue([q for q in questions if "How many structural levels" in q])
        self.assertTrue([q for q in questions if "appears with 2 different elevations" in q])
        self.assertTrue([q for q in questions if "line up" in q])
        self.assertTrue(all(s.status.value == "open" for s in self.project.interpretation_sets))
        self.assertTrue([i for i in self.project.issues if i.severity == IssueSeverity.ERROR])

    def test_layers_are_classified_with_honest_confidence(self):
        self.assertGreater(len(self.arch.layers), 40)
        by_name = {l.name: l for l in self.arch.layers}
        for name, l in by_name.items():
            if l.semantic_class == "unknown":
                self.assertEqual(l.confidence, 0.0, name)
        finish = by_name.get("WALL-FINISH-02")
        if finish is not None:                                           # the brief's own example, if this file has it
            self.assertLess(finish.confidence, 0.6)

    def test_observations_are_architectural_and_hints_are_only_hints(self):
        self.assertGreater(len(self.arch.observations), 1000)
        for o in self.arch.observations:
            self.assertNotIn("structural", o.kind)
        self.assertTrue([o for o in self.arch.observations if o.hint])

    def test_no_building_is_created_and_the_project_is_not_ready(self):
        self.assertIsNone(self.project.building)
        readiness = self.project.readiness()
        self.assertFalse(readiness.ready)
        kinds = {b.kind for b in readiness.blockers}
        self.assertTrue({BlockerKind.NO_BUILDING, BlockerKind.OPEN_INTERPRETATION, BlockerKind.UNREVIEWED_VIEW} <= kinds)

    def test_the_result_validates_reloads_and_reports(self):
        self.project.validate()
        again = OracleProject.from_json(self.project.to_json())
        self.assertEqual(again.to_json(), self.project.to_json())
        report = render_report(self.project)
        for heading in ("UNITS:", "VIEWS:", "HEIGHT EVIDENCE", "OBSERVATIONS:", "CROSS-VIEW FINDINGS", "AMBIGUITIES", "ISSUES:", "READINESS:"):
            self.assertIn(heading, report)

    # ---- Phase 3.5: traceability, resolution and the approved projection on the real drawing ----

    def clone(self):
        return OracleProject.from_json(self.project.to_json())

    def test_an_observation_traces_back_to_entities_of_the_original_drawing(self):
        obs = next(o for o in self.arch.observations if o.kind == "column_symbol")
        tr = self.project.trace(obs.id)
        self.assertEqual(len(tr.gaps), 1, tr.gaps)
        self.assertIn("proposed, not accepted by an engineer", tr.gaps[0])       # the only thing missing is the engineer's approval
        self.assertEqual(tr.sources[0]["sha256"], self.hash_before, "the trace ends at the exact bytes of the original DWG")
        self.assertEqual(tr.sources[0]["file"], DWG.name)
        self.assertIn(obs.entity_ids[0], [e["entity_id"] for e in tr.entities])

    def test_the_unit_conflict_can_be_resolved_and_then_the_state_agrees(self):
        p = self.clone()
        q = next(s for s in p.interpretation_sets if s.question == "What unit is the drawing in?")
        alt = next(a for a in q.alternatives if a.meaning == "mm")
        d = EngineeringDecision("REAL-1", "Test Engineer", DecisionSource.ENGINEER, Target.architectural(self.arch.drawing.id),
                                DecisionCategory.OTHER, "The drawing is in millimetres.", status=DecisionStatus.ACCEPTED)
        p.add_decision(d)
        p.accept_interpretation(q.id, alt.id, d.id)
        self.assertEqual(p.architecture.drawing.units.method, "engineer_decision")
        self.assertEqual(p.value_status_of(Target.architectural(self.arch.drawing.id), "units").status, ValueStatus.ENGINEER_OVERRIDE)
        self.assertFalse([i for i in p.issues if i.interpretation_set_id == q.id and i.is_open])
        self.assertEqual(OracleProject.from_json(p.to_json()).to_json(), p.to_json())

    def test_the_approved_projection_of_a_reviewed_real_plan_is_in_domain_terms_and_millimetres(self):
        p = self.clone()
        plan = next(v for v in p.architecture.views_of(ViewType.FLOOR_PLAN) if v.variant is None and v.level_key == "GROUND")
        p.review_views([plan.id], EngineeringDecision("REAL-2", "Test Engineer", DecisionSource.ENGINEER, Target.architectural(plan.id),
                                                      DecisionCategory.OTHER, "Ground plan checked.", status=DecisionStatus.ACCEPTED))
        ids = [o.id for o in p.architecture.observations_in(plan.id)][:50]
        p.review_observations(ids, EngineeringDecision("REAL-3", "Test Engineer", DecisionSource.ENGINEER, Target.architectural(ids[0]),
                                                       DecisionCategory.OTHER, "Observations checked.", status=DecisionStatus.ACCEPTED))
        approved = p.approved_architecture()
        self.assertEqual([v.id for v in approved.views], [plan.id])
        self.assertEqual(len(approved.observations), len(ids))
        self.assertFalse(approved.ready)
        self.assertFalse(approved.unit_confirmed)
        text = json.dumps(approved.to_dict()).lower()
        for word in ("layer", "handle", "insunits", "unit_code", "sha256"):
            self.assertNotIn(word, text)

    def test_the_project_size_is_dominated_by_provenance_and_observations_not_by_duplicated_geometry(self):
        data = json.loads(self.project.to_json())
        sizes = {k: len(json.dumps(data[k], indent=2, ensure_ascii=False)) for k in ("provenance", "value_status", "architectures")}
        total = len(self.project.to_json())
        self.assertGreater(sum(sizes.values()), 0.85 * total, sizes)
        self.assertLess(total, 40 * 1024 * 1024, "still comfortably a file a laptop can load")

    def test_it_finishes_in_a_reasonable_time(self):
        self.assertLess(self.seconds, 600.0)


if __name__ == "__main__":
    unittest.main()
