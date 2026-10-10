#!/usr/bin/env python3
"""Explorer self-test.

Runs with ``python3 tests/explorer/test_control.py``. It uses a fake transport and an
in-memory log: it touches neither the network nor ``experimentos.jsonl``. It
prints one line per test and exits with a non-zero code if anything fails.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from explorer import log as log_module  # noqa: E402
from explorer.log import (  # noqa: E402
    ExperimentLog,
    InMemoryLog,
    is_control_accepted,
    payload_of,
    scrub,
)
from explorer.client import Client, RateLimiter  # noqa: E402
from explorer.run import InfrastructureLimit, control_plan, resolve_control  # noqa: E402


class FakeTransport:
    """Network-free transport. It returns prepared responses one at a time."""

    def __init__(self):
        self.calls = []
        self.responses = []

    def send(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        if self.responses:
            return self.responses.pop(0)
        return 200, {}, b'{"pong": true}'


class FakeClient:
    """Fake client with the same interface as ``Client.post_msg``."""

    def __init__(self, accepted=None, default=None, infra_in=None):
        self.accepted = dict(accepted or {})
        self.default = default
        self.infra_in = set(infra_in or ())
        self.calls = []

    def post_msg(self, message):
        self.calls.append(message)
        base, control = payload_of(message)
        if base in self.infra_in:
            # Body that would look accepted: the infrastructure mark wins.
            return {
                "status": 429,
                "body": {"pong": True},
                "x_req": "infra",
                "infra": True,
            }
        expected = self.accepted.get(base, self.default)
        if expected is not None and expected == control:
            body = {"cuenta_id": "X", "saldo": 0}
        else:
            body = {"error": "E202", "mensaje": "control inválido"}
        return {
            "status": 200,
            "body": body,
            "x_req": str(len(self.calls)),
            "infra": False,
        }


class FakeClock:
    """Fake monotonic clock and sleep: the rate is tested without real sleeps."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


TESTS = []


def test(name):
    def decorator(function):
        TESTS.append((name, function))
        return function

    return decorator


@test("resolve_control finds the accepted control and reports the cost")
def _t_finds():
    client = FakeClient(accepted={"CONSULTA;AC-1": 42})
    log = InMemoryLog()
    control, cost = resolve_control(
        "CONSULTA;AC-1", "test", "test", client, log
    )
    assert control == 42, control
    assert cost == 43, cost
    assert len(log.records) == 43, len(log.records)


@test("resolve_control returns None and 100 when everything is rejected")
def _t_rejects():
    client = FakeClient()
    log = InMemoryLog()
    control, cost = resolve_control(
        "CONSULTA;NONE", "test", "test", client, log
    )
    assert control is None
    assert cost == 100, cost
    assert len(client.calls) == 100


@test("resolve_control stops at the first hit and does not keep scanning")
def _t_stops():
    client = FakeClient(accepted={"CONSULTA;X": 5})
    log = InMemoryLog()
    control, cost = resolve_control("CONSULTA;X", "test", "test", client, log)
    assert (control, cost) == (5, 6), (control, cost)
    assert len(client.calls) == 6, len(client.calls)
    assert client.calls[-1] == "CONSULTA;X;5"


@test("infrastructure with X-Frontier-Infra is flagged and never accepted")
def _t_infra():
    transport = FakeTransport()
    transport.responses.append(
        (429, {"X-Frontier-Infra": "rate-limit"}, b'{"pong": true}')
    )
    client = Client(
        "http://fake",
        "token-fake",
        transport=transport,
        rate_limiter=RateLimiter(clock=lambda: 0.0, sleep=lambda _s: None),
    )
    response = client.post_msg("PING;64")
    assert response["infra"] is True, response
    # The body would look accepted, but the mark wins.
    assert is_control_accepted(response["body"]) is True

    fake_client = FakeClient(infra_in={"CONSULTA;INFRA"})
    log = InMemoryLog()
    raised = False
    try:
        resolve_control("CONSULTA;INFRA", "test", "test", fake_client, log)
    except InfrastructureLimit:
        raised = True
    assert raised, "infrastructure must stop the scan"
    assert len(log.records) == 1, "infrastructure is recorded anyway"
    assert log.records[0]["response"] == {"pong": True}


@test("the limiter admits at most 8 per second (fake clock)")
def _t_rate():
    clock = FakeClock()
    rate_limiter = RateLimiter(clock=clock.monotonic, sleep=clock.sleep)
    for _ in range(8):
        rate_limiter.wait()
    assert clock.t == 0.0, clock.t
    rate_limiter.wait()
    assert clock.t == 1.0, clock.t
    for _ in range(7):
        rate_limiter.wait()
    assert clock.t == 1.0, clock.t
    rate_limiter.wait()
    assert clock.t == 2.0, clock.t


@test("the log assigns growing ids, continues the file and rejects duplicates")
def _t_log():
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / "experimentos.jsonl"
        first = ExperimentLog(path)
        id1 = first.record("s", "r1", "h1", {"m": "PING;64"}, 200, {"pong": True})
        id2 = first.record("s", "r2", "h2", {"m": "PING;64"}, 200, {"pong": True})
        assert (id1, id2) == (1, 2), (id1, id2)

        second = ExperimentLog(path)  # re-reads what exists
        id3 = second.record("s", "r3", "h3", {"m": "PING;64"}, 200, {"pong": True})
        assert id3 == 3, id3

        failed = False
        try:
            second.record(
                "s", "r4", "h4", {"m": "PING;64"}, 200, {"pong": True}, id=1
            )
        except ValueError:
            failed = True
        assert failed, "a duplicate id must be rejected"

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3, len(lines)


@test("resolve_control reuses an already recorded measurement")
def _t_reuses():
    log = InMemoryLog()
    log.record(
        "old", "r", "h", {"m": "CONSULTA;Z;7"}, 200, {"cuenta_id": "X", "saldo": 0}
    )
    client = FakeClient()  # accepts nothing: if it is called, it fails
    control, cost = resolve_control("CONSULTA;Z", "test", "test", client, log)
    assert (control, cost) == (7, 0), (control, cost)
    assert client.calls == [], client.calls


@test("control_plan in dry_run runs everything with a fake client and writes nothing")
def _t_dry_run():
    accepted = {
        "PING": 64,
        "CONSULTA;AC-0001": 14,
        "CONSULTA;AC-0002": 11,
        "CONSULTA;AC-0009": 87,
    }
    client = FakeClient(accepted=accepted, default=0)
    log = InMemoryLog()
    report = control_plan(client, log, dry_run=True)
    assert report["ok"] is True, report
    assert report["order_independent"] is True, report["order_independent"]
    assert log.records == [], "dry_run must not write"
    assert client.calls, "the plan must query the fake client"


@test("el log oculta un secreto escrito a mano en el request")
def _t_scrub():
    secret = "cn-p-secreto-de-prueba-1234567890"
    previous = log_module._SECRETS
    log_module._SECRETS = {secret}
    try:
        log = InMemoryLog()
        log.record(
            "rutas",
            None,
            "probe manual que escribe el header crudo",
            {"method": "POST", "path": "/msg", "auth": "Bearer " + secret},
            401,
            {"error": "E001"},
            "sin Bearer",
        )
    finally:
        log_module._SECRETS = previous
    record = log.records[0]
    assert secret not in repr(record), "the secret reached the log"
    assert record["request"]["auth"] == "Bearer <oculto>", record["request"]
    nested = scrub({"a": [{"b": "HEADER " + secret + " FIN"}]}, {secret})
    assert nested == {"a": [{"b": "HEADER <oculto> FIN"}]}, nested
    assert scrub("Bearer token-que-no-existe", {secret}) == "Bearer token-que-no-existe"


def run_tests():
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
