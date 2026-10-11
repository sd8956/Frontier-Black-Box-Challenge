#!/usr/bin/env python3
"""End-to-end measurement of the deployed replacement over its public URL.

The delivery asks for one command and its output, so this prints the evidence
and nothing else. It talks to the public URL over HTTP and never calls the
original service: every expectation below is a recording.

Four blocks, one per claim of the delivery:

1. **Frontier** -- the routes, the bearer and the order they resolve in,
   compared against the original's recorded answers. Each line cites the
   ``id`` of the request in ``experimentos.jsonl`` that measured it.
2. **Fidelity** -- ``POST /__reset`` and then the 20 recorded steps of
   ``tests/api/ejemplos_v2.jsonl``, in order, against the public URL, with the
   judge's own comparison rule: same status, JSON key order ignored, arrays in
   order, scalars identical.
3. **Incident FR-0153** -- the eight steps where version 1 and version 2
   disagree, labelled by the change that explains them. The answers are the
   ones block 2 already observed, so nothing is requested twice.
4. **Declared limit** -- the coverage of the measured control table, which is
   the fidelity cost the specification declares as RIE-1.

The team token is read from the environment and never printed. If the
environment does not carry it, it is read from the repository's `.env`
(`--env-file`), one key at a time, so the command needs no `set -a` and no
`source` and works the same in bash, zsh and fish. The same two lines run in
bash and zsh: `set -a; . ./.env; set +a` also works, but only with the `./`:
zsh's `.` searches `$PATH` when the name has no slash.

Runs with::

    python3 tools/verify_deployment.py --url https://<host>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V1_PATH = ROOT / "material" / "pruebas" / "ejemplos.jsonl"
V2_PATH = ROOT / "tests" / "api" / "ejemplos_v2.jsonl"
CONTROL_TABLE_PATH = ROOT / "api" / "control_table.json"

# --- frontier probes, copied from the original's recorded answers -----------
#
# (method, path, auth, raw body, expected status, expected body, evidence)
# auth is "team", "none" or "foreign".
PROBES = (
    ("GET", "/health", "none", None, 401, {"error": "E001", "mensaje": "no autorizado"}, "2330"),
    ("GET", "/health", "team", None, 200, {"ok": True}, "2331"),
    ("POST", "/health", "team", b"{}", 404, {"error": "E404", "mensaje": "ruta desconocida"}, "2332"),
    ("GET", "/nope", "none", None, 404, {"error": "E404", "mensaje": "ruta desconocida"}, "2335"),
    ("POST", "/__reset", "none", None, 401, {"error": "E001", "mensaje": "no autorizado"}, "2334"),
    ("POST", "/msg", "foreign", b'{"m": "PING;64"}', 401, {"error": "E001", "mensaje": "no autorizado"}, "2326"),
    ("POST", "/msg", "none", b'{"m": "PING;64"}', 401, {"error": "E001", "mensaje": "no autorizado"}, "2327"),
    ("POST", "/msg", "team", b"no-json", 400, {"error": "E100", "mensaje": "json inválido"}, "2315"),
    ("POST", "/msg", "team", b"{}", 400, {"error": "E102", "mensaje": "falta m"}, "2317"),
)

# The eight steps where the two recordings disagree, and the measured change
# that explains each one. Sources: `material/pruebas/ejemplos.jsonl` (v1) and
# `tests/api/ejemplos_v2.jsonl` (v2), both replayed in block 2.
CHANGES = {
    5: ("CAM-1", "saldo en texto decimal"),
    8: ("CAM-1", "saldo en texto decimal"),
    11: ("CAM-1", "saldo en texto decimal"),
    19: ("CAM-1", "saldo en texto decimal"),
    10: ("CAM-2", "tope por transferencia en 50000"),
    13: ("CAM-3", "E402 al anular una operación inexistente"),
    14: ("CAM-3", "E402 al anular una operación inexistente"),
    18: ("CAM-2", "cascada: el contador OP- va uno atrás"),
}


class Probe:
    """One HTTP exchange, kept as the raw text it answered."""

    def __init__(self, status, text):
        self.status = status
        self.text = text
        try:
            self.parsed = json.loads(text)
        except ValueError:
            self.parsed = None

    def canonical(self):
        """The judge's comparison form: JSON canonically ordered, else raw."""
        if self.parsed is None:
            return self.text
        return json.dumps(self.parsed, sort_keys=True)


class Client:
    def __init__(self, base, token, timeout):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method, path, body=None, token="team"):
        request = urllib.request.Request(self.base + path, method=method)
        if token == "team":
            request.add_header("Authorization", "Bearer " + self.token)
        elif token == "foreign":
            request.add_header("Authorization", "Bearer token-que-no-existe")
        if body is not None:
            request.data = body
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as answer:
                return Probe(answer.status, answer.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            return Probe(error.code, error.read().decode("utf-8", "replace"))


def same(observed, status, body):
    """The judge's rule, on one recorded answer."""
    if observed.status != status:
        return False
    return observed.canonical() == json.dumps(body, sort_keys=True)


def load(path):
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def token_from_file(path, key):
    """The one named key of a `KEY=value` file, or None.

    Only the requested key is read: the file is never loaded as an
    environment and no other value can leak into this process. Quotes are
    stripped the way a shell would; everything else is left alone.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            value = value.strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


def brief(value, width=52):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= width else text[: width - 1] + "…"


def frontier(client):
    print("1) FRONTERA  (respuesta del original grabada en experimentos.jsonl)")
    passed = 0
    for method, path, token, body, status, expected, evidence in PROBES:
        observed = client.call(method, path, body, token)
        ok = same(observed, status, expected)
        passed += ok
        who = {"team": "team token", "none": "sin token", "foreign": "token ajeno"}[token]
        print(
            "   %-4s %-9s %-12s -> %s  %-52s %s  [id %s]"
            % (method, path, who, observed.status, brief(observed.parsed, 52), "OK" if ok else "DIFF", evidence)
        )
    print("   %d/%d\n" % (passed, len(PROBES)))
    return passed


def fidelity(client, oracle):
    print("2) FIDELIDAD v2  (POST /__reset y los 20 pasos grabados, en orden)")
    reset = client.call("POST", "/__reset")
    if not same(reset, 200, {"ok": True}):
        print("   reset falló: %s %s\n" % (reset.status, brief(reset.parsed, 60)))
        return 0, {}
    observed = {}
    passed = 0
    for step in oracle:
        answer = client.call("POST", "/msg", json.dumps(step["request"]).encode("utf-8"), "team")
        observed[step["n"]] = answer
        ok = same(answer, step["status"], step["response"])
        passed += ok
        print(
            "   %02d  %-36s %s  %-52s %s"
            % (step["n"], step["request"]["m"], answer.status, brief(answer.parsed, 52), "OK" if ok else "DIFF")
        )
    print("   %d/%d pasos con el mismo status y el mismo cuerpo\n" % (passed, len(oracle)))
    return passed, observed


def incident(v1, v2, observed):
    print("3) INCIDENTE FR-0153  (los 8 pasos donde v1 y v2 difieren)")
    passed = 0
    for old, new in zip(v1, v2):
        if old["status"] == new["status"] and old["response"] == new["response"]:
            continue
        label, reason = CHANGES[new["n"]]
        answer = observed.get(new["n"])
        ok = answer is not None and same(answer, new["status"], new["response"])
        passed += ok
        print(
            "   %02d  %-36s %-6s v1 %s -> v2 %s  %s  %s"
            % (
                new["n"],
                new["request"]["m"],
                label,
                brief([old["status"], old["response"]], 46),
                brief([new["status"], new["response"]], 46),
                reason,
                "OK" if ok else "DIFF",
            )
        )
    print("   %d/%d responden la versión 2\n" % (passed, len(CHANGES)))
    return passed


def declared_limit():
    table = json.loads(CONTROL_TABLE_PATH.read_text(encoding="utf-8"))
    known = [payload for payload, entry in table.items() if entry.get("accept")]
    print("4) LÍMITE DECLARADO  (RIE-1: la fórmula del control no se dedujo)")
    print("   control medido: %d payloads, %d con control aceptado" % (len(table), len(known)))
    print("   los demás se aceptan por defecto: un E202 esperado no llega\n")
    return len(table), len(known)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=os.environ.get("REPLACEMENT_URL"), help="public base URL of the replacement")
    parser.add_argument("--token-env", default="TEAM_TOKEN", help="environment variable holding the team token")
    parser.add_argument(
        "--env-file",
        default=str(ROOT / ".env"),
        help="file read for that variable when the environment does not carry it",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    if not args.url:
        print("falta --url (o REPLACEMENT_URL)")
        return 2
    token = os.environ.get(args.token_env)
    source = "entorno" if token else None
    if not token and args.env_file:
        token = token_from_file(Path(args.env_file), args.token_env)
        source = source or ("%s" % args.env_file)
    if not token:
        print("falta %s: ni en el entorno ni en %s" % (args.token_env, args.env_file))
        return 2

    v1, v2 = load(V1_PATH), load(V2_PATH)
    client = Client(args.url, token, args.timeout)

    print("reemplazo: %s" % args.url.rstrip("/"))
    print("token:     Bearer <team token>  (%s, nunca se imprime)\n" % source)

    started = time.time()
    frontier_ok = frontier(client)
    fidelity_ok, observed = fidelity(client, v2)
    incident_ok = incident(v1, v2, observed)
    payloads, known = declared_limit()

    elapsed = time.time() - started
    rounds = len(PROBES) + 1 + len(v2)
    print("RESULTADO")
    print("   frontera            %d/%d" % (frontier_ok, len(PROBES)))
    print("   fidelidad v2        %d/%d" % (fidelity_ok, len(v2)))
    print("   incidente           %d/%d" % (incident_ok, len(CHANGES)))
    print("   control medido      %d payloads, %d aceptados (RIE-1)" % (payloads, known))
    print("   %d requests en %.1f s" % (rounds, elapsed))
    ok = frontier_ok == len(PROBES) and fidelity_ok == len(v2) and incident_ok == len(CHANGES)
    print("   %s" % ("el reemplazo responde como la versión 2" if ok else "HAY DIFERENCIAS: ver las líneas DIFF"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
