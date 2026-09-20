"""Tests for the CAD boundary, observation vocabulary, multi-source readiness and schema 0.4.0 (Phase 3.5)

Protects:
    That oracle.core contains no CAD library, format or vendor vocabulary in its code (imports, names, strings, comments),
    with one deliberate, tested exception: the migration that carries an old project's format-specific keys across; that
    the interpretation layer keeps format codes and vendor layer conventions at the ingestion boundary; that
    architectural observations only DESCRIBE what a drawing shows (a closed vocabulary with stated meanings, no
    structural claim, hints as separate proposals, engineer decision as a third thing); that several sources, revisions
    and interpretation instances can live in one project without colliding and without carrying decisions across; and that
    the 0.3.0 -> 0.4.0 migration keeps everything, invents nothing, refuses what it cannot understand, and that malformed
    or forged data is rejected on load.

Test type:
    Static (AST and token) architecture tests, unit tests and integration tests.

Dependencies:
    oracle.core, oracle.ingestion, oracle.interpretation, tests.support35, tests.drawing_factory.
"""

import ast
import copy
import io
import json
import re
import tokenize
import unittest
from pathlib import Path

from oracle.core import (
    ArchitecturalObservation, DrawingSource, HintKind, OracleProject, SCHEMA_VERSION, SchemaVersionError, Target,
    UnitEstimate, ValidationError, ValueStatus, ViewType,
)
from oracle.core.migrations import MIGRATIONS, migrate
from oracle.ingestion import DrawingDocument, LayerInfo
from oracle.interpretation import interpret_document, interpret_into
from oracle.interpretation.layers import classify_layers
from oracle.interpretation.observations import OBSERVATION_VOCABULARY
from tests.drawing_factory import PLAIN, Sheet
from tests.support35 import decision, drawing_sheet, interpreted, plans, reviewed
from tests.tiers import tier

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
CAD_WORDS = re.compile(r"\b(ezdxf|autocad|dxf|dwg|insunits|defpoints|unit_code|format_version|layouts|paperspace|paper_space|acad|revit)\b", re.I)


def _docstring_ids(tree):
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def cad_words_in_code(path: Path) -> list:
    """CAD vocabulary in code and comments (module prose in docstrings is documentation, and is allowed to name the boundary)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    skip = _docstring_ids(tree)
    hits = []
    for n in ast.walk(tree):
        values = []
        if isinstance(n, ast.Name):
            values = [n.id]
        elif isinstance(n, ast.Attribute):
            values = [n.attr]
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            values = [n.name]
        elif isinstance(n, ast.arg):
            values = [n.arg]
        elif isinstance(n, ast.keyword) and n.arg:
            values = [n.arg]
        elif isinstance(n, ast.alias):
            values = [n.name]
        elif isinstance(n, ast.ImportFrom):
            values = [n.module or ""]
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            values = [n.value]
        hits += [(getattr(n, "lineno", 0), v) for v in values if CAD_WORDS.search(v)]
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT and CAD_WORDS.search(tok.string):
            hits.append((tok.start[0], tok.string))
    return hits


@tier("unit")
class CoreHasNoCadVocabulary(unittest.TestCase):
    def test_no_module_of_oracle_core_names_a_cad_library_format_or_vendor(self):
        offenders = {}
        for path in sorted((REPO_ROOT / "oracle" / "core").glob("*.py")):
            if path.name == "migrations.py":
                continue                       # the one exception, pinned down by the next test
            hits = cad_words_in_code(path)
            if hits:
                offenders[path.name] = hits[:3]
        self.assertEqual(offenders, {})

    def test_the_migration_is_the_only_place_the_old_format_specific_keys_are_named(self):
        hits = cad_words_in_code(REPO_ROOT / "oracle" / "core" / "migrations.py")
        self.assertTrue(hits, "the migration must name the keys it carries across")
        source = (REPO_ROOT / "oracle" / "core" / "migrations.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function_lines = {n.name: (n.lineno, n.end_lineno) for n in tree.body if isinstance(n, ast.FunctionDef)}
        allowed = {name: span for name, span in function_lines.items() if name in ("_0_3_0_to_0_4_0", "_rename_source_ids")}
        module_level_ok = {n.lineno for n in tree.body if isinstance(n, ast.Assign) and any(
            getattr(t, "id", "") in ("_SOURCE_ID",) for t in n.targets)}
        for line, text in hits:
            self.assertTrue(line in module_level_ok or any(a <= line <= b for a, b in allowed.values()), f"line {line}: {text!r}")

    def test_no_core_module_imports_a_cad_ai_geometry_or_plotting_library(self):
        banned = {"ezdxf", "shapely", "matplotlib", "anthropic", "numpy", "tkinter", "win32com", "requests"}
        for path in (REPO_ROOT / "oracle" / "core").glob("*.py"):
            for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else []
                for name in names:
                    self.assertNotIn(name.split(".")[0], banned, f"{path.name} imports {name}")
                    self.assertFalse(name.startswith(("oracle.ingestion", "oracle.interpretation", "oracle.adapters")), f"{path.name} imports {name}")

    def test_the_drawing_source_holds_generic_metadata_only(self):
        fields = set(DrawingSource.__dataclass_fields__)
        self.assertTrue({"declared_unit", "source_metadata", "revision", "interpretation_id", "sha256", "file"} <= fields)
        self.assertFalse(fields & {"unit_code", "format_version", "layouts"})
        source = DrawingSource("SRC-1", "x.any", "a" * 64, UnitEstimate("mm", 1.0, 0.9, "m"), declared_unit="mm",
                               source_metadata={"anything": "goes", "codes": [1, 2], "flag": True})
        self.assertEqual(DrawingSource.from_dict(source.to_dict()), source)
        for bad in ({"nested": {"a": 1}}, {"obj": object()}, {5: "x"}):
            with self.assertRaises((ValidationError, TypeError), msg=str(bad)):
                DrawingSource("SRC-1", "x", "a" * 64, UnitEstimate("mm", 1.0, 0.9, "m"), source_metadata=bad)
        with self.assertRaises(ValidationError):
            DrawingSource("SRC-1", "x", "a" * 64, UnitEstimate("mm", 1.0, 0.9, "m"), declared_unit="furlong")
        with self.assertRaises(ValidationError):
            DrawingSource("DWG-1", "x", "a" * 64, UnitEstimate("mm", 1.0, 0.9, "m"))


@tier("integration")
class TranslationHappensAtTheIngestionBoundary(unittest.TestCase):
    def test_interpretation_modules_hold_no_format_codes_or_vendor_layer_names(self):
        offenders = {}
        for path in sorted((REPO_ROOT / "oracle" / "interpretation").glob("*.py")):
            hits = [h for h in cad_words_in_code(path) if not re.search(r"\b(dxf|dwg)\b", h[1], re.I) or path.name in ("__main__.py",)]
            hits = [h for h in hits if re.search(r"insunits|defpoints|unit_code|autocad|ezdxf|acad", h[1], re.I)]
            if hits:
                offenders[path.name] = hits[:3]
        self.assertEqual(offenders, {})

    def test_only_the_reader_imports_the_cad_library(self):
        importers = []
        for path in (REPO_ROOT / "oracle").rglob("*.py"):
            for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else []
                if any(x.split(".")[0] == "ezdxf" for x in names):
                    importers.append(path.relative_to(REPO_ROOT).as_posix())
        self.assertEqual(sorted(set(importers)), ["oracle/ingestion/dxf_reader.py"])

    def test_the_declared_unit_is_translated_by_the_reader_not_by_the_interpreter(self):
        doc = Sheet(units="inch").document()
        self.assertEqual((doc.unit_code, doc.declared_unit), (1, "inch"))
        self.assertEqual(Sheet(units="unitless").document().declared_unit, None)
        self.assertEqual(doc.source_metadata()["unit_code"], 1)
        project = interpret_document(doc, project_name="I", engineer="E")
        drawing = project.architecture.drawing
        self.assertEqual((drawing.declared_unit, drawing.source_metadata["unit_code"]), ("inch", 1))

    def test_a_layer_the_source_marks_as_not_plotting_is_recognised_without_any_vendor_name_in_the_interpreter(self):
        doc = Sheet().document()
        self.assertTrue(doc.layers["Defpoints"].non_plotting)
        verdicts = {v.name: v for v in classify_layers(doc)}
        self.assertEqual((verdicts["Defpoints"].semantic_class, verdicts["Defpoints"].method), ("non_plotting", "source_flag"))
        renamed = DrawingDocument("x", "0" * 64, layers={"HELPER": LayerInfo("HELPER", non_plotting=True), "WALLS": LayerInfo("WALLS")})
        self.assertEqual({v.name: v.semantic_class for v in classify_layers(renamed)}, {"HELPER": "non_plotting", "WALLS": "wall"})

    def test_a_document_cached_before_the_flag_existed_still_loads(self):
        old = Sheet().document().to_dict()
        old["layers"] = [row[:4] for row in old["layers"]]
        again = DrawingDocument.from_dict(old)
        self.assertFalse(any(l.non_plotting for l in again.layers.values()))


@tier("integration")
class ObservationsDescribeAndNeverAssert(unittest.TestCase):
    BANNED_WORDS = {"retained", "retain", "existing", "structural", "load", "bearing", "shear", "demolish", "demolished", "support",
                    "transfer", "frame", "primary", "secondary", "new", "proposed", "design", "carrying"}

    def kinds_produced(self):
        out = set()
        for scheme in (None, PLAIN):
            s = Sheet(layers=scheme) if scheme else Sheet()
            s.plan((0, 0), "GROUND FLOOR PLAN", labels=("STAIR", "LIFT", "VOID", "DOUBLE HEIGHT SPACE"))
            s.section((0, -16000), "SECTION A-A", partial=(("MEZZANINE", 1700, 0, 3000),))
            s.elevation((30000, -16000), "FRONT ELEVATION")
            p = interpret_document(s.document(), project_name="V", engineer="E")
            out |= {o.kind for o in p.architecture.observations}
        return out

    def test_every_produced_kind_is_in_the_vocabulary_with_a_stated_meaning(self):
        produced = self.kinds_produced()
        self.assertTrue(produced)
        self.assertEqual(produced - set(OBSERVATION_VOCABULARY), set())
        for kind, meaning in OBSERVATION_VOCABULARY.items():
            self.assertTrue(meaning.strip(), kind)

    def test_no_kind_asserts_a_structural_meaning(self):
        for kind in OBSERVATION_VOCABULARY:
            self.assertEqual(set(kind.split("_")) & self.BANNED_WORDS, set(), f"{kind!r} reads as a structural claim")

    def test_the_previously_leaking_names_are_gone(self):
        for old in ("existing_column", "beam_shown"):
            self.assertNotIn(old, OBSERVATION_VOCABULARY)
        self.assertIn("column_symbol", OBSERVATION_VOCABULARY)
        self.assertEqual({h.value for h in HintKind if "column" in h.value or "beam" in h.value}, {"column_candidate", "beam_candidate"})

    def test_a_column_symbol_is_a_symbol_a_hint_is_a_candidate_and_neither_is_a_decision(self):
        p = interpreted()
        symbols = [o for o in p.architecture.observations if o.kind == "column_symbol"]
        self.assertTrue(symbols)
        for o in symbols:
            self.assertEqual(o.hint, HintKind.COLUMN_CANDIDATE)
            self.assertIn(o.basis, (ValueStatus.SOURCE, ValueStatus.INFERRED))
            self.assertEqual(o.review.value, "proposed")
            self.assertIsNone(p.value_status_of(Target.architectural(o.id), "hint"), "no one has approved the reading")
        self.assertIsNone(p.building)
        self.assertEqual(p.evidence_links, [])

    def test_the_observation_class_still_cannot_hold_an_engineers_value(self):
        for bad in (ValueStatus.ENGINEER_DEFINED, ValueStatus.ENGINEER_OVERRIDE):
            with self.assertRaises(ValidationError):
                ArchitecturalObservation("OBS-00001", "column_symbol", "VIEW-01", bad, 0.5)

    def test_layer_classes_that_sound_structural_are_still_only_layer_classes(self):
        p = interpret_document(_plain_plan(), project_name="L", engineer="E")
        column_layer = next(l for l in p.architecture.layers if l.name == "COLUMNS")
        self.assertEqual((column_layer.semantic_class, column_layer.method), ("column", "token_match"))
        self.assertEqual({o.kind for o in p.architecture.observations if o.layer == "COLUMNS"}, {"column_symbol"})


def _plain_plan():
    s = Sheet(layers=PLAIN)
    s.plan((0, 0), "GROUND FLOOR PLAN")
    return s.document()


@tier("integration")
class MultipleSources(unittest.TestCase):
    def two_sources(self):
        p = interpreted()
        reviewed(p)
        interpret_into(p, drawing_sheet().document(), revision="rev B")
        return p

    def test_sources_are_told_apart_by_id_revision_hash_and_instance(self):
        s = Sheet()
        s.plan((0, 0), "GROUND FLOOR PLAN")
        p = interpret_document(s.document(), project_name="Two", engineer="E", revision="A")
        s2 = Sheet()
        s2.plan((0, 0), "GROUND FLOOR PLAN")
        s2.plan((20000, 0), "FIRST FLOOR PLAN")
        d2 = s2.document()
        d2.sha256 = "b" * 64
        interpret_into(p, d2, revision="B")
        (a, b) = [x.drawing for x in p.architectures]
        self.assertEqual((a.id, b.id, a.interpretation_id, b.interpretation_id, a.revision, b.revision),
                         ("SRC-1", "SRC-2", "AINT-1", "AINT-2", "A", "B"))
        self.assertNotEqual(a.sha256, b.sha256)

    def test_object_ids_never_collide_across_sources(self):
        p = self.two_sources()
        first, second = p.architectures
        self.assertEqual(set(first._all) & set(second._all), set())
        issue_ids = [i.id for i in p.issues]
        self.assertEqual(len(issue_ids), len(set(issue_ids)))
        set_ids = [s.id for s in p.interpretation_sets]
        self.assertEqual(len(set_ids), len(set(set_ids)))
        p.validate()

    def test_adding_a_source_changes_nothing_that_already_exists_and_carries_no_decision_over(self):
        p = interpreted()
        reviewed(p)
        before = copy.deepcopy(p.architecture.to_dict())
        decisions_before = [d.id for d in p.decisions]
        views_before = {v.id: v.review for v in p.architecture.views}
        interpret_into(p, drawing_sheet().document(), revision="B")
        self.assertEqual(p.architecture_of("SRC-1").to_dict(), before)
        self.assertEqual([d.id for d in p.decisions], decisions_before, "no decision is carried forward to the new revision")
        second = p.architecture_of("SRC-2")
        self.assertEqual({v.review.value for v in second.views}, {"proposed"}, "the new source starts unreviewed")
        self.assertEqual({v.id: v.review for v in p.architecture_of("SRC-1").views}, views_before)

    def test_readiness_counts_unreviewed_views_of_every_source(self):
        p = self.two_sources()
        unreviewed = [b.reference for b in p.readiness().blockers if b.kind.value == "unreviewed_view"]
        second = {v.id for v in p.architecture_of("SRC-2").views}
        self.assertTrue(unreviewed)
        self.assertTrue(set(unreviewed) <= second and len(unreviewed) == 4)
        self.assertFalse(p.readiness().ready)

    def test_each_source_is_traced_and_projected_on_its_own(self):
        p = self.two_sources()
        second = p.architecture_of("SRC-2")
        obs = second.observations[0]
        self.assertEqual(p.trace(obs.id).sources[0]["id"], "SRC-2")
        self.assertEqual(p.trace(p.architecture_of("SRC-1").observations[0].id).sources[0]["id"], "SRC-1")
        self.assertEqual(p.approved_architecture("SRC-1").source_id, "SRC-1")
        self.assertEqual(p.approved_architecture("SRC-2").views, (), "nothing of the new revision is approved yet")

    def test_the_same_file_interpreted_twice_is_two_interpretations_of_one_source_identity_of_bytes(self):
        p = interpreted()
        interpret_into(p, drawing_sheet().document())
        a, b = (x.drawing for x in p.architectures)
        self.assertEqual(a.sha256, b.sha256)
        self.assertNotEqual((a.id, a.interpretation_id), (b.id, b.interpretation_id))

    def test_a_duplicate_source_or_instance_or_colliding_id_is_refused(self):
        p = interpreted()
        with self.assertRaises(ValidationError):
            p.add_architecture(p.architecture)
        other = interpret_document(drawing_sheet().document(), project_name="O", engineer="E").architecture
        with self.assertRaises(ValidationError):                       # SRC-1 again
            p.add_architecture(other)

    def test_a_multi_source_project_round_trips(self):
        p = self.two_sources()
        text = p.to_json()
        again = OracleProject.from_json(text)
        self.assertEqual(again.to_json(), text)
        self.assertEqual([a.drawing.id for a in again.architectures], ["SRC-1", "SRC-2"])

    def test_engineer_actions_target_the_right_source(self):
        p = self.two_sources()
        second_plan = plans(p, "SRC-2")[0]
        t = Target.architectural(second_plan.id)
        p.set_value(t, "title", "REV B GROUND", decision(t, field="title", value="REV B GROUND"))
        self.assertEqual(p.architecture_of("SRC-2").get(second_plan.id).title, "REV B GROUND")
        self.assertNotEqual(p.architecture_of("SRC-1").views_of(ViewType.FLOOR_PLAN)[0].title, "REV B GROUND")


@tier("unit")
class MigrationFrom030(unittest.TestCase):
    def data(self):
        return json.loads((FIXTURES / "schema_0_3_0_project.json").read_text(encoding="utf-8"))

    def test_the_fixture_is_a_genuine_0_3_0_file(self):
        d = self.data()
        self.assertEqual(d["schema_version"], "0.3.0")
        self.assertIn("architecture", d)
        self.assertNotIn("architectures", d)
        self.assertEqual(SCHEMA_VERSION, "0.4.0")

    def test_it_loads_and_keeps_everything(self):
        old = self.data()
        p = OracleProject.from_dict(old)
        self.assertEqual(p.schema_version, "0.4.0")
        arch = p.architecture
        a0 = old["architecture"]
        self.assertEqual([len(p.architectures)], [1])
        for key in ("views", "observations", "frames", "layers", "heights"):
            self.assertEqual(len(getattr(arch, key)), len(a0[key]), key)
        self.assertEqual(len(p.provenance), len(old["provenance"]))
        self.assertEqual(len(p.value_statuses), len(old["value_status"]))
        self.assertEqual(arch.drawing.sha256, a0["drawing"]["sha256"])
        self.assertEqual(arch.drawing.units.to_dict(), a0["drawing"]["units"])

    def test_the_format_specific_keys_are_carried_verbatim_and_nothing_is_invented(self):
        old = self.data()
        drawing = OracleProject.from_dict(old).architecture.drawing
        o = old["architecture"]["drawing"]
        self.assertEqual(drawing.source_metadata["unit_code"], o["unit_code"])
        self.assertEqual(drawing.source_metadata["format_version"], o["format_version"])
        self.assertEqual(drawing.source_metadata["layouts"], o["layouts"])
        self.assertIsNone(drawing.declared_unit, "the migration does not translate a format code into a unit")
        self.assertIsNone(drawing.revision)
        self.assertEqual(drawing.interpretation_id, "AINT-1")
        self.assertEqual(OracleProject.from_dict(old).evidence_links, [])

    def test_the_drawing_id_prefix_is_renamed_everywhere_it_is_an_id(self):
        old = self.data()
        text = json.dumps(old)
        self.assertIn('"DWG-1"', text)
        p = OracleProject.from_dict(old)
        self.assertEqual(p.architecture.drawing.id, "SRC-1")
        self.assertNotIn('"DWG-1"', p.to_json())
        self.assertTrue([r for r in p.provenance if r.target.id == "SRC-1"])
        self.assertIsNotNone(p.value_status_of(Target.architectural("SRC-1"), "units"))

    def test_observation_kinds_and_hints_that_implied_structure_are_renamed(self):
        old = self.data()
        before = [o for o in old["architecture"]["observations"] if o["kind"] == "existing_column"]
        self.assertTrue(before, "the fixture was chosen to contain them")
        p = OracleProject.from_dict(old)
        kinds = {o.kind for o in p.architecture.observations}
        self.assertNotIn("existing_column", kinds)
        self.assertNotIn("beam_shown", kinds)
        renamed = [o for o in p.architecture.observations if o.kind == "column_symbol"]
        self.assertEqual(len(renamed), len(before))
        self.assertEqual({o.hint for o in renamed}, {HintKind.COLUMN_CANDIDATE})

    def test_migration_does_not_modify_its_input_and_the_result_round_trips(self):
        old = self.data()
        snapshot = copy.deepcopy(old)
        migrated = migrate(old)
        self.assertEqual(old, snapshot)
        self.assertEqual(migrated["schema_version"], "0.4.0")
        text = OracleProject.from_dict(old).to_json()
        self.assertEqual(OracleProject.from_json(text).to_json(), text)

    def test_what_it_cannot_understand_is_refused(self):
        for mutate in (lambda d: d.update(architectures=[]), lambda d: d.update(evidence_links=[]), lambda d: d.pop("architecture"),
                       lambda d: d.update(surprise=1), lambda d: d["architecture"]["drawing"].update(surprise=1),
                       lambda d: d["architecture"].update(drawing="not an object")):
            d = self.data()
            mutate(d)
            with self.assertRaises(ValidationError):
                OracleProject.from_dict(d)

    def test_future_and_unknown_versions_are_still_refused(self):
        for version in ("0.5.0", "1.0.0", "0.0.9", "", None, 4):
            d = self.data()
            d["schema_version"] = version
            with self.assertRaises(SchemaVersionError, msg=repr(version)):
                OracleProject.from_dict(d)
        self.assertEqual(sorted(MIGRATIONS), ["0.1.0", "0.2.0", "0.3.0"])

    def test_older_fixtures_reach_0_4_0_too(self):
        for name in ("schema_0_1_0_project.json", "schema_0_2_0_project.json"):
            p = OracleProject.from_dict(json.loads((FIXTURES / name).read_text(encoding="utf-8")))
            self.assertEqual((p.schema_version, p.architectures, p.evidence_links), ("0.4.0", [], []), name)

    def test_an_empty_0_3_0_project_migrates_to_no_sources(self):
        d = self.data()
        d["architecture"] = None
        d["provenance"] = [r for r in d["provenance"] if r["target"]["scope"] != "architectural"]
        d["value_status"] = [r for r in d["value_status"] if r["target"]["scope"] != "architectural"]
        p = OracleProject.from_dict(d)
        self.assertEqual(p.architectures, [])
        self.assertIsNone(p.architecture)


@tier("integration")
class MalformedAndForgedDataIsRejected(unittest.TestCase):
    def conflict(self):
        from tests.test_resolution_effects import conflict_project
        return json.loads(conflict_project().to_json())

    def levels(self):
        from tests.support35 import with_levels
        return json.loads(with_levels().to_json())

    def test_the_untouched_files_load(self):
        OracleProject.from_dict(self.conflict())
        OracleProject.from_dict(self.levels())

    def test_forged_or_malformed_data_is_rejected(self):
        mutations = {
            "unknown key in the source": ("conflict", lambda d: d["architectures"][0]["drawing"].update(unit_code=4)),
            "old id prefix in a current file": ("conflict", lambda d: d["architectures"][0]["drawing"].update(id="DWG-1")),
            "bad declared unit": ("conflict", lambda d: d["architectures"][0]["drawing"].update(declared_unit="furlong")),
            "bad interpretation id": ("conflict", lambda d: d["architectures"][0]["drawing"].update(interpretation_id="RUN-1")),
            "nested metadata": ("conflict", lambda d: d["architectures"][0]["drawing"].update(source_metadata={"a": {"b": 1}})),
            "architectures not a list": ("conflict", lambda d: d.update(architectures={})),
            "duplicate source": ("conflict", lambda d: d["architectures"].append(copy.deepcopy(d["architectures"][0]))),
            "evidence registry missing": ("conflict", lambda d: d.pop("evidence_links")),
            "the old single slot in a current file": ("conflict", lambda d: d.update(architecture=None)),
            "observation in a view that is not there": ("conflict", lambda d: d["architectures"][0]["observations"][0].update(view_id="VIEW-999")),
            "effect of unknown kind": ("conflict", lambda d: d["interpretations"][0]["alternatives"][0].update(effects=[{"kind": "explode", "params": {}}])),
            "applied records on an unaccepted alternative": ("conflict", lambda d: d["interpretations"][0]["alternatives"][0].update(applied=[{"kind": "acknowledge"}])),
            "issue pointing at a missing set": ("conflict", lambda d: d["issues"][0].update(interpretation_set_id="IS-999")),
            "level with a bad elevation type": ("levels", lambda d: d["building"]["levels"][0].update(elevation_type="roof_of_the_world")),
            "level with an unknown key": ("levels", lambda d: d["building"]["levels"][0].update(surprise=1)),
            "level whose structural elevation contradicts its type": ("levels", lambda d: d["building"]["levels"][0].update(elevation_type="structural", structural_elevation_mm=999.0)),
            "level value that contradicts its decision": ("levels", lambda d: d["decisions"].append({
                "id": "X-1", "author": "E", "source": "engineer", "target": {"scope": "level", "id": "L1"}, "category": "other",
                "instruction": "forged", "status": "accepted", "field": "name", "value": "Forged", "created_at": "2026-01-01T00:00:00Z"})),
        }
        for name, (which, mutate) in mutations.items():
            with self.subTest(name):
                d = self.conflict() if which == "conflict" else self.levels()
                mutate(d)
                with self.assertRaises(ValidationError):
                    OracleProject.from_dict(d)


if __name__ == "__main__":
    unittest.main()
