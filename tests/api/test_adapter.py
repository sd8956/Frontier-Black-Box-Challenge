#!/usr/bin/env python3
"""HTTP frontier adapter tests.

No network beyond loopback and no external dependency. The server is started
on port 0, driven with ``urllib`` and reset between phases. Every row of the
frontier table measured against the original is its own case, in the measured
order: route before token, then auth, then the ``/msg`` body. Last, the 20
recorded examples are replayed in order over the adapter, requiring 20 of 20.

The token is a test constant, never ``.env``. The control strategy is injected
from the recorded evidence so the replay exercises the HTTP path and not the
still-unknown control formula: that unknown belongs to the explorer.

Prints one line per case and exits non-zero on failure.

Runs with: ``python3 tests/api/test_adapter.py``
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.adapter import create_server  # noqa: E402
from api.domain.pipeline import Configuration, ControlStrategy  # noqa: E402
from api.registry import hash_token  # noqa: E402
from api.state import MemoryState  # noqa: E402

EXAMPLES_PATH = ROOT / "material" / "pruebas" / "ejemplos.jsonl"

# Test client and token: never read from `.env`. The pepper is a test constant
# too, and it is never printed; the hasher is the real one from the registry.
TOKEN = "test-token-frontier"
TEST_PEPPER = "adapter-test-pepper"
AUTH = "Bearer " + TOKEN


def _hasher(secret):
    """The real `hash_token` bound to the test pepper."""
    return hash_token(secret, TEST_PEPPER)


class _Client:
    """The resolved-token shape the adapter reads: an `id` and a label."""

    def __init__(self, id, label):
        self.id = id
        self.label = label


class MemoryBook:
    """In-file double of the SQL book: the same seam, no database.

    The adapter only needs `transaction`, `resolve_token`, `lock` and
    `for_client` (plus `reset` for `POST /__reset`), so the offline suite keeps
    exercising the HTTP frontier without opening a connection or importing the
    SQL book. `resolve_token` uses the real `hash_token`, so the token path is
    not faked; only the storage is in memory.
    """

    def __init__(self, state):
        self.state = state
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def transaction(self):
        # The per-client lock of the SQL book is released at commit, so this
        # double serializes the whole transaction the same way.
        with self._lock:
            yield None

    def resolve_token(self, cursor, token_hash):
        if token_hash == _hasher(TOKEN):
            return _Client(id=1, label="adapter-test")
        return None

    def lock(self, cursor, client_id):
        # Taken by `transaction` in this double; the SQL book locks here.
        return None

    def for_client(self, cursor, client_id):
        return self.state

    def reset(self, cursor, client_id):
        self.state = MemoryState(client=self.state.client)

E404_BODY = {"error": "E404", "mensaje": "ruta desconocida"}
E001_BODY = {"error": "E001", "mensaje": "no autorizado"}
E100_BODY = {"error": "E100", "mensaje": "json inválido"}
E102_BODY = {"error": "E102", "mensaje": "falta m"}
E104_BODY = {"error": "E104", "mensaje": "segmento vacío"}
OK_BODY = {"ok": True}
PONG_BODY = {"pong": True}


def _equal(expected, obtained) -> bool:
    """Compares parsed JSON structures; object key order does not matter."""
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


def _decode(response):
    raw = response.read()
    if not raw:
        return ""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


def load_examples() -> list[dict]:
    records = []
    with EXAMPLES_PATH.open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                records.append(json.loads(line))
    return records


class Harness:
    """Starts the adapter on loopback port 0 and drives it with urllib."""

    def __init__(self, configuration=None):
        self.book = MemoryBook(MemoryState(client="adapter-test"))
        self.server = create_server(
            "127.0.0.1", 0, self.book, _hasher, configuration
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
        raw_body=None,
        content_type=None,
    ):
        data = None
        if raw_body is not None:
            data = raw_body.encode("utf-8")
        elif json_body is not None:
            data = json.dumps(json_body).encode("utf-8")

        headers = {}
        if auth is not None:
            headers["Authorization"] = auth
        if content_type is not None:
            headers["Content-Type"] = content_type

        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, _decode(response)
        except urllib.error.HTTPError as error:
            return error.code, _decode(error)


def _recorded_configuration() -> Configuration:
    """Configuration whose control strategy matches the recorded evidence.

    The control formula is unknown; the explorer owns it. The recorded
    evidence is enough for this test: a step that answered E202 means the
    original rejected that control, and any other answer (2xx or a later
    business error) means it accepted it. The strategy is keyed by message,
    which is how the recorded evidence pins it.
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


# Every row is (name, method, path, expected status, expected body) plus the
# optional request arguments passed to ``Harness.call``.
CASES = [
    # --- route first: unknown routes are E404 even without a token --------
    {
        "name": "GET / is E404",
        "method": "GET",
        "path": "/",
        "status": 404,
        "body": E404_BODY,
    },
    {
        "name": "GET /nope is E404 (no token)",
        "method": "GET",
        "path": "/nope",
        "status": 404,
        "body": E404_BODY,
    },
    {
        "name": "POST /nope is E404",
        "method": "POST",
        "path": "/nope",
        "status": 404,
        "body": E404_BODY,
        "json_body": {},
    },
    {
        "name": "GET /msg is E404",
        "method": "GET",
        "path": "/msg",
        "status": 404,
        "body": E404_BODY,
    },
    {
        "name": "PUT /msg is E404",
        "method": "PUT",
        "path": "/msg",
        "status": 404,
        "body": E404_BODY,
        "json_body": {},
    },
    {
        "name": "DELETE /msg is E404",
        "method": "DELETE",
        "path": "/msg",
        "status": 404,
        "body": E404_BODY,
    },
    {
        "name": "PATCH /msg is E404",
        "method": "PATCH",
        "path": "/msg",
        "status": 404,
        "body": E404_BODY,
        "json_body": {},
    },
    {
        "name": "OPTIONS /msg is E404",
        "method": "OPTIONS",
        "path": "/msg",
        "status": 404,
        "body": E404_BODY,
    },
    {
        "name": "HEAD /msg is E404 with empty body",
        "method": "HEAD",
        "path": "/msg",
        "status": 404,
        "body": "",
    },
    {
        "name": "POST /msg/ trailing slash is E404",
        "method": "POST",
        "path": "/msg/",
        "status": 404,
        "body": E404_BODY,
        "auth": AUTH,
        "json_body": {"m": "PING;64"},
    },
    {
        "name": "GET /msg?x=1 is E404",
        "method": "GET",
        "path": "/msg?x=1",
        "status": 404,
        "body": E404_BODY,
    },
    {
        "name": "POST /health is E404",
        "method": "POST",
        "path": "/health",
        "status": 404,
        "body": E404_BODY,
        "auth": AUTH,
        "json_body": {},
    },
    {
        "name": "GET /__reset is E404",
        "method": "GET",
        "path": "/__reset",
        "status": 404,
        "body": E404_BODY,
        "auth": AUTH,
    },
    # --- auth on the three known routes ----------------------------------
    {
        "name": "POST /msg without token is E001",
        "method": "POST",
        "path": "/msg",
        "status": 401,
        "body": E001_BODY,
        "json_body": {"m": "PING;64"},
    },
    {
        "name": "POST /msg with wrong token is E001",
        "method": "POST",
        "path": "/msg",
        "status": 401,
        "body": E001_BODY,
        "auth": "Bearer wrong-token",
        "json_body": {"m": "PING;64"},
    },
    {
        "name": "POST /msg with token but no Bearer prefix is E001",
        "method": "POST",
        "path": "/msg",
        "status": 401,
        "body": E001_BODY,
        "auth": TOKEN,
        "json_body": {"m": "PING;64"},
    },
    {
        "name": "POST /msg with Bearer and no token is E001",
        "method": "POST",
        "path": "/msg",
        "status": 401,
        "body": E001_BODY,
        "auth": "Bearer",
        "json_body": {"m": "PING;64"},
    },
    {
        "name": "GET /health without token is E001",
        "method": "GET",
        "path": "/health",
        "status": 401,
        "body": E001_BODY,
    },
    {
        "name": "GET /health with wrong token is E001",
        "method": "GET",
        "path": "/health",
        "status": 401,
        "body": E001_BODY,
        "auth": "Bearer wrong-token",
    },
    {
        "name": "POST /__reset without token is E001",
        "method": "POST",
        "path": "/__reset",
        "status": 401,
        "body": E001_BODY,
    },
    # --- the two known `{"ok": true}` bodies -----------------------------
    {
        "name": "GET /health with token is 200 ok",
        "method": "GET",
        "path": "/health",
        "status": 200,
        "body": OK_BODY,
        "auth": AUTH,
    },
    {
        "name": "POST /__reset with token is 200 ok",
        "method": "POST",
        "path": "/__reset",
        "status": 200,
        "body": OK_BODY,
        "auth": AUTH,
    },
    # --- /msg body, in the measured order --------------------------------
    {
        "name": "POST /msg with no body is E100",
        "method": "POST",
        "path": "/msg",
        "status": 400,
        "body": E100_BODY,
        "auth": AUTH,
    },
    {
        "name": "POST /msg with malformed JSON is E100",
        "method": "POST",
        "path": "/msg",
        "status": 400,
        "body": E100_BODY,
        "auth": AUTH,
        "raw_body": "{",
    },
    {
        "name": "POST /msg with {} is E102",
        "method": "POST",
        "path": "/msg",
        "status": 400,
        "body": E102_BODY,
        "auth": AUTH,
        "json_body": {},
    },
    {
        "name": "POST /msg with non-string m is E102",
        "method": "POST",
        "path": "/msg",
        "status": 400,
        "body": E102_BODY,
        "auth": AUTH,
        "json_body": {"m": 5},
    },
    # --- content-type is ignored and the domain path is reached ----------
    {
        "name": "POST /msg sent as text/plain is accepted",
        "method": "POST",
        "path": "/msg",
        "status": 200,
        "body": PONG_BODY,
        "auth": AUTH,
        "raw_body": '{"m":"PING;64"}',
        "content_type": "text/plain",
    },
    {
        "name": "POST /msg PING;64 is 200 pong",
        "method": "POST",
        "path": "/msg",
        "status": 200,
        "body": PONG_BODY,
        "auth": AUTH,
        "json_body": {"m": "PING;64"},
    },
    {
        "name": "POST /msg PING;64; is E104",
        "method": "POST",
        "path": "/msg",
        "status": 400,
        "body": E104_BODY,
        "auth": AUTH,
        "json_body": {"m": "PING;64;"},
    },
]


def log_carries_no_client_text(harness: Harness) -> bool:
    """The process log must carry neither the token nor arbitrary client text.

    A message without `;` used to be logged whole, because the command label
    was whatever came before the first separator. Caught in review; this keeps
    it from coming back.
    """
    marker = "SECRETO-DEL-CLIENTE"
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        harness.call("POST", "/msg", auth=AUTH, json_body={"m": marker})
        harness.call("POST", "/msg", auth=AUTH, json_body={"m": marker + ";64"})
        harness.call("GET", "/health", auth=AUTH)
    output = captured.getvalue()
    if not output.strip():
        print("FAIL the adapter wrote no log line at all")
        return False
    leaked = [text for text in (marker, TOKEN) if text in output]
    if leaked:
        print(f"FAIL the log line leaked {leaked[0][:12]!r}")
        return False
    print("OK   the process log carries neither the token nor the raw message")
    return True


def replay_examples(harness: Harness) -> bool:
    """Resets and replays the 20 recorded examples in order over the adapter."""
    status, body = harness.call("POST", "/__reset", auth=AUTH)
    if status != 200 or body != OK_BODY:
        print(f"FAIL replay reset: status {status}, body {body!r}")
        return False

    records = load_examples()
    matches = 0
    for record in records:
        message = record["request"]["m"]
        status, body = harness.call(
            "POST", "/msg", auth=AUTH, json_body={"m": message}
        )
        expected_status = record["status"]
        expected_body = record["response"]
        if status == expected_status and _equal(expected_body, body):
            matches += 1
            print(f"OK   replay step {record['n']:2d}: {status} {body!r}")
        else:
            print(
                f"FAIL replay step {record['n']:2d}: "
                f"got {status} {body!r}, "
                f"expected {expected_status} {expected_body!r}"
            )
    print(f"{matches} de {len(records)}")
    return matches == len(records)


def main() -> int:
    harness = Harness(_recorded_configuration())
    failures = []
    try:
        for case in CASES:
            kwargs = {
                key: case[key]
                for key in ("auth", "json_body", "raw_body", "content_type")
                if key in case
            }
            status, body = harness.call(case["method"], case["path"], **kwargs)
            ok = (
                status == case["status"]
                and _equal(case["body"], body)
            )
            if not ok:
                failures.append(case["name"])
            print(
                f"{'OK  ' if ok else 'FAIL'} {case['name']}: "
                f"status {status}, body {body!r}"
            )

        replay_ok = replay_examples(harness)
        log_ok = log_carries_no_client_text(harness)
    finally:
        harness.close()

    if failures or not replay_ok or not log_ok:
        print(
            f"FAILED: {len(failures)} frontier case(s), "
            f"replay={'OK' if replay_ok else 'FAIL'} "
            f"log={'OK' if log_ok else 'FAIL'}"
        )
        return 1
    print("all adapter cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
