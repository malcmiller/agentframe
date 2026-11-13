"""Execution limits for a run."""

from dataclasses import dataclass, fields, replace

from agentframe.errors import DefinitionError


@dataclass(frozen=True, kw_only=True)
class RunLimits:
    """Execution limits for one run. More fields arrive in later milestones.

    Every field defaults to ``None``, meaning "unset": the value is inherited
    from the next lower level (library defaults -> agent -> skill -> per-run).
    The runtime fills unset fields from ``DEFAULT_LIMITS``.

    Attributes:
        timeout_seconds: Wall-clock time a run may take, including scoped
            service setup. Library default: 120 seconds.
        max_total_tokens: Input plus output tokens all of a run's model calls
            may use together. Library default: 100000.
        max_model_calls: Model calls a run may make, counting failed ones.
            Library default: 10.
        max_input_tokens: Input tokens one model call may send. Unset at
            every level means the model's reported context window; when the
            model does not report one, the run fails with ``DefinitionError``
            instead of guessing.
        max_model_retries: Times one model call is retried after a transient
            provider failure (rate limit, timeout, server error). Each retry
            is a new model call: it counts toward ``max_model_calls`` and the
            token budget. ``0`` disables retries. Library default: 2.
        max_tool_calls: Tool calls a run may make. Every call a model asks
            for counts, including ones with an unknown tool name or invalid
            arguments. Library default: 20.

    Raises:
        DefinitionError: If a set field is not greater than zero, or
            ``max_model_retries`` is negative.
    """

    timeout_seconds: float | None = None
    max_total_tokens: int | None = None
    max_model_calls: int | None = None
    max_input_tokens: int | None = None
    max_model_retries: int | None = None
    max_tool_calls: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "timeout_seconds",
            "max_total_tokens",
            "max_model_calls",
            "max_input_tokens",
            "max_tool_calls",
        ):
            value = getattr(self, name)
            if value is not None and not value > 0:
                raise DefinitionError(f"RunLimits.{name} must be greater than 0")
        if self.max_model_retries is not None and self.max_model_retries < 0:
            raise DefinitionError("RunLimits.max_model_retries must not be negative")


DEFAULT_LIMITS = RunLimits(
    timeout_seconds=120.0,
    max_total_tokens=100_000,
    max_model_calls=10,
    max_model_retries=2,
    max_tool_calls=20,
)
"""The library defaults: the lowest precedence level.

``max_input_tokens`` is left unset because its default is the model's own
context window, known only at run time.
"""


def check_within_ceiling(explicit: RunLimits, ceiling: RunLimits, where: str) -> None:
    """Check that explicitly set limits do not exceed a host's ceiling.

    Only explicit values are checked: library defaults are not requests, and
    ``cap_limits`` lowers them to the ceiling instead.

    Args:
        explicit: Limits set explicitly, for example an agent's merged with a
            skill's, without the library defaults.
        ceiling: The host's ceiling. Its unset fields cap nothing.
        where: Names the checked definition in the error, for example
            ``"Skill 'summarize'"``.

    Raises:
        DefinitionError: A set field is above the ceiling's value.
    """
    for field in fields(RunLimits):
        value = getattr(explicit, field.name)
        maximum = getattr(ceiling, field.name)
        if value is not None and maximum is not None and value > maximum:
            raise DefinitionError(
                f"{where}: RunLimits.{field.name} of {value} exceeds the host "
                f"ceiling of {maximum}"
            )


def cap_limits(limits: RunLimits, ceiling: RunLimits | None) -> RunLimits:
    """Lower each limit to the host's ceiling for that field.

    A field the ceiling sets is capped; a field it leaves unset is unchanged.
    An unset ``max_input_tokens`` (the model's context window) becomes the
    ceiling's value; the context window still bounds each call as well.

    Args:
        limits: Merged limits for one run.
        ceiling: The host's ceiling, or ``None`` for no ceiling.

    Returns:
        The capped limits.
    """
    if ceiling is None:
        return limits
    capped = {}
    for field in fields(RunLimits):
        value = getattr(limits, field.name)
        maximum = getattr(ceiling, field.name)
        if maximum is not None and (value is None or value > maximum):
            capped[field.name] = maximum
    return replace(limits, **capped)
