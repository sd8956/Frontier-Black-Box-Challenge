"""Typed domain results.

One type per outcome. The domain does NOT know HTTP status or JSON keys: each
class carries only the domain datum (ids as text, amounts as numbers) and the
serializer decides how it is written on the wire.

Wire fidelity: the domain keeps the oddities as data, not as text.
`OperationAccepted` does not know the wire says "recieved"; `AccountCreated` and
`Balance` do not know the wire uses "cuentaId" and "cuenta_id" for the same
concept. That translation lives in `api/serializer.py`.
"""

from __future__ import annotations

from dataclasses import dataclass


class Result:
    """Root of all domain outcomes."""


# --- success outcomes ----------------------------------------------------

@dataclass(frozen=True)
class Pong(Result):
    """Pong -> 200 {"pong": true}."""


@dataclass(frozen=True)
class AccountCreated(Result):
    """CuentaCreada(id) -> 201 {"cuentaId": id}."""

    account_id: str


@dataclass(frozen=True)
class Balance(Result):
    """Saldo(id, monto) -> 200 {"saldo": monto, "cuenta_id": id}."""

    account_id: str
    amount: int


@dataclass(frozen=True)
class OperationAccepted(Result):
    """OperacionAceptada(id) -> 200 {"status": "recieved", "operacionId": id}."""

    operation_id: str


@dataclass(frozen=True)
class Annulled(Result):
    """Anulada(id) -> 200 {"ok": true, "anulada": id}."""

    operation_id: str


@dataclass(frozen=True)
class AlreadyAnnulled(Result):
    """YaAnulada -> 200 {"ok": false, "motivo": "ya anulada"}."""


# --- validation errors ---------------------------------------------------

@dataclass(frozen=True)
class EmptySegment(Result):
    """SegmentoVacio -> 400 {"error": "E104", "mensaje": "segmento vacío"}.

    Measured once, on `PING;64;`: the original answers E104 and not the
    control error. Stated as an assumption until it is repeated.
    """


@dataclass(frozen=True)
class MalformedControl(Result):
    """ControlMalFormado -> 400 {"error": "E201", "mensaje": "control mal formado"}."""


@dataclass(frozen=True)
class InvalidControl(Result):
    """ControlInvalido -> 400 {"error": "E202", "mensaje": "control inválido"}."""


@dataclass(frozen=True)
class UnknownCommand(Result):
    """ComandoDesconocido -> 400 {"error": "E301", "mensaje": "comando desconocido "}."""


@dataclass(frozen=True)
class InvalidArity(Result):
    """AridadInvalida -> 400 {"error": "E302", "mensaje": "aridad inválida"}."""


# --- business errors -----------------------------------------------------

@dataclass(frozen=True)
class AccountNotFound(Result):
    """CuentaInexistente -> 404 {"error": "E401", "mensaje": "cuenta inexistente"}."""


@dataclass(frozen=True)
class LimitExceeded(Result):
    """LimiteExcedido -> 422 {"error": "E506", "mensaje": "límite excedido"}."""


@dataclass(frozen=True)
class InsufficientBalance(Result):
    """SaldoInsuficiente -> 422 {"error": "E507", "mensaje": "saldo insuficiente"}."""
