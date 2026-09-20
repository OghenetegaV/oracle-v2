"""Tests for Oracle's structural elements and geometry

Protects:
    Section and Polygon2D validity rules and the per-element relationship checks for columns,
    beams, slabs, walls, stairs, foundations and openings, plus per-element dict round-trips.

Test type:
    Unit tests.

Dependencies:
    oracle.core only.
"""

import unittest

from oracle.core import (
    Beam, Column, Foundation, FoundationType, Node, Opening, Point2D, Polygon2D, Section, Slab, Stair, Wall,
    ValidationError,
)
from tests.fixtures import make_building
from tests.tiers import tier


@tier("unit")
class SectionAndGeometryTests(unittest.TestCase):
    def test_sections(self):
        self.assertEqual(Section.rectangular(225, 450).to_dict(),
                         {"shape": "rectangular", "width_mm": 225, "depth_mm": 450})
        self.assertEqual(Section.circular(400).diameter_mm, 400)
        self.assertEqual(Section.standard("UB 305x165x40").designation, "UB 305x165x40")
        for bad in (lambda: Section.rectangular(0, 450), lambda: Section.rectangular(225, -1),
                    lambda: Section.circular(float("nan")), lambda: Section.standard(" "),
                    lambda: Section("rectangular", width_mm=225, depth_mm=450, diameter_mm=300),
                    lambda: Section("triangular")):
            with self.assertRaises(ValidationError):
                bad()

    def test_polygon_validity(self):
        square = Polygon2D.rectangle(0, 0, 5000, 5000)
        self.assertEqual(square.area_mm2, 25_000_000)
        cw = Polygon2D((Point2D(0, 0), Point2D(0, 1000), Point2D(1000, 1000), Point2D(1000, 0)))
        self.assertEqual(cw.area_mm2, 1_000_000)  # winding direction does not matter
        with self.assertRaises(ValidationError):
            Polygon2D((Point2D(0, 0), Point2D(1000, 0)))  # too few
        with self.assertRaises(ValidationError):
            Polygon2D((Point2D(0, 0), Point2D(1000, 0), Point2D(2000, 0)))  # collinear: zero area
        with self.assertRaises(ValidationError):  # bow-tie
            Polygon2D((Point2D(0, 0), Point2D(1000, 1000), Point2D(1000, 0), Point2D(0, 1000)))
        with self.assertRaises(ValidationError):  # repeated closing vertex
            Polygon2D((Point2D(0, 0), Point2D(1000, 0), Point2D(1000, 1000), Point2D(0, 0)))

    def test_point_must_be_finite(self):
        with self.assertRaises(ValidationError):
            Point2D(float("inf"), 0)


@tier("unit")
class ElementTests(unittest.TestCase):
    def setUp(self):
        self.b = make_building()

    def test_create_column(self):
        c = self.b.add_element(Column("C10", "GF", "FF", Point2D(20000, 0), Section.rectangular(300, 300),
                                      material="C30/37", orientation_deg=45))
        self.assertEqual(c.level_ids, ("GF", "FF"))
        self.assertEqual(self.b.get_element("C10").orientation_deg, 45)

    def test_column_level_relationships(self):
        with self.assertRaises(ValidationError):  # upper below lower
            self.b.add_element(Column("CX", "FF", "GF", Point2D(1, 1), Section.rectangular(225, 225)))
        with self.assertRaises(ValidationError):  # same level
            Column("CX", "GF", "GF", Point2D(1, 1), Section.rectangular(225, 225))
        with self.assertRaises(ValidationError):  # unknown level
            self.b.add_element(Column("CX", "GF", "L99", Point2D(1, 1), Section.rectangular(225, 225)))

    def test_column_may_span_multiple_storeys(self):
        self.b.add_element(Column("CT", "GF", "ROOF", Point2D(30000, 0), Section.rectangular(300, 300)))

    def test_create_beam(self):
        self.b.add_node(Node_("N50", "FF", 20000, 0))
        self.b.add_node(Node_("N51", "FF", 25000, 0))
        beam = self.b.add_element(Beam("B50", "FF", "N50", "N51", Section.rectangular(225, 450)))
        self.assertEqual(self.b.member_length_mm("B50"), 5000)
        self.assertEqual(beam.level_ids, ("FF",))

    def test_beam_relationships(self):
        with self.assertRaises(ValidationError):  # start == end
            Beam("BX", "FF", "N1", "N1", Section.rectangular(225, 450))
        with self.assertRaises(ValidationError):  # unknown node
            self.b.add_element(Beam("BX", "FF", "N1", "N404", Section.rectangular(225, 450)))
        with self.assertRaises(ValidationError):  # unknown level
            self.b.add_element(Beam("BX", "L99", "N1", "N2", Section.rectangular(225, 450)))
        self.b.add_node(Node_("NR", "ROOF", 0, 0))
        with self.assertRaises(ValidationError):  # node on a different level to the beam
            self.b.add_element(Beam("BX", "FF", "N1", "NR", Section.rectangular(225, 450)))

    def test_create_slab(self):
        s = self.b.add_element(Slab("S9", "ROOF", Polygon2D.rectangle(0, 0, 5000, 5000), 150,
                                    supported_by=["B1", "B3"]))
        self.assertEqual(s.thickness_mm, 150)
        self.assertEqual(s.supported_by, ["B1", "B3"])

    def test_slab_with_opening(self):
        self.b.add_element(Opening("O1", "FF", Polygon2D.rectangle(1000, 1000, 2000, 2000), "lift shaft"))
        s = self.b.add_element(Slab("S9", "FF", Polygon2D.rectangle(0, 0, 5000, 5000), 175, opening_ids=["O1"]))
        self.assertEqual(s.opening_ids, ["O1"])

    def test_slab_relationships(self):
        square = Polygon2D.rectangle(0, 0, 5000, 5000)
        with self.assertRaises(ValidationError):
            Slab("SX", "FF", square, 0)
        with self.assertRaises(ValidationError):
            Slab("SX", "FF", square, -175)
        with self.assertRaises(ValidationError):  # support that does not exist
            self.b.add_element(Slab("SX", "FF", square, 175, supported_by=["B404"]))
        with self.assertRaises(ValidationError):  # a slab cannot be supported by a slab
            self.b.add_element(Slab("SX", "FF", square, 175, supported_by=["S1"]))
        with self.assertRaises(ValidationError):  # duplicate support reference
            Slab("SX", "FF", square, 175, supported_by=["B1", "B1"])
        self.b.add_element(Opening("OR", "ROOF", Polygon2D.rectangle(1000, 1000, 2000, 2000)))
        with self.assertRaises(ValidationError):  # opening on a different level
            self.b.add_element(Slab("SX", "FF", square, 175, opening_ids=["OR"]))
        with self.assertRaises(ValidationError):  # not an opening
            self.b.add_element(Slab("SX", "FF", square, 175, opening_ids=["B1"]))

    def test_wall_stair_foundation_opening(self):
        w = self.b.add_element(Wall("W1", "FF", "N1", "N2", 225, load_bearing=True))
        self.assertTrue(w.load_bearing)
        self.b.add_element(Stair("ST1", "GF", "FF", Polygon2D.rectangle(0, 0, 1200, 3000), 150))
        with self.assertRaises(ValidationError):
            self.b.add_element(Stair("ST2", "FF", "GF", Polygon2D.rectangle(0, 0, 1200, 3000)))
        f = self.b.add_element(Foundation("F1", "GF", FoundationType.PAD, Polygon2D.rectangle(-750, -750, 750, 750),
                                          600, supported_column_ids=["C1"]))
        self.assertEqual(f.foundation_type, FoundationType.PAD)
        with self.assertRaises(ValidationError):  # a beam is not a column
            self.b.add_element(Foundation("F2", "GF", "pad", Polygon2D.rectangle(0, 0, 1, 1000), 600,
                                          supported_column_ids=["B1"]))
        with self.assertRaises(ValidationError):
            Foundation("F3", "GF", "floating", Polygon2D.rectangle(0, 0, 1000, 1000))

    def test_every_kind_roundtrips_through_dict(self):
        self.b.add_element(Opening("O1", "FF", Polygon2D.rectangle(1000, 1000, 2000, 2000), "riser"))
        self.b.add_element(Wall("W1", "FF", "N1", "N2", 225))
        self.b.add_element(Stair("ST1", "GF", "FF", Polygon2D.rectangle(0, 0, 1200, 3000), 150))
        self.b.add_element(Foundation("F1", "GF", "raft", Polygon2D.rectangle(-750, -750, 750, 750), 600,
                                      supported_column_ids=["C1"]))
        for e in self.b.elements:
            self.assertEqual(type(e).from_dict(e.to_dict()).to_dict(), e.to_dict())


def Node_(node_id, level_id, x, y):
    return Node(node_id, level_id, Point2D(x, y))


if __name__ == "__main__":
    unittest.main()
