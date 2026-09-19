"""Tests for Oracle's legacy fixture generator (generate_test_dwg.py)

Protects:
    Importing generate_test_dwg must not generate or overwrite any DXF (it used to rewrite
    input_dwgs/test_floor.dxf on import), while calling main() still builds the same drawing:
    walls, gridlines and circle columns on their layers.

Test type:
    Regression tests (legacy development utility).

Dependencies:
    ezdxf, the legacy config module and generate_test_dwg. Not oracle.core. The real fixture is
    snapshotted and restored, so a regression here cannot damage input_dwgs/test_floor.dxf; main()
    is only ever pointed at a temporary directory.
"""

import contextlib
import importlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ezdxf

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "input_dwgs" / "test_floor.dxf"


def _fresh_import():
    sys.modules.pop("generate_test_dwg", None)
    return importlib.import_module("generate_test_dwg")


class GenerateTestDwgTests(unittest.TestCase):
    def setUp(self):
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))

    def test_import_does_not_touch_the_fixture(self):
        self.assertTrue(FIXTURE.exists(), "input_dwgs/test_floor.dxf is a tracked fixture")
        original_bytes = FIXTURE.read_bytes()
        original_stat = FIXTURE.stat()

        def restore():
            if FIXTURE.read_bytes() != original_bytes:
                FIXTURE.write_bytes(original_bytes)
            os.utime(FIXTURE, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        self.addCleanup(restore)

        with contextlib.redirect_stdout(io.StringIO()):
            module = _fresh_import()

        self.assertTrue(callable(module.main))
        self.assertEqual(FIXTURE.read_bytes(), original_bytes, "importing rewrote test_floor.dxf")
        self.assertEqual(FIXTURE.stat().st_mtime_ns, original_stat.st_mtime_ns)

    def test_import_does_not_create_a_dxf_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("config.INPUT_DIR", Path(tmp)):
                with contextlib.redirect_stdout(io.StringIO()):
                    _fresh_import()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_main_builds_the_same_drawing(self):
        module = _fresh_import()
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with mock.patch.object(module, "INPUT_DIR", Path(tmp)), contextlib.redirect_stdout(out):
                module.main()
            path = Path(tmp) / "test_floor.dxf"
            self.assertTrue(path.exists())
            self.assertIn(str(path), out.getvalue())

            doc = ezdxf.readfile(str(path))
            msp = doc.modelspace()
            layers = {layer.dxf.name for layer in doc.layers}
            self.assertTrue({"Walls", "Gridlines", "Columns"} <= layers)
            self.assertEqual(len(msp.query('LINE[layer=="Walls"]')), 5)
            self.assertEqual(len(msp.query('LINE[layer=="Gridlines"]')), 2)
            columns = msp.query('CIRCLE[layer=="Columns"]')
            self.assertEqual(len(columns), 9)
            self.assertTrue(all(abs(c.dxf.radius - 0.3) < 1e-9 for c in columns))
            centres = {(round(c.dxf.center.x), round(c.dxf.center.y)) for c in columns}
            self.assertEqual(centres, {(x, y) for x in (0, 5, 10) for y in (0, 5, 10)})


if __name__ == "__main__":
    unittest.main()
