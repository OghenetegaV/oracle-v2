"""Tests for Oracle: Test Tiers (tests/tiers.py)

Protects:
    Provides the one mechanism that decides which tests run: every test class (or single test) is declared
    with @tier("unit" | "integration" | "slow" | "release"), and the ORACLE_TESTS environment variable, read
    when 'python -m unittest discover -s tests -t .' loads the suite, selects tiers. Nothing is deleted or
    skipped: unselected tests are simply not loaded, and every tier remains one command away. The default
    (ORACLE_TESTS unset) is 'dev' = unit + integration and never touches the real architectural drawing.

Test type:
    Test infrastructure (not tests themselves). The tests OF this mechanism are in tests/test_tiers.py.

Dependencies:
    Standard library only (unittest, os, collections).

Details:
    unit         pure domain/core logic and small pure functions; no synthetic drawings, no subprocesses, no real files.
    integration  several Oracle subsystems together: synthetic DXFs built in memory, the interpretation pipeline,
                 the legacy-GA adapter on its fixture, static scans of the code base, fresh-process import checks.
    slow         the real squash-court architectural drawing (a 65 MB DXF after conversion; about 30 s).
    release      needs external software or is only worth running before a release (the ODA File Converter on a
                 small DWG). Runs in 'all'.
    Selectors (ORACLE_TESTS, comma-separated, case-insensitive): unit, integration, slow, release; the aliases
    fast = unit, dev = unit+integration (the default), full = unit+integration+slow, all = every tier.
    Selection applies only to discovery with the default file pattern; naming a module, a class, a test or a custom
    -p pattern always runs exactly what you named.
"""

from __future__ import annotations

import os
import sys
import unittest
from collections import Counter

TIERS = ("unit", "integration", "slow", "release")
ALIASES = {"fast": ("unit",), "dev": ("unit", "integration"), "full": ("unit", "integration", "slow"), "all": TIERS}
DEFAULT_SELECTION = "dev"
ENV_VAR = "ORACLE_TESTS"


def tier(name: str):
    """Declare the tier of a test class or of a single test method."""
    if name not in TIERS:
        raise ValueError(f"Unknown test tier {name!r}; use one of {TIERS}.")

    def mark(obj):
        obj.oracle_tier = name
        return obj
    return mark


def selected_tiers(value: str | None = None) -> tuple:
    """The tiers named by ORACLE_TESTS (or by `value`), in canonical order."""
    text = (os.environ.get(ENV_VAR) if value is None else value) or DEFAULT_SELECTION
    chosen = set()
    for word in (w.strip().lower() for w in text.split(",") if w.strip()):
        if word in ALIASES:
            chosen.update(ALIASES[word])
        elif word in TIERS:
            chosen.add(word)
        else:
            raise ValueError(f"{ENV_VAR}={text!r}: unknown tier or alias {word!r}. Tiers: {TIERS}; "
                             f"aliases: {sorted(ALIASES)}.")
    return tuple(t for t in TIERS if t in chosen)


def tier_of(test) -> str | None:
    """The declared tier of one loaded test (method first, then class); None if undeclared or not a normal test."""
    method = getattr(test, getattr(test, "_testMethodName", ""), None)
    return getattr(method, "oracle_tier", None) or getattr(type(test), "oracle_tier", None)


def walk(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from walk(item)
        else:
            yield item


def filter_suite(suite: unittest.TestSuite, wanted: tuple):
    """(the suite without unselected tiers, Counter of what was left out by tier). Tests with no declared tier, and
    import failures, are always kept: an unclassified test must never disappear silently (tests/test_tiers.py fails
    until it is classified)."""
    left_out: Counter = Counter()

    def keep(node):
        if isinstance(node, unittest.TestSuite):
            children = [c for c in (keep(x) for x in node) if c is not None]
            return type(node)(children) if children else None
        declared = tier_of(node)
        if declared is None or declared in wanted:
            return node
        left_out[declared] += 1
        return None

    return keep(suite) or unittest.TestSuite(), left_out


def load_selected(loader, standard_tests, pattern):
    """The load_tests hook for the tests package: discover normally, then keep the selected tiers."""
    package_dir = os.path.dirname(os.path.abspath(__file__))
    found = loader.discover(start_dir=package_dir, pattern=pattern)
    standard_tests.addTests(found)
    if pattern != "test*.py":
        return standard_tests                        # an explicit -p pattern runs exactly what it matches
    wanted = selected_tiers()
    selected, left_out = filter_suite(standard_tests, wanted)
    if left_out:
        total = sum(left_out.values())
        detail = ", ".join(f"{n} {t}" for t, n in sorted(left_out.items()))
        print(f"[{ENV_VAR}={os.environ.get(ENV_VAR) or DEFAULT_SELECTION}] running tiers {'+'.join(wanted)}; "
              f"{total} test(s) not selected ({detail}). Everything: python -m tests all", file=sys.stderr)
    return selected
