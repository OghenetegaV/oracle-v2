"""Tests for Oracle: Tier Runner (python -m tests)

Protects:
    A convenient, cross-shell front end to the tier mechanism in tests/tiers.py, so the exact same selection works
    in PowerShell, Git Bash and cmd without environment-variable syntax:

        python -m tests                 # dev: unit + integration (the default; same as the plain unittest command)
        python -m tests unit            # only the fastest tier
        python -m tests integration     # only integration
        python -m tests slow            # only the real architectural drawing
        python -m tests full            # unit + integration + slow
        python -m tests all             # every tier (complete regression)
        python -m tests all --durations 15
        python -m tests --list          # every test class with its tier and count

    It only sets ORACLE_TESTS and runs discovery; it selects and runs exactly what
    'python -m unittest discover -s tests -t .' would.

Test type:
    Test infrastructure.

Dependencies:
    tests.tiers, unittest.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unittest
from collections import Counter, OrderedDict
from pathlib import Path

from . import tiers

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Timed(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings: list = []
        self._start = 0.0

    def startTest(self, test):
        self._start = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        self.timings.append((time.perf_counter() - self._start, test.id()))
        super().stopTest(test)


def _discover():
    return unittest.TestLoader().discover(str(Path(__file__).resolve().parent), pattern="test*.py", top_level_dir=str(REPO_ROOT))


def list_tests() -> None:
    os.environ[tiers.ENV_VAR] = "all"
    rows: "OrderedDict[tuple, list]" = OrderedDict()
    for test in tiers.walk(_discover()):
        cls = type(test)
        key = (cls.__module__.split(".")[-1], cls.__name__)
        rows.setdefault(key, [tiers.tier_of(test) or "UNCLASSIFIED", 0])
        rows[key][1] += 1
        if tiers.tier_of(test) and rows[key][0] != tiers.tier_of(test):
            rows[key][0] = "mixed"
    per_tier: Counter = Counter()
    for (module, name), (tier, count) in rows.items():
        print(f"{tier:13s} {count:4d}  {module}.{name}")
        per_tier[tier] += count
    print("\n" + "  ".join(f"{t}={per_tier[t]}" for t in (*tiers.TIERS, "mixed", "UNCLASSIFIED") if per_tier[t]) + f"  total={sum(per_tier.values())}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests", description="Run Oracle's tests by tier.")
    parser.add_argument("selector", nargs="?", help="a tier (unit, integration, slow, release), an alias (fast, dev, full, all) or a "
                                                    f"comma-separated list; default: {tiers.DEFAULT_SELECTION}")
    parser.add_argument("--list", action="store_true", help="list every test class with its tier and exit")
    parser.add_argument("--durations", type=int, default=0, metavar="N", help="print the N slowest tests")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-f", "--failfast", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        list_tests()
        return 0
    if args.selector:
        try:
            tiers.selected_tiers(args.selector)
        except ValueError as exc:
            parser.error(str(exc))
        os.environ[tiers.ENV_VAR] = args.selector
    started = time.perf_counter()
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, failfast=args.failfast,
                                     resultclass=_Timed if args.durations else unittest.TextTestResult)
    result = runner.run(_discover())
    if args.durations:
        print(f"\nSlowest {args.durations} tests:")
        for secs, name in sorted(result.timings, reverse=True)[: args.durations]:
            print(f"  {secs:6.2f}s  {name}")
    print(f"\nTiers run: {'+'.join(tiers.selected_tiers())}   wall time: {time.perf_counter() - started:.1f}s")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
