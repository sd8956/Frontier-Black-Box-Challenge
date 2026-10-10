#!/usr/bin/env python3
"""Version-2 replay of the 20 recorded examples and the three measured changes.

`tests/api/ejemplos_v2.jsonl` is the version-2 oracle for the same 20 steps as
`material/pruebas/ejemplos.jsonl` (which stays untouched as the version-1
recording). It was produced from the measurements of today in
`experimentos.jsonl`: the records with `seq` `"incidente"` whose hypothesis
mentions the replay of the examples after the deployment (the `x_req` 1..20
block). Each line here carries the `source_id` of the incident record it was
taken from. Nothing was re-derived by hand.

Two replays run, both over a fresh `MemoryState` and the real pipeline and
serializer, and both must reach 20 of 20:

  - the recorded control strategy (keyed by the recorded messages): a control
    that the recording shows was rejected with E202 is rejected, any other is
    accepted;
  - the shipped default strategy (`MeasuredControls`). It is keyed by the
    measured payload, so `ALTA;Lucia` accepts 82 and rejects 83: step 2 now
    answers the recorded E202 and its old cascade over steps 3 and 4 is gone.
    The previously declared control difference no longer exists, so nothing is
    declared here anymore.

It also pins the three changes directly. The direct business cases use the
measured control of each measured payload, because the shipped default is now
the measured table. Prints one line per case and exits non-zero on failure.

Runs with: ``python3 tests/api/test_v2.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.state import MemoryState  # noqa: E402
from api.serializer import serialize  # noqa: E402
from api.domain.state import Annulment  # noqa: E402
from api.domain.pipeline import (  # noqa: E402
    Configuration,
    ControlStrategy,
    is_numeric_control,
    process,
)

EXAMPLES_V2_PATH = ROOT / "tests" / "api" / "ejemplos_v2.jsonl"


def _equal(expected, obtained) -> bool:
    """Compares JSON bodies; object key order does not matter."""
    if isinstance(expected, dict) and isinstance(obtained, dict):
        if set(expected) != set(obtained):
            return False
        return all(_equal(expected[k], obtained[k]) for k in expected)
    if isinstance(expected, list) and isinstance(obtained, list):
        if len(expected) != len(obtained):
            return False
        return all(_equal(a, b) for a, b in zip(expected, obtained))
    if isinstance(expected, bool) or isinstance(obtained, bool):
        return expected is obtained
    if isinstance(expected, (int, float)) and isinstance(obtained, (int, float)):
        return expected == obtained
    return type(expected) is type(obtained) and expected == obtained


def load_examples() -> list[dict]:
    records = []
    with EXAMPLES_V2_PATH.open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                records.append(json.loads(line))
    return records


def _recorded_configuration() -> Configuration:
    """Configuration whose control strategy matches the version-2 recording.

    The control formula is unknown; the recorded evidence is enough: a step
    that answered E202 means the original rejected that control, and any other
    answer means it accepted it. The strategy is keyed by message.
    """
    accepted = {
        record["request"]["m"]
        for record in load_examples()
        if record["response"].get("error") != "E202"
    }

    class _RecordedControl(ControlStrategy):
        def accept(self, message, control):
            return message in accepted

    return Configuration(control_strategy=_RecordedControl())


def replay(configuration: Configuration):
    """Replays the 20 steps in order; returns (matches, mismatches)."""
    records = load_examples()
    state = MemoryState(client="v2")
    matches = 0
    mismatches = []
    for record in records:
        message = record["request"]["m"]
        status, body = serialize(process(message, state, configuration))
        if status == record["status"] and _equal(record["response"], body):
            matches += 1
        else:
            mismatches.append(
                (record["n"], status, body, record["status"], record["response"])
            )
    return matches, mismatches


def control_agreement():
    """Compares the default control decisions with the version-2 recording.

    Returns (matches, differing step numbers). A malformed control never
    reaches the strategy, so both decide "rejected" for it.
    """
    recorded = _recorded_configuration().control_strategy
    default = Configuration().control_strategy
    matches = 0
    differing = []
    for record in load_examples():
        message = record["request"]["m"]
        control = message.split(";")[-1]
        numeric = is_numeric_control(control)
        recorded_accepts = numeric and recorded.accept(message, control)
        default_accepts = numeric and default.accept(message, control)
        if recorded_accepts == default_accepts:
            matches += 1
        else:
            differing.append(record["n"])
    return matches, differing


# --- direct cases for the three changes ----------------------------------

TESTS = []


def test(name):
    def decorator(function):
        TESTS.append((name, function))
        return function

    return decorator


@test("balance is a decimal string of units with two decimals")
def _t_balance_string():
    state = MemoryState()
    configuration = Configuration()
    _, created = serialize(process("ALTA;A;1", state, configuration))
    account = created["cuentaId"]

    status, body = serialize(
        process(f"CONSULTA;{account};14", state, configuration)
    )
    assert (status, body) == (
        200,
        {"cuenta_id": account, "saldo": "0.00"},
    ), (status, body)

    process(f"DEPOSITO;{account};12345;96", state, configuration)
    _, body = serialize(process(f"CONSULTA;{account};14", state, configuration))
    assert body["saldo"] == "123.45", body

    state = MemoryState()
    _, created = serialize(process("ALTA;B;1", state, configuration))
    account = created["cuentaId"]
    process(f"DEPOSITO;{account};250000;86", state, configuration)
    _, body = serialize(process(f"CONSULTA;{account};14", state, configuration))
    assert body["saldo"] == "2500.00", body


@test("the transfer cap is 50000: 50000 accepted, 50001 rejected")
def _t_cap():
    state = MemoryState()
    configuration = Configuration()
    _, source = serialize(process("ALTA;A;1", state, configuration))
    _, destination = serialize(process("ALTA;B;1", state, configuration))
    src, dst = source["cuentaId"], destination["cuentaId"]
    process(f"DEPOSITO;{src};250000;86", state, configuration)

    status, body = serialize(
        process(f"TRANSFER;{src};{dst};50000;59", state, configuration)
    )
    assert status == 200 and body["status"] == "recieved", (status, body)

    status, body = serialize(
        process(f"TRANSFER;{src};{dst};50001;56", state, configuration)
    )
    assert status == 422 and body["error"] == "E506", (status, body)


@test("the cap does not apply to deposits")
def _t_deposit_exempt():
    state = MemoryState()
    configuration = Configuration()
    _, created = serialize(process("ALTA;A;1", state, configuration))
    account = created["cuentaId"]
    status, body = serialize(
        process(f"DEPOSITO;{account};250000;86", state, configuration)
    )
    assert status == 200 and body["status"] == "recieved", (status, body)


@test("annulling a nonexistent operation is 404 E402")
def _t_annul_nonexistent():
    state = MemoryState()
    configuration = Configuration()
    status, body = serialize(
        process("ANULA;OP-0001;1", state, configuration)
    )
    assert status == 404 and body == {
        "error": "E402",
        "mensaje": "operación inexistente",
    }, (status, body)
    # The port itself reports the third case too.
    assert state.annul("OP-9999") is Annulment.NOT_FOUND


@test("annulling an existing, already annulled operation is 'ya anulada'")
def _t_annul_already():
    state = MemoryState()
    configuration = Configuration()
    _, created = serialize(process("ALTA;A;1", state, configuration))
    account = created["cuentaId"]
    _, accepted = serialize(
        process(f"DEPOSITO;{account};700;1", state, configuration)
    )
    operation = accepted["operacionId"]

    status, body = serialize(
        process(f"ANULA;{operation};1", state, configuration)
    )
    assert status == 200 and body == {
        "ok": True,
        "anulada": operation,
    }, (status, body)

    status, body = serialize(
        process(f"ANULA;{operation};1", state, configuration)
    )
    assert status == 200 and body == {
        "ok": False,
        "motivo": "ya anulada",
    }, (status, body)


@test("a message with no separator is E103; PING;64; stays E104")
def _t_missing_control():
    state = MemoryState()
    configuration = Configuration()
    for message in ("PING", ""):
        status, body = serialize(process(message, state, configuration))
        assert (status, body) == (
            400,
            {"error": "E103", "mensaje": "falta control"},
        ), (message, status, body)
    status, body = serialize(process("PING;64;", state, configuration))
    assert (status, body) == (
        400,
        {"error": "E104", "mensaje": "segmento vacío"},
    ), (status, body)


def run_tests() -> int:
    failures = 0
    for name, function in TESTS:
        try:
            function()
        except Exception as error:  # noqa: BLE001
            failures += 1
            print("FAIL   %s: %s: %s" % (name, type(error).__name__, error))
        else:
            print("PASS   %s" % name)
    return failures


def main() -> int:
    print("== version-2 replay (recorded control) ==")
    recorded_matches, recorded_mismatches = replay(_recorded_configuration())
    for n, status, body, expected_status, expected_body in recorded_mismatches:
        print(
            "step %2d: got %s %r, expected %s %r"
            % (n, status, body, expected_status, expected_body)
        )
    print("%d de %d" % (recorded_matches, len(load_examples())))

    print("\n== version-2 replay (shipped default) ==")
    default_matches, default_mismatches = replay(Configuration())
    for n, status, body, expected_status, expected_body in default_mismatches:
        print(
            "step %2d: got %s %r, expected %s %r"
            % (n, status, body, expected_status, expected_body)
        )
    print("%d de %d" % (default_matches, len(load_examples())))

    print("\n== default control agreement with the recording ==")
    agreement, differing = control_agreement()
    print("%d de %d" % (agreement, len(load_examples())))

    print("\n== direct cases ==")
    failures = run_tests()

    problems = []
    if recorded_matches != len(load_examples()):
        problems.append(
            "recorded replay %d/%d" % (recorded_matches, len(load_examples()))
        )
    if recorded_mismatches:
        problems.append("recorded replay has mismatches")
    if default_mismatches:
        problems.append(
            "default replay has mismatches: %s"
            % [n for n, _s, _b, _es, _eb in default_mismatches]
        )
    if agreement != len(load_examples()):
        problems.append(
            "default control agreement %d/%d"
            % (agreement, len(load_examples()))
        )
    if differing:
        problems.append(
            "default control differs from the recording: %s" % differing
        )
    if failures:
        problems.append("%d direct case(s) failed" % failures)

    print()
    if problems:
        print("resultado: FALLA (%s)" % "; ".join(problems))
        return 1
    print("resultado: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
