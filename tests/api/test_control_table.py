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
    process,
)
from api.serializer import serialize  # noqa: E402
from api.state import MemoryState  # noqa: E402

TABLE_PATH = ROOT / "api" / "control_table.json"

TESTS = []


def test(name):
    def decorator(function):
        TESTS.append((name, function))
        return function

    return decorator


@test("the table loads: object of payloads with sorted accept/reject lists")
def _t_loads():
    assert TABLE_PATH.is_file(), TABLE_PATH
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), type(raw)
    for base, entry in raw.items():
        assert isinstance(base, str), base
        assert set(entry) == {"accept", "reject"}, (base, entry)
        for key in ("accept", "reject"):
            controls = entry[key]
            assert isinstance(controls, list), (base, key, controls)
            assert all(isinstance(control, int) for control in controls), (
                base,
                key,
                controls,
            )
            assert controls == sorted(controls), (base, key, controls)
    # The loader read the same file the repository ships.
    assert _CONTROL_TABLE_PATH == TABLE_PATH, (
        _CONTROL_TABLE_PATH,
        TABLE_PATH,
    )
    assert set(raw) == set(_CONTROL_TABLE), "loaded table differs from the file"
    assert len(raw) == 76, len(raw)


@test("case 1: a non-numeric control is rejected")
def _t_not_numeric():
    strategy = MeasuredControls()
    assert strategy.accept("ALTA;Lucia;82", "8X") is False
    assert strategy.accept("NUNCA;SONDEADO;7", "") is False


@test("case 2: an unmeasured payload still accepts any numeric control")
def _t_unmeasured():
    strategy = MeasuredControls()
    assert strategy.accept("NUNCA;SONDEADO;7", "7") is True
    assert strategy.accept("NUNCA;SONDEADO;7", "999999") is True
    assert strategy.accept("NUNCA;SONDEADO;7", "not-numeric") is False


@test("case 3: a non-empty accept list accepts only its control and rejects the rest")
def _t_lucia():
    strategy = MeasuredControls()
    assert strategy.accept("ALTA;Lucia;82", "82") is True
    assert strategy.accept("ALTA;Lucia;83", "83") is False


@test("PING accepts its measured control 64 and rejects 65")
def _t_ping():
    strategy = MeasuredControls()
    assert strategy.accept("PING;64", "64") is True
    assert strategy.accept("PING;65", "65") is False


@test("case 4: an empty accept list rejects the probed controls and accepts the rest")
def _t_empty_accept():
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    # Kept on purpose: this payload was probed with control 1 and rejected, so
    # 1 is a measured rejection while the rest of the space is unmeasured.
    assert raw["ALTA;X;Y;Z"] == {"accept": [], "reject": [1]}, raw["ALTA;X;Y;Z"]
    strategy = MeasuredControls()
    assert strategy.accept("ALTA;X;Y;Z;1", "1") is False
    assert strategy.accept("ALTA;X;Y;Z;2", "2") is True
    assert strategy.accept("ANULA;OP-9999;66", "66") is False
    assert strategy.accept("ANULA;OP-9999;67", "67") is True


@test("ALTA;X;Y;Z;1 answers E202, not E302")
def _t_x_y_z_pipeline():
    status, body = serialize(process("ALTA;X;Y;Z;1", MemoryState(), Configuration()))
    assert (status, body) == (
        400,
        {"error": "E202", "mensaje": "control inválido"},
    ), (status, body)


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
