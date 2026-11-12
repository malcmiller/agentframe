"""Route free-text A2A messages to a skill with an application-supplied router."""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from a2a.types import Message

from agentframe.definitions import AgentDefinition, SkillDefinition
from agentframe.errors import AgentFrameError, InputValidationError, LimitExceededError
from agentframe.principal import Principal

ROUTER_FAILED_MESSAGE = "The skill router failed."
"""What callers see when the router raises an exception."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillCall:
    """A skill to run and its unvalidated business arguments.

    Attributes:
        skill: Id of the skill to run.
        arguments: Business arguments; the runtime validates them like any
            other caller's arguments.
    """

    skill: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Clarification:
    """A question for the caller when a request is ambiguous or incomplete.

    Attributes:
        question: Safe text telling the caller what is missing or ambiguous.
    """

    question: str


class SkillRouter(Protocol):
    """Chooses the skill for a free-text A2A message, or asks the caller.

    Messages that activate the skill-call extension name their skill and never
    reach the router. Any class with a matching ``route`` method works; no
    inheritance is needed. A router selects; it cannot grant permissions.
    """

    async def route(
        self, conversation: Sequence[Message], skills: Sequence[SkillDefinition]
    ) -> SkillCall | Clarification:
        """Select a skill and extract its arguments, or ask a question.

        Return a ``Clarification`` when the request is ambiguous or misses
        input, rather than guessing.

        Args:
            conversation: The task's native A2A messages so far, oldest first:
                the caller's messages and the agent's earlier questions. The
                last message is the one being routed.
            skills: The skills this caller may run. Selecting any other skill
                rejects the request.

        Returns:
            The skill to run with its arguments, or a question for the caller.
        """
        ...


def eligible_skills(
    agent: AgentDefinition, principal: Principal | None
) -> list[SkillDefinition]:
    """Return the agent's skills whose required permissions the caller holds.

    Args:
        agent: The hosted agent's definition.
        principal: Verified caller identity, or ``None`` when there is none.

    Returns:
        The skills the caller may be routed to. The runtime still checks
        permissions and the authorization policy when the selected skill runs.
    """
    permissions = principal.permissions if principal else frozenset()
    return [
        skill
        for skill in agent.skills.values()
        if skill.requires_permissions <= permissions
    ]


async def route(
    router: SkillRouter,
    conversation: Sequence[Message],
    skills: Sequence[SkillDefinition],
    deadline: float,
    invocation_id: str,
) -> SkillCall | Clarification:
    """Ask ``router`` about ``conversation`` before ``deadline`` and check its answer.

    Args:
        router: The application's router.
        conversation: The task's messages so far, oldest first.
        skills: The skills the caller may be routed to.
        deadline: Event-loop time by which the router must answer; the run's
            timeout, so routing time counts against it.
        invocation_id: The run being routed, logged with router failures.

    Returns:
        The router's decision: a call to one of ``skills`` or a non-empty
        question.

    Raises:
        LimitExceededError: The router did not answer before the deadline.
        AgentFrameError: The router raised an exception. The caller gets a
            generic message; the details are only logged.
        InputValidationError: The router returned something other than a call
            to one of ``skills`` or a non-empty question.
    """
    try:
        async with asyncio.timeout_at(deadline) as timeout:
            decision = await router.route(conversation, skills)
    except TimeoutError:
        if not timeout.expired():
            raise _router_failed(invocation_id) from None
        raise LimitExceededError(
            "Routing the request exceeded the run's timeout",
            invocation_id=invocation_id,
        ) from None
    except Exception:
        raise _router_failed(invocation_id) from None
    return _checked_decision(decision, skills)


def _router_failed(invocation_id: str) -> AgentFrameError:
    # Router exceptions may carry the caller's text or secrets, so only the
    # log keeps the details, under the run's ID that the caller can quote.
    logger.exception("Skill router failed (invocation_id=%s)", invocation_id)
    return AgentFrameError(ROUTER_FAILED_MESSAGE, invocation_id=invocation_id)


def _checked_decision(
    decision: object, skills: Sequence[SkillDefinition]
) -> SkillCall | Clarification:
    if isinstance(decision, Clarification):
        if not isinstance(decision.question, str) or not decision.question.strip():
            raise InputValidationError("The skill router asked an empty question")
        return decision
    if isinstance(decision, SkillCall):
        if decision.skill not in {skill.id for skill in skills}:
            # The id is not echoed: it may name a skill this caller cannot see.
            raise InputValidationError(
                "The skill router selected a skill that is not available"
            )
        return decision
    raise InputValidationError("The skill router returned an invalid decision")
