#!/usr/bin/env python3
"""Live, opt-in HTTP tests for the adapter over the real book and registry.

It runs against a real Postgres, but only when asked: if neither
``DATABASE_URL`` nor ``DB_URL`` is set it prints one ``SKIP`` line and exits 0.
If the environment does not provide a ``REGISTRO_PEPPER`` it sets a test-only
one, so the suite never depends on the deployment secret and never prints it.

It registers its own throwaway tokens through ``Registry.register``, with
labels that carry the process id, and deletes them at the end through the book
(the foreign keys cascade the accounts, operations and counters). It never
reads, revokes or lists another client's secret.

Covered, with the numbers it observes:

  - the 20 recorded examples replayed through the whole stack over HTTP,
    requiring 20 of 20 with the recorded control strategy;
  - an unknown token is 401;
  - a revoked token is 401;
  - ``POST /__reset`` really clears one client (the balance is gone) while the
    other client's balance survives.

Never prints the DSN, the password or the pepper.

Runs with: ``/tmp/dbenv/bin/python tests/api/test_adapter_live.py``
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

from api.adapter import create_server  # noqa: E402
from api.domain.pipeline import Configuration, ControlStrategy  # noqa: E402
from api.ledger import Book  # noqa: E402
from api.registry import Registry, hash_token  # noqa: E402

EXAMPLES_PATH = ROOT / "material" / "pruebas" / "ejemplos.jsonl"

E001_BODY = {"error": "E001", "mensaje": "no autorizado"}
E401_BODY = {"error": "E401", "mensaje": "cuenta inexistente"}
OK_BODY = {"ok": True}

# The throwaway secrets carry ~256 bits, like the registry's own `create`.
SECRET_BYTES = 32


def _describe(error: BaseException) -> str:
    """Safe description: a raw psycopg2 message can carry connection details."""
    if isinstance(error, psycopg2.Error):
        return type(error).__name__
    return "%s: %s" % (type(error).__name__, error)


def _bearer(secret: str) -> str:
    return "Bearer " + secret


def _equal(expected, obtained) -> bool:
    """Compares parsed JSON bodies; object key order does not matter."""
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
    with EXAMPLES_PATH.open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                records.append(json.loads(line))
    return records


def _recorded_configuration() -> Configuration:
    """The recorded control strategy the offline adapter suite already uses.

    The control formula is unknown; the explorer owns it. A step that answered
    E202 means the original rejected that control, and any other answer means
    it accepted it. The strategy is keyed by message, which is how the recorded
    evidence pins it.
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


class Harness:
    """Starts the adapter on loopback port 0 and drives it with urllib."""

    def __init__(self, book, configuration=None):
        self.server = create_server(
            "127.0.0.1", 0, book, hash_token, configuration
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(
        self,
        method,
        path,
        auth=None,
        json_body=None,
    ):
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")

        headers = {}
        if auth is not None:
            headers["Authorization"] = auth

        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, _decode(response)
        except urllib.error.HTTPError as error:
            return error.code, _decode(error)


def _decode(response):
    raw = response.read()
    if not raw:
        return ""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


class Fixture:
    """This run's throwaway tokens, the book and the two servers."""

    def __init__(self):
        self.book = Book()
        self.registry = Registry(book=self.book)
        self.prefix = "adapter-live-%d" % os.getpid()
        self.initial_valid = 0
        self.secrets: dict[str, str] = {}
        self.replay: Harness | None = None
        self.sql: Harness | None = None

    def label(self, name: str) -> str:
        return "%s-%s" % (self.prefix, name)

    def seed(self) -> None:
        """Registers the throwaway tokens and starts the servers."""
        for name in ("a", "b", "revoked"):
            secret = secrets.token_urlsafe(SECRET_BYTES)
            self.registry.register(
                secret, self.label(name), "adapter-live", days=1
            )
            self.secrets[name] = secret
        # Keep two valid so the last-valid refusal cannot fire.
        self.registry.revoke(self.label("revoked"), "adapter-live")
        self.replay = Harness(self.book, _recorded_configuration())
        self.sql = Harness(self.book, Configuration())

    def close(self) -> None:
        for harness in (self.replay, self.sql):
            if harness is not None:
                harness.close()

    def cleanup(self) -> None:
        """Deletes only this run's token rows; the cascades remove the rest."""
        with self.book.transaction() as cursor:
            for name in ("a", "b", "revoked"):
                self.book.delete_tokens_by_label(cursor, self.label(name))


# --- cases ---------------------------------------------------------------

def _case_replay(fixture: Fixture) -> str:
    """Resets and replays the 20 examples in order over HTTP."""
    harness = fixture.replay
    assert harness is not None
    auth = _bearer(fixture.secrets["a"])
    status, body = harness.call("POST", "/__reset", auth=auth)
    assert status == 200 and body == OK_BODY, (status, body)

    records = load_examples()
    matches = 0
    for record in records:
        status, body = harness.call(
            "POST",
            "/msg",
            auth=auth,
            json_body={"m": record["request"]["m"]},
        )
        if status == record["status"] and _equal(record["response"], body):
            matches += 1
        else:
            print(
                "       step %2d got %s %r, expected %s %r"
                % (
                    record["n"],
                    status,
                    body,
                    record["status"],
                    record["response"],
                )
            )
    assert matches == len(records), "%d de %d" % (matches, len(records))
    return "%d de %d over HTTP" % (matches, len(records))


def _case_unknown_token(fixture: Fixture) -> str:
    harness = fixture.sql
    assert harness is not None
    unknown = _bearer(secrets.token_urlsafe(SECRET_BYTES))
    status, body = harness.call(
        "POST", "/msg", auth=unknown, json_body={"m": "PING;64"}
    )
    assert status == 401 and body == E001_BODY, (status, body)
    return "unknown token answered 401 E001"


def _case_revoked_token(fixture: Fixture) -> str:
    harness = fixture.sql
    assert harness is not None
    auth = _bearer(fixture.secrets["revoked"])
    status, body = harness.call(
        "POST", "/msg", auth=auth, json_body={"m": "PING;64"}
    )
    assert status == 401 and body == E001_BODY, (status, body)
    return "revoked token answered 401 E001"


def _case_reset_isolation(fixture: Fixture) -> str:
    """Reset really clears the SQL rows of one client and not the other's."""
    harness = fixture.sql
    assert harness is not None
    auth_a = _bearer(fixture.secrets["a"])
    auth_b = _bearer(fixture.secrets["b"])

    for auth in (auth_a, auth_b):
        status, body = harness.call("POST", "/__reset", auth=auth)
        assert status == 200 and body == OK_BODY, (status, body)

    status, body = harness.call(
        "POST", "/msg", auth=auth_a, json_body={"m": "ALTA;Ana;1"}
    )
    assert status == 201 and body == {"cuentaId": "AC-0001"}, (status, body)
    status, body = harness.call(
        "POST", "/msg", auth=auth_a, json_body={"m": "DEPOSITO;AC-0001;700;1"}
    )
    assert status == 200 and body.get("operacionId") == "OP-0001", (status, body)
    status, body = harness.call(
        "POST", "/msg", auth=auth_a, json_body={"m": "CONSULTA;AC-0001;1"}
    )
    assert status == 200 and body.get("saldo") == 700, (status, body)

    status, body = harness.call(
        "POST", "/msg", auth=auth_b, json_body={"m": "ALTA;Beto;1"}
    )
    assert status == 201 and body == {"cuentaId": "AC-0001"}, (status, body)
    status, body = harness.call(
        "POST", "/msg", auth=auth_b, json_body={"m": "DEPOSITO;AC-0001;300;1"}
    )
    assert status == 200 and body.get("operacionId") == "OP-0001", (status, body)

    status, body = harness.call("POST", "/__reset", auth=auth_a)
    assert status == 200 and body == OK_BODY, (status, body)

    status, body = harness.call(
        "POST", "/msg", auth=auth_a, json_body={"m": "CONSULTA;AC-0001;1"}
    )
    assert status == 404 and body == E401_BODY, (status, body)

    status, body = harness.call(
        "POST", "/msg", auth=auth_b, json_body={"m": "CONSULTA;AC-0001;1"}
    )
    assert status == 200 and body.get("saldo") == 300, (status, body)
    return "A's balance gone after reset; B kept AC-0001/300"


CASES = (
    ("20/20 examples over HTTP", _case_replay),
    ("unknown token is 401", _case_unknown_token),
    ("revoked token is 401", _case_revoked_token),
    ("reset clears one client and keeps the other", _case_reset_isolation),
)


def main() -> int:
    if os.environ.get("DATABASE_URL"):
        source = "DATABASE_URL"
    elif os.environ.get("DB_URL"):
        source = "DB_URL"
    else:
        print("SKIP adapter live: neither DATABASE_URL nor DB_URL is set")
        return 0

    print("adapter live: using %s (value never printed)" % source)
    # A test-only pepper when the environment does not provide one. A real one,
    # when set, is respected and used as is; the value is never printed.
    os.environ.setdefault("REGISTRO_PEPPER", "adapter-live-test-pepper")

    fixture = Fixture()
    failures = 0
    try:
        fixture.initial_valid = fixture.registry.count_valid()
        fixture.seed()
        for name, case in CASES:
            try:
                detail = case(fixture)
            except Exception as error:  # noqa: BLE001
                failures += 1
                print("FAIL   %s: %s" % (name, _describe(error)))
            else:
                print("PASS   %s  [%s]" % (name, detail))
    except Exception as error:  # noqa: BLE001
        failures += 1
        print("FAIL   setup boundary: %s" % _describe(error))
    finally:
        try:
            fixture.close()
            fixture.cleanup()
        except Exception as error:  # noqa: BLE001
            failures += 1
            print("FAIL   cleanup: %s" % _describe(error))

    print()
    if failures:
        print("Result: %d live failure(s)" % failures)
        return 1
    print("Result: %d live cases, all pass" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
