"""State port used by the domain.

The domain performs no I/O and knows no SQL: it talks to this interface and the
adapter (`api/state.py` in memory, `api/ledger.py` against Postgres in another
work unit) decides how it stores.

Isolation: a client is the authenticated token. The key of an account is
(client, account_id); another client's data is neither read nor modified. This
port is already bound to a single client (the adapter fixes it when it is
built), so it does not expose the client as an argument.

Counters: `AC-` and `OP-` per client, they start at 1 and advance only after a
success. The domain does not manage them: the ids are returned by the port on
each successful creation, deposit or transfer, and a failing path does not call
the port, so it does not consume an id.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class Annulment(Enum):
    """Distinguishes the three outcomes of an annulment request.

    `ANNULLED` is "annulled now"; `ALREADY_ANNULLED` is "exists and was
    already annulled"; `NOT_FOUND` is "does not exist".
    """

    ANNULLED = "annulled"
    ALREADY_ANNULLED = "already_annulled"
    NOT_FOUND = "not_found"


class State(ABC):
    """Domain state port."""

    @abstractmethod
    def create_account(self, name: str) -> str:
        """Creates a client account and returns the next `AC-` id."""

    @abstractmethod
    def read_balance(self, account_id: str) -> int | None:
        """Returns the balance of a client account, or `None` if it does not exist."""

    @abstractmethod
    def deposit(self, account_id: str, amount: int) -> str:
        """Credits the amount to an existing account and returns the next `OP-`."""

    @abstractmethod
    def transfer(self, source: str, destination: str, amount: int) -> str:
        """Debits source and credits destination, both existing; returns the `OP-`."""

    @abstractmethod
    def operation_exists(self, operation_id: str) -> bool:
        """True if this client has that operation.

        The in-memory adapter returns `Annulment.NOT_FOUND` from `annul`
        directly, but the SQL one cannot tell an unknown id from an already
        annulled one from the guarded `update` alone (both return no row). This
        read settles the difference for that port without changing the meaning
        its existing tests pin.
        """

    @abstractmethod
    def annul(self, operation_id: str) -> Annulment:
        """Annulls an operation and reverts its effect.

        `ANNULLED` if it was just annulled, `ALREADY_ANNULLED` if it already was
        or if the operation is unknown (the domain uses `operation_exists` to
        tell those two apart), `NOT_FOUND` when the adapter can already prove
        the operation does not exist.
        """
