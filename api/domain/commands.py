"""Domain commands.

One handler per command, each declaring its arity and its effect. The handler
receives the arguments (the middle fields of the message) and an `Environment`
with the state port and the domain configuration, and returns a `Result`.

The arity does not include the command or the control: it is the number of
middle fields. `PING;64` has 0 arguments; `ALTA;Lucia;82`, 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from api.domain.state import Annulment
from api.domain.result import (
    Annulled,
    InvalidArity,
    AccountCreated,
    AccountNotFound,
    LimitExceeded,
    OperationAccepted,
    OperationNotFound,
    Pong,
    Result,
    Balance,
    InsufficientBalance,
    AlreadyAnnulled,
)

if TYPE_CHECKING:
    # For typing only; importing `pipeline` at runtime would create a cycle.
    from api.domain.pipeline import Environment


def _to_int(text: str) -> int | None:
    """Converts an amount from text to a number, or `None` if it is not an integer.

    Assumption: wire amounts are integers. The contract table has no result for
    a malformed amount and no example brings one. `InvalidArity` is chosen as
    the closest error ("malformed message") and is marked as an assumption, not
    as a rule.
    """
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Command:
    """A command: its uppercase name, its arity and its effect."""

    name: str
    arity: int
    handle: Callable[[list[str], "Environment"], Result]


COMMANDS: dict[str, Command] = {}


def _register(name: str, arity: int):
    def decorator(function):
        COMMANDS[name] = Command(name=name, arity=arity, handle=function)
        return function

    return decorator


# --- handlers ------------------------------------------------------------

@_register("PING", 0)
def _ping(arguments: list[str], env: "Environment") -> Result:
    """No arguments. Example 1: `PING;64` -> Pong."""
    return Pong()


@_register("ALTA", 1)
def _create_account(arguments: list[str], env: "Environment") -> Result:
    """One name. Example 3: `ALTA;Lucia;82` -> CuentaCreada(AC-0001)."""
    return AccountCreated(env.state.create_account(arguments[0]))


@_register("CONSULTA", 1)
def _query(arguments: list[str], env: "Environment") -> Result:
    """One account id. Example 5: `CONSULTA;AC-0001;14` -> Saldo.

    Example 15: `CONSULTA;AC-0009;87` -> CuentaInexistente.
    """
    account_id = arguments[0]
    balance = env.state.read_balance(account_id)
    if balance is None:
        return AccountNotFound()
    return Balance(account_id=account_id, amount=balance)


@_register("DEPOSITO", 2)
def _deposit(arguments: list[str], env: "Environment") -> Result:
    """One account id and one amount. Example 6: `DEPOSITO;AC-0001;250000;86`."""
    account_id, amount_text = arguments
    amount = _to_int(amount_text)
    if amount is None:
        return InvalidArity()
    # Assumption: depositing into a nonexistent account is CuentaInexistente.
    # No example brings one; the closest table result is E401.
    if env.state.read_balance(account_id) is None:
        return AccountNotFound()
    return OperationAccepted(env.state.deposit(account_id, amount))


@_register("TRANSFER", 3)
def _transfer(arguments: list[str], env: "Environment") -> Result:
    """Source, destination and amount.

    Example 7: `TRANSFER;AC-0001;AC-0002;40000;86` -> OperacionAceptada.
    Example 9: `TRANSFER;AC-0001;AC-0002;120000;55` -> LimiteExcedido.
    Example 16: `TRANSFER;AC-0002;AC-0001;50000;23` -> SaldoInsuficiente.
    """
    source, destination, amount_text = arguments
    amount = _to_int(amount_text)
    if amount is None:
        return InvalidArity()
    source_balance = env.state.read_balance(source)
    destination_balance = env.state.read_balance(destination)
    if source_balance is None or destination_balance is None:
        return AccountNotFound()
    # Measured against version 2: the cap is 50000 cents, it is per operation
    # (three consecutive 40000 transfers were all accepted), and it does not
    # apply to deposits. 50000 is accepted and 50001 is rejected.
    if amount > env.operation_limit:
        return LimitExceeded()
    if source_balance < amount:
        return InsufficientBalance()
    return OperationAccepted(env.state.transfer(source, destination, amount))


@_register("ANULA", 1)
def _annul(arguments: list[str], env: "Environment") -> Result:
    """One operation id.

    Example 13: `ANULA;OP-0003;66` -> Anulada.
    Example 14: `ANULA;OP-0003;66` (again) -> YaAnulada.
    Version 2: an id that does not exist -> OperacionInexistente (404 E402).
    """
    operation_id = arguments[0]
    result = env.state.annul(operation_id)
    if result is Annulment.ANNULLED:
        return Annulled(operation_id=operation_id)
    if result is Annulment.NOT_FOUND:
        return OperationNotFound()
    # `ALREADY_ANNULLED` covers two cases in the SQL port, which cannot tell
    # them apart from the guarded update alone: an id that exists and was
    # already annulled, and one that does not exist. Version 2 answers the
    # first with "ya anulada" and the second with 404 E402; one read settles
    # which one this is.
    if env.state.operation_exists(operation_id):
        return AlreadyAnnulled()
    return OperationNotFound()
