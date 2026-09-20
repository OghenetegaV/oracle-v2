"""Tests for spatial segmentation, vertical evidence, alignment and cross-view reconciliation

Protects:
    The stage-level behaviour behind the pipeline: views are found by where geometry sits (never by layer),
    sheet frames are recognised and not merged into their contents, titles attach to the nearest view,
    level tags pair each name with its own elevation number, level lines are told from partial floor lines,
    written elevations beat drawn spacing, plan alignment prefers shared grid labels and refuses to be
    confident from footprints alone, and reconciliation turns every disagreement into an interpretation
    set plus an issue while agreements stay quiet.

Test type:
    Unit tests of oracle.interpretation.segmentation, .vertical, .alignment and .reconcile on small
    synthetic drawings and hand-built rows.

Dependencies:
    oracle.interpretation, oracle.core, tests.drawing_factory.
"""

import unittest

from oracle.core import IssueSeverity, ViewType
from oracle.interpretation.alignment import alignment_candidates, grid_label_positions, grid_vote, view_geometry
from oracle.interpretation.layers import classify_layers
from oracle.interpretation.naming import LevelNamer
from oracle.interpretation.reconcile import reconcile
from oracle.interpretation.segmentation import dominant_rotation, find_frames, segment
from oracle.interpretation.vertical import analyse_elevation, analyse_section, extract_level_tags, horizontal_lines
from tests.drawing_factory import Sheet, three_storey_sheet
from tests.tiers import tier


def regions_of(sheet, namer=None):
    doc = sheet.document()
    layer_class = {v.name: v.semantic_class for v in classify_layers(doc)}
    return segment(doc.entities, layer_class, namer or LevelNamer()) + (layer_class,)


@tier("integration")
class Segmentation(unittest.TestCase):
    def test_side_by_side_plans_are_separate_regions_with_their_own_titles(self):
        regions, frames, _stray, _lc = regions_of(three_storey_sheet())
        self.assertEqual(len(regions), 3)
        self.assertEqual(frames, [])
        self.assertEqual(sorted(r.title.text for r in regions), ["FIRST FLOOR PLAN", "GROUND FLOOR PLAN", "SECOND FLOOR PLAN"])
        for r in regions:
            self.assertEqual(r.view_type, ViewType.FLOOR_PLAN)

    def test_segmentation_ignores_layer_names_when_placing_regions(self):
        from tests.drawing_factory import UNKNOWN
        a, b = three_storey_sheet(), three_storey_sheet(layers=UNKNOWN)
        boxes = lambda s: sorted(tuple(round(x) for x in r.box) for r in regions_of(s)[0])
        self.assertEqual(boxes(a), boxes(b))

    def test_a_far_away_view_is_its_own_region_and_a_nearby_small_mark_joins_its_neighbour(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.scatter("text", 3, origin=(14500.0, 200.0), spacing=100.0)         # a few marks just beside the plan
        s.plan((500000, 500000), "FIRST FLOOR PLAN")
        regions, *_ = regions_of(s)
        self.assertEqual(len(regions), 2)

    def test_a_sheet_frame_is_found_and_not_merged_into_what_it_contains(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((20000, 0), "FIRST FLOOR PLAN")
        s.scatter("text", 150, origin=(-2500.0, -3500.0), spacing=60.0)      # a title block's many small marks
        s.msp.add_lwpolyline([(-3000, -4000), (36000, -4000), (36000, 12000), (-3000, 12000)], close=True,
                             dxfattribs={"layer": "A-ANNO-TTLB"})
        doc = s.document()
        self.assertEqual(len(find_frames(doc.entities)), 1)
        regions, frames, *_ = regions_of(s)
        self.assertEqual((len(regions), len(frames)), (2, 1))
        self.assertTrue(all(r.frame is frames[0] for r in regions))

    def test_the_title_below_a_view_is_preferred_to_text_above_it(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s._text("text", "FIRST FLOOR PLAN", (6000, 12000), 350)               # a stray title above the view
        regions, *_ = regions_of(s)
        self.assertEqual(regions[0].title.text, "GROUND FLOOR PLAN")

    def test_confidence_and_evidence_come_with_every_classification(self):
        regions, *_ = regions_of(three_storey_sheet())
        for r in regions:
            kinds = {k for k, _d, _w in r.evidence}
            self.assertIn("title_text", kinds)
            self.assertIn("layer_class", kinds)
            self.assertGreater(r.confidence, 0.8)

    def test_rotation_is_estimated_from_wall_directions(self):
        for angle in (0.0, 20.0, -30.0, 44.0):
            s = Sheet()
            s.plan((0, 0), "GROUND FLOOR PLAN", rotation_deg=angle)
            regions, _f, _s, lc = regions_of(s)
            found, support = dominant_rotation(regions[0].geometry, lc)
            self.assertAlmostEqual(found, angle, delta=0.6, msg=str(angle))
            self.assertGreater(support, 0.5)


@tier("integration")
class VerticalEvidence(unittest.TestCase):
    def section_region(self, **kw):
        s = Sheet()
        s.section((0, 0), "SECTION A-A", **kw)
        regions, *_ = regions_of(s)
        return regions[0]

    def test_each_level_name_pairs_with_its_own_number(self):
        region = self.section_region(levels=(("GROUND FLOOR", 0), ("FIRST FLOOR", 3300), ("SECOND FLOOR", 6900)))
        tags = extract_level_tags(region.texts, LevelNamer())
        self.assertEqual([(t.key, t.elevation_mm) for t in tags], [("GROUND", 0.0), ("FLOOR:1", 3300.0), ("FLOOR:2", 6900.0)])

    def test_a_tightly_packed_stack_does_not_swap_numbers(self):
        region = self.section_region(levels=(("GROUND FLOOR", 0), ("FIRST FLOOR", 300), ("SECOND FLOOR", 600), ("ROOF", 900)))
        tags = extract_level_tags(region.texts, LevelNamer())
        self.assertEqual([(t.key, t.elevation_mm) for t in tags],
                         [("GROUND", 0.0), ("FLOOR:1", 300.0), ("FLOOR:2", 600.0), ("ROOF", 900.0)])

    def test_heights_between_written_elevations_are_source(self):
        found = analyse_section(self.section_region(levels=(("GROUND FLOOR", 0), ("FIRST FLOOR", 3300), ("SECOND FLOOR", 6600))),
                                LevelNamer(), 1.0)
        self.assertEqual([(a, b, h, src, basis) for a, b, h, src, basis, _c in found.heights],
                         [("GROUND", "FLOOR:1", 3300.0, "level_tags", "source"), ("FLOOR:1", "FLOOR:2", 3300.0, "level_tags", "source")])

    def test_a_vertical_dimension_between_two_labelled_levels_corroborates_the_height(self):
        found = analyse_section(self.section_region(dims=True), LevelNamer(), 1.0)      # default levels: ground, first, roof
        self.assertIn(("GROUND", "FLOOR:1", 3300.0, "section_dimension"), [(a, b, h, s) for a, b, h, s, _basis, _c in found.heights])

    def test_full_width_lines_are_levels_and_a_short_one_is_a_partial_floor(self):
        region = self.section_region(partial=(("MEZZANINE", 1700, 0, 3000),))
        lines = horizontal_lines(region)
        full = [l for l in lines if l.coverage >= 0.7]
        partial = [l for l in lines if l.coverage < 0.7]
        self.assertEqual(len(full), 3)
        self.assertEqual(len(partial), 1)
        self.assertAlmostEqual(partial[0].y, 1700.0, delta=5.0)

    def test_no_numbers_and_no_labelled_lines_means_no_height(self):
        s = Sheet()
        s.section((0, 0), "SECTION A-A", levels=(("A", 0), ("B", 3300), ("C", 6600)))
        region = regions_of(s)[0][0]
        found = analyse_section(region, LevelNamer(), 1.0)
        self.assertEqual(found.heights, [])

    def test_drawn_spacing_never_overrides_written_numbers(self):
        # the tags say 0 / 3300 but the drawn lines are 0 / 1000 apart: the written numbers win and the disagreement is noted
        s = Sheet()
        s.section((0, 0), "SECTION A-A", levels=(("GROUND FLOOR", 0), ("FIRST FLOOR", 1000)))
        for t in s.msp.query("TEXT"):
            if t.dxf.text == "1000":
                t.dxf.text = "3300"
        region = regions_of(s)[0][0]
        found = analyse_section(region, LevelNamer(), 1.0)
        self.assertEqual(found.heights[0][2], 3300.0)
        self.assertFalse(found.tag_geometry_consistent)
        self.assertTrue(found.notes)

    def test_an_elevation_gives_level_evidence_too_but_is_kept_apart_from_a_section(self):
        s = Sheet()
        s.elevation((0, 0), "FRONT ELEVATION")
        region = regions_of(s)[0][0]
        self.assertEqual(region.view_type, ViewType.ELEVATION)
        found = analyse_elevation(region, LevelNamer(), 1.0)
        self.assertEqual([t.key for t in found.tags], ["GROUND", "FLOOR:1", "ROOF"])
        self.assertTrue(all(h[3] == "level_tags" for h in found.heights))


@tier("integration")
class Alignment(unittest.TestCase):
    def geometries(self, second_origin=(20000, 0), **kw):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan(second_origin, "FIRST FLOOR PLAN", **kw)
        regions, _f, _s, lc = regions_of(s)
        regions.sort(key=lambda r: r.box[0])
        geos = [view_geometry(r, 0.0) for r in regions]
        grids = [grid_label_positions(r, lc, g) for r, g in zip(regions, geos)]
        return geos, grids

    def test_identical_plans_with_the_same_grid_agree_strongly(self):
        (g0, g1), (l0, l1) = self.geometries()
        best = alignment_candidates(g0, l0, g1, l1)[0]
        self.assertGreaterEqual(best[2], 0.95)
        self.assertAlmostEqual(best[0][0], 0.0, places=6)
        self.assertAlmostEqual(best[0][1], 0.0, places=6)
        self.assertIn("grid_labels", best[1])

    def test_a_plan_shifted_within_its_sheet_is_found_by_grid_labels_not_by_footprint(self):
        # the second plan has an extra 3 m annexe on its left, so its footprint starts 3 m further left than its grid does
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        s.plan((20000, 0), "FIRST FLOOR PLAN")
        s._rect("wall_ext", 17000, 0, 20000, 8000, s._xf((0, 0)))
        regions, _f, _s, lc = regions_of(s)
        regions.sort(key=lambda r: r.box[0])
        geos = [view_geometry(r, 0.0) for r in regions]
        grids = [grid_label_positions(r, lc, g) for r, g in zip(regions, geos)]
        best = alignment_candidates(geos[0], grids[0], geos[1], grids[1])[0]
        # the annexed plan's own origin (its footprint corner) is 2.4 m left of its grid line A, the ground plan's only 0.6 m
        self.assertAlmostEqual(best[0][0], -2400.0, delta=1.0)
        self.assertIn("grid_labels", best[1])

    def test_without_grid_labels_footprints_alone_are_not_confident_when_sizes_differ(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN", grid=False)
        s.plan((20000, 0), "FIRST FLOOR PLAN", grid=False, width=10000.0)
        regions, _f, _s, lc = regions_of(s)
        regions.sort(key=lambda r: r.box[0])
        geos = [view_geometry(r, 0.0) for r in regions]
        candidates = alignment_candidates(geos[0], {}, geos[1], {})
        self.assertLess(candidates[0][2], 0.75)
        self.assertGreaterEqual(len(candidates), 3)
        self.assertEqual(len({tuple(round(x) for x in c[0]) for c in candidates}), len(candidates), "each alternative is a different translation")

    def test_grid_vote_needs_three_shared_labels(self):
        a = {"A": [(0.0, 0.0)], "B": [(10.0, 0.0)]}
        self.assertIsNone(grid_vote(a, a, 100.0))
        a["C"] = [(20.0, 0.0)]
        translation, agreeing, shared = grid_vote(a, {k: [(x + 5.0, y) for x, y in v] for k, v in a.items()}, 100.0)
        self.assertEqual((agreeing, shared), (3, 3))
        self.assertAlmostEqual(translation[0], -5.0)


@tier("unit")
class Reconciliation(unittest.TestCase):
    NAMER = LevelNamer()

    def run_reconcile(self, plans=(), verticals=(), heights=(), sheets=(), views=()):
        return reconcile(list(plans), list(verticals), list(heights), list(sheets), list(views), self.NAMER)

    def test_agreement_is_reported_quietly(self):
        r = self.run_reconcile(plans=[("VIEW-01", "GROUND", None, "G", 0.9), ("VIEW-02", "FLOOR:1", None, "F", 0.9)],
                               verticals=[("VIEW-03", ViewType.SECTION, [("GROUND", 0.0), ("FLOOR:1", 3300.0)], 2)],
                               heights=[("GROUND", "FLOOR:1", 3300.0, "level_tags", "VIEW-03"),
                                        ("GROUND", "FLOOR:1", 3310.0, "section_dimension", "VIEW-03")])
        self.assertEqual(r.sets, [])
        self.assertEqual(r.issues, [])
        self.assertEqual({f.agreement for f in r.findings}, {"agree"})

    def test_heights_within_tolerance_agree_and_beyond_it_disagree_with_both_kept(self):
        near = self.run_reconcile(heights=[("GROUND", "FLOOR:1", 3300.0, "a", "V1"), ("GROUND", "FLOOR:1", 3340.0, "b", "V2")])
        self.assertEqual(near.sets, [])
        far = self.run_reconcile(heights=[("GROUND", "FLOOR:1", 3300.0, "plan_text", "V1"), ("GROUND", "FLOOR:1", 3600.0, "level_tags", "V2")])
        (s,) = far.sets
        self.assertEqual(sorted(a[0] for a in s.alternatives), ["3300_mm", "3600_mm"])
        (issue,) = far.issues
        self.assertEqual(issue.severity, IssueSeverity.ERROR)
        self.assertIn("3300", issue.message)
        self.assertIn("3600", issue.message)

    def test_the_number_of_levels_is_compared_across_plans_sections_and_elevations(self):
        r = self.run_reconcile(plans=[("VIEW-01", "GROUND", None, "G", 0.9), ("VIEW-02", "FLOOR:1", None, "F", 0.9)],
                               verticals=[("VIEW-03", ViewType.SECTION, [("GROUND", 0.0), ("FLOOR:1", 3300.0), ("FLOOR:2", 6600.0)], 3),
                                          ("VIEW-04", ViewType.ELEVATION, [("GROUND", 0.0), ("FLOOR:1", 3300.0)], 2)])
        (s,) = r.sets
        self.assertEqual({a[0] for a in s.alternatives}, {"floor_plans", "sections", "elevations"})
        self.assertTrue(any(i.severity == IssueSeverity.ERROR for i in r.issues))

    def test_one_level_written_with_two_elevations_is_a_question_not_an_answer(self):
        r = self.run_reconcile(verticals=[("VIEW-03", ViewType.SECTION, [("FLOOR:1", 3300.0), ("FLOOR:1", 3900.0)], 2)])
        (s,) = [x for x in r.sets if "different elevations" in x.question]
        self.assertEqual(len(s.alternatives), 2)

    def test_two_plans_claiming_one_level_and_a_missing_floor_are_reported(self):
        r = self.run_reconcile(plans=[("VIEW-01", "GROUND", None, "G", 0.9), ("VIEW-02", "GROUND", None, "G2", 0.9),
                                      ("VIEW-03", "FLOOR:3", None, "F3", 0.9)])
        text = " ".join(i.message for i in r.issues)
        self.assertIn("claim the GROUND level", text)
        self.assertIn("none for floor(s) [1, 2]", text)
        self.assertTrue([s for s in r.sets if "duplicate_plan" in s.key])

    def test_variants_of_a_plan_are_not_rival_plans(self):
        r = self.run_reconcile(plans=[("VIEW-01", "GROUND", None, "G", 0.9), ("VIEW-02", "GROUND", "furniture", "G furn", 0.9)])
        self.assertEqual(r.sets, [])

    def test_the_sheet_list_is_checked_against_the_views_found(self):
        views = [("VIEW-01", ViewType.FLOOR_PLAN, "GROUND FLOOR PLAN", "GROUND", None, None, None)]
        r = self.run_reconcile(sheets=["GROUND FLOOR PLAN", "SECTION A-A"], views=views)
        (f,) = r.findings
        self.assertEqual(f.agreement, "disagree")
        self.assertIn("SECTION A-A", f.summary)

    def test_duplicate_section_titles_are_flagged(self):
        views = [("VIEW-01", ViewType.SECTION, "SECTION A-A", None, None, "A-A", None),
                 ("VIEW-02", ViewType.SECTION, "SECTION A-A", None, None, "A-A", None)]
        r = self.run_reconcile(views=views)
        self.assertTrue([i for i in r.issues if "same title" in i.message])

    def test_reconciliation_is_deterministic(self):
        kw = dict(heights=[("GROUND", "FLOOR:1", 3300.0, "a", "V1"), ("GROUND", "FLOOR:1", 3600.0, "b", "V2")])
        a, b = self.run_reconcile(**kw), self.run_reconcile(**kw)
        self.assertEqual([(s.question, s.alternatives) for s in a.sets], [(s.question, s.alternatives) for s in b.sets])


if __name__ == "__main__":
    unittest.main()
