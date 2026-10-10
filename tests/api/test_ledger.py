#!/usr/bin/env python3
"""Offline tests for the SQL book.

No network and no database: a fake cursor records every statement and replays
scripted rows, and a fake connection drives the transaction boundary. It
asserts the things the book promises before any database exists:

  - every data statement carries `client_id`;
  - the ids and the counters come out as `AC-0001` / `OP-0001`;
  - a deposit stores only the credited account and a transfer stores both,
    which is the shape the schema allows;
  - an unknown operation is `ALREADY_ANNULLED`, and annulling reverts the
    balances;
  - `reset` filters by `client_id` and never truncates;
  - `BookUnavailable` is raised when the connection or the lock fails.

It must not require a live database. Prints one line per case and exits with a
non-zero code if anything fails.

Runs with: ``/tmp/dbenv/bin/python tests/api/test_ledger.py``
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

import api.ledger as ledger  # noqa: E402
from api.domain.state import Annulment  # noqa: E402
from api.ledger import (  # noqa: E402
    DEFAULT_LOCK_TIMEOUT_MS,
    DSN_VARIABLES,
    STATEMENT_TIMEOUT_MS,
    Book,
    BookUnavailable,
    Client,
    Ledger,
    dsn_from_environment,
)

CLIENT_ID = 7
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


# --- doubles -------------------------------------------------------------

class FakeCursor:
    """Records statements and replays one scripted row per ``execute``."""

    def __init__(self, results=None, fail_on=()):
        self.statements = []
        self._results = list(results or [])
        self._last = None
        self._fail_on = tuple(fail_on)
        self.closed = False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        for needle in self._fail_on:
            if needle in sql:
                raise psycopg2.OperationalError("scripted failure")
        self._last = self._results.pop(0) if self._results else None

    def fetchone(self):
        return self._last

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = True
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@contextmanager
def patched(target, name, value):
    original = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


def make_ledger(results, client_id=CLIENT_ID):
    cursor = FakeCursor(results)
    return cursor, Ledger(cursor=cursor, client_id=client_id)


def assert_client_id_in(cursor, expected=CLIENT_ID):
    for sql, params in cursor.statements:
        if params is None:
            raise AssertionError("statement without params: %r" % (sql,))
        if expected not in params:
            raise AssertionError(
                "statement does not carry client_id: %r %r" % (sql, params)
            )


# --- cases ---------------------------------------------------------------

TESTS = []


def test(name):
    def decorator(function):
        TESTS.append((name, function))
        return function

    return decorator


@test("create_account returns AC-0001 and carries client_id")
def _t_create():
    cursor, book = make_ledger([None, (1,), None])
    account = book.create_account("Lucia")
    assert account == "AC-0001", account
    sqls = [sql for sql, _ in cursor.statements]
    assert "insert into counters" in sqls[0]
    assert sqls[1].lstrip().startswith("update counters set next_account")
    assert "returning next_account - 1" in sqls[1]
    assert cursor.statements[1][1] == (CLIENT_ID,)
    assert "insert into accounts" in sqls[2]
    assert cursor.statements[2][1] == (CLIENT_ID, "AC-0001", 0, "Lucia")
    assert_client_id_in(cursor)


@test("the counter advances: the second account is AC-0002")
def _t_create_second():
    cursor, book = make_ledger([None, (2,), None])
    assert book.create_account("Ana") == "AC-0002"
    assert_client_id_in(cursor)


@test("read_balance returns the balance, or None when the account is unknown")
def _t_read_balance():
    cursor, book = make_ledger([(500,)])
    assert book.read_balance("AC-0001") == 500
    sql, params = cursor.statements[0]
    assert "where client_id = %s and account_id = %s" in sql
    assert params == (CLIENT_ID, "AC-0001")

    cursor2, book2 = make_ledger([None])
    assert book2.read_balance("AC-0009") is None
    assert_client_id_in(cursor2)


@test("deposit stores only the credited account (kind 'deposit')")
def _t_deposit_shape():
    cursor, book = make_ledger([None, (1,), None, None])
    operation = book.deposit("AC-0001", 250000)
    assert operation == "OP-0001", operation
    insert_sql, insert_params = cursor.statements[2]
    assert "insert into operations" in insert_sql
    assert insert_params == (
        CLIENT_ID,
        "OP-0001",
        "deposit",
        250000,
        None,
        "AC-0001",
    )
    update_sql, update_params = cursor.statements[3]
    assert "balance + %s" in update_sql
    assert update_params == (250000, CLIENT_ID, "AC-0001")
    assert_client_id_in(cursor)


@test("transfer stores both accounts (kind 'transfer') and moves the money")
def _t_transfer_shape():
    cursor, book = make_ledger([None, (1,), None, None, None])
    operation = book.transfer("AC-0001", "AC-0002", 40000)
    assert operation == "OP-0001", operation
    insert_params = cursor.statements[2][1]
    assert insert_params == (
        CLIENT_ID,
        "OP-0001",
        "transfer",
        40000,
        "AC-0001",
        "AC-0002",
    )
    debit_sql, debit_params = cursor.statements[3]
    credit_sql, credit_params = cursor.statements[4]
    assert "balance - %s" in debit_sql
    assert debit_params == (40000, CLIENT_ID, "AC-0001")
    assert "balance + %s" in credit_sql
    assert credit_params == (40000, CLIENT_ID, "AC-0002")
    assert_client_id_in(cursor)


@test("an unknown operation is ALREADY_ANNULLED and moves no money")
def _t_annul_unknown():
    cursor, book = make_ledger([None])
    assert book.annul("OP-9999") is Annulment.ALREADY_ANNULLED
    assert len(cursor.statements) == 1, cursor.statements
    mark_sql, mark_params = cursor.statements[0]
    # The guard is what makes a previously annulled row fall into the same
    # "no row returned" branch, which is the documented assumption.
    assert "annulled = false" in mark_sql
    assert mark_params == (CLIENT_ID, "OP-9999")


@test("annulling a deposit reverts the credited balance")
def _t_annul_deposit():
    cursor, book = make_ledger([("deposit", 700, None, "AC-0001"), None])
    assert book.annul("OP-0001") is Annulment.ANNULLED
    revert_sql, revert_params = cursor.statements[1]
    assert "balance - %s" in revert_sql
    assert revert_params == (700, CLIENT_ID, "AC-0001")
    assert len(cursor.statements) == 2, cursor.statements
    assert_client_id_in(cursor)


@test("annulling a transfer reverts both balances")
def _t_annul_transfer():
    cursor, book = make_ledger(
        [("transfer", 200, "AC-0001", "AC-0002"), None, None]
    )
    assert book.annul("OP-0002") is Annulment.ANNULLED
    credited_sql, credited_params = cursor.statements[1]
    debited_sql, debited_params = cursor.statements[2]
    assert "balance - %s" in credited_sql
    assert credited_params == (200, CLIENT_ID, "AC-0002")
    assert "balance + %s" in debited_sql
    assert debited_params == (200, CLIENT_ID, "AC-0001")
    assert_client_id_in(cursor)


@test("reset filters by client_id, never truncates and resets counters to 1")
def _t_reset():
    cursor = FakeCursor([])
    Book().reset(cursor, CLIENT_ID)
    sqls = [sql for sql, _ in cursor.statements]
    assert any("delete from operations where client_id = %s" in s for s in sqls)
    assert any("delete from accounts where client_id = %s" in s for s in sqls)
    assert "on conflict (client_id) do update" in sqls[2]
    assert "next_account = 1, next_operation = 1" in sqls[2]
    assert all("truncate" not in sql.lower() for sql in sqls)
    assert_client_id_in(cursor)
    assert len(cursor.statements) == 3, cursor.statements


@test("lock sets a transaction-local lock_timeout then the advisory lock")
def _t_lock():
    cursor = FakeCursor([])
    Book().lock(cursor, CLIENT_ID)
    timeout_sql, timeout_params = cursor.statements[0]
    assert "set local lock_timeout" in timeout_sql
    assert timeout_params == (DEFAULT_LOCK_TIMEOUT_MS,)
    lock_sql, lock_params = cursor.statements[1]
    assert "pg_advisory_xact_lock" in lock_sql
    assert lock_params == (CLIENT_ID,)


@test("resolve_token returns the valid row with that hash")
def _t_resolve_valid():
    cursor = FakeCursor([(3, "Lucia")])
    client = Book().resolve_token(cursor, "hash-A", now=NOW)
    assert client == Client(id=3, label="Lucia"), client
    sql, params = cursor.statements[0]
    assert "revoked_at is null" in sql
    assert "expires_at > %s" in sql
    assert params == ("hash-A", NOW)


@test("resolve_token returns None for a missing, revoked or expired row")
def _t_resolve_invalid():
    cursor = FakeCursor([None])
    assert Book().resolve_token(cursor, "hash-X", now=NOW) is None


@test("base_responde is True when the database answers and False when not")
def _t_base_responde():
    assert Book().base_responde(FakeCursor([(1,)])) is True
    assert Book().base_responde(FakeCursor([None])) is False


@test("for_client binds the port to that cursor and that client")
def _t_for_client():
    cursor = FakeCursor([(42,)])
    bound = Book().for_client(cursor, CLIENT_ID)
    assert isinstance(bound, Ledger)
    assert bound.read_balance("AC-0001") == 42
    assert cursor.statements[-1][1] == (CLIENT_ID, "AC-0001")


@test("a full sequence carries client_id in every data statement")
def _t_every_statement():
    cursor, book = make_ledger(
        [
            None, (1,), None,                       # create_account
            None, (1,), None, None,                 # deposit
            None, (2,), None, None, None,           # transfer
            ("transfer", 100, "AC-0001", "AC-0002"), None, None,  # annul
        ]
    )
    assert book.create_account("Lucia") == "AC-0001"
    assert book.deposit("AC-0001", 100) == "OP-0001"
    assert book.transfer("AC-0001", "AC-0002", 100) == "OP-0002"
    assert book.annul("OP-0002") is Annulment.ANNULLED
    assert_client_id_in(cursor)


@test("dsn_from_environment prefers DATABASE_URL, then DB_URL, else fails")
def _t_dsn():
    saved = {name: os.environ.get(name) for name in DSN_VARIABLES}
    try:
        for name in DSN_VARIABLES:
            os.environ.pop(name, None)
        raised = False
        try:
            dsn_from_environment()
        except BookUnavailable:
            raised = True
        assert raised, "a missing DSN must raise BookUnavailable"

        os.environ["DB_URL"] = "postgres://local"
        assert dsn_from_environment() == "postgres://local"
        os.environ["DATABASE_URL"] = "postgres://deploy"
        assert dsn_from_environment() == "postgres://deploy"
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@test("connect uses connect_timeout=4 and leaves autocommit off")
def _t_connect():
    seen = {}

    class FakePsycopgConnection:
        autocommit = True

    def fake_connect(dsn, connect_timeout=None):
        seen["dsn"] = dsn
        seen["connect_timeout"] = connect_timeout
        return FakePsycopgConnection()

    with patched(ledger.psycopg2, "connect", fake_connect):
        connection = ledger.connect("postgres://example")
    assert seen["dsn"] == "postgres://example"
    assert seen["connect_timeout"] == 4
    assert connection.autocommit is False


@test("connect turns a psycopg2 connection error into BookUnavailable")
def _t_connect_failure():
    def fake_connect(dsn, connect_timeout=None):
        raise psycopg2.OperationalError("scripted")

    with patched(ledger.psycopg2, "connect", fake_connect):
        raised = False
        try:
            ledger.connect("postgres://example")
        except BookUnavailable:
            raised = True
    assert raised, "a connection error must surface as BookUnavailable"


@test("transaction commits on a clean exit and closes everything")
def _t_commit():
    cursor = FakeCursor([])
    connection = FakeConnection(cursor)
    with patched(ledger, "connect", lambda dsn: connection):
        with Book(dsn="fake").transaction() as yielded:
            assert yielded is cursor
    assert connection.committed is True
    assert connection.rolled_back is False
    assert connection.closed is True
    assert cursor.closed is True
    timeout_sql, timeout_params = cursor.statements[0]
    assert timeout_sql.lstrip().startswith("set local statement_timeout")
    assert timeout_params == (STATEMENT_TIMEOUT_MS,)


@test("transaction rolls back on any exception and re-raises the original")
def _t_rollback():
    cursor = FakeCursor([])
    connection = FakeConnection(cursor)
    raised = False
    with patched(ledger, "connect", lambda dsn: connection):
        try:
            with Book(dsn="fake").transaction():
                raise ValueError("boom")
        except ValueError:
            raised = True
    assert raised, "the original exception must not be swallowed"
    assert connection.rolled_back is True
    assert connection.committed is False
    assert connection.closed is True


@test("a lock wait that fails surfaces as BookUnavailable and rolls back")
def _t_lock_failure():
    cursor = FakeCursor(fail_on=("pg_advisory_xact_lock",))
    connection = FakeConnection(cursor)
    raised = False
    with patched(ledger, "connect", lambda dsn: connection):
        try:
            with Book(dsn="fake").transaction() as current:
                Book(dsn="fake").lock(current, CLIENT_ID)
        except BookUnavailable:
            raised = True
    assert raised, "a lock failure must surface as BookUnavailable"
    assert connection.rolled_back is True
    assert connection.committed is False
    assert connection.closed is True


@test("a psycopg2 error inside the transaction becomes BookUnavailable")
def _t_statement_failure():
    cursor = FakeCursor(fail_on=("select 1",))
    connection = FakeConnection(cursor)
    raised = False
    with patched(ledger, "connect", lambda dsn: connection):
        try:
            with Book(dsn="fake").transaction():
                Book().base_responde(cursor)
        except BookUnavailable:
            raised = True
    assert raised, "a statement failure must surface as BookUnavailable"
    assert connection.rolled_back is True
    assert connection.closed is True


@test("a connection that does not open raises BookUnavailable")
def _t_transaction_connect_failure():
    def fake_connect(dsn):
        raise BookUnavailable("scripted")

    with patched(ledger, "connect", fake_connect):
        raised = False
        try:
            with Book(dsn="fake").transaction():
                pass
        except BookUnavailable:
            raised = True
    assert raised, "a connection failure must surface as BookUnavailable"


# --- runner --------------------------------------------------------------

def run_tests():
    failures = 0
    for name, function in TESTS:
        try:
            function()
        except Exception as error:  # noqa: BLE001
            failures += 1
            print("FAIL   %s" % name)
            print("       %s: %s" % (type(error).__name__, error))
        else:
            print("PASS   %s" % name)
    print()
    if failures:
        print("Result: %d failure(s) out of %d tests" % (failures, len(TESTS)))
        return 1
    print("Result: %d tests, all pass" % len(TESTS))
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
