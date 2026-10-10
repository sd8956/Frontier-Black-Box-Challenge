"""Domain validation pipeline.

The message `m` is split by `;`. The first field, uppercased, selects the
command; the last is the control; the middle are the arguments. The stages are
in an ordered, configurable list because the real order between them is NOT
confirmed: it is changed in a single place (`Configuration.stages`).

Each stage is a small function that returns a `Result` to stop or `None` to
continue. Default order: control, command, arity, business.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Mapping

from api.domain.commands import COMMANDS, Command
from api.domain.state import State
from api.domain.result import (
    InvalidArity,
    UnknownCommand,
    InvalidControl,
    EmptySegment,
    MalformedControl,
    Result,
)

_DIGITS = frozenset("0123456789")


def is_numeric_control(control: str) -> bool:
    """A "numeric" control is non-empty and ASCII digits only.

    Assumption: no control with a sign, a decimal point, a space or unicode was
    seen.
    """
    return bool(control) and all(char in _DIGITS for char in control)


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
    explorer learns it. Until then the declared default is to accept any
    numeric control.
    """

    @abstractmethod
    def accept(self, message: str, control: str) -> bool:
        """True if the numeric control is valid for this message."""


class AcceptAnyNumericControl(ControlStrategy):
    """Declared default: any numeric control is accepted.

    Measured cost: the steps that test E202 on purpose (1 in 20 in the visible
    sample) fail on purpose. It is an assumption and it is written as such; it
    is better than rejecting everything.
    """

    def accept(self, message: str, control: str) -> bool:
        return True


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

def empty_segment_stage(context: Context) -> Result | None:
    """Any empty segment wins over the control error.

    Measured once: `PING;64;` answers E104 and not E201. The stage runs first
    so the empty segment wins, as measured. Assumption until it is repeated.
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

    control_strategy: ControlStrategy = field(
        default_factory=AcceptAnyNumericControl
    )
    commands: dict[str, Command] = field(default_factory=lambda: dict(COMMANDS))
    # Assumption: cap per operation. Example 10 (100000) is accepted and 9
    # (120000) is rejected, so the cap is between 100000 and 120000; 100000 is
    # chosen as the smallest consistent one. It was not confirmed whether it is
    # per operation or accumulated.
    operation_limit: int = 100000
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
