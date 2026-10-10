#!/usr/bin/env python3
"""Offline checks for the measured control table and its strategy.

The table (`api/control_table.json`) is generated from `experimentos.jsonl`
(the exact rule lives in the module docstring of `api/domain/pipeline.py`).
These checks touch neither the network nor the original service nor the
experiment log: they read the shipped table, exercise `MeasuredControls`, and
confirm the shipped default is the measured one.

Prints one line per case and exits non-zero on failure.

Runs with: ``python3 tests/api/test_control_table.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.domain.pipeline import (  # noqa: E402
    Configuration,
    MeasuredControls,
    _CONTROL_TABLE,
    _CONTROL_TABLE_PATH,
)

TABLE_PATH = ROOT / "api" / "control_table.json"

TESTS = []


def test(name):
    def decorator(function):
        TESTS.append((name, function))
        return function

    return decorator


@test("the table loads: object of sorted control lists")
def _t_loads():
    assert TABLE_PATH.is_file(), TABLE_PATH
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), type(raw)
    for base, controls in raw.items():
        assert isinstance(base, str), base
        assert isinstance(controls, list), (base, controls)
        assert all(isinstance(control, int) for control in controls), (
            base,
            controls,
        )
        assert controls == sorted(controls), (base, controls)
    # The loader read the same file the repository ships.
    assert _CONTROL_TABLE_PATH == TABLE_PATH, (
        _CONTROL_TABLE_PATH,
        TABLE_PATH,
    )
    assert set(raw) == set(_CONTROL_TABLE), "loaded table differs from the file"
    assert len(raw) == 76, len(raw)


@test("PING accepts its measured control 64 and rejects 65")
def _t_ping():
    strategy = MeasuredControls()
    assert strategy.accept("PING;64", "64") is True
    assert strategy.accept("PING;65", "65") is False


@test("ALTA;Lucia accepts its measured control 82 and rejects 83")
def _t_lucia():
    strategy = MeasuredControls()
    assert strategy.accept("ALTA;Lucia;82", "82") is True
    assert strategy.accept("ALTA;Lucia;83", "83") is False


@test("an unmeasured payload still accepts any numeric control")
def _t_unmeasured():
    strategy = MeasuredControls()
    assert strategy.accept("NUNCA;SONDEADO;7", "7") is True
    assert strategy.accept("NUNCA;SONDEADO;7", "999999") is True
    assert strategy.accept("NUNCA;SONDEADO;7", "not-numeric") is False


@test("a payload with no known accepted control falls back, it does not reject everything")
def _t_empty():
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    # Kept on purpose: this payload was probed with a single control (66) and
    # rejected, which says nothing about the rest of the space. Treating it as
    # "rejects everything" would turn one rejected probe into a wrong answer
    # for a control the original may accept, so it counts as unmeasured.
    assert raw["ANULA;OP-9999"] == [], raw["ANULA;OP-9999"]
    assert MeasuredControls().accept("ANULA;OP-9999;66", "66") is True


@test("the shipped default strategy is the measured one")
def _t_default():
    assert isinstance(Configuration().control_strategy, MeasuredControls)


def run_tests() -> int:
    failures = 0
    for name, function in TESTS:
        try:
            function()
        except Exception as error:  # noqa: BLE001
            failures += 1
            print("FAIL   %s" % name)
            print("       %s: %s" % (type(error).__name__, error))
        else:
            print("PASS   %s" % name)
    print()
    if failures:
        print("Result: %d failure(s) out of %d tests" % (failures, len(TESTS)))
        return 1
    print("Result: %d tests, all pass" % len(TESTS))
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
