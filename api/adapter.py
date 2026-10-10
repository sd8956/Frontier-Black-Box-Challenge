"""HTTP frontier adapter.

It is the only place that knows HTTP: routes, the auth header, JSON parsing
and the frontier literals. It calls the domain pipeline and then the
serializer, so the domain never learns HTTP vocabulary and the serializer stays
the only place that knows the status and JSON keys of a domain result.

The frontier table (E404, E001, E100, E102 and the two ``{"ok": true}`` bodies)
lives here as literals, never built by concatenation. The route is resolved
before the token: any method+path that is not exactly ``POST /msg``,
``GET /health`` or ``POST /__reset`` is E404 even when no token is sent.

One request, one transaction. The adapter opens it through the injected book,
resolves the token *inside* it (so a revocation committed mid-request cannot be
confirmed), takes the per-client lock, binds the domain state with
``for_client`` and answers only after the transaction committed. The book owns
all the SQL and the hasher owns the pepper; the adapter owns neither and never
reads ``TEAM_TOKEN``.

Runs with: ``DATABASE_URL=... REGISTRO_PEPPER=... python3 -m api.adapter``
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api.domain.pipeline import Configuration, process
from api.ledger import Book, BookUnavailable
from api.registry import hash_token
from api.serializer import serialize

# --- frontier literals, copied from the measured original -----------------

_E404 = (404, {"error": "E404", "mensaje": "ruta desconocida"})
_E001 = (401, {"error": "E001", "mensaje": "no autorizado"})
_E100 = (400, {"error": "E100", "mensaje": "json inválido"})
_E102 = (400, {"error": "E102", "mensaje": "falta m"})
_HEALTH = (200, {"ok": True})
_RESET = (200, {"ok": True})
# The 503 is NOT a measured fact: it is the design's declared assumption for
# "the database does not answer or the lock does not fire and nothing was
# committed" (docs/design.md section 4.6). It stays in this table next to the
# literals copied from the original so the single place that can change it is
# obvious. An unexpected exception is answered the same way: nothing was
# committed, so it must not become a framework 500 with a body we never saw.
_UNAVAILABLE = (503, {"error": "no-disponible"})

# The only three routes that exist. Everything else is E404, whatever the
# method. Query strings are part of the path and never match.
_ROUTES = frozenset(
    {
        ("POST", "/msg"),
        ("GET", "/health"),
        ("POST", "/__reset"),
    }
)

# The header must be exactly ``Bearer <secret>`` with the space. A missing,
# prefix-less or empty secret never reaches the database. The shape is checked
# before the transaction; whether the secret is a known token is decided by
# `resolve_token` inside it.
_BEARER_PREFIX = "Bearer "

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


def _command_label(message, commands):
    """The command name, and only when it is one the registry knows.

    The trace carries the command because the design asks for it, but the first
    field of the message is client text: a message without `;` would put
    whatever the caller typed into the log. Anything outside the registry is
    logged as `-`, which is what an unknown command is anyway.
    """
    candidate = message.split(";")[0].strip().upper()
    return candidate if candidate in commands else "-"


# --- server and handler --------------------------------------------------

class _Server(ThreadingHTTPServer):
    """One thread per request; every handler shares the same book."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, book, hasher, configuration):
        super().__init__(address, handler)
        self.book = book
        self.hasher = hasher
        self.configuration = configuration
        self._request_ids = itertools.count(1)

    def next_request_id(self):
        return next(self._request_ids)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "reto2-replacement/1.0"

    def log_message(self, format, *args):
        """Silences the default stderr line; our own log is the only one."""
        return

    def __getattr__(self, name):
        """Sends any other method through the same catch-all.

        So ``PATCH /msg`` is E404 and not the framework's 501.
        """
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    # --- the single entry point -----------------------------------------

    def _dispatch(self):
        started = time.monotonic()
        route = (self.command, self.path)
        token_state = "absent"
        command = None
        failure = None
        committed = False

        if route not in _ROUTES:
            # Route first: an unknown route is E404 even without a token.
            status, body = _E404
            failure = "ruta"
        else:
            secret = self._bearer_secret()
            if secret is None:
                status, body = _E001
                if self.headers.get("Authorization") is not None:
                    token_state = "invalid"
                failure = "auth"
            else:
                try:
                    (
                        status,
                        body,
                        token_state,
                        command,
                        committed,
                        failure,
                    ) = self._run(route, secret)
                except BookUnavailable:
                    # Nothing was committed: the database is down or the lock
                    # wait hit its cap.
                    status, body = _UNAVAILABLE
                    failure = "unavailable"
                except Exception:  # noqa: BLE001
                    # The transaction rolled back before this surfaced, so
                    # nothing was committed either. Keep the body we declared
                    # rather than a framework 500 nobody ever measured.
                    status, body = _UNAVAILABLE
                    failure = "internal"

        self._respond(status, body)
        self._log(
            started, route, token_state, command, status, committed, failure
        )

    def _bearer_secret(self):
        """The secret of a well-formed ``Bearer <secret>``, or ``None``."""
        header = self.headers.get("Authorization")
        if header is None or not header.startswith(_BEARER_PREFIX):
            return None
        secret = header[len(_BEARER_PREFIX):]
        return secret or None

    # --- the authenticated path, inside one transaction ------------------

    def _run(self, route, secret):
        """Resolves the token, locks the client and runs the route.

        The whole thing happens inside one ``book.transaction()``: the token is
        re-read here so a revocation committed mid-request cannot be confirmed,
        and the response is returned only after the context manager commits.
        The book opens one connection per transaction on purpose: concurrent
        requests must not share a connection. That is also the seam where a
        connection pool goes later.
        """
        with self.server.book.transaction() as cursor:
            client = self.server.book.resolve_token(
                cursor, self.server.hasher(secret)
            )
            if client is None:
                # Unknown, revoked or expired: 401 and nothing is committed.
                return (*_E001, "invalid", None, False, "auth")

            if route == ("GET", "/health"):
                # Authentication only; no lock, no client state, no domain.
                # Dependency-aware readiness lives on the internal admin port.
                return (*_HEALTH, "valid", None, False, None)

            self.server.book.lock(cursor, client.id)

            if route == ("POST", "/__reset"):
                self.server.book.reset(cursor, client.id)
                return (*_RESET, "valid", None, True, None)

            ledger = self.server.book.for_client(cursor, client.id)
            status, body, command = self._handle_message(ledger)
            return (status, body, "valid", command, 200 <= status < 300, None)

    def _handle_message(self, ledger):
        """Parses the body in the measured order: JSON, then the ``m`` field."""
        raw = self._read_body()
        if not raw:
            return (*_E100, None)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return (*_E100, None)
        if not isinstance(payload, dict) or not isinstance(payload.get("m"), str):
            return (*_E102, None)

        message = payload["m"]
        command = _command_label(
            message, self.server.configuration.commands
        )
        result = process(message, ledger, self.server.configuration)
        status, body = serialize(result)
        return (status, body, command)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _respond(self, status, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        # HEAD keeps the headers and drops the body, as the original does.
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _log(self, started, route, token_state, command, status, committed, failure):
        """One line per request, without token, Authorization or raw `m`.

        It never raises: logging must not take the request down.
        """
        try:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            moment = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            line = (
                f"{moment} req={self.server.next_request_id()} "
                f"route={route[0]} {route[1]} token={token_state} "
                f"command={command or '-'} result={status} "
                f"committed={'true' if committed else 'false'} "
                f"ms={elapsed_ms:.1f} failure={failure or '-'}"
            )
            print(line, file=sys.stdout, flush=True)
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


# --- factory and entry point ---------------------------------------------

def create_server(host, port, book, hasher, configuration=None):
    """Builds the HTTP server without starting it.

    `book` is the SQL book of `api/ledger.py` (or a double) and `hasher` is
    `hash_token` with its pepper already bound. The caller owns the thread and
    the shutdown; the tests use port 0.
    """
    return _Server(
        (host, port),
        _Handler,
        book,
        hasher,
        configuration or Configuration(),
    )


def main():
    host = os.environ.get("HOST", DEFAULT_HOST)
    try:
        port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT

    server = create_server(host, port, Book(), hash_token)
    print(f"listening on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
