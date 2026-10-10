"""Differential replay: version-1 recordings against the live service.

Incident FR-0153: the provider deployed version 2 without notice and says three
behaviours changed. This module replays exchanges that were recorded against
version 1 and reports every exchange that no longer matches, writing one line
per request to ``experimentos.jsonl``, which is where the incident asks the
changes to be registered.

Two comparisons, because not every recorded exchange can be replayed the same
way:

* **Exact** — the same status and the same body. Only valid for messages whose
  answer cannot depend on mutable state: ``PING`` and the validation errors
  (malformed control, invalid control, unknown command, bad arity).
* **Acceptance** — whether the control was accepted at all. A balance or a
  counter differs on every run, so a state-dependent message cannot be compared
  field by field; what survives is whether the oracle took the control.

Nothing here guesses the hypothesis: the caller states it before the request.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .client import Client, _decode, _header
    from .log import ExperimentLog, is_control_accepted
except ImportError:  # run as a standalone script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from explorer.client import Client, _decode, _header  # type: ignore
    from explorer.log import ExperimentLog, is_control_accepted  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
MATERIAL = ROOT / "material" / "pruebas" / "ejemplos.jsonl"
CORPUS = ROOT / "experimentos.jsonl"

# Validation errors: pure functions of the message, no state involved.
STATELESS_ERRORS = ("E201", "E202", "E301", "E302")

# The recorded frontier probes wrote a placeholder instead of the real token.
AUTH_PLACEHOLDERS = ("<token>", "<oculto>", "Bearer <token>", "Bearer <oculto>")
NO_AUTH = ("<sin header>", "sin header")
FRONTIER_SEQ = ("rutas", "rutas2")
USE_REAL_TOKEN = "<usar-token-real>"
USE_BARE_TOKEN = "<usar-token-sin-prefijo>"


class Tailored:
    """What ``Client`` does not expose: any method or path, a raw body, no auth.

    The frontier probes need exactly that, and they have to be replayed as they
    were recorded or they stop measuring the same thing.
    """

    def __init__(self, client):
        self._client = client

    def send(self, method, path, body=None, raw=False, auth=None, content_type=None):
        client = self._client
        client._rate_limiter.wait()
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = body.encode("utf-8") if raw else json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type or "application/json"
        if auth is not None:
            headers["Authorization"] = auth
        request = urllib.request.Request(
            client.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                status, response_headers, payload = (
                    response.status, dict(response.headers), response.read())
        except urllib.error.HTTPError as error:
            status, response_headers, payload = (
                error.code, dict(error.headers), error.read())
        return {
            "status": int(status),
            "body": _decode(payload),
            "x_req": _header(response_headers, "X-Req"),
            "infra": _header(response_headers, "X-Frontier-Infra") is not None,
        }


def same_value(recorded, live):
    """Strict comparison: ``True`` is not ``1``, key order does not matter."""
    if isinstance(recorded, bool) or isinstance(live, bool):
        return recorded is live
    if isinstance(recorded, (int, float)) and isinstance(live, (int, float)):
        return recorded == live
    if isinstance(recorded, str) and isinstance(live, str):
        return recorded == live
    if isinstance(recorded, list) and isinstance(live, list):
        return len(recorded) == len(live) and all(
            same_value(a, b) for a, b in zip(recorded, live)
        )
    if isinstance(recorded, dict) and isinstance(live, dict):
        if set(recorded) != set(live):
            return False
        return all(same_value(recorded[key], live[key]) for key in recorded)
    return False


def error_code(response):
    return response.get("error") if isinstance(response, dict) else None


def exact_comparison(recorded, live):
    """(matched, conclusion) for a state-independent message."""
    if recorded["status"] == live["status"] and same_value(
        recorded["response"], live["body"]
    ):
        return True, "igual que la version 1"
    return False, "DIFERENTE: la version 1 daba %s %s; la version 2 da %s %s" % (
        recorded["status"],
        json.dumps(recorded["response"], ensure_ascii=False),
        live["status"],
        json.dumps(live["body"], ensure_ascii=False),
    )


def acceptance_comparison(recorded, live):
    """(matched, conclusion) for a message whose body may legitimately differ."""
    was = is_control_accepted(recorded["response"])
    now = is_control_accepted(live["body"])
    if was == now:
        return True, "el control sigue %s (cuerpo no comparable)" % (
            "aceptado" if now else "rechazado",
        )
    return False, "DIFERENTE: el control era %s en la version 1 y es %s en la 2 (%s %s)" % (
        "aceptado" if was else "rechazado",
        "aceptado" if now else "rechazado",
        live["status"],
        json.dumps(live["body"], ensure_ascii=False),
    )


def load_material(path=MATERIAL):
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def load_corpus(path=CORPUS):
    records = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("request"), dict):
            records.append(record)
    return records


def replay(client, log, exchanges, seq, hypothesis, reset_first=False):
    """Send every exchange and log it. Returns one measurement per exchange."""
    if reset_first:
        client.reset()
    measurements = []
    for exchange in exchanges:
        message = exchange["request"]["m"]
        live = client.post_msg(message)
        if exchange.get("compare") == "acceptance":
            matched, conclusion = acceptance_comparison(exchange, live)
        else:
            matched, conclusion = exact_comparison(exchange, live)
        if live.get("infra"):
            conclusion = "respuesta de infraestructura; no es evidencia"
            matched = None
        log.record(
            seq, live.get("x_req"), hypothesis(message),
            {"m": message}, live["status"], live["body"], conclusion,
        )
        measurements.append(
            {"m": message, "recorded": exchange, "live": live,
             "matched": matched, "conclusion": conclusion}
        )
    return measurements


def report(measurements, title):
    differences = [m for m in measurements if m["matched"] is False]
    unknown = [m for m in measurements if m["matched"] is None]
    print("%s: %d exchanges, %d differences" % (title, len(measurements), len(differences)))
    for measurement in differences:
        print("  - %-32s %s" % (measurement["m"], measurement["conclusion"]))
    if unknown:
        print("  (%d answered by infrastructure, not evidence)" % len(unknown))
    return differences


def stateless_exchanges(records, limit=None):
    """Distinct messages whose recorded answer cannot depend on state."""
    seen, out = set(), []
    for record in records:
        request = record.get("request")
        if not isinstance(request, dict):
            continue
        message = request.get("m")
        if not isinstance(message, str):
            continue
        if message in seen:
            continue
        code = error_code(record.get("response"))
        if message.startswith("PING;") or code in STATELESS_ERRORS:
            seen.add(message)
            out.append({"request": {"m": message}, "status": record.get("status"),
                        "response": record.get("response")})
        if limit and len(out) >= limit:
            break
    return out


def auth_for(request, hypothesis="", conclusion=""):
    """What the Authorization header must carry to replay this probe faithfully.

    The recorded ``auth`` field alone is not enough: one probe sent no header
    at all (it says so in its conclusion) and another sent the token without
    the ``Bearer`` prefix (it says so in its hypothesis). Replaying those with
    a valid ``Bearer`` header turns a 401 into a 200 and invents a difference.
    """
    recorded = request.get("auth")
    text = ("%s %s" % (hypothesis or "", conclusion or "")).lower()
    if recorded in NO_AUTH or "sin header" in text or "sin token" in text:
        return {"auth": None}
    if "sin el prefijo" in text or "sin prefijo" in text:
        return {"auth": USE_BARE_TOKEN}
    if recorded is None or recorded in AUTH_PLACEHOLDERS:
        return {"auth": USE_REAL_TOKEN}
    return {"auth": recorded}  # a recorded bogus header: replay it as it was


def frontier_exchanges(records, limit=None):
    """Recorded HTTP frontier probes: method, path, body shape, auth, content type."""
    out = []
    for record in records:
        if record.get("seq") not in FRONTIER_SEQ:
            continue
        request = record.get("request")
        if not isinstance(request, dict) or "method" not in request:
            continue
        hypothesis = record.get("hipotesis")
        hypothesis = hypothesis if isinstance(hypothesis, str) else ""
        conclusion = record.get("conclusion")
        conclusion = conclusion if isinstance(conclusion, str) else ""
        body = request.get("body") if "body" in request else None
        probe = {
            "request": request,
            "record": record,
            "status": record.get("status"),
            "response": record.get("response"),
            "method": request["method"],
            "path": request["path"],
            "body": body,
            "raw": isinstance(body, str),
            "content_type": "text/plain" if "texto plano" in hypothesis else None,
            "hypothesis": hypothesis,
            "auth": USE_REAL_TOKEN,
        }
        probe.update(auth_for(request, hypothesis, conclusion))
        out.append(probe)
        if limit and len(out) >= limit:
            break
    return out


def stateful_exchanges(records, limit=200):
    """Distinct messages whose control acceptance is comparable, not the body."""
    seen, out = set(), []
    for record in records:
        message = record["request"]["m"]
        if message in seen or ";" not in message:
            continue
        seen.add(message)
        out.append({"request": {"m": message}, "status": record.get("status"),
                    "response": record.get("response"), "compare": "acceptance"})
        if limit and len(out) >= limit:
            break
    return out


def main(argv):
    mode = argv[1] if len(argv) > 1 else "examples"
    limit = int(argv[2]) if len(argv) > 2 else None
    client, log = Client(), ExperimentLog()

    if mode == "examples":
        exchanges = [{"request": record["request"], "status": record["status"],
                      "response": record["response"]} for record in load_material()]
        hypothesis = lambda m: (
            "incidente FR-0153: tras el reset, el paso %r sigue dando lo grabado "
            "en la version 1" % m
        )
        measurements = replay(client, log, exchanges, "incidente", hypothesis,
                              reset_first=True)
        differences = report(measurements, "20 ejemplos, version 1 vs 2")
    elif mode == "stateless":
        exchanges = stateless_exchanges(load_corpus(), limit=limit)
        hypothesis = lambda m: (
            "incidente FR-0153: %r no depende del estado, sigue dando lo grabado "
            "en la version 1" % m
        )
        measurements = replay(client, log, exchanges, "incidente", hypothesis)
        differences = report(measurements, "mensajes sin estado, version 1 vs 2")
    elif mode == "frontier":
        exchanges = frontier_exchanges(load_corpus(), limit=limit)
        tailored = Tailored(client)
        measurements = []
        for exchange in exchanges:
            if exchange["auth"] == USE_REAL_TOKEN:
                auth = "Bearer " + client._token
            elif exchange["auth"] == USE_BARE_TOKEN:
                auth = client._token
            else:
                auth = exchange["auth"]
            live = tailored.send(
                exchange["method"], exchange["path"], body=exchange["body"],
                raw=exchange["raw"], auth=auth, content_type=exchange["content_type"],
            )
            matched, conclusion = exact_comparison(exchange, live)
            if live.get("infra"):
                matched, conclusion = None, "respuesta de infraestructura; no es evidencia"
            log.record(
                "incidente", live.get("x_req"),
                "incidente FR-0153: %s sigue respondiendo como en la version 1"
                % (exchange["method"] + " " + exchange["path"]),
                exchange["request"], live["status"], live["body"], conclusion,
            )
            measurements.append({"m": "%s %s" % (exchange["method"], exchange["path"]),
                                 "recorded": exchange, "live": live,
                                 "matched": matched, "conclusion": conclusion})
        differences = report(measurements, "frente HTTP, version 1 vs 2")
    elif mode == "stateful":
        exchanges = stateful_exchanges(load_corpus(), limit=limit or 200)
        hypothesis = lambda m: (
            "incidente FR-0153: el control de %r sigue aceptandose como en la "
            "version 1" % m
        )
        measurements = replay(client, log, exchanges, "incidente", hypothesis)
        differences = report(measurements, "aceptacion de control, version 1 vs 2")
    else:
        print(__doc__)
        return 2
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
