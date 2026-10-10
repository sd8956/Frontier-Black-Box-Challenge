"""HTTP frontier adapter.

It is the only place that knows HTTP: routes, the token, JSON parsing and the
frontier literals. It calls the domain pipeline and then the serializer, so the
domain never learns HTTP vocabulary and the serializer stays the only place
that knows the status and JSON keys of a domain result.

The frontier table (E404, E001, E100, E102 and the two ``{"ok": true}`` bodies)
lives here as literals, never built by concatenation. The route is resolved
before the token: any method+path that is not exactly ``POST /msg``,
``GET /health`` or ``POST /__reset`` is E404 even when no token is sent.

State is the ``api.state.MemoryState`` adapter behind the domain ``State``
port. The ledger, its per-client lock and its SQL are a later work unit; until
then a single in-process client is served and reset by swapping the in-memory
adapter for an empty one.

Runs with: ``TEAM_TOKEN=... python3 -m api.adapter``
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api.domain.pipeline import Configuration, process
from api.serializer import serialize
from api.state import MemoryState

# --- frontier literals, copied from the measured original -----------------

_E404 = (404, {"error": "E404", "mensaje": "ruta desconocida"})
_E001 = (401, {"error": "E001", "mensaje": "no autorizado"})
_E100 = (400, {"error": "E100", "mensaje": "json inválido"})
_E102 = (400, {"error": "E102", "mensaje": "falta m"})
_HEALTH = (200, {"ok": True})
_RESET = (200, {"ok": True})

# The only three routes that exist. Everything else is E404, whatever the
# method. Query strings are part of the path and never match.
_ROUTES = frozenset(
    {
        ("POST", "/msg"),
        ("GET", "/health"),
        ("POST", "/__reset"),
    }
)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


# --- per-client state ----------------------------------------------------

class _Session:
    """One authenticated client: state, configuration and the lock around it.

    The ``State`` port has no ``reset()`` in this work unit (the ledger adds it
    bound to the client), so a reset swaps the in-memory adapter for an
    equivalent empty instance that keeps its label. The lock serializes access
    to the in-memory adapter; the ledger's per-client advisory lock replaces it
    later.
    """

    def __init__(self, state, configuration):
        self.state = state
        self.configuration = configuration
        self._lock = threading.Lock()

    def run(self, message):
        with self._lock:
            return process(message, self.state, self.configuration)

    def reset(self):
        with self._lock:
            self.state = _empty_like(self.state)


def _empty_like(state):
    """Builds an empty adapter of the same type, keeping the client label."""
    try:
        return type(state)(client=getattr(state, "client", "test"))
    except TypeError:
        return type(state)()


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
    """One thread per request; every handler shares the same session."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, token, session):
        super().__init__(address, handler)
        self.token = token
        self.session = session
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
        elif not self._authorized():
            status, body = _E001
            token_state = (
                "absent" if self.headers.get("Authorization") is None else "invalid"
            )
            failure = "auth"
        else:
            token_state = "valid"
            if route == ("GET", "/health"):
                status, body = _HEALTH
            elif route == ("POST", "/__reset"):
                self.server.session.reset()
                status, body = _RESET
                committed = True
            else:
                status, body, command = self._handle_message()
                committed = 200 <= status < 300

        self._respond(status, body)
        self._log(
            started, route, token_state, command, status, committed, failure
        )

    def _authorized(self):
        """The header must be exactly ``Bearer <token>``, with the space."""
        return self.headers.get("Authorization") == "Bearer " + self.server.token

    def _handle_message(self):
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
            message, self.server.session.configuration.commands
        )
        result = self.server.session.run(message)
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

def create_server(host, port, token, state, configuration=None):
    """Builds the HTTP server without starting it.

    The caller owns the thread and the shutdown; the tests use port 0.
    """
    session = _Session(state, configuration or Configuration())
    return _Server((host, port), _Handler, token, session)


def main():
    token = os.environ.get("TEAM_TOKEN")
    if not token:
        print("TEAM_TOKEN is required", file=sys.stderr)
        return 2
    host = os.environ.get("HOST", DEFAULT_HOST)
    try:
        port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT

    server = create_server(host, port, token, MemoryState(client="primary"))
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
