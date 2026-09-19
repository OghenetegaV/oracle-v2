"""Tests for Oracle's DesignBasis

Protects:
    Required values, numeric and cover validation, per-level loading lookup, wind/seismic
    statements, missing_items() reporting, strict deserialisation and round-trip.

Test type:
    Unit tests.

Dependencies:
    oracle.core only.
"""

import unittest

from oracle.core import DesignBasis, LevelLoading, SeismicBasis, ValidationError, WindBasis
from tests.fixtures import make_design_basis

REQUIRED = dict(design_code="BS 8110-1:1997", concrete_grade="C25/30", reinforcement_grade="Y (high-yield)")


class DesignBasisTests(unittest.TestCase):
    def test_minimal_basis_is_valid_and_defaults_nothing(self):
        db = DesignBasis(**REQUIRED)
        self.assertIsNone(db.concrete_fcu_n_mm2)
        self.assertEqual(db.cover_mm, {})
        self.assertIsNone(db.wind)
        self.assertGreaterEqual(len(db.missing_items()), 8)  # everything optional is reported as open

    def test_required_values(self):
        for field in REQUIRED:
            for bad in ("", "   ", None):
                with self.assertRaises(ValidationError, msg=f"{field}={bad!r}"):
                    DesignBasis(**{**REQUIRED, field: bad})

    def test_numeric_validation(self):
        for field in ("concrete_fcu_n_mm2", "reinforcement_fy_n_mm2", "steel_fy_n_mm2",
                      "concrete_unit_weight_kn_m3", "default_storey_height_mm"):
            for bad in (0, -1, float("nan"), "25"):
                with self.assertRaises(ValidationError, msg=f"{field}={bad!r}"):
                    DesignBasis(**REQUIRED, **{field: bad})
        for bad in (0, -30, 1.5, True):
            with self.assertRaises(ValidationError):
                DesignBasis(**REQUIRED, fire_resistance_min=bad)

    def test_cover_validation(self):
        DesignBasis(**REQUIRED, cover_mm={"column": 40, "beam": 25})
        for bad in ({"column": 0}, {"column": -40}, {"bridge": 40}, {"column": 400}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                DesignBasis(**REQUIRED, cover_mm=bad)

    def test_loading_validation_and_lookup(self):
        with self.assertRaises(ValidationError):
            LevelLoading(imposed_kn_m2=-2.5)
        with self.assertRaises(ValidationError):
            DesignBasis(**REQUIRED, loadings=[LevelLoading("FF", imposed_kn_m2=2.5), LevelLoading("FF", imposed_kn_m2=3)])
        db = make_design_basis()
        self.assertEqual(db.loading_for_level("ROOF").imposed_kn_m2, 0.75)
        self.assertEqual(db.loading_for_level("FF").imposed_kn_m2, 2.5)  # falls back to the default entry
        self.assertIsNone(DesignBasis(**REQUIRED).loading_for_level("FF"))

    def test_wind_and_seismic(self):
        with self.assertRaises(ValidationError):
            WindBasis(applicable="yes")
        with self.assertRaises(ValidationError):
            WindBasis(True, basic_wind_speed_m_s=-5)
        with self.assertRaises(ValidationError):
            SeismicBasis(True, peak_ground_acceleration_g=0)
        db = DesignBasis(**REQUIRED, wind=WindBasis(False), seismic=SeismicBasis(False))
        self.assertNotIn("wind assumption (state 'not applicable' if so)", db.missing_items())

    def test_complete_basis_reports_nothing_missing(self):
        self.assertEqual(make_design_basis().missing_items(), [])

    def test_roundtrip(self):
        db = make_design_basis()
        again = DesignBasis.from_dict(db.to_dict())
        self.assertEqual(again.to_dict(), db.to_dict())
        self.assertEqual(again.wind.basic_wind_speed_m_s, 40.0)
        self.assertEqual(again.cover_mm, {"column": 40, "beam": 25, "slab": 25})

    def test_deserialisation_validates(self):
        data = make_design_basis().to_dict()
        data["cover_mm"]["column"] = -1
        with self.assertRaises(ValidationError):
            DesignBasis.from_dict(data)
        data = make_design_basis().to_dict()
        del data["design_code"]
        with self.assertRaises(ValidationError):
            DesignBasis.from_dict(data)
        data = make_design_basis().to_dict()
        data["typo_field"] = 1
        with self.assertRaises(ValidationError):
            DesignBasis.from_dict(data)

    def test_engineer_choices_are_not_restricted_to_a_fixed_list(self):
        db = DesignBasis(design_code="EN 1992-1-1:2004", concrete_grade="C30/37", reinforcement_grade="B500B")
        self.assertEqual(DesignBasis.from_dict(db.to_dict()).design_code, "EN 1992-1-1:2004")


if __name__ == "__main__":
    unittest.main()
