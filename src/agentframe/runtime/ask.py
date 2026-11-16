"""Answer a plain prompt with one of an agent's skills: ``Agent.ask``."""

import asyncio
import uuid
from typing import Any

from a2a.helpers import new_text_message
from a2a.types import Role

from agentframe.authoring.agent import Agent
from agentframe.authoring.definitions import AgentDefinition, SkillDefinition
from agentframe.errors import (
    AgentFrameError,
    ClarificationNeededError,
    DefinitionError,
    InputValidationError,
)
from agentframe.runtime.accounting import RunAccounting
from agentframe.runtime.options import RunOptions
from agentframe.runtime.routing import (
    Clarification,
    SkillRouter,
    eligible_skills,
    new_routing_context,
    route,
)
from agentframe.runtime.runtime import Runtime
from agentframe.security.principal import Principal


async def ask(
    agent: Agent,
    prompt: str,
    *,
    router: SkillRouter | None,
    runtime: Runtime,
    principal: Principal | None,
    options: RunOptions | None,
) -> Any:
    """Run the skill that answers ``prompt`` and return its validated value.

    See ``Agent.ask`` for how the skill is chosen and what is raised.
    """
    if not prompt.strip():
        raise InputValidationError("The prompt has no text for a skill")
    definition = type(agent).definition()
    text_skill = _only_text_skill(definition)
    if text_skill is not None:
        assert text_skill.text_argument is not None  # _only_text_skill checked.
        return await runtime.invoke(
            agent,
            skill=text_skill.id,
            arguments={text_skill.text_argument: prompt},
            principal=principal,
            options=options,
        )
    if router is None:
        skill_ids = ", ".join(sorted(definition.skills))
        raise DefinitionError(
            f"Agent '{definition.name}' cannot choose a skill for a prompt: it "
            "has no router and not exactly one skill that takes a single str. "
            "Pass router=ModelRouter(model), or call agent.invoke() with one "
            f"of its skills: {skill_ids}"
        )
    return await _routed(agent, prompt, router, runtime, principal, options)


def _only_text_skill(definition: AgentDefinition) -> SkillDefinition | None:
    """Return the agent's skill that takes a single ``str``, if it has exactly one.

    Uses the same rule as an app's default skill (``text_skill_argument``).
    """
    text_skills = [skill for skill in definition.skills.values() if skill.text_argument]
    return text_skills[0] if len(text_skills) == 1 else None


async def _routed(
    agent: Agent,
    prompt: str,
    router: SkillRouter,
    runtime: Runtime,
    principal: Principal | None,
    options: RunOptions | None,
) -> Any:
    """Let ``router`` choose the skill and its arguments, then run it.

    Routing runs like a hosted free-text message: under the agent's limits,
    with its model calls charged to the same run as the skill, whose
    timeout counts from when routing started.
    """
    invocation_id = str(uuid.uuid4())
    definition = type(agent).definition()
    with runtime.telemetry.run(
        agent=definition.name, invocation_id=invocation_id
    ) as span:
        accounting = RunAccounting()
        started_at = asyncio.get_running_loop().time()
        ctx = new_routing_context(
            agent,
            invocation_id,
            principal,
            started_at,
            accounting,
            policy=runtime.policy,
            telemetry=runtime.telemetry,
        )
        conversation = [new_text_message(prompt, role=Role.ROLE_USER)]
        try:
            decision = await route(
                router, conversation, eligible_skills(definition, principal), ctx
            )
        except AgentFrameError as error:
            error.invocation_id = invocation_id
            raise
        if isinstance(decision, Clarification):
            raise ClarificationNeededError(
                "The agent's router asked a question instead of choosing a skill",
                question=decision.question,
                invocation_id=invocation_id,
            )
        # Only a declared skill labels the metrics: a router may name any ID.
        if decision.skill in definition.skills:
            span.set_skill(decision.skill)
        result = await runtime.run_invocation(
            invocation_id,
            agent,
            decision.skill,
            decision.arguments,
            principal,
            options or RunOptions(),
            started_at,
            accounting,
        )
    return result.value
