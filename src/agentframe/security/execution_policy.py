"""Application-supplied content checks on a run's input, model requests and output.

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
        output_checks: Run after the skill's value is validated.
    """

    input_checks: Sequence[InputCheck] = ()
    model_request_checks: Sequence[ModelRequestCheck] = ()
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
    unavailable = (
        f"{subject} could not be checked: an execution policy check is unavailable"
    )
    for check in checks:
        try:
            # Checks share the run's deadline; running out of time while one
            # is deciding is an outage, never permission.
            async with asyncio.timeout_at(deadline):
                allowed = await check(skill, principal, content)
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
