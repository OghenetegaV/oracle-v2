"""Tests for the test-tier mechanism (tests/tiers.py)

Protects:
    That every test class in the repository is explicitly classified (so a new test can never silently fall out of every
    tier), that tier selection behaves as documented (default dev = unit + integration; aliases; comma lists; unknown
    names refused), that selection removes only what was not selected and never drops an unclassified test, that the
    default run can never reach the real architectural drawing, and that the old competing environment variable is gone.

Test type:
    Unit tests of test infrastructure (static scan of the tests directory plus small synthetic suites).

Dependencies:
    tests.tiers, tests.__main__ (for --list), unittest, ast.
"""

import ast
import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from tests import tiers
from tests.tiers import tier

TESTS_DIR = Path(__file__).resolve().parent


def declared_tiers():
    """(module, class, tier-or-None, has_tests) for every class in tests/test_*.py, read from the source."""
    out = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            has_tests = any(isinstance(n, ast.FunctionDef) and n.name.startswith("test") for n in node.body)
            declared = None
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "id", None) == "tier" and d.args and isinstance(d.args[0], ast.Constant):
                    declared = d.args[0].value
            out.append((path.stem, node.name, declared, has_tests))
    return out


@tier("unit")
class EveryTestIsClassified(unittest.TestCase):
    def test_every_test_class_declares_a_valid_tier(self):
        problems = [(m, c, t) for m, c, t, has in declared_tiers() if has and t not in tiers.TIERS]
        self.assertEqual(problems, [], "add @tier(...) to these classes (see tests/tiers.py)")

    def test_the_real_drawing_is_only_ever_touched_by_slow_or_release_tests(self):
        needle = "Squash Court" + " Extension"
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            if path.name == "test_tiers.py" or needle not in path.read_text(encoding="utf-8"):
                continue
            for module, name, declared, has_tests in declared_tiers():
                if module == path.stem and has_tests:
                    self.assertIn(declared, ("slow", "release"), f"{module}.{name} uses the real drawing but is tier {declared!r}")

    def test_the_default_selection_contains_no_slow_or_release_test(self):
        with mock.patch.dict(os.environ, {tiers.ENV_VAR: "all"}):
            everything = unittest.TestLoader().discover(str(TESTS_DIR), pattern="test*.py", top_level_dir=str(TESTS_DIR.parent))
        selected, left_out = tiers.filter_suite(everything, tiers.selected_tiers(""))
        kept = {tiers.tier_of(t) for t in tiers.walk(selected)}
        self.assertTrue(kept <= {"unit", "integration", None})
        self.assertEqual(set(left_out), {"slow", "release"})
        self.assertNotIn("test_real_architectural_drawing", {type(t).__module__.split(".")[-1] for t in tiers.walk(selected)})

    def test_the_old_competing_switch_is_gone(self):
        old = "ORACLE_SKIP" + "_REAL_DWG"
        for path in TESTS_DIR.glob("*.py"):
            if path.name != "test_tiers.py":
                self.assertNotIn(old, path.read_text(encoding="utf-8"), path.name)

    def test_the_listing_classifies_everything(self):
        from tests.__main__ import list_tests
        buffer = io.StringIO()
        with mock.patch.dict(os.environ), contextlib.redirect_stdout(buffer):
            list_tests()
        self.assertNotIn("UNCLASSIFIED", buffer.getvalue())
        self.assertIn("total=", buffer.getvalue())


@tier("unit")
class SelectionRules(unittest.TestCase):
    def test_the_default_is_dev_and_never_slow(self):
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(tiers.ENV_VAR, None)
            self.assertEqual(tiers.selected_tiers(), ("unit", "integration"))
        self.assertEqual(tiers.selected_tiers(""), ("unit", "integration"))

    def test_aliases_tiers_and_lists(self):
        cases = {"fast": ("unit",), "unit": ("unit",), "dev": ("unit", "integration"), "integration": ("integration",),
                 "slow": ("slow",), "full": ("unit", "integration", "slow"), "all": tiers.TIERS, "release": ("release",),
                 "unit,slow": ("unit", "slow"), " Integration , SLOW ": ("integration", "slow")}
        for text, expected in cases.items():
            self.assertEqual(tiers.selected_tiers(text), expected, text)

    def test_unknown_names_are_refused_not_ignored(self):
        for bad in ("everything", "unit,fastest", "slowest"):
            with self.assertRaises(ValueError, msg=bad):
                tiers.selected_tiers(bad)
        with self.assertRaises(ValueError):
            tier("medium")

    def test_the_environment_variable_is_what_selects(self):
        with mock.patch.dict(os.environ, {tiers.ENV_VAR: "slow"}):
            self.assertEqual(tiers.selected_tiers(), ("slow",))


def _suite(*classes):
    loader = unittest.TestLoader()
    return unittest.TestSuite(loader.loadTestsFromTestCase(c) for c in classes)


@tier("unit")
class FilteringRules(unittest.TestCase):
    def build(self):
        @tier("unit")
        class U(unittest.TestCase):
            def test_a(self): pass
            def test_b(self): pass

        @tier("integration")
        class I(unittest.TestCase):
            def test_c(self): pass

        @tier("slow")
        class S(unittest.TestCase):
            def test_d(self): pass
            @tier("release")
            def test_e(self): pass

        class Unclassified(unittest.TestCase):
            def test_f(self): pass

        return _suite(U, I, S, Unclassified)

    def names(self, suite):
        return sorted(t._testMethodName for t in tiers.walk(suite))

    def test_only_the_selected_tiers_are_kept_and_the_rest_are_counted(self):
        selected, left = tiers.filter_suite(self.build(), ("unit", "integration"))
        self.assertEqual(self.names(selected), ["test_a", "test_b", "test_c", "test_f"])
        self.assertEqual(dict(left), {"slow": 1, "release": 1})

    def test_a_single_test_can_have_a_different_tier_from_its_class(self):
        selected, _ = tiers.filter_suite(self.build(), ("slow",))
        self.assertEqual(self.names(selected), ["test_d", "test_f"])
        selected, _ = tiers.filter_suite(self.build(), ("release",))
        self.assertEqual(self.names(selected), ["test_e", "test_f"])

    def test_an_unclassified_test_is_never_dropped(self):
        for wanted in (("unit",), ("slow",), ()):
            selected, _ = tiers.filter_suite(self.build(), wanted)
            self.assertIn("test_f", self.names(selected))

    def test_selecting_everything_leaves_nothing_out(self):
        selected, left = tiers.filter_suite(self.build(), tiers.TIERS)
        self.assertEqual(len(self.names(selected)), 6)
        self.assertEqual(sum(left.values()), 0)

    def test_an_empty_result_is_still_a_suite(self):
        selected, _ = tiers.filter_suite(_suite(), ("unit",))
        self.assertEqual(selected.countTestCases(), 0)


if __name__ == "__main__":
    unittest.main()
