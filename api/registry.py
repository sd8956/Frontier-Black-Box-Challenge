"""Token registry: the policy layer over the SQL book.

The secret is never stored, never logged and never returned by `list_tokens`.
What is stored is the HMAC-SHA256 of the secret, keyed with a pepper that
lives in the environment (`REGISTRO_PEPPER`). `hash_token` is the only place
that touches the pepper.

Valid means not revoked and `expires_at` in the future, in UTC. Expired or
revoked is someone else's token. The registry never opens a connection: it
uses `Book.transaction`, `Book.resolve_token` and the book's token methods,
so all SQL stays in `api/ledger.py`.

This is the source of truth for credentials: an operator revokes and rotates
without redeploying. There is no public administration route; the CLI
(`api/cli.py`) is the only operator surface.

Rotation overlaps: create the new token first, then revoke the old one. The
registry refuses to revoke the last valid token, so the book enforces that
refusal inside the same transaction that revokes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from api.ledger import Book, Client, Revocation, TokenRow

# The pepper is a deployment secret: it lives in the environment, it is never
# printed, and it is not a token. It does not change between rotations.
PEPPER_VARIABLE = "REGISTRO_PEPPER"

# How long a new token lives when the operator does not say otherwise.
DEFAULT_DAYS = 30

# 32 random bytes give a secret with ~256 bits of entropy.
SECRET_BYTES = 32


class RegistryError(RuntimeError):
    """Base class for the errors the CLI shows to the operator."""


class PepperMissing(RegistryError):
    """`REGISTRO_PEPPER` is not in the environment."""


class DuplicateToken(RegistryError):
    """That secret already has a row."""


class TokenNotFound(RegistryError):
    """No token with that id or label exists."""


class TokenNotValid(RegistryError):
    """The token exists but it is revoked or expired, so it is foreign."""


class AmbiguousLabel(RegistryError):
    """A label matched more than one valid token; the id is required."""


class LastValidToken(RegistryError):
    """Refused: revoking it would leave the registry without a valid token."""


def pepper_from_environment() -> str:
    """The pepper from `REGISTRO_PEPPER`, or `PepperMissing`.

    Its value is never printed, logged or included in the error.
    """
    value = os.environ.get(PEPPER_VARIABLE)
    if not value:
        raise PepperMissing(
            "falta %s en el entorno" % PEPPER_VARIABLE
        )
    return value


def hash_token(secret: str, pepper: str | None = None) -> str:
    """HMAC-SHA256 of `secret` keyed with the pepper, as lowercase hex.

    The pepper comes from the environment when it is not passed. This is what
    is stored: the secret itself never reaches the database.
    """
    if pepper is None:
        pepper = pepper_from_environment()
    return hmac.new(
        pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_valid(row: TokenRow, now: datetime) -> bool:
    """Valid is not revoked and not expired, in UTC. The book says the same."""
    return row.revoked_at is None and row.expires_at > now


class Registry:
    """Credentials of the service, backed by the SQL book.

    `dsn` is optional: when absent the book resolves it from the environment.
    `book` is a test seam; production builds the book from the DSN.
    """

    def __init__(self, dsn: str | None = None, book: Book | None = None):
        self._book = book if book is not None else Book(dsn)

    # --- creation --------------------------------------------------------

    def create(
        self, label: str, created_by: str, days: int = DEFAULT_DAYS
    ) -> str:
        """Creates a token and returns its secret exactly once.

        The secret is generated here and never stored; only its hash is. If
        the caller does not show it now, it cannot be recovered.
        """
        secret = secrets.token_urlsafe(SECRET_BYTES)
        self.register(secret, label, created_by, days)
        return secret

    def register(
        self, secret: str, label: str, created_by: str, days: int = DEFAULT_DAYS
    ) -> int:
        """Stores the hash of a secret the client already has; returns the id.

        It reads nothing from `argv`: the CLI passes the secret it read from
        stdin. An empty secret is refused; a repeated one is `DuplicateToken`.
        """
        if not isinstance(secret, str) or not secret:
            raise RegistryError("el secreto está vacío")
        token_hash = hash_token(secret)
        expires_at = _now() + timedelta(days=days)
        with self._book.transaction() as cursor:
            token_id = self._book.create_token(
                cursor, token_hash, label, created_by, expires_at
            )
            if token_id is None:
                raise DuplicateToken("ese secreto ya está registrado")
            return token_id

    # --- revocation ------------------------------------------------------

    def revoke(
        self, identifier: str, revoked_by: str, now: datetime | None = None
    ) -> TokenRow:
        """Revokes a valid token by numeric id or by label.

        The find and the guarded revoke share one transaction, so the refusal
        to revoke the last valid token is decided by the book, not by a check
        that another request could slip past.
        """
        now = now or _now()
        with self._book.transaction() as cursor:
            target = self._identify(cursor, identifier, now)
            outcome = self._book.revoke_token(
                cursor, target.id, revoked_by, now
            )
            if outcome is Revocation.LAST_VALID:
                raise LastValidToken(
                    "no se puede revocar el último token vigente"
                )
            if outcome is not Revocation.REVOKED:
                raise TokenNotValid(
                    "el token dejó de estar vigente durante la operación"
                )
            return target

    def _identify(
        self, cursor, identifier: str, now: datetime
    ) -> TokenRow:
        """Resolves an id or a label to one valid token row."""
        text = str(identifier).strip()
        if not text:
            raise TokenNotFound("falta el id o la etiqueta del token")
        if text.isdigit():
            row = self._book.token_by_id(cursor, int(text))
            if row is None:
                raise TokenNotFound("no existe un token con id %s" % text)
            if not _is_valid(row, now):
                raise TokenNotValid(
                    "el token %s está revocado o vencido" % text
                )
            return row
        rows = self._book.tokens_by_label(cursor, text)
        valid = [row for row in rows if _is_valid(row, now)]
        if not valid:
            if rows:
                raise TokenNotValid(
                    "la etiqueta %r está revocada o vencida" % text
                )
            raise TokenNotFound(
                "no existe un token con etiqueta %r" % text
            )
        if len(valid) > 1:
            raise AmbiguousLabel(
                "la etiqueta %r coincide con %d tokens vigentes; usá el id"
                % (text, len(valid))
            )
        return valid[0]

    # --- reading ---------------------------------------------------------

    def list_tokens(self) -> list[TokenRow]:
        """Every token, oldest first. It never carries the hash or secret."""
        with self._book.transaction() as cursor:
            return self._book.list_tokens(cursor)

    def resolve(self, secret: str) -> Client | None:
        """Hashes the secret and looks up a valid token, in one transaction."""
        token_hash = hash_token(secret)
        with self._book.transaction() as cursor:
            return self._book.resolve_token(cursor, token_hash)

    def count_valid(self, now: datetime | None = None) -> int:
        """How many tokens are valid now."""
        with self._book.transaction() as cursor:
            return self._book.count_valid(cursor, now)
