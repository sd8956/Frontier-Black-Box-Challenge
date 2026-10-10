"""Explorer runner and, for now, control solver.

It claims no formula: it measures against the oracle and records every request.
The accepted control is searched by scanning candidates because the oracle only
answers yes or no per candidate.
"""

from __future__ import annotations

try:
    from .log import (
        InMemoryLog,
        ReadOnlyLog,
        is_control_accepted,
        payload_of,
    )
except ImportError:  # run as a standalone script
    from log import (  # type: ignore
        InMemoryLog,
        ReadOnlyLog,
        is_control_accepted,
        payload_of,
    )

SEQ = "control"
PING_ANCHOR = 64
CONSULTA_ANCHORS = {
    "CONSULTA;AC-0001": 14,
    "CONSULTA;AC-0002": 11,
    "CONSULTA;AC-0009": 87,
}


class InfrastructureLimit(RuntimeError):
    """The original (or its infrastructure) flagged rate-limit. The plan stops."""

    def __init__(self, payload, message=None):
        self.payload = payload
        super().__init__(
            message
            or "infrastructure response (rate-limit) to %r; stopping the plan"
            % (payload,)
        )


def _candidate_order(known):
    order = []
    if known:
        for control in known:
            if isinstance(control, int) and 0 <= control <= 99 and control not in order:
                order.append(control)
    for control in range(100):
        if control not in order:
            order.append(control)
    return order


def resolve_control(
    payload,
    seq,
    base_hypothesis,
    client,
    log,
    known_candidates=None,
):
    """Searches for the accepted control of ``payload``.

    Order: first the known candidates of sibling payloads, then the rest of
    ``0..99``. It stops at the first accepted one. It returns
    ``(control | None, requests_cost)``. It records every request, one per line,
    with the candidate inside the hypothesis.

    If it reuses an already recorded measurement, it spends no requests: cost 0.
    """
    cached = log.accepted_control(payload)
    if cached is not None:
        return cached, 0

    cost = 0
    for control in _candidate_order(known_candidates):
        message = "%s;%d" % (payload, control)
        hypothesis = "%s (candidato %d)" % (base_hypothesis, control)
        response = client.post_msg(message)
        cost += 1

        if response.get("infra"):
            # Infrastructure is not evidence: it is recorded and we stop.
            log.record(
                seq=seq,
                x_req=response.get("x_req"),
                hipotesis=hypothesis,
                request={"m": message},
                status=response.get("status"),
                response=response.get("body"),
                conclusion="respuesta de infraestructura; no es evidencia",
            )
            raise InfrastructureLimit(payload)

        accepted = is_control_accepted(response.get("body"))
        conclusion = None
        if accepted:
            conclusion = "control aceptado para %s: %d" % (payload, control)
        log.record(
            seq=seq,
            x_req=response.get("x_req"),
            hipotesis=hypothesis,
            request={"m": message},
            status=response.get("status"),
            response=response.get("body"),
            conclusion=conclusion,
        )
        if accepted:
            return control, cost
    return None, cost


def _command(payload):
    return payload.split(";")[0]


def _controls_by_command(existing):
    """Already known controls, grouped by command, including the anchors."""
    result = {"PING": [PING_ANCHOR]}
    for payload, control in CONSULTA_ANCHORS.items():
        result.setdefault("CONSULTA", []).append(control)
    for payload, control in existing.items():
        result.setdefault(_command(payload), []).append(control)
    for command, controls in result.items():
        seen = []
        for control in controls:
            if control not in seen:
                seen.append(control)
        result[command] = seen
    return result


def _sibling_candidates(payload, existing, controls_by_command):
    candidates = list(controls_by_command.get(_command(payload), []))
    own = existing.get(payload)
    if own is not None:
        candidates = [c for c in candidates if c != own]
    return candidates


def _plan():
    """First probe plan. Each hypothesis names what it separates."""
    return [
        {
            "payload": "PING",
            "hypothesis": "baseline: confirma que el arnés reproduce el 64 registrado antes de gastar en el resto",
            "anchor": PING_ANCHOR,
        },
        {
            "payload": "CONSULTA;AC-0001",
            "hypothesis": "confirma que el arnés reproduce el 14 registrado",
            "anchor": 14,
        },
        {
            "payload": "CONSULTA;A",
            "hypothesis": "¿el control depende de la longitud del argumento? contraste con CONSULTA;AA",
        },
        {
            "payload": "CONSULTA;AA",
            "hypothesis": "¿el control depende de la longitud del argumento? contraste con CONSULTA;A",
        },
        {
            "payload": "CONSULTA;AB",
            "hypothesis": "¿la función es independiente del orden? contraste con CONSULTA;BA",
        },
        {
            "payload": "CONSULTA;BA",
            "hypothesis": "¿la función es independiente del orden? contraste con CONSULTA;AB",
        },
        {
            "payload": "CONSULTA;1",
            "hypothesis": "¿sube 1 por unidad (suma de dígitos)? solo si AB/BA coinciden",
            "condition": "order_independent",
        },
        {
            "payload": "CONSULTA;2",
            "hypothesis": "¿sube 1 por unidad (suma de dígitos)? solo si AB/BA coinciden",
            "condition": "order_independent",
        },
        {
            "payload": "CONSULTA;3",
            "hypothesis": "¿sube 1 por unidad (suma de dígitos)? solo si AB/BA coinciden",
            "condition": "order_independent",
        },
        {
            "payload": "CONSULTA;AC-0002",
            "hypothesis": "ancla 11 de los ejemplos para ampliar el ajuste",
            "anchor": 11,
        },
        {
            "payload": "CONSULTA;AC-0009",
            "hypothesis": "ancla 87 de los ejemplos para ampliar el ajuste",
            "anchor": 87,
        },
    ]


def _summarize(measurements, stopped, ok):
    lines = ["== Control plan summary =="]
    for med in measurements:
        suffix = " [%s]" % med["note"] if med.get("note") else ""
        lines.append(
            "- %s: control=%s cost=%s | %s%s"
            % (
                med["payload"],
                med["control"],
                med["cost"],
                med["hypothesis"],
                suffix,
            )
        )
    if stopped:
        lines.append(
            "PLAN STOPPED at %s: %s" % (stopped["payload"], stopped["reason"])
        )
    lines.append("Result: %s" % ("OK" if ok else "INCOMPLETE"))
    return "\n".join(lines)


def control_plan(client, log, dry_run=False):
    """Runs the first probe plan.

    It reads what is already recorded and reuses any accepted control, so the
    plan is resumable. With ``dry_run=True`` it does not write: it wraps the log
    in a read-only layer. It returns a report and prints it.
    """
    effective = log if not dry_run else ReadOnlyLog(log)
    existing = effective.accepted_controls()
    controls_by_command = _controls_by_command(existing)

    measurements = []
    stopped = None
    ok = True
    order_independent = None
    control_ab = None
    control_ba = None

    for step in _plan():
        payload = step["payload"]

        if step.get("condition") == "order_independent" and order_independent is not True:
            measurements.append(
                {
                    "payload": payload,
                    "control": None,
                    "cost": 0,
                    "hypothesis": step["hypothesis"],
                    "note": "skipped: AB/BA did not match",
                }
            )
            continue

        known = _sibling_candidates(payload, existing, controls_by_command)
        if step.get("anchor") is not None:
            known = [step["anchor"]] + [c for c in known if c != step["anchor"]]

        try:
            control, cost = resolve_control(
                payload,
                SEQ,
                step["hypothesis"],
                client,
                effective,
                known_candidates=known,
            )
        except InfrastructureLimit as limit:
            ok = False
            stopped = {"payload": limit.payload, "reason": "rate-limit"}
            measurements.append(
                {
                    "payload": payload,
                    "control": None,
                    "cost": None,
                    "hypothesis": step["hypothesis"],
                    "note": "stopped by rate-limit",
                }
            )
            break

        measurements.append(
            {
                "payload": payload,
                "control": control,
                "cost": cost,
                "hypothesis": step["hypothesis"],
            }
        )

        if step.get("anchor") is not None:
            if control is None:
                ok = False
                stopped = {
                    "payload": payload,
                    "reason": "expected anchor %r was not found"
                    % (step["anchor"],),
                }
                break
            if control != step["anchor"]:
                ok = False
                stopped = {
                    "payload": payload,
                    "reason": "the harness does not reproduce the record: expected %r and measured %r"
                    % (step["anchor"], control),
                }
                break

        if payload == "CONSULTA;AB":
            control_ab = control
        elif payload == "CONSULTA;BA":
            control_ba = control
            if control_ab is not None and control_ba is not None:
                order_independent = control_ab == control_ba
                measurements.append(
                    {
                        "payload": "CONSULTA;AB vs CONSULTA;BA",
                        "control": "%s vs %s" % (control_ab, control_ba),
                        "cost": 0,
                        "hypothesis": (
                            "same value => order-independent function "
                            "(per-character sum); different => position matters"
                        ),
                    }
                )

    summary = _summarize(measurements, stopped, ok)
    print(summary)
    return {
        "ok": ok,
        "stopped": stopped,
        "measurements": measurements,
        "order_independent": order_independent,
        "summary": summary,
    }


__all__ = [
    "InfrastructureLimit",
    "control_plan",
    "resolve_control",
    "InMemoryLog",
    "payload_of",
    "is_control_accepted",
]
