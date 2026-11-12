"""Execution limits for a run."""

from dataclasses import dataclass

from agentframe.errors import DefinitionError


@dataclass(frozen=True)
class RunLimits:
    """Execution limits for one run. More fields arrive in later milestones.

    Attributes:
        timeout_seconds: Wall-clock time a run may take, including scoped
            service setup. Must be greater than zero.

    Raises:
        DefinitionError: If ``timeout_seconds`` is not greater than zero.
    """

    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not self.timeout_seconds > 0:
            raise DefinitionError("RunLimits.timeout_seconds must be greater than 0")
