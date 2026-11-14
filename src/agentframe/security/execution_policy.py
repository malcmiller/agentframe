"""Application-supplied checks on a run's input, model requests, tool calls and output.

Checks add to authorization; they never replace it. Like an
``AuthorizationPolicy``, a check allows only by returning exactly ``True``.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from agentframe.errors import PermissionDeniedError, PolicyUnavailableError
from agentframe.models.base import ModelRequest
from agentframe.security.principal import Principal

if TYPE_CHECKING:
    # Imported only for type checking: the generation layer imports this
    # module, and skill definitions import the generation layer.
    from agentframe.authoring.definitions import SkillDefinition
    from agentframe.orchestration.plan import Plan


class InputCheck(Protocol):
    """Checks a skill's validated arguments before the skill runs.

    Any async function or callable object taking these three
    arguments works; they are passed by position.
    """

    async def __call__(
        self,
        skill: "SkillDefinition",
        principal: Principal | None,
        arguments: Mapping[str, Any],
        /,
    ) -> bool:
        """Return ``True`` to allow the run; any other value denies it.

        Args:
            skill: Definition of the skill about to run.
            principal: Verified caller identity, or ``None``.
            arguments: The skill's validated business arguments, read-only.
        """
        ...


class ModelRequestCheck(Protocol):
    """Checks every request before it is sent to a model.

    That includes structured-output requests, every tool-loop continuation
    and the built-in router's requests. Any async function or callable object
    taking these three arguments works; they are passed by position.
    """

    async def __call__(
        self,
        skill: "SkillDefinition | None",
        principal: Principal | None,
        request: ModelRequest,
        /,
    ) -> bool:
        """Return ``True`` to send the request; any other value denies it.

        Args:
            skill: Definition of the running skill, or ``None`` while the
                request is routed, before any skill is selected.
            principal: Verified caller identity, or ``None``.
            request: The complete request: messages, settings, output
                schema and tool specs.
        """
        ...


class ToolCheck(Protocol):
    """Checks every tool call the model asks for, just before the tool runs.

    Any async function or callable object taking these four
    arguments works; they are passed by position.
    """

    async def __call__(
        self,
        skill: "SkillDefinition",
        principal: Principal | None,
        tool: str,
        arguments: Mapping[str, Any],
        /,
    ) -> bool:
        """Return ``True`` to run the tool; any other value denies the call.

        Args:
            skill: Definition of the running skill.
            principal: Verified caller identity, or ``None``.
            tool: Name of the tool about to run.
            arguments: The tool's validated arguments, read-only.
        """
        ...


class PlanCheck(Protocol):
    """Checks a whole plan in ``RunContext.run_plan``, before any step is sent.

    Any async function or callable object taking these three
    arguments works; they are passed by position.
    """

    async def __call__(
        self,
        skill: "SkillDefinition",
        principal: Principal | None,
        plan: "Plan",
        /,
    ) -> bool:
        """Return ``True`` to run the plan; any other value denies it.

        Args:
            skill: Definition of the skill running the plan.
            principal: Verified caller identity, or ``None``.
            plan: A copy of the checked plan: every step's entry, arguments,
                text and references. Changing it changes nothing that runs.
        """
        ...


class OutputCheck(Protocol):
    """Checks a skill's validated value before it is returned.

    Any async function or callable object taking these three
    arguments works; they are passed by position.
    """

    async def __call__(
        self,
        skill: "SkillDefinition",
        principal: Principal | None,
        value: Any,
        /,
    ) -> bool:
        """Return ``True`` to return the value; any other value withholds it.

        Args:
            skill: Definition of the skill that produced ``value``.
            principal: Verified caller identity, or ``None``.
            value: The skill's validated return value.
        """
        ...


@dataclass(frozen=True, kw_only=True)
class ExecutionPolicy:
    """Content checks at a run's boundaries, configured with ``Runtime(policy=...)``.

    Each group runs in order and stops at the first failure. A check that
    returns anything but ``True`` raises ``PermissionDeniedError``; one that
    raises or is still running at the run's deadline raises
    ``PolicyUnavailableError``. Either way the run fails closed.

    Attributes:
        input_checks: Run after authorization, before the skill.
        model_request_checks: Run once before each model request is sent,
            not again when a transient failure is retried.
        tool_checks: Run before every tool call, after its arguments are
            validated and the call is authorized. Tool calls are never retried.
        plan_checks: Run once by ``RunContext.run_plan``, after the plan is
            checked against the catalog and before its first step is sent.
            Tool checks still run for every step.
        output_checks: Run after the skill's value is validated.
    """

    input_checks: Sequence[InputCheck] = ()
    model_request_checks: Sequence[ModelRequestCheck] = ()
    tool_checks: Sequence[ToolCheck] = ()
    plan_checks: Sequence[PlanCheck] = ()
    output_checks: Sequence[OutputCheck] = ()


async def enforce_checks[S: "SkillDefinition | None", C](
    checks: Sequence[Callable[[S, Principal | None, C], Awaitable[bool]]],
    skill: S,
    principal: Principal | None,
    content: C,
    deadline: float,
) -> None:
    """Run ``checks`` in order on ``content``, failing closed at the first failure.

    Errors name only the skill, never ``content``, so they are safe to show
    callers and to log.

    Args:
        checks: The checks to run.
        skill: The running skill, or ``None`` while routing.
        principal: Verified caller identity, or ``None``.
        content: What is checked: arguments, a model request or a value.
        deadline: Event-loop time by which every check must answer.

    Raises:
        PermissionDeniedError: A check returned something other than ``True``.
        PolicyUnavailableError: A check raised, or had not answered by
            ``deadline``.
    """
    subject = "The request" if skill is None else f"Skill '{skill.id}'"
    for check in checks:
        await ask_check(check, (skill, principal, content), subject, deadline)


async def enforce_tool_checks(
    checks: Sequence[ToolCheck],
    skill: "SkillDefinition",
    principal: Principal | None,
    tool: str,
    arguments: Mapping[str, Any],
    deadline: float,
) -> None:
    """Run tool ``checks`` in order on one tool call, failing closed.

    Errors name only the tool, never its arguments, so they are safe to show
    callers and to log.

    Raises:
        PermissionDeniedError: A check returned something other than ``True``.
        PolicyUnavailableError: A check raised, or had not answered by
            ``deadline``.
    """
    subject = f"Tool '{tool}'"
    for check in checks:
        await ask_check(check, (skill, principal, tool, arguments), subject, deadline)


async def enforce_plan_checks(
    checks: Sequence[PlanCheck],
    skill: "SkillDefinition",
    principal: Principal | None,
    plan: "Plan",
    deadline: float,
) -> None:
    """Run plan ``checks`` in order on ``plan``, failing closed.

    Each check gets its own deep copy, so no check can change what runs or
    what a later check sees. Errors never describe the plan's content.

    Raises:
        PermissionDeniedError: A check returned something other than ``True``.
        PolicyUnavailableError: A check raised, or had not answered by
            ``deadline``.
    """
    for check in checks:
        arguments = (skill, principal, plan.model_copy(deep=True))
        await ask_check(check, arguments, "The plan", deadline)


async def ask_check(
    check: Callable[..., Awaitable[bool]],
    arguments: tuple[Any, ...],
    subject: str,
    deadline: float,
) -> None:
    """Call one check with ``arguments``; only an explicit ``True`` passes.

    Args:
        check: The check to call.
        arguments: The check's positional arguments.
        subject: What is checked, for the error message, such as
            ``"Skill 'echo'"``. Never the checked content.
        deadline: Event-loop time by which the check must answer.

    Raises:
        PermissionDeniedError: The check returned something other than ``True``.
        PolicyUnavailableError: The check raised, or had not answered by
            ``deadline``.
    """
    unavailable = (
        f"{subject} could not be checked: an execution policy check is unavailable"
    )
    try:
        # Checks share the run's deadline; running out of time while one
        # is deciding is an outage, never permission.
        async with asyncio.timeout_at(deadline):
            allowed = await check(*arguments)
    except asyncio.CancelledError:
        # Inside a skill, the run's own timeout cancels the check at the
        # same deadline. That is still a policy outage, not a skill
        # timeout. A cancellation before the deadline is a real one.
        if asyncio.get_running_loop().time() < deadline:
            raise
        raise PolicyUnavailableError(unavailable) from None
    except Exception as error:
        raise PolicyUnavailableError(unavailable) from error
    # Fail closed: only an explicit True allows, not any truthy value.
    # noinspection PySimplifyBooleanCheck
    if allowed is not True:
        raise PermissionDeniedError(
            f"{subject} was blocked by an execution policy check"
        )
