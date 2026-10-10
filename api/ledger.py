"""The book: the only module that writes SQL.

It implements the `api.domain.state.State` port against Postgres and keeps the
per-client bookkeeping: accounts, operations and the two per-client counters.
The domain does not know SQL; the adapter does not know SQL either. Every
statement lives here.

Identity and isolation
----------------------
A row of `tokens` is a client. `resolve_token` returns the still-valid row for
a hash, and every other statement carries `client_id` in its `where`, so an
account id of another client is unknown here, not forbidden. `reset` deletes
only the rows of one `client_id`; it never truncates and never touches another
client.

Connection and role
-------------------
`Book.transaction` opens one connection per request: this is the seam where a
connection pool goes later, and it is deliberate that concurrent requests do
not share a connection. The process connects with the server role, which
bypasses row level security: the RLS policies in the migration exist to close
the Data API path (which enters as `anon`), not to defend against this process.
No query here relies on RLS.

Time budget
-----------
Each transaction sets a transaction-local `statement_timeout` of 4 seconds, and
`lock` sets a transaction-local `lock_timeout` of the same 4 seconds before
taking the per-client advisory lock. The client gives up at 5 seconds, so
nothing can hang past the budget. The token is re-read inside the transaction
by `resolve_token`, so a token revoked mid-request does not commit.

Failures
--------
Any `psycopg2` error (a connection that does not open, a lock wait that hits
`lock_timeout`, a statement that fails) surfaces as `BookUnavailable`, so the
adapter can answer 503 knowing that nothing was committed. Nothing else is
swallowed.

Counters
--------
`AC-%04d` and `OP-%04d`, one pair of counters per client, starting at 1 and
advancing only on success. The counter row may not exist yet: it is created
with `on conflict do nothing` and then allocated with a single
`update ... returning`. A failure rolls the whole transaction back, so it does
not consume an id.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator

import psycopg2

from api.domain.state import Annulment, State

# The design caps the lock wait at 4 seconds; the client gives up at 5.
DEFAULT_LOCK_TIMEOUT_MS = 4000

# Nothing may hang past the same budget.
STATEMENT_TIMEOUT_MS = 4000

# The process itself is expected to answer within 5 seconds per step.
CONNECT_TIMEOUT_S = 4

# The local name in `.env` is `DB_URL`; `DATABASE_URL` is the deployment name.
DSN_VARIABLES = ("DATABASE_URL", "DB_URL")


class BookUnavailable(RuntimeError):
    """Nothing was committed: the database or the per-client lock failed."""


@dataclass(frozen=True)
class Client:
    """A resolved, still valid token row. The row is the client."""

    id: int
    label: str


@dataclass(frozen=True)
class TokenRow:
    """One token row for the registry. It never carries the hash or secret."""

    id: int
    label: str
    created_by: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class Revocation(Enum):
    """Outcome of a guarded revoke: it decides the policy in the registry."""

    REVOKED = "revoked"
    LAST_VALID = "last_valid"
    NOT_FOUND = "not_found"


# The columns the registry may see. `token_hash` is deliberately absent: the
# list and the registry never read it back. Only `resolve_token` looks it up.
_TOKEN_COLUMNS = "id, label, created_by, created_at, expires_at, revoked_at"


def _token_row(row: Any) -> TokenRow:
    """Builds a `TokenRow` from one `select` of `_TOKEN_COLUMNS`."""
    return TokenRow(
        id=int(row[0]),
        label=str(row[1]),
        created_by=str(row[2]),
        created_at=row[3],
        expires_at=row[4],
        revoked_at=row[5],
    )


def dsn_from_environment() -> str:
    """Returns the DSN from `DATABASE_URL`, then from `DB_URL`.

    Raises `BookUnavailable` when neither is set. The value is never printed,
    logged or included in an exception message.
    """
    for name in DSN_VARIABLES:
        value = os.environ.get(name)
        if value:
            return value
    raise BookUnavailable(
        "neither DATABASE_URL nor DB_URL is set"
    )


def connect(dsn: str | None = None) -> Any:
    """Opens a `psycopg2` connection with a 4-second connect timeout.

    The connection is not autocommit: the caller owns the transaction boundary.
    A connection error surfaces as `BookUnavailable`.
    """
    if dsn is None:
        dsn = dsn_from_environment()
    try:
        connection = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_S)
    except psycopg2.Error as error:
        raise BookUnavailable("could not connect to the database") from error
    connection.autocommit = False
    return connection


def _safe_rollback(connection: Any) -> None:
    """Rolls back during cleanup; a close-time error must not mask the cause."""
    try:
        connection.rollback()
    except Exception:  # noqa: BLE001 - cleanup must not raise
        pass


def _safe_close(resource: Any) -> None:
    """Closes during cleanup; a close-time error must not mask the cause."""
    try:
        resource.close()
    except Exception:  # noqa: BLE001 - cleanup must not raise
        pass


class Book:
    """The SQL book. One instance per process; one transaction per request."""

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn

    # --- transactions ----------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Opens a connection, begins, yields a cursor, and closes.

        Commits on a clean exit, rolls back on any exception. Every `psycopg2`
        error becomes `BookUnavailable`, because at that point nothing was
        committed. The connection is not shared between requests.
        """
        connection = connect(self._dsn)
        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(
                "set local statement_timeout = %s", (STATEMENT_TIMEOUT_MS,)
            )
            try:
                yield cursor
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
        except psycopg2.Error as error:
            _safe_rollback(connection)
            raise BookUnavailable("the transaction did not commit") from error
        finally:
            if cursor is not None:
                _safe_close(cursor)
            _safe_close(connection)

    # --- token and lock --------------------------------------------------

    def resolve_token(
        self, cursor: Any, token_hash: str, now: datetime | None = None
    ) -> Client | None:
        """The valid token row for `token_hash`, or `None`.

        Valid means not revoked and `expires_at` in the future, in UTC. The
        caller passes `now` to make the comparison testable; when absent, the
        current UTC time is used.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        cursor.execute(
            "select id, label from tokens "
            "where token_hash = %s and revoked_at is null and expires_at > %s",
            (token_hash, now),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return Client(id=int(row[0]), label=str(row[1]))

    def lock(self, cursor: Any, client_id: int) -> None:
        """Takes the per-client advisory lock, capping the wait at 4 seconds.

        A wait that hits `lock_timeout` raises `psycopg2`'s error, which the
        transaction turns into `BookUnavailable`.
        """
        cursor.execute(
            "set local lock_timeout = %s", (DEFAULT_LOCK_TIMEOUT_MS,)
        )
        cursor.execute("select pg_advisory_xact_lock(%s)", (client_id,))

    def for_client(self, cursor: Any, client_id: int) -> "Ledger":
        """Binds the port to this cursor and this client. One per request."""
        return Ledger(cursor=cursor, client_id=client_id)

    # --- reset and health ------------------------------------------------

    def reset(self, cursor: Any, client_id: int) -> None:
        """Deletes this client's accounts and operations and resets counters.

        It filters by `client_id` and never truncates, so another client is not
        touched. Deleting the operations first keeps the foreign keys honest
        even without the cascade.
        """
        cursor.execute(
            "delete from operations where client_id = %s", (client_id,)
        )
        cursor.execute("delete from accounts where client_id = %s", (client_id,))
        cursor.execute(
            "insert into counters (client_id, next_account, next_operation) "
            "values (%s, 1, 1) "
            "on conflict (client_id) do update "
            "set next_account = 1, next_operation = 1",
            (client_id,),
        )

    def base_responde(self, cursor: Any) -> bool:
        """True when the database answers. Used by the internal admin port."""
        cursor.execute("select 1")
        row = cursor.fetchone()
        return bool(row) and int(row[0]) == 1

    # --- registry: tokens ------------------------------------------------

    def create_token(
        self,
        cursor: Any,
        token_hash: str,
        label: str,
        created_by: str,
        expires_at: datetime,
    ) -> int | None:
        """Inserts a token row and returns its id, or `None` if it exists.

        The conflict is settled by the unique `token_hash`: a repeated secret
        is not an error of the database but a duplicate the registry reports.
        """
        cursor.execute(
            "insert into tokens (token_hash, label, created_by, expires_at) "
            "values (%s, %s, %s, %s) "
            "on conflict (token_hash) do nothing returning id",
            (token_hash, label, created_by, expires_at),
        )
        row = cursor.fetchone()
        return None if row is None else int(row[0])

    def token_by_id(self, cursor: Any, token_id: int) -> TokenRow | None:
        """The token row with that id, revoked or not, or `None`."""
        cursor.execute(
            "select " + _TOKEN_COLUMNS + " from tokens where id = %s",
            (token_id,),
        )
        row = cursor.fetchone()
        return None if row is None else _token_row(row)

    def tokens_by_label(self, cursor: Any, label: str) -> list[TokenRow]:
        """Every token row with that label, revoked or not, oldest first."""
        cursor.execute(
            "select "
            + _TOKEN_COLUMNS
            + " from tokens where label = %s order by id",
            (label,),
        )
        return [_token_row(row) for row in cursor.fetchall()]

    def list_tokens(self, cursor: Any) -> list[TokenRow]:
        """Every token row, oldest first, never the hash nor the secret."""
        cursor.execute(
            "select " + _TOKEN_COLUMNS + " from tokens order by id"
        )
        return [_token_row(row) for row in cursor.fetchall()]

    def count_valid(self, cursor: Any, now: datetime | None = None) -> int:
        """How many tokens are valid now: not revoked and not expired."""
        if now is None:
            now = datetime.now(timezone.utc)
        cursor.execute(
            "select count(*) from tokens "
            "where revoked_at is null and expires_at > %s",
            (now,),
        )
        return int(cursor.fetchone()[0])

    def revoke_token(
        self,
        cursor: Any,
        token_id: int,
        revoked_by: str,
        now: datetime | None = None,
    ) -> Revocation:
        """Revokes one valid token, refusing to leave the registry empty.

        The check and the write are one transaction: the table is locked and
        a single conditional `update` revokes only when more than one valid
        token exists. Two concurrent revocations cannot both take the last
        one, because the second one, under the lock and in a new statement,
        sees the first one committed and its guard fails.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        # Serializes token mutations for the rest of the transaction. Plain
        # `select`s (resolve) do not conflict; inserts and updates wait.
        cursor.execute("lock table tokens in share row exclusive mode")
        cursor.execute(
            "update tokens set revoked_at = %s, revoked_by = %s "
            "where id = %s and revoked_at is null and expires_at > %s "
            "and (select count(*) from tokens "
            "     where revoked_at is null and expires_at > %s) > 1 "
            "returning id",
            (now, revoked_by, token_id, now, now),
        )
        if cursor.fetchone() is not None:
            return Revocation.REVOKED
        # Nothing was revoked: the target was not valid, or it was the last
        # valid one. One more read tells the registry which one it was.
        cursor.execute(
            "select count(*) from tokens "
            "where id = %s and revoked_at is null and expires_at > %s",
            (token_id, now),
        )
        row = cursor.fetchone()
        if row is not None and int(row[0]) > 0:
            return Revocation.LAST_VALID
        return Revocation.NOT_FOUND

    def delete_tokens_by_label(self, cursor: Any, label: str) -> None:
        """Deletes every token row with this label; the cascades do the rest.

        Test and operator cleanup only: the registry and the CLI never delete
        a credential, they revoke it.
        """
        cursor.execute("delete from tokens where label = %s", (label,))


class Ledger(State):
    """The `State` port bound to one cursor and one client.

    Every query and every write carries `client_id`; there is no path that
    reads or modifies another client's rows.
    """

    def __init__(self, cursor: Any, client_id: int):
        self._cursor = cursor
        self._client_id = client_id

    # --- accounts --------------------------------------------------------

    def create_account(self, name: str) -> str:
        """Creates an account and returns the next `AC-%04d` of this client.

        The counters row may not exist: it is created if missing, then the
        allocation is a single `update ... returning`.
        """
        number = self._allocate_account()
        account_id = "AC-%04d" % number
        self._cursor.execute(
            "insert into accounts (client_id, account_id, balance, name) "
            "values (%s, %s, %s, %s)",
            (self._client_id, account_id, 0, name),
        )
        return account_id

    def read_balance(self, account_id: str) -> int | None:
        """The balance of this client's account, or `None` if it is unknown.

        An account of another client is unknown here, not forbidden.
        """
        self._cursor.execute(
            "select balance from accounts "
            "where client_id = %s and account_id = %s",
            (self._client_id, account_id),
        )
        row = self._cursor.fetchone()
        return None if row is None else int(row[0])

    # --- operations ------------------------------------------------------

    def deposit(self, account_id: str, amount: int) -> str:
        """Credits `amount` and returns the next `OP-%04d`.

        The operation stores `kind = 'deposit'` and only the credited account.
        """
        operation_id = "OP-%04d" % self._allocate_operation()
        self._cursor.execute(
            "insert into operations "
            "(client_id, operation_id, kind, amount, debited_account, "
            "credited_account) "
            "values (%s, %s, %s, %s, %s, %s)",
            (
                self._client_id,
                operation_id,
                "deposit",
                amount,
                None,
                account_id,
            ),
        )
        self._cursor.execute(
            "update accounts set balance = balance + %s "
            "where client_id = %s and account_id = %s",
            (amount, self._client_id, account_id),
        )
        return operation_id

    def transfer(self, source: str, destination: str, amount: int) -> str:
        """Debits `source` and credits `destination`; returns the `OP-%04d`.

        The operation stores `kind = 'transfer'` and both accounts.
        """
        operation_id = "OP-%04d" % self._allocate_operation()
        self._cursor.execute(
            "insert into operations "
            "(client_id, operation_id, kind, amount, debited_account, "
            "credited_account) "
            "values (%s, %s, %s, %s, %s, %s)",
            (
                self._client_id,
                operation_id,
                "transfer",
                amount,
                source,
                destination,
            ),
        )
        self._cursor.execute(
            "update accounts set balance = balance - %s "
            "where client_id = %s and account_id = %s",
            (amount, self._client_id, source),
        )
        self._cursor.execute(
            "update accounts set balance = balance + %s "
            "where client_id = %s and account_id = %s",
            (amount, self._client_id, destination),
        )
        return operation_id

    def operation_exists(self, operation_id: str) -> bool:
        """True if this client has that operation, annulled or not.

        The guarded `update` in `annul` returns no row both for an unknown id
        and for an already annulled one, so it cannot tell them apart on its
        own. This read lets the domain split the two version-2 answers while
        the book's existing tests keep pinning `annul`'s behavior.
        """
        self._cursor.execute(
            "select 1 from operations "
            "where client_id = %s and operation_id = %s",
            (self._client_id, operation_id),
        )
        return self._cursor.fetchone() is not None

    def annul(self, operation_id: str) -> Annulment:
        """Annulls an operation and reverts its effect.

        The `update ... where annulled = false returning` decides "annulled
        now" or "already annulled" atomically: a row comes back only when the
        operation exists, is this client's and was not annulled. An unknown or
        already annulled operation is `ALREADY_ANNULLED`; the domain uses
        `operation_exists` to answer 404 E402 for the unknown one (version 2).
        """
        self._cursor.execute(
            "update operations set annulled = true "
            "where client_id = %s and operation_id = %s and annulled = false "
            "returning kind, amount, debited_account, credited_account",
            (self._client_id, operation_id),
        )
        row = self._cursor.fetchone()
        if row is None:
            return Annulment.ALREADY_ANNULLED

        _kind, amount, debited, credited = row
        # Mirror `MemoryState._revert`: a credited account gives the money
        # back, a debited one has it returned.
        if credited is not None:
            self._cursor.execute(
                "update accounts set balance = balance - %s "
                "where client_id = %s and account_id = %s",
                (amount, self._client_id, credited),
            )
        if debited is not None:
            self._cursor.execute(
                "update accounts set balance = balance + %s "
                "where client_id = %s and account_id = %s",
                (amount, self._client_id, debited),
            )
        return Annulment.ANNULLED

    # --- internal --------------------------------------------------------

    def _allocate_account(self) -> int:
        """Reserves the next `AC-` number for this client and returns it."""
        self._create_counters_row()
        self._cursor.execute(
            "update counters set next_account = next_account + 1 "
            "where client_id = %s returning next_account - 1",
            (self._client_id,),
        )
        return int(self._cursor.fetchone()[0])

    def _allocate_operation(self) -> int:
        """Reserves the next `OP-` number for this client and returns it."""
        self._create_counters_row()
        self._cursor.execute(
            "update counters set next_operation = next_operation + 1 "
            "where client_id = %s returning next_operation - 1",
            (self._client_id,),
        )
        return int(self._cursor.fetchone()[0])

    def _create_counters_row(self) -> None:
        """Ensures the counters row exists; the first allocation may find it
        missing."""
        self._cursor.execute(
            "insert into counters (client_id) values (%s) "
            "on conflict (client_id) do nothing",
            (self._client_id,),
        )
