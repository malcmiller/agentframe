"""Execution limits for a run."""

from dataclasses import dataclass

from agentframe.errors import DefinitionError


@dataclass(frozen=True, kw_only=True)
class RunLimits:
    """Execution limits for one run. More fields arrive in later milestones.

    Every field defaults to ``None``, meaning "unset": the value is inherited
    from the next lower level (library defaults -> agent -> skill -> per-run).
    The runtime fills every unset field from ``DEFAULT_LIMITS``.

    Attributes:
        timeout_seconds: Wall-clock time a run may take, including scoped
            service setup. Must be greater than zero when set. Library
            default: 120 seconds.

    Raises:
        DefinitionError: If ``timeout_seconds`` is set but not greater than zero.
    """

    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and not self.timeout_seconds > 0:
            raise DefinitionError("RunLimits.timeout_seconds must be greater than 0")


DEFAULT_LIMITS = RunLimits(timeout_seconds=120.0)
"""The library defaults: the lowest precedence level, with every field set."""
