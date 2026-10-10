"""In-memory state adapter.

Implements the `api.domain.state.State` port without a database, for the tests.
It is a single-client adapter: the instance already represents the
authenticated client and its internal key is only `account_id`, because there
is no other client to collide with inside this instance.

The two counters (`AC-` and `OP-`) are per instance/client, they start at 1 and
advance only when the operation succeeds. Since the domain calls the port only
on the success path, an error does not consume an id.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.domain.state import Annulment, State


@dataclass
class _Account:
    name: str
    balance: int = 0


@dataclass
class _Operation:
    kind: str
    amount: int
    # A deposit fills only `credited`; a transfer fills both.
    debited: str | None = None
    credited: str | None = None
    annulled: bool = False


@dataclass
class MemoryState(State):
    """In-memory state of a single client."""

    client: str = "test"
    _accounts: dict[str, _Account] = field(default_factory=dict)
    _operations: dict[str, _Operation] = field(default_factory=dict)
    _next_account: int = 1
    _next_operation: int = 1

    # --- accounts --------------------------------------------------------

    def create_account(self, name: str) -> str:
        account_id = f"AC-{self._next_account:04d}"
        self._next_account += 1
        self._accounts[account_id] = _Account(name=name)
        return account_id

    def read_balance(self, account_id: str) -> int | None:
        account = self._accounts.get(account_id)
        return None if account is None else account.balance

    # --- operations ------------------------------------------------------

    def deposit(self, account_id: str, amount: int) -> str:
        operation_id = self._new_operation()
        self._operations[operation_id] = _Operation(
            kind="DEPOSITO", amount=amount, credited=account_id
        )
        self._accounts[account_id].balance += amount
        return operation_id

    def transfer(self, source: str, destination: str, amount: int) -> str:
        operation_id = self._new_operation()
        self._operations[operation_id] = _Operation(
            kind="TRANSFER", amount=amount, debited=source, credited=destination
        )
        self._accounts[source].balance -= amount
        self._accounts[destination].balance += amount
        return operation_id

    def operation_exists(self, operation_id: str) -> bool:
        return operation_id in self._operations

    def annul(self, operation_id: str) -> Annulment:
        operation = self._operations.get(operation_id)
        # Version 2 distinguishes the two cases: a nonexistent operation is
        # `NOT_FOUND` (the wire answers 404 E402), an existing but already
        # annulled one is `ALREADY_ANNULLED` ("ya anulada").
        if operation is None:
            return Annulment.NOT_FOUND
        if operation.annulled:
            return Annulment.ALREADY_ANNULLED
        self._revert(operation)
        operation.annulled = True
        return Annulment.ANNULLED

    # --- internal --------------------------------------------------------

    def _new_operation(self) -> str:
        operation_id = f"OP-{self._next_operation:04d}"
        self._next_operation += 1
        return operation_id

    def _revert(self, operation: _Operation) -> None:
        """Undoes the operation effect; annulling returning money lives here."""
        if operation.credited is not None:
            self._accounts[operation.credited].balance -= operation.amount
        if operation.debited is not None:
            self._accounts[operation.debited].balance += operation.amount
