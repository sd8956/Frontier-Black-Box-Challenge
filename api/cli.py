"""Administration CLI for the token registry.

The contract admits no administration route, so credentials are administered
from the command line. This is a thin shell over `api.registry.Registry`; all
policy lives there and all SQL lives in `api.ledger`.

Usage::

    python3 -m api.cli crear --label X [--days N] [--by who]
    python3 -m api.cli registrar --label X [--days N] [--by who]
    python3 -m api.cli revocar <id|label> [--by who]
    python3 -m api.cli listar

`crear` prints the new secret exactly once, to stdout, and says on stderr that
it will not be shown again. `registrar` reads the secret from stdin, never
from an argument, so it does not land in the shell history or in `ps`. No
other path prints a secret. Errors go to stderr with a non-zero exit.

Operator text is Spanish, like the design and the wire; identifiers and code
are English, like the rest of the repository.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timezone

from api.ledger import BookUnavailable
from api.registry import DEFAULT_DAYS, PepperMissing, Registry, RegistryError


def _default_by() -> str:
    """Who is running the command, for `created_by` and `revoked_by`."""
    return (
        os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or os.environ.get("USERNAME")
        or "cli"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m api.cli",
        description="Administración del registro de tokens.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.required = True

    crear = commands.add_parser(
        "crear", help="crea un token y muestra su secreto una sola vez"
    )
    crear.add_argument("--label", required=True, help="para quién es")
    crear.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help="días de vigencia"
    )
    crear.add_argument("--by", default=None, help="quién lo administra")

    registrar = commands.add_parser(
        "registrar", help="registra un secreto existente, leído por stdin"
    )
    registrar.add_argument("--label", required=True, help="para quién es")
    registrar.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help="días de vigencia"
    )
    registrar.add_argument("--by", default=None, help="quién lo administra")

    revocar = commands.add_parser(
        "revocar", help="revoca un token por id o por etiqueta"
    )
    revocar.add_argument("identifier", help="id numérico o etiqueta")
    revocar.add_argument("--by", default=None, help="quién lo revoca")

    commands.add_parser(
        "listar", help="lista los tokens, sin secretos ni hashes"
    )
    return parser


def _format_moment(value) -> str:
    if value is None:
        return "-"
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _print_tokens(rows: list) -> None:
    """One line per token; never the secret nor the hash (they are absent)."""
    if not rows:
        print("No hay tokens registrados.")
        return
    header = ("ID", "ETIQUETA", "CREADO_POR", "CREADO", "VENCE", "REVOCADO")
    table = [header]
    for row in rows:
        table.append(
            (
                str(row.id),
                row.label,
                row.created_by,
                _format_moment(row.created_at),
                _format_moment(row.expires_at),
                _format_moment(row.revoked_at),
            )
        )
    widths = [max(len(line[index]) for line in table) for index in range(6)]
    for line in table:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(line)))


def _read_secret_stdin() -> str:
    """The secret arrives on stdin, never as an argument. Only the newline
    is trimmed, so a secret with spaces is not silently altered."""
    return sys.stdin.readline().rstrip("\r\n")


def main(argv=None, registry=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    created_by = getattr(args, "by", None) or _default_by()

    try:
        if registry is None:
            registry = Registry()

        if args.command == "crear":
            secret = registry.create(args.label, created_by, args.days)
            print(secret)
            print("Guardá este secreto: no se vuelve a mostrar.", file=sys.stderr)
            return 0

        if args.command == "registrar":
            secret = _read_secret_stdin()
            if not secret:
                print(
                    "error: no se leyó ningún secreto por stdin",
                    file=sys.stderr,
                )
                return 1
            token_id = registry.register(
                secret, args.label, created_by, args.days
            )
            print(
                "Token registrado: id=%d etiqueta=%s"
                % (token_id, args.label)
            )
            return 0

        if args.command == "revocar":
            row = registry.revoke(args.identifier, created_by)
            print("Token revocado: id=%d etiqueta=%s" % (row.id, row.label))
            return 0

        if args.command == "listar":
            _print_tokens(registry.list_tokens())
            return 0

        parser.error("comando desconocido: %s" % args.command)
        return 2
    except PepperMissing:
        print(
            "error: falta REGISTRO_PEPPER en el entorno", file=sys.stderr
        )
        return 2
    except BookUnavailable as error:
        print(
            "error: la base no está disponible (%s)" % error, file=sys.stderr
        )
        return 2
    except RegistryError as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
