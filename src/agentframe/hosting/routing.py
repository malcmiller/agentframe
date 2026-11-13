"""Route free-text A2A messages to a skill with an application-supplied router."""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, overload

from a2a.types import Message
from pydantic import BaseModel

from agentframe.authoring.agent import Agent
from agentframe.authoring.context import RunContext
from agentframe.authoring.definitions import AgentDefinition, SkillDefinition
from agentframe.authoring.services import Services
from agentframe.errors import (
    AgentFrameError,
    DefinitionError,
    InputValidationError,
    TimeoutLimitExceededError,
)
from agentframe.models.base import DEFAULT_MODEL_SETTINGS, Model, ModelSettings
from agentframe.models.base import Message as ModelMessage
from agentframe.runtime.generation import GenerationState, RunAccounting
from agentframe.runtime.limits import DEFAULT_LIMITS
from agentframe.runtime.options import merge_settings
from agentframe.security.principal import Principal

ROUTER_FAILED_MESSAGE = "The skill router failed."
"""What callers see when the router raises an exception."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillCall:
    """A skill to run and its unvalidated business arguments.

    Attributes:
        skill: ID of the skill to run.
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


@dataclass(frozen=True)
class RoutingContext:
    """Per-message context passed to a router. Never shared between messages.

    Model calls made through ``generate`` are charged to the same run as the
    skill the router selects: they count toward the agent's token and
    model-call limits, so the skill only gets what remains.

    Attributes:
        invocation_id: Library-generated identifier of the run being routed.
        principal: Verified caller identity, or ``None`` when there is none.
        deadline: Event-loop time (``loop.time()``) when routing times out:
            the agent's timeout, counted from when the run started.
    """

    invocation_id: str
    principal: Principal | None
    deadline: float
    # Private: the run's shared accounting plus agent-level limits and
    # settings. Its model is unset; every call names its model explicitly.
    _generation: GenerationState = field(repr=False, compare=False)

    @overload
    async def generate(
        self,
        model: Model,
        text: str | Sequence[ModelMessage],
        *,
        instructions: str | None = None,
        model_settings: ModelSettings | None = None,
    ) -> str: ...

    @overload
    async def generate[T: BaseModel](
        self,
        model: Model,
        text: str | Sequence[ModelMessage],
        *,
        instructions: str | None = None,
        output: type[T],
        model_settings: ModelSettings | None = None,
    ) -> T: ...

    async def generate(
        self,
        model: Model,
        text: str | Sequence[ModelMessage],
        *,
        instructions: str | None = None,
        output: type[BaseModel] | None = None,
        model_settings: ModelSettings | None = None,
    ) -> str | BaseModel:
        """Generate text, or a validated ``output`` instance, with ``model``.

        Follows the same rules as ``RunContext.generate``, without tools:
        ``instructions`` is sent as a trusted ``system`` message and ``text``
        after it as a separate ``user`` message, or as the given conversation.
        The agent's own instructions are not sent; they are for its skills.
        The call is checked against and charged to the run's limits.

        Args:
            model: The router's model. The agent's model is never used
                implicitly.
            text: Untrusted content: the caller's text, or the conversation
                as ``user`` messages (the caller) and ``assistant`` messages
                (the agent's earlier questions), oldest first.
            instructions: Trusted routing instructions. Never put untrusted
                content here.
            output: A Pydantic ``BaseModel`` subclass to validate the reply as.
            model_settings: Settings for this call. Each field set overrides
                the library's and agent's value.

        Returns:
            The model's complete answer, or an ``output`` instance.

        Raises:
            DefinitionError: The conversation has a message that is not a
                plain ``user`` or ``assistant`` message.
            AgentFrameError: The same errors as ``RunContext.generate``, such
                as ``ModelCallLimitExceededError`` or ``ModelRefusalError``.
                They reach the caller with their own safe messages.
        """
        if not isinstance(text, str):
            _check_conversation(text)
        generation = replace(
            self._generation,
            model=model,
            settings=merge_settings(self._generation.settings, model_settings),
        )
        # The generation layer reads the deadline from a RunContext and hands
        # it to tools. Routing offers no tools, so this context stays internal.
        # Execution-policy model-request checks (#77) run in that layer too,
        # with no skill, since none is selected yet.
        run_ctx = RunContext(
            self.invocation_id, self.principal, self.deadline, generation
        )
        if output is None:
            return await generation.generate_text(run_ctx, text, instructions)
        return await generation.generate_output(run_ctx, text, instructions, output)


def _check_conversation(conversation: Sequence[ModelMessage]) -> None:
    # Untrusted content must never arrive as trusted system text or as tool traffic.
    for message in conversation:
        if message.role not in ("user", "assistant") or message.tool_calls:
            raise DefinitionError(
                "A routing conversation may only contain plain user and "
                "assistant messages"
            )


def new_routing_context(
    agent: Agent,
    invocation_id: str,
    principal: Principal | None,
    started_at: float,
    accounting: RunAccounting,
) -> RoutingContext:
    """Return the context for routing one message of a run.

    The skill is not known yet, so routing uses the library defaults merged
    with the agent's limits and model settings. Its deadline is the agent's
    timeout from ``started_at``; the selected skill's deadline counts from
    the same start, and its model calls share ``accounting``.

    Args:
        agent: The hosted agent instance.
        invocation_id: The run being routed.
        principal: Verified caller identity, or ``None``.
        started_at: Event-loop time when the run started.
        accounting: The run's accounting, shared with the selected skill.
    """
    # Host limit ceilings (#71) will cap these limits, and so the deadline.
    limits = merge_settings(DEFAULT_LIMITS, agent.limits)
    assert limits.timeout_seconds is not None  # DEFAULT_LIMITS sets every field.
    generation = GenerationState(
        model=None,
        instructions=None,
        settings=merge_settings(DEFAULT_MODEL_SETTINGS, agent.model_settings),
        limits=limits,
        # Routing offers no tools, so it needs no services.
        services=Services().scope(AsyncExitStack()),
        accounting=accounting,
    )
    return RoutingContext(
        invocation_id, principal, started_at + limits.timeout_seconds, generation
    )


class SkillRouter(Protocol):
    """Chooses the skill for a free-text A2A message, or asks the caller.

    Messages that activate the skill-call extension name their skill and never
    reach the router. Any class with a matching ``route`` method works; no
    inheritance is needed. A router selects; it cannot grant permissions.
    """

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
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
            ctx: The run's routing context. Use ``ctx.generate`` for model
                calls, so they are charged to the run.

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
    ctx: RoutingContext,
) -> SkillCall | Clarification:
    """Ask ``router`` about ``conversation`` before ``ctx.deadline``; check its answer.

    Args:
        router: The application's router.
        conversation: The task's messages so far, oldest first.
        skills: The skills the caller may be routed to.
        ctx: The routing context. Its deadline is the run's timeout, so
            routing time counts against it; its invocation ID is logged with
            router failures.

    Returns:
        The router's decision: a call to one of ``skills`` or a non-empty
        question.

    Raises:
        TimeoutLimitExceededError: The router did not answer before the deadline.
        AgentFrameError: The router raised one, for example a limit or
            model error from ``ctx.generate``; it keeps its safe message.
            Any other exception becomes an ``AgentFrameError`` with a generic
            message; the details are only logged.
        InputValidationError: The router returned something other than a call
            to one of ``skills`` or a non-empty question.
    """
    invocation_id = ctx.invocation_id
    try:
        async with asyncio.timeout_at(ctx.deadline) as timeout:
            decision = await router.route(conversation, skills, ctx)
    except AgentFrameError:
        raise
    except TimeoutError:
        if not timeout.expired():
            raise _router_failed(invocation_id) from None
        raise TimeoutLimitExceededError(
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
