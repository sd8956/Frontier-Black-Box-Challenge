"""Wire serializer.

It is the ONLY place that knows the HTTP status and the JSON keys. The domain
does not know them. It returns the status and an already parsed body structure
(a `dict`), never text: it is impossible for a caller to build a body by
concatenation.

Wire fidelity, kept on purpose and uncorrected:
  - "recieved" is misspelled in the original and stays misspelled;
  - `ALTA` responds `cuentaId` and `CONSULTA` responds `cuenta_id`, two
    different names for the same concept;
  - the unknown-command message has a trailing space inside.
"""

from __future__ import annotations

from api.domain.result import (
    Annulled,
    InvalidArity,
    UnknownCommand,
    InvalidControl,
    MalformedControl,
    AccountCreated,
    AccountNotFound,
    LimitExceeded,
    OperationAccepted,
    Pong,
    Result,
    Balance,
    InsufficientBalance,
    AlreadyAnnulled,
)

# The translation table lives here, in code, not in the database.
# Each branch returns the literal status and body of the contract.
def serialize(result: Result) -> tuple[int, dict]:
    """Translates a domain result to the wire (status, body)."""
    if isinstance(result, Pong):
        return 200, {"pong": True}
    if isinstance(result, AccountCreated):
        return 201, {"cuentaId": result.account_id}
    if isinstance(result, Balance):
        return 200, {"saldo": result.amount, "cuenta_id": result.account_id}
    if isinstance(result, OperationAccepted):
        # "recieved" is the original's oddity. Do not correct.
        return 200, {"status": "recieved", "operacionId": result.operation_id}
    if isinstance(result, Annulled):
        return 200, {"ok": True, "anulada": result.operation_id}
    if isinstance(result, AlreadyAnnulled):
        return 200, {"ok": False, "motivo": "ya anulada"}
    if isinstance(result, MalformedControl):
        return 400, {"error": "E201", "mensaje": "control mal formado"}
    if isinstance(result, InvalidControl):
        return 400, {"error": "E202", "mensaje": "control inválido"}
    if isinstance(result, UnknownCommand):
        # The trailing space is the original's. Do not correct.
        return 400, {"error": "E301", "mensaje": "comando desconocido "}
    if isinstance(result, InvalidArity):
        return 400, {"error": "E302", "mensaje": "aridad inválida"}
    if isinstance(result, AccountNotFound):
        return 404, {"error": "E401", "mensaje": "cuenta inexistente"}
    if isinstance(result, LimitExceeded):
        return 422, {"error": "E506", "mensaje": "límite excedido"}
    if isinstance(result, InsufficientBalance):
        return 422, {"error": "E507", "mensaje": "saldo insuficiente"}
    raise TypeError(f"result with no wire translation: {type(result).__name__}")
