"""Tests for Oracle's BuildingModel

Protects:
    Validation of levels (ordering, uniqueness, elevations, storey heights), nodes, grid lines,
    ID rules, element lookup by kind and level, derived beam geometry, failed adds leaving the
    model unchanged, and validate() after in-place edits.

Test type:
    Unit tests.

Dependencies:
    oracle.core only.
"""

import unittest

from oracle.core import (
    Beam, BuildingModel, Column, ElementKind, GridLine, Level, Node, Opening, Point2D, Polygon2D, Section,
    Slab, ValidationError,
)
from tests.fixtures import make_building, make_levels
from tests.tiers import tier


@tier("unit")
class LevelTests(unittest.TestCase):
    def test_levels_ordered_by_elevation_with_indices(self):
        b = BuildingModel("B", "Bldg")
        b.add_level(Level("ROOF", "Roof", 6000))
        b.add_level(Level("GF", "Ground", 0))
        b.add_level(Level("BASE", "Basement", -3000))
        b.add_level(Level("FF", "First", 3000))
        self.assertEqual([lv.id for lv in b.levels], ["BASE", "GF", "FF", "ROOF"])
        self.assertEqual([lv.index for lv in b.levels], [0, 1, 2, 3])

    def test_duplicate_level_id_name_and_elevation_rejected(self):
        b = BuildingModel("B", "Bldg")
        b.add_level(Level("GF", "Ground", 0))
        with self.assertRaises(ValidationError):
            b.add_level(Level("GF", "Other", 3000))
        with self.assertRaises(ValidationError):
            b.add_level(Level("FF", "ground", 3000))
        with self.assertRaises(ValidationError):
            b.add_level(Level("FF", "First", 0))
        self.assertEqual(len(b.levels), 1)

    def test_invalid_elevation_and_storey_height(self):
        for bad in (float("nan"), float("inf"), "3000", None):
            with self.assertRaises(ValidationError):
                Level("GF", "Ground", bad)
        with self.assertRaises(ValidationError):
            Level("GF", "Ground", 0, storey_height_mm=0)
        with self.assertRaises(ValidationError):
            Level("GF", "Ground", 0, storey_height_mm=-3000)

    def test_storey_height_must_match_next_level(self):
        b = BuildingModel("B", "Bldg")
        b.add_level(Level("GF", "Ground", 0, 3000))
        with self.assertRaises(ValidationError):
            b.add_level(Level("FF", "First", 3500))
        b.add_level(Level("FF", "First", 3000))
        self.assertEqual(b.storey_height_mm("GF"), 3000)
        self.assertIsNone(b.storey_height_mm("FF"))

    def test_inserting_a_level_between_checks_the_lower_neighbour(self):
        b = BuildingModel("B", "Bldg")
        b.add_level(Level("GF", "Ground", 0, 6000))
        b.add_level(Level("FF", "First", 6000))
        with self.assertRaises(ValidationError):
            b.add_level(Level("MZ", "Mezzanine", 3000))  # GF declares 6000 to the next level
        self.assertEqual([lv.id for lv in b.levels], ["GF", "FF"])

    def test_bad_ids_rejected(self):
        for bad in ("", " GF", "G F", "-GF", "G/F", "x" * 65, None, 5):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                Level(bad, "Ground", 0)


@tier("unit")
class BuildingTests(unittest.TestCase):
    def test_build_and_query(self):
        b = make_building()
        self.assertEqual(len(b.elements_of(ElementKind.COLUMN)), 9)
        self.assertEqual(len(b.elements_of(ElementKind.BEAM)), 12)
        self.assertEqual(len(b.elements_of(ElementKind.SLAB)), 4)
        self.assertEqual(len(b.nodes), 9)

    def test_elements_on_level(self):
        b = make_building()
        ff = {e.id for e in b.elements_on_level("FF")}
        gf = {e.id for e in b.elements_on_level("GF")}
        self.assertEqual(len(ff), 9 + 12 + 4)  # columns end at FF, so they touch it
        self.assertEqual(gf, {f"C{i}" for i in range(1, 10)})
        self.assertEqual(b.elements_on_level("ROOF"), [])
        with self.assertRaises(ValidationError):
            b.elements_on_level("NOPE")

    def test_element_ids_are_unique_across_kinds(self):
        b = make_building()
        with self.assertRaises(ValidationError):
            b.add_element(Opening("C1", "FF", Polygon2D.rectangle(0, 0, 1000, 1000)))
        with self.assertRaises(ValidationError):
            b.add_element(Column("B1", "GF", "FF", Point2D(99000, 0), Section.rectangular(225, 225)))

    def test_duplicate_node_id_and_coincident_node_rejected(self):
        b = make_building()
        with self.assertRaises(ValidationError):
            b.add_node(Node("N1", "GF", Point2D(1, 1)))
        with self.assertRaises(ValidationError):
            b.add_node(Node("N99", "FF", Point2D(0.4, 0)))  # within 1 mm of N1 on the same level
        b.add_node(Node("N98", "GF", Point2D(0, 0)))  # same plan point on a different level is fine

    def test_node_needs_a_real_level(self):
        b = make_building()
        with self.assertRaises(ValidationError):
            b.add_node(Node("NX", "NOPE", Point2D(1, 1)))

    def test_failed_add_leaves_model_unchanged(self):
        b = make_building()
        n = len(b.elements)
        with self.assertRaises(ValidationError):
            b.add_element(Beam("BX", "FF", "N1", "MISSING", Section.rectangular(225, 450)))
        self.assertEqual(len(b.elements), n)
        self.assertFalse(b.has_element("BX"))

    def test_grid_lines(self):
        b = BuildingModel("B", "Bldg")
        b.add_grid(GridLine("A", Point2D(0, 0), Point2D(0, 10000)))
        with self.assertRaises(ValidationError):
            b.add_grid(GridLine("A", Point2D(5000, 0), Point2D(5000, 10000)))
        with self.assertRaises(ValidationError):
            GridLine("B", Point2D(0, 0), Point2D(0, 0))

    def test_derived_beam_geometry(self):
        b = make_building()
        self.assertEqual(b.member_length_mm("B1"), 5000.0)      # N1-N2 along x
        self.assertEqual(b.member_orientation_deg("B1"), 0.0)
        self.assertEqual(b.member_orientation_deg("B7"), 90.0)  # N1-N4 along y
        with self.assertRaises(ValidationError):
            b.member_length_mm("C1")

    def test_building_roundtrip(self):
        b = make_building()
        b.add_grid(GridLine("A", Point2D(0, 0), Point2D(0, 10000)))
        c = BuildingModel.from_dict(b.to_dict())
        self.assertEqual(c.to_dict(), b.to_dict())
        self.assertEqual([lv.index for lv in c.levels], [0, 1, 2])

    def test_stored_level_index_must_match_elevation(self):
        d = make_building().to_dict()
        d["levels"][1]["index"] = 7
        with self.assertRaises(ValidationError):
            BuildingModel.from_dict(d)

    def test_validate_catches_in_place_mutation(self):
        b = make_building()
        b.validate()
        b.get_element("B1").end_node_id = "N1"  # now zero-length: start == end
        with self.assertRaises(ValidationError):
            b.validate()
        b.get_element("B1").end_node_id = "N2"
        b.get_level("FF").elevation_mm = -50  # FF below GF
        with self.assertRaises(ValidationError):
            b.validate()


if __name__ == "__main__":
    unittest.main()
