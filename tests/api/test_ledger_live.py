#!/usr/bin/env python3
"""Live, opt-in tests for the SQL book.

It runs against a real Postgres, but only when asked: if neither
``DATABASE_URL`` nor ``DB_URL`` is set it prints one ``SKIP`` line and exits 0.
The driver is required; there is no dependency-free fallback that would hide a
missing ``psycopg2``.

It uses its own throwaway token rows, with a label that carries the process id,
so it never touches another client's rows. The rows are created through the
registry, so this suite carries no SQL of its own; everything it inserts is
deleted at the end (the foreign keys cascade the accounts, operations and
counters).

Covered, with the numbers it observes:

  - the port round-trip, including `ANULA`;
  - persistence read through a new transaction;
  - isolation between two clients, and reset that does not touch the other;
  - reset that puts the counters back to 1;
  - concurrency: two simultaneous transfers from the same source, repeated,
    with zero double debits.

Never prints the DSN, the password or the pepper.

The number of concurrency rounds defaults to 10 because the book opens one
connection per transaction and this environment's database is remote; the
design's 100 rounds are available with ``LEDGER_LIVE_ROUNDS=100``. The observed
numbers are printed either way.

Runs with: ``/tmp/dbenv/bin/python tests/api/test_ledger_live.py``
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

from api.domain.state import Annulment  # noqa: E402
from api.ledger import Book, BookUnavailable  # noqa: E402
from api.registry import Registry, hash_token  # noqa: E402


def _rounds() -> int:
    raw = os.environ.get("LEDGER_LIVE_ROUNDS", "10")
    try:
        value = int(raw)
    except ValueError:
        return 10
    return value if value > 0 else 10


ROUNDS = _rounds()


@dataclass(frozen=True)
class Seeded:
    """The throwaway clients of this run. The hashes are never printed."""

    a_id: int
    a_hash: str
    b_id: int
    b_hash: str
    c_id: int
    c_hash: str


def _describe(error: BaseException) -> str:
    """Safe description: a raw psycopg2 message can carry connection details."""
    if isinstance(error, psycopg2.Error):
        return type(error).__name__
    return "%s: %s" % (type(error).__name__, error)


def _seed(book: Book, label: str) -> Seeded:
    """Creates this run's throwaway clients through the registry.

    The registry is what stores the token rows, so the test does not carry SQL
    of its own and the book stays the only module that writes SQL.
    """
    registry = Registry(book=book)
    tokens = {}
    for name in ("a", "b", "c"):
        secret = registry.create(label, "ledger-live-test", days=1)
        client = registry.resolve(secret)
        assert client is not None, "the seeded token did not resolve"
        tokens[name] = (client.id, hash_token(secret))
    return Seeded(
        a_id=tokens["a"][0],
        a_hash=tokens["a"][1],
        b_id=tokens["b"][0],
        b_hash=tokens["b"][1],
        c_id=tokens["c"][0],
        c_hash=tokens["c"][1],
    )


def _cleanup(book: Book, label: str) -> None:
    """Deletes only this run's token rows; the cascades remove the rest."""
    with book.transaction() as cursor:
        book.delete_tokens_by_label(cursor, label)


def _reset(book: Book, client_id: int) -> None:
    with book.transaction() as cursor:
        book.reset(cursor, client_id)


# --- cases ---------------------------------------------------------------

def _case_round_trip(book: Book, seeded: Seeded) -> str:
    _reset(book, seeded.a_id)
    with book.transaction() as cursor:
        client = book.resolve_token(cursor, seeded.a_hash)
        assert client is not None, "the seeded token did not resolve"
        assert client.id == seeded.a_id, (client.id, seeded.a_id)
        ledger = book.for_client(cursor, client.id)
        account = ledger.create_account("Lucia")
        assert account == "AC-0001", account
        operation = ledger.deposit(account, 700)
        assert operation == "OP-0001", operation
        assert ledger.read_balance(account) == 700
        destination = ledger.create_account("Ana")
        assert destination == "AC-0002", destination
        moved = ledger.transfer(account, destination, 200)
        assert moved == "OP-0002", moved
        assert ledger.read_balance(account) == 500
        assert ledger.read_balance(destination) == 200
        assert ledger.annul(moved) is Annulment.ANNULLED
        assert ledger.read_balance(account) == 700
        assert ledger.read_balance(destination) == 0
        assert ledger.annul(moved) is Annulment.ALREADY_ANNULLED
        assert ledger.annul("OP-9999") is Annulment.ALREADY_ANNULLED
    return "AC-0001/OP-0001, ANULA reverted to 700/0"


def _case_persistence(book: Book, seeded: Seeded) -> str:
    """The design's persistence check: state survives a new transaction.

    A new transaction opens a new connection, so the balance and the counters
    cannot live in the connection that wrote them.
    """
    _reset(book, seeded.a_id)
    with book.transaction() as cursor:
        ledger = book.for_client(cursor, seeded.a_id)
        account = ledger.create_account("Persistente")
        ledger.deposit(account, 1234)
    with book.transaction() as cursor:
        ledger = book.for_client(cursor, seeded.a_id)
        balance = ledger.read_balance(account)
        assert balance == 1234, balance
        following = ledger.create_account("Siguiente")
    with book.transaction() as cursor:
        ledger = book.for_client(cursor, seeded.a_id)
        assert ledger.read_balance(following) == 0
        assert following == "AC-0002", following
    return "balance 1234 and counter AC-0002 survived new transactions"


def _case_isolation(book: Book, seeded: Seeded) -> str:
    _reset(book, seeded.a_id)
    _reset(book, seeded.b_id)
    with book.transaction() as cursor:
        ledger_a = book.for_client(cursor, seeded.a_id)
        account_a = ledger_a.create_account("SoloA")
        assert account_a == "AC-0001", account_a
        ledger_a.deposit(account_a, 500)
    with book.transaction() as cursor:
        ledger_b = book.for_client(cursor, seeded.b_id)
        # B must not see A's account, even with the same id shape.
        assert ledger_b.read_balance(account_a) is None
        account_b = ledger_b.create_account("SoloB")
        assert account_b == "AC-0001", account_b
        ledger_b.deposit(account_b, 300)
    _reset(book, seeded.a_id)
    with book.transaction() as cursor:
        ledger_a = book.for_client(cursor, seeded.a_id)
        ledger_b = book.for_client(cursor, seeded.b_id)
        assert ledger_a.read_balance(account_a) is None
        assert ledger_b.read_balance(account_b) == 300
    return "B kept AC-0001/300 after A's reset; A saw nothing of B"


def _case_reset_counters(book: Book, seeded: Seeded) -> str:
    _reset(book, seeded.a_id)
    with book.transaction() as cursor:
        ledger = book.for_client(cursor, seeded.a_id)
        assert ledger.create_account("Primera") == "AC-0001"
        assert ledger.deposit("AC-0001", 10) == "OP-0001"
    _reset(book, seeded.a_id)
    with book.transaction() as cursor:
        ledger = book.for_client(cursor, seeded.a_id)
        assert ledger.create_account("TrasReset") == "AC-0001"
        assert ledger.deposit("AC-0001", 20) == "OP-0001"
    return "after reset the counters returned to AC-0001 and OP-0001"


def _case_concurrency(book: Book, seeded: Seeded) -> str:
    successes = 0
    unavailable = 0
    other = 0
    both = 0
    neither = 0

    _reset(book, seeded.c_id)
    with book.transaction() as cursor:
        ledger = book.for_client(cursor, seeded.c_id)
        source = ledger.create_account("Source")
        destination_a = ledger.create_account("DestA")
        destination_b = ledger.create_account("DestB")

    for round_index in range(ROUNDS):
        # Exactly 100 available: only one of the two concurrent transfers of
        # 100 can commit, so the round is a clean race.
        with book.transaction() as cursor:
            ledger = book.for_client(cursor, seeded.c_id)
            ledger.deposit(source, 100)

        results = [None, None]

        def worker(index: int, destination: str) -> None:
            try:
                with book.transaction() as cursor:
                    client = book.resolve_token(cursor, seeded.c_hash)
                    assert client is not None
                    book.lock(cursor, client.id)
                    ledger = book.for_client(cursor, client.id)
                    ledger.transfer(source, destination, 100)
                results[index] = "ok"
            except BookUnavailable:
                results[index] = "unavailable"
            except Exception as error:  # noqa: BLE001
                results[index] = type(error).__name__

        threads = [
            threading.Thread(target=worker, args=(0, destination_a)),
            threading.Thread(target=worker, args=(1, destination_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        ok = sum(1 for result in results if result == "ok")
        successes += ok
        unavailable += sum(
            1 for result in results if result == "unavailable"
        )
        other += sum(
            1 for result in results if result not in ("ok", "unavailable")
        )
        if ok == 2:
            both += 1
        if ok == 0:
            neither += 1

    with book.transaction() as cursor:
        ledger = book.for_client(cursor, seeded.c_id)
        source_balance = ledger.read_balance(source)
        balance_a = ledger.read_balance(destination_a)
        balance_b = ledger.read_balance(destination_b)

    # One transfer per round: the source ends empty and the total money is
    # exactly what was deposited, so nothing was debited twice.
    assert source_balance == 0, source_balance
    assert balance_a + balance_b == 100 * ROUNDS, (balance_a, balance_b)
    assert both == 0, "double debit in %d round(s)" % both
    assert neither == 0, "no transfer committed in %d round(s)" % neither
    return (
        "rounds=%d successes=%d unavailable=%d both=%d neither=%d other=%d"
        % (ROUNDS, successes, unavailable, both, neither, other)
    )


CASES = (
    ("port round-trip", _case_round_trip),
    ("persistence in a new transaction", _case_persistence),
    ("isolation and reset of one client", _case_isolation),
    ("reset returns the counters to 1", _case_reset_counters),
    ("concurrency: no double debit", _case_concurrency),
)


def main() -> int:
    if os.environ.get("DATABASE_URL"):
        source = "DATABASE_URL"
    elif os.environ.get("DB_URL"):
        source = "DB_URL"
    else:
        print("SKIP ledger live: neither DATABASE_URL nor DB_URL is set")
        return 0

    print("ledger live: using %s (value never printed)" % source)
    # A test-only pepper when the environment does not provide one. The value
    # is never printed. A real one, when set, is respected and used as is.
    os.environ.setdefault("REGISTRO_PEPPER", "ledger-live-test-pepper")
    label = "ledger-live-%d" % os.getpid()
    book = Book()
    failures = 0
    seeded = None
    try:
        seeded = _seed(book, label)
        for name, case in CASES:
            try:
                detail = case(book, seeded)
            except Exception as error:  # noqa: BLE001
                failures += 1
                print("FAIL   %s: %s" % (name, _describe(error)))
            else:
                print("PASS   %s  [%s]" % (name, detail))
    except Exception as error:  # noqa: BLE001
        failures += 1
        print("FAIL   setup or cleanup boundary: %s" % _describe(error))
    finally:
        try:
            _cleanup(book, label)
        except Exception as error:  # noqa: BLE001
            failures += 1
            print("FAIL   cleanup: %s" % _describe(error))

    print()
    if failures:
        print("Result: %d live failure(s)" % failures)
        return 1
    print("Result: %d live cases, all pass" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
