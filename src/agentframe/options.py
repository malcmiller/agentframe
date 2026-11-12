"""Per-run options and field-by-field settings precedence."""

from dataclasses import dataclass, fields, replace

from agentframe.limits import RunLimits
from agentframe.models import ModelSettings


@dataclass(frozen=True, kw_only=True)
class RunOptions:
    """Execution settings for one local run, kept apart from skill arguments.

    Per-run options have the highest precedence: each field they set
    overrides the skill's, agent's and library's value for that field.
    Hosted requests never accept per-run options from callers.

    Attributes:
        limits: Limits for this run. Unset fields are inherited.
        model_settings: Model settings for this run. Unset fields are inherited.
    """

    limits: RunLimits | None = None
    model_settings: ModelSettings | None = None


def merge_settings[T: (RunLimits, ModelSettings)](base: T, *overrides: T | None) -> T:
    """Combine settings field by field, from lowest to highest precedence.

    A field set in a later override replaces the value from earlier levels.
    A field left unset (``None``) keeps the earlier value, so overriding one
    field never resets the others. ``None`` overrides are skipped.

    Args:
        base: The lowest level, usually the library defaults.
        overrides: Higher levels in order, for example agent, skill and run.

    Returns:
        A new settings object of the same type as ``base``.
    """
    merged = base
    for override in overrides:
        if override is None:
            continue
        explicitly_set = {
            field.name: getattr(override, field.name)
            for field in fields(override)
            if getattr(override, field.name) is not None
        }
        merged = replace(merged, **explicitly_set)
    return merged
