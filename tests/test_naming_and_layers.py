"""Tests for level-name normalisation, title classification and layer semantic classification

Protects:
    That level names are understood from many conventions (GF, G/F PLAN, GROUND FLR, LEVEL 00, L00, L-00,
    FF, LEVEL 01, 1/F, ...) without one hard-coded office standard, that configured aliases and numbering
    offsets override the defaults, that room names and ordinary words are NOT mistaken for levels, that a
    typo is tolerated only with lower confidence, and that layer names are classified from tokens,
    discipline prefixes and qualifiers with honest confidence (WALL-FINISH-02 stays uncertain), with
    configured aliases, geometry and block-name fallbacks.

Test type:
    Unit tests of oracle.interpretation.naming and oracle.interpretation.layers.

Dependencies:
    oracle.interpretation, oracle.ingestion (DrawnEntity), oracle.core (ViewType).
"""

import unittest

from oracle.core import ViewType
from oracle.ingestion import DrawingDocument, DrawnEntity, LayerInfo
from oracle.interpretation.layers import LayerConfig, classify_layers, classify_name, tokenise
from oracle.interpretation.naming import (
    LevelNamer, classify_title, edit_distance, fuzzy_match, level_display_name, level_id_for, level_sort_key, normalise,
    parse_elevation_mm,
)
from tests.tiers import tier


@tier("unit")
class LevelNames(unittest.TestCase):
    def setUp(self):
        self.namer = LevelNamer()

    def key(self, text):
        found = self.namer.parse(text)
        return found.key if found else None

    def test_the_conventions_in_the_brief(self):
        cases = {"GF": "GROUND", "G/F PLAN": "GROUND", "GROUND FLR": "GROUND", "GROUND FLOOR PLAN": "GROUND", "LEVEL 00": "GROUND",
                 "L00": "GROUND", "L-00": "GROUND", "FF": "FLOOR:1", "LEVEL 01": "FLOOR:1", "L1": "FLOOR:1",
                 "FIRST FLOOR": "FLOOR:1", "1ST FLOOR": "FLOOR:1", "2ND FLR": "FLOOR:2", "SECOND FLOOR": "FLOOR:2",
                 "THIRD FLOOR": "FLOOR:3", "1/F": "FLOOR:1", "2/F PLAN": "FLOOR:2", "R/F": "ROOF", "FLOOR 3": "FLOOR:3",
                 "ROOF PLAN": "ROOF", "MEZZANINE": "MEZZANINE", "PENTHOUSE": "PENTHOUSE", "NGL": "DATUM",
                 "First Floor Plan": "FLOOR:1", "01- GROUND FLOOR": "GROUND"}
        for text, key in cases.items():
            self.assertEqual(self.key(text), key, text)

    def test_below_ground_is_not_read_as_above_ground(self):
        self.assertEqual(self.key("LEVEL -1"), "BASEMENT:1")
        self.assertEqual(self.key("BASEMENT"), "BASEMENT:1")
        self.assertEqual(self.key("LEVEL-1"), "FLOOR:1")           # the L-01 style: a hyphen is a separator, not a sign

    def test_ordinary_words_and_room_names_are_not_levels(self):
        for text in ("CHANGING ROOM", "STORE", "OFFICE", "TYPICAL FLOOR PLAN", "SQUASH COURT", "AVOID", "ROOMS", "B1", "DETAIL 3"):
            self.assertIsNone(self.key(text), text)

    def test_a_typo_is_tolerated_but_with_less_confidence(self):
        exact, typo = self.namer.parse("GROUND FLOOR"), self.namer.parse("GRUOND FLOOR")
        self.assertEqual(typo.key, "GROUND")
        self.assertEqual(typo.method, "fuzzy")
        self.assertLess(typo.confidence, exact.confidence)
        self.assertIsNone(self.namer.parse("ROOM"), "a four-letter word one letter from ROOF is not ROOF")

    def test_a_title_naming_two_levels_is_ambiguous_not_guessed(self):
        both = self.namer.parse_all("FIRST & SECOND FLOOR PLAN")
        self.assertEqual(sorted(l.key for l in both), ["FLOOR:1", "FLOOR:2"])
        self.assertIsNone(self.namer.parse("FIRST & SECOND FLOOR PLAN"))

    def test_configured_aliases_override_and_are_not_specific_to_one_country(self):
        namer = LevelNamer({"MAIN HALL": "GROUND", "PODIUM": "FLOOR:1", "Lower Ground": "BASEMENT:1"})
        self.assertEqual(namer.parse("MAIN HALL PLAN").key, "GROUND")
        self.assertEqual(namer.parse("Podium Level").key, "FLOOR:1")
        self.assertEqual(namer.parse("LOWER GROUND FLOOR").key, "BASEMENT:1")
        self.assertEqual(namer.parse("PODIUM").method, "alias")
        self.assertEqual(LevelNamer().parse("LOWER GROUND FLOOR").key, "GROUND", "without the alias the default reading holds")

    def test_the_numbering_convention_is_configurable(self):
        uk = LevelNamer(numeric_offset=0)
        us = LevelNamer(numeric_offset=-1)                                # level 1 is the ground floor
        self.assertEqual((uk.parse("LEVEL 1").key, us.parse("LEVEL 1").key), ("FLOOR:1", "GROUND"))
        self.assertEqual(us.parse("LEVEL 2").key, "FLOOR:1")

    def test_ids_display_names_and_order(self):
        self.assertEqual([level_id_for(k) for k in ("DATUM", "GROUND", "FLOOR:2", "BASEMENT:1", "ROOF")], ["NGL", "GF", "L2", "B1", "ROOF"])
        self.assertEqual(level_display_name("FLOOR:2"), "Second Floor")
        order = sorted(["ROOF", "FLOOR:2", "MEZZANINE", "GROUND", "BASEMENT:1", "FLOOR:1", "DATUM"], key=level_sort_key)
        self.assertEqual(order, ["DATUM", "BASEMENT:1", "GROUND", "MEZZANINE", "FLOOR:1", "FLOOR:2", "ROOF"])

    def test_text_helpers(self):
        self.assertEqual(normalise("  g/f  plan "), "G F PLAN")
        self.assertEqual(edit_distance("GRUOND", "GROUND"), 1)
        self.assertTrue(fuzzy_match("GRUOND", "GROUND"))
        self.assertFalse(fuzzy_match("ROOM", "ROOF"))


@tier("unit")
class ElevationNumbers(unittest.TestCase):
    def test_written_elevations(self):
        for text, expected in (("3300", 3300.0), ("3.3", 3300.0), ("+3.300", 3300.0), ("3300 mm", 3300.0), ("3.3m", 3300.0),
                               ("0", 0.0), ("+0.000", 0.0), ("300", 300.0)):
            self.assertEqual(parse_elevation_mm(text), expected, text)

    def test_text_that_is_not_an_elevation_is_refused(self):
        for text in ("FFL +3300", "GROUND", "A-A", "%%p", "", "3300 x 2100"):
            self.assertIsNone(parse_elevation_mm(text), text)


@tier("unit")
class Titles(unittest.TestCase):
    def test_what_a_title_announces(self):
        cases = {"SECTION A-A": ViewType.SECTION, "FRONT ELEVATION": ViewType.ELEVATION, "GROUND FLOOR PLAN": ViewType.FLOOR_PLAN,
                 "SCHEDULE OF DOORS": ViewType.SCHEDULE, "NOTES": ViewType.NOTES, "DETAIL 1": ViewType.DETAIL,
                 "LEFT SIDE ELEVATION": ViewType.ELEVATION, "ROOF PLAN": ViewType.FLOOR_PLAN}
        for text, expected in cases.items():
            self.assertEqual(classify_title(text).view_type, expected, text)

    def test_section_labels_and_elevation_orientation(self):
        self.assertEqual(classify_title("SECTION B-B").section_label, "B-B")
        self.assertEqual(classify_title("REAR ELEVATION").orientation, "rear")
        self.assertEqual(classify_title("NORTH ELEVATION").orientation, "north")

    def test_variants_are_not_rival_plans(self):
        self.assertEqual(classify_title("CHANGING ROOM BLOWUP").variant, "blowup")
        self.assertEqual(classify_title("FIRST FLOOR FURNITURE PLAN").variant, "furniture")

    def test_ordinary_text_is_not_a_title(self):
        for text in ("OFFICE", "3300", "SEE DRAWING 12", ""):
            self.assertIsNone(classify_title(text).view_type, text)


@tier("unit")
class LayerNames(unittest.TestCase):
    def test_tokens_split_on_any_separator(self):
        self.assertEqual(tokenise("A-WALL-EXT"), ["A", "WALL", "EXT"])
        self.assertEqual(tokenise("a_wall_ext"), ["A", "WALL", "EXT"])
        self.assertEqual(tokenise("ExternalWalls"), ["EXTERNAL", "WALLS"])

    def test_the_brief_examples(self):
        ext = classify_name("A-WALL-EXT")
        self.assertEqual(ext.semantic_class, "wall_external")
        self.assertGreaterEqual(ext.confidence, 0.95)
        finish = classify_name("WALL-FINISH-02")
        self.assertEqual(finish.semantic_class, "unknown_wall_related")
        self.assertAlmostEqual(finish.confidence, 0.52, delta=0.05)
        self.assertLess(finish.confidence, 0.6, "a finish layer must not pass for a structural wall")

    def test_naming_conventions_beyond_one_standard(self):
        cases = {"A-WALL-INT": "wall_internal", "WALLS": "wall", "Walls-Ext": "wall_external", "external walls": "wall_external",
                 "A_WALLS_EXTERNAL": "wall_external", "A-DOOR": "door", "DOORS": "door", "A-GLAZ": "window", "GLAZING": "window",
                 "A-COLS": "column", "STRUCTURAL COLUMNS": "column", "S-BEAM": "beam", "A-STRS": "stair", "LIFT": "lift",
                 "A-FLOR-LEVL": "level_marker", "A-GRID-IDEN": "grid_label", "GRID": "grid", "AXIS": "grid", "A-ANNO-DIMS": "dimension",
                 "A-ROOF": "roof", "G-ANNO-SCHD": "schedule", "A-WALL-PATT": "wall_pattern", "A-WALL-HIDD": "wall_hidden",
                 "TITLEBLOCK": "title_block"}
        for name, expected in cases.items():
            self.assertEqual(classify_name(name).semantic_class, expected, name)

    def test_unrecognisable_names_are_unknown_with_zero_confidence(self):
        for name in ("Q-SPCQ", "XZ_101", "0", "A-WLL", "VPORT"):
            v = classify_name(name)
            self.assertEqual((v.semantic_class, v.confidence), ("unknown", 0.0), name)

    def test_a_discipline_prefix_that_disagrees_with_the_word_lowers_confidence(self):
        v = classify_name("S-STRS")
        self.assertEqual(v.semantic_class, "stair")
        self.assertLess(v.confidence, classify_name("A-STRS").confidence)
        self.assertIn("structure", v.note)

    def test_configured_aliases_win_and_ignore_case_and_punctuation(self):
        config = LayerConfig({"XZ_101": "wall_external", "my special layer": "door"})
        self.assertEqual(classify_name("xz-101", config).semantic_class, "wall_external")
        self.assertEqual(classify_name("xz-101", config).method, "configured_alias")
        self.assertEqual(classify_name("MY SPECIAL LAYER", config).semantic_class, "door")
        self.assertEqual(classify_name("XZ_102", config).semantic_class, "unknown")


def entity(i, layer, kind="LINE", **kw):
    box = kw.pop("box", (0.0, 0.0, 100.0, 0.0))
    return DrawnEntity(f"E{i}", kind, layer, box, **kw)


@tier("unit")
class LayerGeometryFallbacks(unittest.TestCase):
    def doc(self, entities, layers):
        return DrawingDocument("x.dxf", "0" * 64, "AC1032", 4, None, layers={n: LayerInfo(n) for n in layers}, entities=entities,
                               layouts=("Model",))

    def test_a_layer_of_only_text_is_a_text_layer_and_a_layer_of_door_blocks_is_a_weak_door_layer(self):
        entities = [entity(i, "XZ_1", "TEXT", text="hello", height=100.0) for i in range(8)]
        entities += [entity(20 + i, "XZ_2", "INSERT", block="DOOR-900", box=(0, 0, 900, 900)) for i in range(4)]
        verdicts = {v.name: v for v in classify_layers(self.doc(entities, ["XZ_1", "XZ_2"]))}
        self.assertEqual(verdicts["XZ_1"].semantic_class, "text_annotation")
        self.assertEqual(verdicts["XZ_1"].method, "geometry")
        self.assertEqual(verdicts["XZ_2"].semantic_class, "door")
        self.assertEqual(verdicts["XZ_2"].method, "block_name")
        self.assertLess(verdicts["XZ_2"].confidence, 0.8)

    def test_counts_are_reported_and_empty_layers_are_kept(self):
        entities = [entity(i, "A-WALL-EXT") for i in range(5)]
        verdicts = {v.name: v for v in classify_layers(self.doc(entities, ["A-WALL-EXT", "A-DOOR"]))}
        self.assertEqual(verdicts["A-WALL-EXT"].entity_count, 5)
        self.assertEqual(verdicts["A-DOOR"].entity_count, 0)


if __name__ == "__main__":
    unittest.main()
