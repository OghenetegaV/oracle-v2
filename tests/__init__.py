"""Tests for Oracle: Package Marker and Tier Selection

Protects:
    Makes tests importable so 'python -m unittest discover -s tests -t .' works, and installs the tier selection
    hook (see tests/tiers.py): discovery keeps only the tiers named by ORACLE_TESTS (default: unit + integration),
    so the everyday command never touches the real architectural drawing while every tier stays one command away
    (python -m tests all).

Test type:
    Package marker and test-loading hook.

Dependencies:
    tests.tiers.
"""

from .tiers import load_selected


def load_tests(loader, standard_tests, pattern):
    """unittest's package load_tests protocol: load everything under tests/, then apply the selected tiers."""
    return load_selected(loader, standard_tests, pattern or "test*.py")
