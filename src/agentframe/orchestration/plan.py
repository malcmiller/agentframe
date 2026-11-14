"""Execution plans: a typed graph of remote calls, checked before it runs.

A ``Plan`` is a flat list of ``PlanStep``s, each one call to a skill catalog
entry. Steps depend on each other through references to earlier results and
through ``after``; the plan must be a directed acyclic graph. ``check_plan``
validates a plan against a catalog without any I/O and returns its steps in
parallel groups. Running a plan is a separate, later stage.
"""

import re
import string
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import BaseModel, ConfigDict, JsonValue

from agentframe.errors import DefinitionError, InputValidationError

if TYPE_CHECKING:
    # Imported only for type checking: the catalog module imports this one.
    from agentframe.orchestration.catalog import RemoteSkill

DEFAULT_MAX_STEPS = 8
"""Steps a plan may have unless the author passes another ``max_steps``."""

MAX_PLAN_BYTES = 64 * 1024
"""The largest a plan may be, serialized as JSON."""

_STEP_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_REFERENCE = re.compile(
    r"(?P<step>[a-z][a-z0-9_]{0,31})(?:\.(?P<field>[A-Za-z0-9_-]{1,64}))?"
)
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")

type StepGroups = tuple[tuple["PlanStep", ...], ...]


class PlanStep(BaseModel):
    """One call in a ``Plan``: a catalog entry with its arguments or text.

    Attributes:
        id: The step's name, unique in the plan, matching
            ``^[a-z][a-z0-9_]{0,31}$``. Other steps refer to it.
        use: The name of the skill catalog entry the step calls, for example
            ``"research-agent.find_sources"``, or an agent's name for ``ask``.
        arguments: JSON arguments for a structured entry. Any value, at any
            depth, may be a reference ``{"$ref": "step"}`` or
            ``{"$ref": "step.field"}``: the step's whole result, or one
            top-level field of it.
        text: The message for an ``ask`` entry. It may contain ``{step}`` or
            ``{step.field}`` placeholders; ``{{`` and ``}}`` are literal
            braces.
        after: Steps that must complete first although no data flows from
            them, for example "notify after saving".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    use: str
    arguments: dict[str, JsonValue] = {}
    text: str | None = None
    after: list[str] = []


class Plan(BaseModel):
    """An execution plan: steps whose order comes from their dependencies.

    Construct one by hand for tests or reviewed plans; there is no builder.
    Check it with ``SkillCatalog.check`` before running it.

    Attributes:
        steps: The steps, in any order.
        result: The ID of the step whose value is the plan's value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: list[PlanStep]
    result: str


def check_plan(
    plan: Plan,
    entry: Callable[[str], "RemoteSkill | None"],
    max_steps: int,
) -> StepGroups:
    """Check ``plan`` against a catalog without I/O; see ``SkillCatalog.check``.

    Args:
        plan: The plan to check.
        entry: Looks a catalog entry up by name.
        max_steps: The most steps the plan may have.

    Returns:
        The steps in parallel groups, in a deterministic topological order.

    Raises:
        InputValidationError: The plan is invalid; see ``SkillCatalog.check``.
    """
    if len(plan.steps) > max_steps:
        raise InputValidationError(
            f"The plan has {len(plan.steps)} steps, more than the {max_steps} "
            "this run allows"
        )
    if len(plan.model_dump_json().encode()) > MAX_PLAN_BYTES:
        raise InputValidationError(
            f"The plan is larger than {MAX_PLAN_BYTES // 1024} KiB serialized"
        )
    _check_ids(plan.steps)
    steps = {step.id: step for step in plan.steps}
    for step in plan.steps:
        _check_entry(step, entry(step.use))
    # Only now are ask steps known to have text, which references rely on.
    for step in plan.steps:
        _check_references(step, steps)
    if plan.result not in steps:
        raise InputValidationError("Plan field 'result' names no step of the plan")
    return _groups(plan.steps)


def checked_max_steps(max_steps: int) -> int:
    """Return ``max_steps`` if it is a whole number greater than 0.

    Raises:
        DefinitionError: It is not.
    """
    # bool is an int subclass, but True is not a step count.
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
        raise DefinitionError("max_steps= must be a whole number greater than 0")
    return max_steps


def dependencies(step: PlanStep) -> dict[str, str]:
    """Return the steps ``step`` depends on, each with the field that says so.

    The field is ``"arguments"``, ``"text"`` or ``"after"``, the first that
    names the step. Assumes the step's references are well formed.
    """
    found: dict[str, str] = {}
    for reference in _argument_references(list(step.arguments.values())):
        found.setdefault(_reference_step(reference["$ref"]), "arguments")
    for placeholder in _placeholders(step.text or ""):
        found.setdefault(_reference_step(placeholder), "text")
    for name in step.after:
        found.setdefault(name, "after")
    return found


def _check_ids(steps: Sequence[PlanStep]) -> None:
    seen: set[str] = set()
    for position, step in enumerate(steps, start=1):
        # A malformed ID is never echoed: the plan may come from a model.
        if not _STEP_ID.fullmatch(step.id):
            raise InputValidationError(
                f"Plan step {position} field 'id' must match ^[a-z][a-z0-9_]{{0,31}}$"
            )
        if step.id in seen:
            raise InputValidationError(
                f"Plan step '{step.id}' field 'id' is used by more than one step"
            )
        seen.add(step.id)


def _check_entry(step: PlanStep, entry: "RemoteSkill | None") -> None:
    """Check the step's entry exists and suits its ``arguments`` or ``text``."""
    if entry is None:
        raise InputValidationError(
            f"Plan step '{step.id}' field 'use' names no entry of the skill catalog"
        )
    if entry.skill is None:
        if step.arguments:
            raise InputValidationError(
                f"Plan step '{step.id}' field 'arguments' is only for structured "
                f"entries; '{entry.name}' is an ask entry"
            )
        if step.text is None:
            raise InputValidationError(
                f"Plan step '{step.id}' field 'text' is required for the ask "
                f"entry '{entry.name}'"
            )
        return
    if step.text is not None:
        raise InputValidationError(
            f"Plan step '{step.id}' field 'text' is only for ask entries; "
            f"'{entry.name}' takes arguments"
        )
    if "$ref" in step.arguments:
        raise InputValidationError(
            f"Plan step '{step.id}' field 'arguments' must name arguments; put a "
            "reference in an argument's value"
        )
    _check_top_level_arguments(step, entry)


def _check_top_level_arguments(step: PlanStep, entry: "RemoteSkill") -> None:
    """Check required and unknown arguments against the schema's top level.

    A referenced value counts as present. The receiving agent validates
    everything else, as it does for every call.
    """
    schema = entry.arguments_schema or {}
    required = schema.get("required", [])
    for name in required if isinstance(required, list) else []:
        if isinstance(name, str) and name not in step.arguments:
            raise InputValidationError(
                f"Plan step '{step.id}' field '{_argument_field(name)}' is "
                f"required by '{entry.name}'"
            )
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is not False:
        return
    for name in step.arguments:
        if not isinstance(properties, dict) or name not in properties:
            raise InputValidationError(
                f"Plan step '{step.id}' field '{_argument_field(name)}' is not "
                f"an argument of '{entry.name}'"
            )


def _check_references(step: PlanStep, steps: Mapping[str, PlanStep]) -> None:
    """Check every reference, placeholder and ``after`` names a plan step."""
    for name, value in step.arguments.items():
        for reference in _argument_references(value):
            target = reference.get("$ref")
            if len(reference) != 1 or not isinstance(target, str):
                raise InputValidationError(
                    f"Plan step '{step.id}' field '{_argument_field(name)}' has a "
                    'malformed reference; use {"$ref": "step"} or '
                    '{"$ref": "step.field"}'
                )
            _check_reference(step, _argument_field(name), target, steps)
    try:
        placeholders = list(_placeholders(step.text or ""))
    except ValueError:
        raise InputValidationError(
            f"Plan step '{step.id}' field 'text' has a malformed placeholder; "
            "use {step} or {step.field}, and {{ or }} for a literal brace"
        ) from None
    for placeholder in placeholders:
        _check_reference(step, "text", placeholder, steps)
    for name in step.after:
        if name not in steps:
            raise InputValidationError(
                f"Plan step '{step.id}' field 'after' names no step of the plan"
            )


def _check_reference(
    step: PlanStep, field: str, target: str, steps: Mapping[str, PlanStep]
) -> None:
    match = _REFERENCE.fullmatch(target)
    if match is None:
        raise InputValidationError(
            f"Plan step '{step.id}' field '{field}' has a malformed reference; "
            "use a step ID, optionally followed by one .field"
        )
    referenced = steps.get(match["step"])
    if referenced is None:
        raise InputValidationError(
            f"Plan step '{step.id}' field '{field}' refers to no step of the plan"
        )
    # An ask step's value is text, which has no fields.
    if match["field"] is not None and referenced.text is not None:
        raise InputValidationError(
            f"Plan step '{step.id}' field '{field}' refers to a field of "
            f"'{referenced.id}', an ask step whose value is text"
        )


def _groups(steps: Sequence[PlanStep]) -> StepGroups:
    """Return the steps in groups whose dependencies are all in earlier groups.

    Steps keep their plan order within a group, so the result is
    deterministic.

    Raises:
        InputValidationError: The dependencies form a cycle.
    """
    needs = {step.id: dependencies(step) for step in steps}
    remaining = list(steps)
    done: set[str] = set()
    groups: list[tuple[PlanStep, ...]] = []
    while remaining:
        ready = tuple(s for s in remaining if needs[s.id].keys() <= done)
        if not ready:
            _raise_cycle(remaining, needs)
        groups.append(ready)
        done.update(step.id for step in ready)
        remaining = [step for step in remaining if step.id not in done]
    return tuple(groups)


def _raise_cycle(
    remaining: Sequence[PlanStep], needs: Mapping[str, Mapping[str, str]]
) -> NoReturn:
    """Raise for a step on a cycle among ``remaining``, every one of which
    depends on another of them; following first dependencies must repeat."""
    waiting = {step.id for step in remaining}
    current = remaining[0].id
    visited: list[str] = []
    while current not in visited:
        visited.append(current)
        current = _waiting_dependency(needs[current], waiting)
    following = _waiting_dependency(needs[current], waiting)
    raise InputValidationError(
        f"Plan step '{current}' field '{needs[current][following]}' is part of a "
        "dependency cycle"
    )


def _waiting_dependency(needs: Mapping[str, str], waiting: set[str]) -> str:
    """Return the first of ``needs`` still waiting; there always is one,
    since no waiting step was ready."""
    found = next((name for name in needs if name in waiting), None)
    assert found is not None
    return found


def _argument_references(value: Any) -> Iterator[dict[str, Any]]:
    """Yield every object in ``value`` with a ``"$ref"`` key, at any depth."""
    if isinstance(value, dict):
        if "$ref" in value:
            yield value
            return
        for item in value.values():
            yield from _argument_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _argument_references(item)


def _placeholders(text: str) -> Iterator[str]:
    """Yield each ``{...}`` placeholder of ``text``, as written.

    Raises:
        ValueError: A brace is unmatched, or a placeholder is empty or has a
            conversion or format, which plans do not allow.
    """
    for _, name, spec, conversion in string.Formatter().parse(text):
        if name is None:
            continue
        if not name or spec or conversion:
            raise ValueError("not a plain placeholder")
        yield name


def _reference_step(target: str) -> str:
    return target.split(".", 1)[0]


def _argument_field(name: str) -> str:
    """Return ``arguments.<name>``, or just ``arguments`` for an unsafe name,
    which may be model-written text or come from another agent's card."""
    return f"arguments.{name}" if _SAFE_NAME.fullmatch(name) else "arguments"
