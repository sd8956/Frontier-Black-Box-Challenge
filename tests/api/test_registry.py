#!/usr/bin/env python3
"""Offline tests for the token registry and its administration CLI.

No network and no database. The `Book` methods are exercised through a fake
cursor that records statements and replays scripted rows (the same style as
`tests/api/test_ledger.py`); the registry is exercised through a scripted
in-memory book; the CLI is driven with a fake registry. It asserts the things
the registry promises before any database exists:

  - what is stored is the HMAC, never the secret;
  - `resolve` accepts a valid token and rejects revoked, expired and unknown;
  - `revoke` refuses the last valid token and allows it when another remains;
  - the new SQL is where it must be, with the last-valid guard in the update;
  - `listar` never carries the secret nor the hash;
  - the CLI's `registrar` reads stdin and `crear` prints the secret once.

It must not require a live database. Prints one line per case and exits with a
non-zero code if anything fails.

Runs with: ``/tmp/dbenv/bin/python tests/api/test_registry.py``
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.cli import main  # noqa: E402
from api.ledger import Book, Client, Revocation, TokenRow  # noqa: E402
from api.registry import (  # noqa: E402
    AmbiguousLabel,
    LastValidToken,
    PepperMissing,
    Registry,
    RegistryError,
    TokenNotFound,
    TokenNotValid,
    hash_token,
)

PEPPER = "pepper-de-prueba"
FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)
CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- doubles -------------------------------------------------------------

class FakeCursor:
    """Records statements and replays one scripted result per ``execute``."""

    def __init__(self, results=None):
        self.statements = []
        self._results = list(results or [])
        self._last = None
        self.closed = False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self._last = self._results.pop(0) if self._results else None

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else []

    def close(self):
        self.closed = True


class FakeBook:
    """Scripted double for the SQL book.

    It records every call, keeps an in-memory table of valid and revoked
    tokens, and mirrors the book's contract closely enough for the registry's
    policy to be tested without a database.
    """

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.hashes = {}
        self.created = []
        self.deleted_labels = []
        self.calls = []
        self._next_id = max((row.id for row in self.rows), default=0) + 1

    @contextmanager
    def transaction(self):
        self.calls.append("transaction")
        yield self

    def create_token(self, cursor, token_hash, label, created_by, expires_at):
        self.created.append((token_hash, label, created_by, expires_at))
        if token_hash in self.hashes:
            return None
        token_id = self._next_id
        self._next_id += 1
        row = TokenRow(token_id, label, created_by, CREATED, expires_at, None)
        self.rows.append(row)
        self.hashes[token_hash] = token_id
        return token_id

    def token_by_id(self, cursor, token_id):
        for row in self.rows:
            if row.id == token_id:
                return row
        return None

    def tokens_by_label(self, cursor, label):
        return [row for row in self.rows if row.label == label]

    def list_tokens(self, cursor):
        return list(self.rows)

    def count_valid(self, cursor, now=None):
        now = now or _now()
        return sum(
            1
            for row in self.rows
            if row.revoked_at is None and row.expires_at > now
        )

    def resolve_token(self, cursor, token_hash, now=None):
        now = now or _now()
        token_id = self.hashes.get(token_hash)
        if token_id is None:
            return None
        row = self.token_by_id(cursor, token_id)
        if row is not None and row.revoked_at is None and row.expires_at > now:
            return Client(id=row.id, label=row.label)
        return None

    def revoke_token(self, cursor, token_id, revoked_by, now=None):
        now = now or _now()
        row = self.token_by_id(cursor, token_id)
        if row is None or row.revoked_at is not None or row.expires_at <= now:
            return Revocation.NOT_FOUND
        valid = [
            candidate
            for candidate in self.rows
            if candidate.revoked_at is None and candidate.expires_at > now
        ]
        if len(valid) <= 1:
            return Revocation.LAST_VALID
        self.rows[self.rows.index(row)] = TokenRow(
            row.id, row.label, row.created_by, row.created_at, row.expires_at, now
        )
        return Revocation.REVOKED

    def delete_tokens_by_label(self, cursor, label):
        self.deleted_labels.append(label)
        self.rows = [row for row in self.rows if row.label != label]


class FakeRegistry:
    """Scripted double for the registry, for the CLI's tests."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []
        self.secret = "SECRETO-DE-PRUEBA"
        self.error = None

    def create(self, label, created_by, days):
        self.calls.append(("create", label, created_by, days))
        if self.error is not None:
            raise self.error
        return self.secret

    def register(self, secret, label, created_by, days):
        self.calls.append(("register", secret, label, created_by, days))
        if self.error is not None:
            raise self.error
        return 7

    def revoke(self, identifier, revoked_by):
        self.calls.append(("revoke", identifier, revoked_by))
        if self.error is not None:
            raise self.error
        return TokenRow(7, "Lucia", "op", CREATED, FUTURE, None)

    def list_tokens(self):
        return list(self.rows)


def make_registry(pepper=PEPPER):
    """A registry over a fake book, with the pepper set for the test."""
    saved = os.environ.get("REGISTRO_PEPPER")
    os.environ["REGISTRO_PEPPER"] = pepper
    return saved


def restore_pepper(saved):
    if saved is None:
        os.environ.pop("REGISTRO_PEPPER", None)
    else:
        os.environ["REGISTRO_PEPPER"] = saved


# --- cases ---------------------------------------------------------------

TESTS = []


def test(name):
    def decorator(function):
        TESTS.append((name, function))
        return function

    return decorator


@test("hash_token is HMAC-SHA256 with the pepper, not the secret")
def _t_hash_known():
    obtained = hash_token("secreto", pepper="pimienta")
    expected = hmac.new(
        b"pimienta", b"secreto", hashlib.sha256
    ).hexdigest()
    assert obtained == expected, obtained
    assert obtained != "secreto"
    assert len(obtained) == 64, len(obtained)


@test("hash_token takes the pepper from the environment and fails without it")
def _t_hash_env():
    saved = os.environ.get("REGISTRO_PEPPER")
    try:
        os.environ["REGISTRO_PEPPER"] = "pimienta-entorno"
        expected = hmac.new(
            b"pimienta-entorno", b"secreto", hashlib.sha256
        ).hexdigest()
        assert hash_token("secreto") == expected

        os.environ.pop("REGISTRO_PEPPER", None)
        raised = False
        try:
            hash_token("secreto")
        except PepperMissing:
            raised = True
        assert raised, "a missing pepper must raise PepperMissing"
    finally:
        if saved is None:
            os.environ.pop("REGISTRO_PEPPER", None)
        else:
            os.environ["REGISTRO_PEPPER"] = saved


@test("create stores the HMAC and never the secret")
def _t_create_stores_hash():
    saved = make_registry()
    try:
        book = FakeBook()
        registry = Registry(book=book)
        secret = registry.create("Lucia", "operadora")
        stored, label, created_by, expires_at = book.created[0]
        assert stored == hash_token(secret, pepper=PEPPER)
        assert stored != secret
        assert secret not in stored
        assert label == "Lucia"
        assert created_by == "operadora"
        # The book is what decides the row; the registry only hands it args.
        assert book.hashes[stored] == 1
    finally:
        restore_pepper(saved)


@test("register stores the secret it is given, without reading argv")
def _t_register_secret():
    saved = make_registry()
    try:
        book = FakeBook()
        registry = Registry(book=book)
        token_id = registry.register("token-del-cliente", "Cliente", "op")
        assert token_id == 1, token_id
        stored = book.created[0][0]
        assert stored == hash_token("token-del-cliente", pepper=PEPPER)
        assert "token-del-cliente" not in stored
    finally:
        restore_pepper(saved)


@test("register refuses a repeated secret and an empty one")
def _t_register_duplicate():
    saved = make_registry()
    try:
        book = FakeBook()
        registry = Registry(book=book)
        registry.register("repetido", "Uno", "op")
        raised = False
        try:
            registry.register("repetido", "Dos", "op")
        except RegistryError:
            raised = True
        assert raised, "a repeated secret must be refused"

        raised = False
        try:
            registry.register("", "Vacio", "op")
        except RegistryError:
            raised = True
        assert raised, "an empty secret must be refused"
    finally:
        restore_pepper(saved)


@test("resolve accepts a valid token and rejects revoked, expired and unknown")
def _t_resolve():
    saved = make_registry()
    try:
        book = FakeBook()
        registry = Registry(book=book)
        valid = registry.create("Valido", "op", days=30)
        expiring = registry.create("Vence", "op", days=-1)
        revoked = registry.create("Revocado", "op", days=30)

        assert registry.resolve(valid) == Client(id=1, label="Valido")
        assert registry.resolve(expiring) is None
        registry.revoke("Revocado", "op")
        assert registry.resolve(revoked) is None
        assert registry.resolve("desconocido") is None
    finally:
        restore_pepper(saved)


@test("revoke refuses the last valid token with a clear error")
def _t_revoke_last():
    saved = make_registry()
    try:
        book = FakeBook()
        registry = Registry(book=book)
        registry.create("Unica", "op", days=30)
        raised = False
        try:
            registry.revoke("Unica", "op")
        except LastValidToken as error:
            raised = True
            assert "último" in str(error)
        assert raised, "revoking the last valid token must be refused"
        assert registry.count_valid() == 1
        # Nothing was written: the row is still valid.
        assert book.rows[0].revoked_at is None
    finally:
        restore_pepper(saved)


@test("revoke allows it when another valid token remains")
def _t_revoke_overlap():
    saved = make_registry()
    try:
        book = FakeBook()
        registry = Registry(book=book)
        old = registry.create("Vieja", "op", days=30)
        new = registry.create("Nueva", "op", days=30)
        row = registry.revoke("Vieja", "op")
        assert row.label == "Vieja"
        assert registry.count_valid() == 1
        # Rotation overlaps: the new one is still valid, the old one is not.
        assert registry.resolve(old) is None
        assert registry.resolve(new) == Client(id=2, label="Nueva")
        assert [r.label for r in registry.list_tokens()] == ["Vieja", "Nueva"]
    finally:
        restore_pepper(saved)


@test("revoke resolves by id and refuses unknown or ambiguous identifiers")
def _t_revoke_identifiers():
    saved = make_registry()
    try:
        book = FakeBook()
        registry = Registry(book=book)
        registry.create("Ana", "op", days=30)
        registry.create("Luis", "op", days=30)
        registry.create("Luis", "op", days=30)
        row = registry.revoke("1", "op")
        assert row.id == 1, row.id

        raised = False
        try:
            registry.revoke("404", "op")
        except TokenNotFound:
            raised = True
        assert raised, "an unknown id must be TokenNotFound"

        raised = False
        try:
            registry.revoke("Luis", "op")
        except AmbiguousLabel:
            raised = True
        assert raised, "an ambiguous label must be AmbiguousLabel"

        raised = False
        try:
            registry.revoke("Fantasma", "op")
        except TokenNotFound:
            raised = True
        assert raised, "an unknown label must be TokenNotFound"
    finally:
        restore_pepper(saved)


@test("revoke rejects a token that exists but is revoked or expired")
def _t_revoke_invalid():
    saved = make_registry()
    try:
        book = FakeBook()
        registry = Registry(book=book)
        registry.create("Otro", "op", days=30)  # keeps the count above one
        registry.create("Vencida", "op", days=-1)
        raised = False
        try:
            registry.revoke("Vencida", "op")
        except TokenNotValid:
            raised = True
        assert raised, "an expired token must be TokenNotValid"
    finally:
        restore_pepper(saved)


@test("list_tokens never carries the secret nor the hash")
def _t_list_no_secret():
    saved = make_registry()
    try:
        book = FakeBook()
        registry = Registry(book=book)
        secret = registry.create("Lucia", "op", days=30)
        rows = registry.list_tokens()
        text = repr(rows)
        assert secret not in text
        assert hash_token(secret, pepper=PEPPER) not in text
        assert "token_hash" not in text
        assert rows[0].label == "Lucia"
    finally:
        restore_pepper(saved)


@test("count_valid counts only not revoked and not expired tokens")
def _t_count_valid():
    saved = make_registry()
    try:
        book = FakeBook(
            rows=[
                TokenRow(1, "A", "op", CREATED, FUTURE, None),
                TokenRow(2, "B", "op", CREATED, PAST, None),
                TokenRow(3, "C", "op", CREATED, FUTURE, CREATED),
            ]
        )
        registry = Registry(book=book)
        assert registry.count_valid() == 1
    finally:
        restore_pepper(saved)


# --- the SQL book's new statements ---------------------------------------

@test("create_token inserts the hash and settles the conflict in SQL")
def _t_book_create():
    cursor = FakeCursor([(5,)])
    token_id = Book().create_token(
        cursor, "hash", "Lucia", "op", FUTURE
    )
    assert token_id == 5, token_id
    sql, params = cursor.statements[0]
    assert "insert into tokens" in sql
    assert "on conflict (token_hash) do nothing" in sql
    assert "returning id" in sql
    assert params == ("hash", "Lucia", "op", FUTURE)

    duplicate = FakeCursor([None])
    assert Book().create_token(duplicate, "hash", "L", "op", FUTURE) is None


@test("revoke_token locks, guards with the count and reports REVOKED")
def _t_book_revoke_ok():
    cursor = FakeCursor([None, (1,), None])
    outcome = Book().revoke_token(cursor, 1, "op", now=_now())
    assert outcome is Revocation.REVOKED, outcome
    lock_sql, _ = cursor.statements[0]
    assert "lock table tokens in share row exclusive mode" in lock_sql
    update_sql, update_params = cursor.statements[1]
    assert "revoked_at = %s" in update_sql
    assert "returning id" in update_sql
    assert "count(*)" in update_sql and "> 1" in update_sql
    assert len(update_params) == 5
    assert len(cursor.statements) == 2, cursor.statements


@test("revoke_token reports LAST_VALID when the guard blocks the update")
def _t_book_revoke_last():
    cursor = FakeCursor([None, None, (1,)])
    outcome = Book().revoke_token(cursor, 1, "op", now=_now())
    assert outcome is Revocation.LAST_VALID, outcome
    diagnostic_sql, diagnostic_params = cursor.statements[2]
    assert "count(*)" in diagnostic_sql
    assert "id = %s" in diagnostic_sql
    assert len(diagnostic_params) == 2


@test("revoke_token reports NOT_FOUND when the token is not valid")
def _t_book_revoke_missing():
    cursor = FakeCursor([None, None, (0,)])
    outcome = Book().revoke_token(cursor, 9, "op", now=_now())
    assert outcome is Revocation.NOT_FOUND, outcome


@test("list_tokens selects the six columns and never token_hash")
def _t_book_list():
    row = (1, "Lucia", "op", CREATED, FUTURE, None)
    cursor = FakeCursor([[row]])
    rows = Book().list_tokens(cursor)
    sql, _ = cursor.statements[0]
    assert "token_hash" not in sql
    for column in (
        "id",
        "label",
        "created_by",
        "created_at",
        "expires_at",
        "revoked_at",
    ):
        assert column in sql, column
    assert rows == [TokenRow(*row)], rows


@test("token_by_id and tokens_by_label return rows, revoked included")
def _t_book_finders():
    row = (3, "Luis", "op", CREATED, FUTURE, None)
    by_id = FakeCursor([row])
    assert Book().token_by_id(by_id, 3) == TokenRow(*row)
    by_label = FakeCursor([[row]])
    assert Book().tokens_by_label(by_label, "Luis") == [TokenRow(*row)]


@test("count_valid filters revoked and expired in SQL")
def _t_book_count():
    cursor = FakeCursor([(2,)])
    now = _now()
    assert Book().count_valid(cursor, now) == 2
    sql, params = cursor.statements[0]
    assert "revoked_at is null" in sql
    assert "expires_at > %s" in sql
    assert params == (now,)


@test("delete_tokens_by_label filters by label and touches nothing else")
def _t_book_delete():
    cursor = FakeCursor([])
    Book().delete_tokens_by_label(cursor, "limpieza")
    sql, params = cursor.statements[0]
    assert sql == "delete from tokens where label = %s"
    assert params == ("limpieza",)
    assert "truncate" not in sql.lower()


# --- the CLI -------------------------------------------------------------

@test("CLI crear prints the secret exactly once and warns it is not shown again")
def _t_cli_crear():
    fake = FakeRegistry()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(
            ["crear", "--label", "Lucia", "--days", "5", "--by", "op"],
            registry=fake,
        )
    assert code == 0, code
    assert out.getvalue() == fake.secret + "\n"
    assert out.getvalue().count(fake.secret) == 1
    assert "no se vuelve a mostrar" in err.getvalue()
    assert fake.calls == [("create", "Lucia", "op", 5)], fake.calls


@test("CLI registrar reads the secret from stdin and never prints it")
def _t_cli_registrar():
    fake = FakeRegistry()
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin
    sys.stdin = io.StringIO("secreto-por-stdin\n")
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                ["registrar", "--label", "Cliente", "--by", "op"],
                registry=fake,
            )
    finally:
        sys.stdin = saved
    assert code == 0, code
    assert fake.calls == [("register", "secreto-por-stdin", "Cliente", "op", 30)]
    assert "secreto-por-stdin" not in out.getvalue()
    assert "secreto-por-stdin" not in err.getvalue()
    assert "id=7" in out.getvalue()


@test("CLI registrar fails cleanly when stdin is empty")
def _t_cli_registrar_empty():
    fake = FakeRegistry()
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                ["registrar", "--label", "Cliente"], registry=fake
            )
    finally:
        sys.stdin = saved
    assert code == 1, code
    assert "stdin" in err.getvalue()
    assert fake.calls == []


@test("CLI listar prints the rows and no secret nor hash")
def _t_cli_listar():
    secret = "SECRETO-QUE-NO-DEBE-SALIR"
    rows = [TokenRow(1, "Lucia", "op", CREATED, FUTURE, None)]
    fake = FakeRegistry(rows)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["listar"], registry=fake)
    assert code == 0, code
    text = out.getvalue()
    assert "Lucia" in text
    assert "ID" in text and "ETIQUETA" in text
    assert secret not in text
    assert "token_hash" not in text


@test("CLI revocar prints the revoked row and uses --by")
def _t_cli_revocar():
    fake = FakeRegistry()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["revocar", "7", "--by", "op"], registry=fake)
    assert code == 0, code
    assert fake.calls == [("revoke", "7", "op")]
    assert "Token revocado: id=7 etiqueta=Lucia" in out.getvalue()


@test("CLI reports a missing pepper on stderr with a non-zero exit")
def _t_cli_pepper():
    fake = FakeRegistry()
    fake.error = PepperMissing("falta REGISTRO_PEPPER en el entorno")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["crear", "--label", "Lucia"], registry=fake)
    assert code == 2, code
    assert out.getvalue() == ""
    assert "REGISTRO_PEPPER" in err.getvalue()


@test("CLI reports a registry error on stderr with a non-zero exit")
def _t_cli_error():
    fake = FakeRegistry()
    fake.error = RegistryError("no se puede revocar el último token vigente")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["revocar", "Lucia"], registry=fake)
    assert code == 1, code
    assert out.getvalue() == ""
    assert "último token vigente" in err.getvalue()


# --- runner --------------------------------------------------------------

def run_tests() -> int:
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
