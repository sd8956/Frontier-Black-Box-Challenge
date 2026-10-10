"""Domain validation pipeline.

The message `m` is split by `;`. The first field, uppercased, selects the
command; the last is the control; the middle are the arguments. The stages are
in an ordered, configurable list because the real order between them is NOT
confirmed: it is changed in a single place (`Configuration.stages`).

Each stage is a small function that returns a `Result` to stop or `None` to
continue. Default order: missing-control, empty-segment, control, command,
arity, business.

The control table (`api/control_table.json`) is generated from
`experimentos.jsonl`, never hand-written. Generator rule, exactly:

  - keep every record whose `request` is a dict with a string `m` containing
    `;`;
  - split off the last field with `explorer.log.payload_of` (`base`,
    `control`);
  - a record whose `explorer.log.is_control_accepted(record["response"])` is
    true adds its control to `accept`; a record whose response error is
    `E202` adds it to `reject`;
  - map each `base` to both sorted lists. A base with an empty `accept` list
    is kept on purpose: it was probed and the probe was rejected, which is a
    measurement, not a missing entry.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from api.domain.commands import COMMANDS, Command
from api.domain.state import State
from api.domain.result import (
    InvalidArity,
    UnknownCommand,
    InvalidControl,
    EmptySegment,
    MalformedControl,
    MissingControl,
    Result,
)

_DIGITS = frozenset("0123456789")


def is_numeric_control(control: str) -> bool:
    """A "numeric" control is non-empty and ASCII digits only.

    Assumption: no control with a sign, a decimal point, a space or unicode was
    seen.
    """
    return bool(control) and all(char in _DIGITS for char in control)


# --- the measured control table ------------------------------------------

# Loaded once, at import time, from the package directory. Generated from
# `experimentos.jsonl`; the rule lives in the module docstring above, because
# JSON has no comments.
_CONTROL_TABLE_PATH = Path(__file__).resolve().parent.parent / "control_table.json"


def _load_control_table(
    path: Path = _CONTROL_TABLE_PATH,
) -> dict[str, tuple[frozenset, frozenset]]:
    """Loads the measured control oracle, or fails loudly.

    There is no fallback: a missing table would silently turn the measured
    default back into "accept everything", which is the assumption the table
    replaces. The failure names the file instead.
    """
    try:
        with path.open(encoding="utf-8") as file:
            raw = json.load(file)
    except FileNotFoundError as error:
        raise RuntimeError(
            "the measured control table is missing: %s. It is generated from "
            "experimentos.jsonl; refusing to fall back to accepting every "
            "control." % path
        ) from error
    return {
        base: (frozenset(entry["accept"]), frozenset(entry["reject"]))
        for base, entry in raw.items()
    }


_CONTROL_TABLE = _load_control_table()


def _payload_base(message: str) -> str | None:
    """The message without its last `;`-separated field, or ``None``.

    This repeats the split the table generator used: the payload is what stays
    fixed while the control changes. The domain does not import the explorer
    (it is a separate, closed component), so the split is written here.
    """
    if not isinstance(message, str) or ";" not in message:
        return None
    return message.rsplit(";", 1)[0]


# --- the control function: the seam and the honesty ----------------------

class ControlStrategy(ABC):
    """Injected strategy that decides whether a numeric control is accepted.

    The control rule is UNKNOWN. Three offline attempts to derive it failed
    (about 30 candidates: sums of ordinals and position-weighted sums, XOR,
    polynomial hashes in several bases, CRC32, Adler32, MD5, SHA1 and SHA256,
    crossed with five normalizations and looking for pure modulus and affine
    transformations). What was measured, and only what was measured:

      - it is deterministic and a pure function of the message (it is not a
        nonce or a counter);
      - it is case-insensitive;
      - it depends on the order: `CONSULTA;AB` was accepted with 92 and
        `CONSULTA;BA` with 65;
      - the contribution of the last character depends on its ordinal modulo 9
        (`A` 98, `B` 95, ... `I` 74, and `J` 98 again).

    No formula is invented and the measured pairs are not fixed as if they were
    the rule. This is the seam: the real validator enters here when the
    explorer learns it. Until then the default is ``MeasuredControls``: it
    accepts the measured control of a payload that was measured, and any
    numeric control only for a payload that was not.
    """

    @abstractmethod
    def accept(self, message: str, control: str) -> bool:
        """True if the numeric control is valid for this message."""


class AcceptAnyNumericControl(ControlStrategy):
    """Declared fallback seam: any numeric control is accepted.

    It is no longer the shipped default (``MeasuredControls`` is); it is kept
    as the documented fallback for a payload that was never measured, and as
    the seam where the real formula enters once the explorer learns it. Where
    a payload was not measured, the steps that test E202 on purpose (1 in 20
    in the visible sample) still fail on purpose. It is an assumption and it
    is written as such; it is better than rejecting everything.
    """

    def accept(self, message: str, control: str) -> bool:
        return True


class MeasuredControls(ControlStrategy):
    """Default: the measured table, with a declared fallback.

    The decision has four cases, in this order:

    1. the control is not numeric -> rejected. Measured: the accepted control
       is always numeric, so a non-numeric one can never be it;
    2. the payload is absent from the table -> accepted (any numeric control).
       Declared assumption: that payload was never probed, so there is no
       measurement for it; accepting repeats the assumption
       :class:`AcceptAnyNumericControl` declares;
    3. the payload is present with a non-empty ``accept`` list -> accepted only
       when the control is in that list, rejected otherwise. Measured: the
       explorer swept those payloads and found their single valid control, so
       an unlisted control is a measured rejection;
    4. the payload is present with an empty ``accept`` list -> rejected when
       the control is in its ``reject`` list, accepted otherwise. Measured
       half: the ``reject`` list is what the explorer probed and saw rejected
       with E202. Declared half: the rest of the space was never probed, so it
       keeps the declared assumption instead of rejecting everything.

    The formula is still unknown; the table is only the sample of it the
    explorer measured.
    """

    def accept(self, message: str, control: str) -> bool:
        if not is_numeric_control(control):
            return False
        entry = _CONTROL_TABLE.get(_payload_base(message))
        if entry is None:
            return True
        accepted, rejected = entry
        if accepted:
            return int(control) in accepted
        return int(control) not in rejected


# --- context -------------------------------------------------------------

@dataclass(frozen=True)
class Environment:
    """What the domain needs from outside: state and configuration."""

    state: State
    operation_limit: int
    control_strategy: ControlStrategy
    commands: Mapping[str, Command]


@dataclass
class Context:
    """State of a message through the stages."""

    message: str
    fields: list[str]
    command: str
    arguments: list[str]
    control: str
    env: Environment
    command_def: Command | None = None


Stage = Callable[[Context], "Result | None"]


# --- stages --------------------------------------------------------------

def missing_control_stage(context: Context) -> Result | None:
    """A message with no `;` at all is E103, before any other error.

    Measured twice: id 2340 (`m` = `PING`) and id 2341 (`m` = `""`) both
    answer 400 E103 `falta control`. The stage runs before the empty-segment
    stage so that a message with no `;` gets E103 while `PING;64;` keeps its
    measured E104. The order against E104 follows from those two probes, not
    from a formula.
    """
    if ";" not in context.message:
        return MissingControl()
    return None


def empty_segment_stage(context: Context) -> Result | None:
    """Any empty segment wins over the control error.

    Measured once: `PING;64;` answers E104 and not E201. The stage runs before
    the control stage so the empty segment wins, as measured. Assumption until
    it is repeated.
    """
    if any(field == "" for field in context.fields):
        return EmptySegment()
    return None


def control_stage(context: Context) -> Result | None:
    """The control is non-empty and numeric; then the strategy decides."""
    if not is_numeric_control(context.control):
        return MalformedControl()
    if not context.env.control_strategy.accept(
        context.message, context.control
    ):
        return InvalidControl()
    return None


def command_stage(context: Context) -> Result | None:
    """The first field, uppercased, has to exist."""
    context.command_def = context.env.commands.get(context.command)
    if context.command_def is None:
        return UnknownCommand()
    return None


def arity_stage(context: Context) -> Result | None:
    """The arguments are exactly the ones the command declares."""
    command = context.command_def
    if command is None:
        # If the order changes and arity goes before command, an unknown
        # command is not decided here: `command_stage` decides it.
        command = context.env.commands.get(context.command)
        if command is None:
            return None
    if len(context.arguments) != command.arity:
        return InvalidArity()
    return None


def business_stage(context: Context) -> Result | None:
    """Executes the effect of the command."""
    if context.command_def is None:
        return None
    return context.command_def.handle(context.arguments, context.env)


DEFAULT_ORDER: list[Stage] = [
    missing_control_stage,
    empty_segment_stage,
    control_stage,
    command_stage,
    arity_stage,
    business_stage,
]


# --- configuration and execution ----------------------------------------

@dataclass
class Configuration:
    """Domain configuration. The cap and the order are configuration, not constants."""

    control_strategy: ControlStrategy = field(default_factory=MeasuredControls)
    commands: dict[str, Command] = field(default_factory=lambda: dict(COMMANDS))
    # Measured against version 2: the cap is 50000 cents. 50000 is accepted
    # and 50001 is rejected; the cap is per operation, not accumulated (three
    # consecutive transfers of 40000 were all accepted). Deposits are exempt:
    # `DEPOSITO;AC-0001;250000;86` is accepted. The measured cap replaces the
    # version-1 assumption of 100000 (which version 1 did accept).
    operation_limit: int = 50000
    stages: list[Stage] = field(default_factory=lambda: list(DEFAULT_ORDER))


def _split(message: str) -> tuple[list[str], str, list[str], str]:
    fields = message.split(";")
    command = fields[0].upper()
    control = fields[-1]
    arguments = fields[1:-1]
    return fields, command, arguments, control


def process(
    message: str, state: State, configuration: Configuration | None = None
) -> Result:
    """Runs the full pipeline and returns the typed result."""
    if configuration is None:
        configuration = Configuration()

    fields, command, arguments, control = _split(message)
    context = Context(
        message=message,
        fields=fields,
        command=command,
        arguments=arguments,
        control=control,
        env=Environment(
            state=state,
            operation_limit=configuration.operation_limit,
            control_strategy=configuration.control_strategy,
            commands=configuration.commands,
        ),
    )

    for stage in configuration.stages:
        result = stage(context)
        if result is not None:
            return result

    # If the business stage is not in the list, the message was left unresolved.
    raise RuntimeError("the pipeline finished without producing a result")
