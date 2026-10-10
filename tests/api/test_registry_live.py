#!/usr/bin/env python3
"""Live, opt-in tests for the token registry.

It runs against a real Postgres, but only when asked: if neither
``DATABASE_URL`` nor ``DB_URL`` is set it prints one ``SKIP`` line and exits 0.
If the environment does not provide a ``REGISTRO_PEPPER`` it sets a test-only
one, so the suite never depends on the deployment secret and never prints it.

It creates its own throwaway tokens, with labels that carry the process id and
a random suffix, and deletes them at the end through the book (the foreign
keys cascade the accounts, operations and counters). It never touches, revokes
or lists another client's secret.

Covered, with the numbers it observes:

  - create -> resolve -> revoke -> resolve returns `None`;
  - `expires_at` in the past does not resolve and cannot be revoked;
  - the last valid token cannot be revoked (deterministically when this run is
    the only holder of valid tokens; otherwise it asserts the invariant that a
    revoke never leaves the registry without a valid token);
  - its own rows are deleted and the valid count returns to where it started.

Never prints the DSN, the password or the pepper.

Runs with: ``/tmp/dbenv/bin/python tests/api/test_registry_live.py``
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

from api.ledger import Book  # noqa: E402
from api.registry import (  # noqa: E402
    LastValidToken,
    Registry,
    TokenNotFound,
    TokenNotValid,
    hash_token,
)


def _describe(error: BaseException) -> str:
    """Safe description: a raw psycopg2 message can carry connection details."""
    if isinstance(error, psycopg2.Error):
        return type(error).__name__
    return "%s: %s" % (type(error).__name__, error)


class Fixture:
    """This run's throwaway tokens and the book that created them."""

    def __init__(self):
        self.book = Book()
        self.registry = Registry(book=self.book)
        self.prefix = "registry-live-%d-%s" % (
            os.getpid(),
            secrets.token_hex(6),
        )
        self.labels: list[str] = []
        self.initial_valid = 0
        self.a_label = ""
        self.b_label = ""
        self.old_label = ""

    def label(self, name: str) -> str:
        value = "%s-%s" % (self.prefix, name)
        self.labels.append(value)
        return value

    def cleanup(self) -> None:
        """Deletes only this run's rows; the cascades remove the rest."""
        with self.book.transaction() as cursor:
            for label in self.labels:
                self.book.delete_tokens_by_label(cursor, label)

    def rows_with_prefix(self) -> list:
        rows = []
        with self.book.transaction() as cursor:
            for label in self.labels:
                rows.extend(self.book.tokens_by_label(cursor, label))
        return rows


# --- cases ---------------------------------------------------------------

def _case_round_trip(fixture: Fixture) -> str:
    registry = fixture.registry
    secret_a = registry.create(fixture.a_label, "registry-live", days=30)
    secret_b = registry.create(fixture.b_label, "registry-live", days=30)

    client = registry.resolve(secret_a)
    assert client is not None, "the created token did not resolve"
    assert client.label == fixture.a_label, (client.label, fixture.a_label)

    text = repr(registry.list_tokens())
    assert secret_a not in text, "list_tokens leaked the secret"
    assert hash_token(secret_a) not in text, "list_tokens leaked the hash"

    revoked = registry.revoke(fixture.a_label, "registry-live")
    assert revoked.label == fixture.a_label, revoked.label
    assert registry.resolve(secret_a) is None, "a revoked token still resolves"
    assert registry.resolve(secret_b) is not None, "the new token stopped resolving"
    return "create -> resolve -> revoke -> None; the new token still resolves"


def _case_expired(fixture: Fixture) -> str:
    registry = fixture.registry
    secret_old = registry.create(fixture.old_label, "registry-live", days=-1)
    assert registry.resolve(secret_old) is None, "an expired token resolved"
    raised = False
    try:
        registry.revoke(fixture.old_label, "registry-live")
    except (TokenNotFound, TokenNotValid):
        raised = True
    assert raised, "revoking an expired token must be refused"
    return "expires_at in the past does not resolve and cannot be revoked"


def _case_last_valid(fixture: Fixture) -> str:
    registry = fixture.registry
    valid_before = registry.count_valid()
    assert valid_before >= 1, valid_before
    try:
        registry.revoke(fixture.b_label, "registry-live")
    except LastValidToken:
        assert valid_before == 1, (
            "the last-valid refusal fired with %d valid tokens" % valid_before
        )
        return "refused: it was the last valid token (valid=%d)" % valid_before
    after = registry.count_valid()
    assert after == valid_before - 1, (valid_before, after)
    assert after >= 1, "a revoke left the registry without a valid token"
    return (
        "revoked with %d other valid tokens; %d remain"
        % (valid_before - 1, after)
    )


def _case_cleanup(fixture: Fixture) -> str:
    fixture.cleanup()
    remaining = fixture.rows_with_prefix()
    assert remaining == [], remaining
    now_valid = fixture.registry.count_valid()
    assert now_valid == fixture.initial_valid, (now_valid, fixture.initial_valid)
    return "own rows deleted; valid tokens back to %d" % now_valid


CASES = (
    ("create -> resolve -> revoke -> None", _case_round_trip),
    ("expiry in the past is rejected", _case_expired),
    ("the last valid token cannot be revoked", _case_last_valid),
    ("cleanup of its own rows", _case_cleanup),
)


def main() -> int:
    if os.environ.get("DATABASE_URL"):
        source = "DATABASE_URL"
    elif os.environ.get("DB_URL"):
        source = "DB_URL"
    else:
        print("SKIP registry live: neither DATABASE_URL nor DB_URL is set")
        return 0

    print("registry live: using %s (value never printed)" % source)
    # A test-only pepper when the environment does not provide one. If a real
    # one is set it is respected and used as is; the value is never printed.
    os.environ.setdefault("REGISTRO_PEPPER", "registry-live-test-pepper")

    fixture = Fixture()
    fixture.a_label = fixture.label("a")
    fixture.b_label = fixture.label("b")
    fixture.old_label = fixture.label("old")
    failures = 0
    try:
        fixture.initial_valid = fixture.registry.count_valid()
        for name, case in CASES:
            try:
                detail = case(fixture)
            except Exception as error:  # noqa: BLE001
                failures += 1
                print("FAIL   %s: %s" % (name, _describe(error)))
            else:
                print("PASS   %s  [%s]" % (name, detail))
    except Exception as error:  # noqa: BLE001
        failures += 1
        print("FAIL   setup boundary: %s" % _describe(error))
    finally:
        try:
            fixture.cleanup()
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
