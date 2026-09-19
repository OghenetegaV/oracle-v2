"""Tests for Oracle: Shared Builders

Protects:
    Provides make_building(), make_design_basis() and make_project(), a 3x3-column, two-storey
    layout mirroring the legacy ga_output.json sample (9 columns, 12 beams, 4 slabs), so tests
    share one realistic model.

Test type:
    Test fixture builders (not tests themselves).

Dependencies:
    oracle.core only.

Details (original module notes, retained):
    Shared builders. make_building() mirrors the legacy output_json/ga_output.json layout:
    a 3x3 column grid on 5 m bays (9 columns, 12 beams, 4 slabs) -- here over ground/first/roof.
"""

from oracle.core import (
    Beam, BuildingModel, Column, DesignBasis, Level, LevelLoading, Node, OracleProject, Point2D,
    Polygon2D, Section, Slab, WindBasis, SeismicBasis,
)

GRID_MM = 5000.0


def make_levels(building):
    building.add_level(Level("GF", "Ground Floor", 0.0, 3000.0))
    building.add_level(Level("FF", "First Floor", 3000.0, 3000.0))
    building.add_level(Level("ROOF", "Roof", 6000.0))


def make_building():
    b = BuildingModel("B1", "Test Building")
    make_levels(b)
    for r in range(3):
        for c in range(3):
            n = r * 3 + c + 1
            b.add_element(Column(f"C{n}", "GF", "FF", Point2D(c * GRID_MM, r * GRID_MM),
                                 Section.rectangular(225, 225), material="C25/30"))
            b.add_node(Node(f"N{n}", "FF", Point2D(c * GRID_MM, r * GRID_MM)))
    beam_no = 0
    for a, z in [(1, 2), (2, 3), (4, 5), (5, 6), (7, 8), (8, 9), (1, 4), (4, 7), (2, 5), (5, 8), (3, 6), (6, 9)]:
        beam_no += 1
        b.add_element(Beam(f"B{beam_no}", "FF", f"N{a}", f"N{z}", Section.rectangular(225, 450), material="C25/30"))
    for i, (x0, y0) in enumerate([(0, 0), (5000, 0), (0, 5000), (5000, 5000)], start=1):
        b.add_element(Slab(f"S{i}", "FF", Polygon2D.rectangle(x0, y0, x0 + 5000, y0 + 5000), 175))
    return b


def make_design_basis():
    return DesignBasis(
        design_code="BS 8110-1:1997", concrete_grade="C25/30", reinforcement_grade="Y (high-yield)",
        concrete_fcu_n_mm2=25, reinforcement_fy_n_mm2=410, concrete_unit_weight_kn_m3=24,
        cover_mm={"column": 40, "beam": 25, "slab": 25}, exposure_class="Mild", fire_resistance_min=60,
        loadings=[LevelLoading(None, "Offices (general use)", 2.5, 1.5), LevelLoading("ROOF", "Roof - no access", 0.75, 1.5)],
        wind=WindBasis(True, 40.0, "Suburban"), seismic=SeismicBasis(False), default_storey_height_mm=3000,
        assumptions=["Ground bearing capacity to be confirmed by soil investigation."],
    )


def make_project():
    p = OracleProject.create("Lekki Office Block", "A. Engineer", client="Client Ltd", location="Lagos",
                             description="Test project")
    p.set_design_basis(make_design_basis())
    p.set_building(make_building())
    return p
