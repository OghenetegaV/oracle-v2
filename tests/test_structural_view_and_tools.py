"""Tests for the structural review view, point-pick alignment, split preview and view-type correction (tests/test_structural_view_and_tools.py)

Protects:
    That the structural review rendering hides furnishing and presentation clutter (and only renders less: nothing is deleted from the drawing, the
    project or the provenance, and the Original Drawing view shows everything) while walls, openings, stairs, grids and their names, levels and
    structural symbols stay; that a reserved layer exists for later slab panels; that point-pick alignment computes the right translation and rotation
    from correspondence points, previews without changing anything, records ONE engineer decision on acceptance, keeps source coordinates untouched
    and survives save and load; that a split can be previewed before it is accepted; and that correcting a view's TYPE is an engineer decision that keeps
    the view, its evidence and Oracle's original reading, and stays a different operation from rejecting the view.

Test type:
    Integration (tier "integration"): synthetic DXFs interpreted through the real pipeline and read through the application layer; the canvas checks use
    a hidden Tk root.

Dependencies:
    oracle.application, oracle.ui.preview_canvas, tests.drawing_factory.
"""

import gc
import hashlib
import math
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from oracle.application import ActionRefused, ArchitecturalSession
from oracle.application.alignment import solve
from oracle.core import DecisionSource, DecisionStatus, ReviewStatus, Target, ViewType
from tests.drawing_factory import three_storey_sheet
from tests.tiers import tier


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Base(unittest.TestCase):
    furnished = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        sheet = three_storey_sheet()
        if self.furnished:
            doc = sheet.doc
            for name in ("A-FLOR-STRS", "A-DETL"):
                if name not in doc.layers:
                    doc.layers.add(name)
            msp = doc.modelspace()
            for i in range(3):                                                  # a bed, a sofa and a table on the furniture layer
                x = 1500 + i * 1200
                msp.add_lwpolyline([(x, 1500), (x + 900, 1500), (x + 900, 2100), (x, 2100)], close=True, dxfattribs={"layer": "A-FURN"})
            msp.add_text("BED", height=150, dxfattribs={"layer": "A-FURN", "insert": (1600, 1600)})
            block = doc.blocks.new("FURNITURE_ARMCHAIR")
            block.add_lwpolyline([(0, 0), (600, 0), (600, 600), (0, 600)], close=True)
            msp.add_blockref("FURNITURE_ARMCHAIR", (5000, 3000), dxfattribs={"layer": "A-DETL"})
            msp.add_lwpolyline([(6000, 5000), (7500, 5000), (7500, 6800), (6000, 6800)], close=True, dxfattribs={"layer": "A-FLOR-STRS"})   # a stair
        self.path = self.dir / "plan.dxf"
        sheet.save(self.path)
        self.hash = sha(self.path)
        self.s = ArchitecturalSession(engineer="Test Engineer")
        self.s.interpret(self.path)
        self.p = self.s.project
        self.arch = self.p.architecture

    def reopen(self) -> ArchitecturalSession:
        saved = self.s.save_project(self.dir / "p.oracle.json")
        fresh = ArchitecturalSession()
        fresh.open_project(saved)
        return fresh


@tier("integration")
class TheStructuralReviewRendering(Base):
    furnished = True

    def layer_of(self, name):
        return next(l.semantic_class for l in self.arch.layers if l.name == name)

    def paths(self, layer):
        pv = self.s.preview()
        return [i for i, p in enumerate(pv.paths) if p.layer == layer]

    def test_furniture_is_hidden_from_the_structural_review(self):
        pv = self.s.preview()
        self.assertEqual(self.layer_of("A-FURN"), "furniture")
        furniture = self.paths("A-FURN")
        self.assertEqual(len(furniture), 3)
        self.assertTrue(all(pv.is_hidden(i, True) for i in furniture))
        armchair = [i for i, p in enumerate(pv.paths) if p.block == "FURNITURE_ARMCHAIR"]
        self.assertTrue(armchair and all(pv.is_hidden(i, True) for i in armchair))            # a furniture block on an ordinary layer is hidden too
        self.assertGreaterEqual(pv.hidden_count, 4)

    def test_walls_openings_stairs_grids_levels_and_structural_symbols_stay(self):
        pv = self.s.preview()
        for layer in ("A-WALL-EXT", "A-WALL-INT", "A-DOOR", "A-GLAZ", "A-GRID", "A-COLS", "A-FLOR-STRS"):
            found = self.paths(layer)
            self.assertTrue(found, layer)
            self.assertFalse(any(pv.is_hidden(i, True) for i in found), f"{layer} must stay visible")

    def test_grid_names_levels_and_dimensions_stay_but_furniture_labels_and_room_text_go(self):
        pv = self.s.preview()
        by_layer = {}
        for _x, _y, text, _h, layer in pv.texts:
            by_layer.setdefault(layer, []).append(text)
        self.assertIn("A-GRID-IDEN", by_layer)
        self.assertTrue(pv.shows_text("A-GRID-IDEN", True))                                     # grid bubbles and names
        self.assertTrue(pv.shows_text("A-FLOR-LEVL", True))                                     # level names and elevations
        self.assertFalse(pv.shows_text("A-FURN", True))                                         # "BED"
        self.assertFalse(pv.shows_text("A-ANNO-TEXT", True))                                    # room labels and general notes
        self.assertTrue(all(pv.shows_text(layer, False) for layer in by_layer))                 # the original drawing shows every label

    def test_the_original_drawing_view_shows_everything(self):
        pv = self.s.preview()
        self.assertFalse(any(pv.is_hidden(i, False) for i in range(len(pv.paths))))

    def test_rendering_never_touches_the_drawing_the_project_or_the_provenance(self):
        before_project = self.p.to_json()
        entities = len(self.s.documents["SRC-1"].entities)
        provenance = len(self.p.provenance)
        pv = self.s.preview()
        pv.set_structural_filter({l.name: l.semantic_class for l in self.arch.layers})
        self.assertEqual(self.p.to_json(), before_project)
        self.assertEqual(len(self.s.documents["SRC-1"].entities), entities)                     # every entity is still in the document
        self.assertEqual(len(self.p.provenance), provenance)
        self.assertEqual(sha(self.path), self.hash)                                              # and the file on disk is byte-for-byte the same

    def test_the_canvas_draws_less_in_structural_review_and_everything_in_original(self):
        from oracle.ui.preview_canvas import ORIGINAL, STRUCTURAL, PreviewCanvas
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        root.geometry("900x600+-2600+0")
        try:
            canvas = PreviewCanvas(root)
            canvas.pack(fill="both", expand=True)
            root.update()
            canvas.set_preview(self.s.preview())
            for _ in range(4):
                root.update()
            counts = {}
            for mode in (STRUCTURAL, ORIGINAL):
                canvas.mode = mode
                canvas.redraw()
                counts[mode] = len(canvas.find_all())
            self.assertLess(counts[STRUCTURAL], counts[ORIGINAL])
            self.assertIn("hidden", self._note(canvas, STRUCTURAL))                              # and it says so
            canvas.cancel_pending()
        finally:
            root.destroy()
            gc.collect()

    def _note(self, canvas, mode):
        notes = []
        canvas.status_callback = notes.append
        canvas.mode = mode
        canvas.redraw()
        return notes[-1]

    def test_a_layer_is_reserved_for_future_slab_panels_and_is_empty_now(self):
        from oracle.application import Overlay
        from oracle.ui.preview_canvas import STYLES, PreviewCanvas
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        root.withdraw()
        try:
            canvas = PreviewCanvas(root)
            self.assertEqual(canvas.structural.rects, [])                                         # nothing generates slab panels in this phase
            self.assertIn("panel", STYLES)
            panel = Overlay()
            panel.rects.append(((0, 0, 5000, 4000), "panel", "S1", ""))
            canvas.set_structural_overlay(panel)                                                   # a later phase can show a panel here
            self.assertEqual(len(canvas.structural.rects), 1)
            canvas.cancel_pending()
        finally:
            root.destroy()
            gc.collect()


@tier("integration")
class PointPickAlignment(Base):
    def corners(self):
        b1, b2 = self.s.view_box("VIEW-01"), self.s.view_box("VIEW-02")
        return b1, b2

    def test_one_pair_collects_and_moves_the_plan_onto_the_reference(self):
        b1, b2 = self.corners()
        p, q = (b2[0] + 3000, b2[1] + 2000), (b1[0] + 3000, b1[1] + 2000)
        sol = self.s.alignment_preview("VIEW-02", "VIEW-01", [(p, q)])
        self.assertEqual(sol.rotation_deg, 0.0)
        xs = [x for line in sol.moved for x, _ in line]
        ys = [y for line in sol.moved for _, y in line]
        self.assertAlmostEqual(min(xs), b1[0], delta=1e-6)                                       # the moved plan lands exactly on the reference plan
        self.assertAlmostEqual(max(ys), b1[3], delta=1e-6)

    def test_two_pairs_give_translation_and_rotation(self):
        b1, b2 = self.corners()
        p1, p2 = (b2[0], b2[1]), (b2[2], b2[1])
        for angle in (90.0, -90.0, 30.0, 180.0 - 1e-3):
            length = p2[0] - p1[0]
            q1 = (b1[0], b1[1])
            q2 = (q1[0] + length * math.cos(math.radians(-angle)), q1[1] + length * math.sin(math.radians(-angle)))
            sol = self.s.alignment_preview("VIEW-02", "VIEW-01", [(p1, q1), (p2, q2)])
            self.assertAlmostEqual(sol.rotation_deg, angle, places=3)
            self.assertLess(sol.residual_mm, 1e-6)
            self.assertIsNone(self.warnings_of(sol, "scale"))

    @staticmethod
    def warnings_of(sol, word):
        return next((w for w in sol.warnings if word in w), None)

    def test_the_transformation_maps_the_picked_points_onto_each_other(self):
        b1, b2 = self.corners()
        p1, p2 = (b2[0] + 500, b2[1] + 700), (b2[2] - 900, b2[3] - 1100)
        q1, q2 = (b1[0] + 1000, b1[1] + 300), (b1[0] + 1000 + 8000, b1[1] + 300 + 6000)       # a different line: the pair distance differs a little
        sol = solve(self.arch, self.arch.get("VIEW-02"), self.arch.get("VIEW-01"), [(p1, q1), (p2, q2)])
        c, s = math.cos(math.radians(sol.rotation_deg)), math.sin(math.radians(sol.rotation_deg))
        view = self.arch.get("VIEW-02")
        local1 = self.arch.from_source(view.frame_id, p1)
        q1_building = self.arch.from_source(self.arch.get("VIEW-01").alignment_frame_id or self.arch.get("VIEW-01").frame_id, q1)
        mapped = (sol.frame_translation[0] + c * q1_building[0] - s * q1_building[1], sol.frame_translation[1] + s * q1_building[0] + c * q1_building[1])
        self.assertAlmostEqual(mapped[0], local1[0], places=6)
        self.assertAlmostEqual(mapped[1], local1[1], places=6)

    def test_scale_is_never_changed_and_a_mismatch_is_reported(self):
        b1, b2 = self.corners()
        p1, p2 = (b2[0], b2[1]), (b2[0] + 4000, b2[1])
        q1, q2 = (b1[0], b1[1]), (b1[0] + 6000, b1[1])                                           # 50% longer on the reference
        sol = self.s.alignment_preview("VIEW-02", "VIEW-01", [(p1, q1), (p2, q2)])
        self.assertTrue(self.warnings_of(sol, "scale"))
        self.s.apply_alignment("VIEW-02", "VIEW-01", [(p1, q1), (p2, q2)])
        self.assertEqual(self.arch.frames[-1].scale, 1.0)

    def test_bad_picks_are_refused_in_words(self):
        b1, b2 = self.corners()
        same = (b2[0], b2[1])
        with self.assertRaises(ActionRefused):
            self.s.alignment_preview("VIEW-02", "VIEW-01", [(same, (b1[0], b1[1])), (same, (b1[0] + 5, b1[1]))])
        with self.assertRaises(ActionRefused):
            self.s.alignment_preview("VIEW-02", "VIEW-01", [])
        self.s.change_view_type("VIEW-03", "section")
        with self.assertRaises(ActionRefused):
            self.s.alignment_preview("VIEW-03", "VIEW-01", [(same, (b1[0], b1[1]))])            # only a plan can be aligned to a plan

    def test_the_preview_changes_nothing(self):
        b1, b2 = self.corners()
        before = self.p.to_json()
        decisions = len(self.p.decisions)
        self.s.alignment_preview("VIEW-02", "VIEW-01", [((b2[0], b2[1]), (b1[0], b1[1]))])
        self.assertEqual((self.p.to_json(), len(self.p.decisions)), (before, decisions))

    def test_accepting_records_one_engineer_decision_and_keeps_the_points_and_the_source_untouched(self):
        b1, b2 = self.corners()
        pairs = [((b2[0], b2[1]), (b1[0], b1[1])), ((b2[2], b2[1]), (b1[0], b1[1] + (b2[2] - b2[0])))]      # turned by 90 degrees
        source_points = [tuple(pt) for e in self.s.documents["SRC-1"].entities for pt in e.points]
        frames_before, decisions_before = len(self.arch.frames), len(self.p.decisions)
        d = self.s.apply_alignment("VIEW-02", "VIEW-01", pairs, "matched the column at the corner")
        self.assertEqual(len(self.p.decisions), decisions_before + 1)
        self.assertEqual((d.source, d.status, d.field, d.target), (DecisionSource.ENGINEER, DecisionStatus.ACCEPTED, "alignment_frame_id", Target.architectural("VIEW-02")))
        self.assertIn("Point-picked alignment", d.reason)
        self.assertIn("matched the column at the corner", d.reason)
        self.assertIn("rotation", d.instruction)
        frame = self.arch.frames[-1]
        self.assertEqual((len(self.arch.frames), frame.id, round(frame.rotation_deg, 6)), (frames_before + 1, d.value, -90.0))
        self.assertEqual(self.arch.get("VIEW-02").alignment_frame_id, frame.id)
        self.assertEqual([tuple(pt) for e in self.s.documents["SRC-1"].entities for pt in e.points], source_points)   # source coordinates unchanged
        self.assertEqual(sha(self.path), self.hash)

    def test_the_alignment_survives_save_and_load_and_reaches_the_approved_model(self):
        b1, b2 = self.corners()
        self.s.review_views(["VIEW-01", "VIEW-02"], accept=True)
        self.s.apply_alignment("VIEW-02", "VIEW-01", [((b2[0], b2[1]), (b1[0], b1[1]))])
        fresh = self.reopen()
        self.assertEqual(fresh.project.to_dict(), self.s.project.to_dict())
        frame_id = fresh.project.architecture.get("VIEW-02").alignment_frame_id
        self.assertEqual(fresh.project.architecture.frames[-1].id, frame_id)
        approved = {v.id: v.frame for v in fresh.project.approved_architecture().views}
        self.assertEqual(approved["VIEW-02"], "building")

    def test_a_plan_can_be_aligned_again_and_the_history_keeps_both(self):
        b1, b2 = self.corners()
        first = self.s.apply_alignment("VIEW-02", "VIEW-01", [((b2[0], b2[1]), (b1[0], b1[1]))])
        second = self.s.apply_alignment("VIEW-02", "VIEW-01", [((b2[0] + 100, b2[1]), (b1[0], b1[1]))])
        history = self.p.decision_history(Target.architectural("VIEW-02"), "alignment_frame_id")
        self.assertEqual([d.id for d in history if d.id in (first.id, second.id)], [first.id, second.id])

    def test_the_choices_are_the_other_plans_and_the_view_box_is_available(self):
        self.assertEqual([i for i, _n in self.s.plan_choices("VIEW-02")], ["VIEW-01", "VIEW-03"])
        self.assertEqual(len(self.s.view_box("VIEW-01")), 4)


@tier("integration")
class SplitWithAPreview(Base):
    def test_the_preview_shows_the_parts_and_changes_nothing_then_the_split_is_accepted(self):
        b = self.s.view_box("VIEW-03")
        mid = (b[0] + b[2]) / 2
        before = self.p.to_json()
        preview = self.s.split_preview("VIEW-03", "x", mid)
        self.assertEqual(self.p.to_json(), before)
        self.assertEqual(sum(preview["counts"]), len(self.arch.get("VIEW-03").entity_ids))
        self.assertLess(preview["low"][2], preview["high"][2])
        ids = self.s.split_view("VIEW-03", "x", mid, "two plans on one sheet")
        self.assertEqual(self.arch.get("VIEW-03").review, ReviewStatus.SUPERSEDED)
        self.assertEqual(len(ids), 2)
        self.assertEqual(sha(self.path), self.hash)

    def test_a_line_outside_the_view_is_refused_in_words(self):
        b = self.s.view_box("VIEW-03")
        with self.assertRaises(ActionRefused) as caught:
            self.s.split_preview("VIEW-03", "x", b[2] + 5000)
        self.assertIn("Second Floor Plan", str(caught.exception))


@tier("integration")
class ChangingAViewsType(Base):
    def test_plan_to_section_is_an_engineer_decision_that_keeps_the_view_and_oracles_reading(self):
        view_before = self.arch.get("VIEW-01")
        d = self.s.change_view_type("VIEW-01", "section", "It is the section through the stair core.")
        self.assertEqual((d.source, d.status, d.field, d.value, d.previous_value), (DecisionSource.ENGINEER, DecisionStatus.ACCEPTED, "view_type", "section", "floor_plan"))
        self.assertEqual(d.reason, "It is the section through the stair core.")
        view = self.arch.get("VIEW-01")
        self.assertEqual(view.view_type, ViewType.SECTION)
        self.assertEqual((view.review, view.bbox, view.entity_ids, view.title), (view_before.review, view_before.bbox, view_before.entity_ids, view_before.title))
        self.assertIn("VIEW-01", [v.id for v in self.arch.views])                                 # not deleted, not replaced
        self.assertEqual(len([v for v in self.arch.views if v.title == "GROUND FLOOR PLAN"]), 1)  # no unrelated replacement view
        self.assertEqual(self.p.value_status_of(Target.architectural("VIEW-01"), "view_type").decision_id, d.id)

    def test_plan_to_elevation_works_too(self):
        d = self.s.change_view_type("VIEW-02", "elevation")
        self.assertEqual(self.arch.get("VIEW-02").view_type, ViewType.ELEVATION)
        self.assertEqual((d.previous_value, d.value), ("floor_plan", "elevation"))

    def test_it_is_not_a_rejection(self):
        self.s.change_view_type("VIEW-01", "section")
        self.assertNotEqual(self.arch.get("VIEW-01").review, ReviewStatus.REJECTED)
        self.assertEqual(self.s.rejected_views(), [])
        self.assertIsNone(self.p.decisions[-1].reason_code)
        rejected = self.s.reject_views(["VIEW-02"], "duplicate")
        self.assertEqual(self.arch.get("VIEW-02").review, ReviewStatus.REJECTED)
        self.assertEqual(self.arch.get("VIEW-02").view_type, ViewType.FLOOR_PLAN)                # rejecting does not re-classify
        self.assertEqual(rejected.reason_code, "duplicate")
        self.assertEqual(self.arch.get("VIEW-01").review, ReviewStatus.PROPOSED)                # correcting does not accept either

    def test_the_history_shows_oracles_original_reading_and_each_correction(self):
        self.s.change_view_type("VIEW-01", "section", "first thought")
        self.s.change_view_type("VIEW-01", "elevation", "on reflection")
        history = self.p.decision_history(Target.architectural("VIEW-01"), "view_type")
        self.assertEqual([(d.previous_value, d.value) for d in history], [("floor_plan", "section"), ("section", "elevation")])
        self.assertEqual(history[0].status, DecisionStatus.SUPERSEDED)                          # the first is history now, not deleted
        row = [r for r in self.s.decision_rows() if "view_type" in r[6]][0]
        self.assertIn("was 'floor_plan'", row[6])
        self.assertIn("Oracle read this view as: Plan", " ".join(self.s.card("view:VIEW-01").facts))

    def test_it_survives_save_and_load(self):
        self.s.change_view_type("VIEW-01", "detail")
        fresh = self.reopen()
        self.assertEqual(fresh.project.architecture.get("VIEW-01").view_type, ViewType.DETAIL)
        self.assertEqual(fresh.project.to_dict(), self.s.project.to_dict())
        self.assertEqual(fresh.card("view:VIEW-01").type_label, "Detail")

    def test_the_level_a_plan_had_is_dropped_openly_when_it_stops_being_a_plan(self):
        self.assertEqual(self.arch.get("VIEW-01").level_key, "GROUND")
        d = self.s.change_view_type("VIEW-01", "section")
        self.assertIsNone(self.arch.get("VIEW-01").level_key)
        self.assertIn("no longer applies", d.instruction)
        self.reopen()                                                                              # and the project still validates
        self.s.set_view_field("VIEW-02", "level_key", "FLOOR:1", "confirmed")
        self.s.change_view_type("VIEW-02", "section")                                             # even with an engineer ruling on the level
        self.reopen()

    def test_the_menu_keeps_change_type_and_reject_as_separate_entries(self):
        menu = self.s.card("view:VIEW-01").menu
        self.assertEqual(menu, ["change_type", "align_plan", "split_view", "reject_view", "ask_engineer", "review_evidence"])
        self.s.change_view_type("VIEW-01", "section")
        self.assertNotIn("align_plan", self.s.card("view:VIEW-01").menu)                          # only plans are aligned
        self.assertEqual(self.s.card("view:VIEW-01").actions, ["accept_view", "view_actions"])

    def test_bad_requests_are_refused_and_change_nothing(self):
        before = self.p.to_json()
        for new in ("floor_plan", "spaceship", ""):
            with self.assertRaises(ActionRefused):
                self.s.change_view_type("VIEW-01", new)
        with self.assertRaises(ActionRefused):
            self.s.change_view_type("NOPE-1", "section")
        self.assertEqual(self.p.to_json(), before)
        self.assertEqual(sha(self.path), self.hash)
