"""Per-run context passed to skills."""

from dataclasses import dataclass

from agentframe.principal import Principal


@dataclass(frozen=True)
class RunContext:
    """Per-run information passed to every skill. Never shared between runs.

    Attributes:
        invocation_id: Library-generated identifier of this run.
        principal: Verified caller identity, or ``None`` when none was supplied.
        deadline: Event-loop time (``loop.time()``) when the run times out.
    """

    invocation_id: str
    principal: Principal | None
    deadline: float
