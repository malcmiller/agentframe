"""Application code that the multi-agent release test serves with ``agentframe run``.

The planner is served straight from ``examples/multi_agent``. The research
agent is the example's, with a stand-in model and one addition so the test
can see what reached it. The relay is a deliberately recursive agent: it
asks its ``next`` agent whatever it is asked.

Every factory reads its wiring with ``dev_settings()``, as the example's do.
"""

import asyncio
from collections.abc import Sequence
from typing import Literal

from a2a.helpers import get_message_text
from a2a.types import Message

from agentframe import (
    Agent,
    AgentApp,
    HostConfig,
    ModelRequest,
    ModelResponse,
    Remote,
    RoutingContext,
    RunContext,
    SkillCall,
    SkillDefinition,
    Usage,
    agent,
    dev_settings,
    skill,
)
from agentframe.runtime.usage import FinishReason
from agentframe.testing import ScriptedModel
from examples.multi_agent import research

BRIEF_TOKENS = 300


class BriefModel(ScriptedModel):
    """Answers with the question it was asked, reporting ``BRIEF_TOKENS`` used."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text=f"Brief: {request.messages[-1].content}",
            usage=Usage(
                input_tokens=BRIEF_TOKENS - 20,
                output_tokens=20,
                total_tokens=BRIEF_TOKENS,
                estimated=False,
            ),
            finish_reason=FinishReason.COMPLETED,
        )


@agent(name="Research", version="0.1.0")
class ReportingResearch(research.Research):
    """The example's research agent; each brief also says who asked and the
    seconds its run had left."""

    @skill(id="brief", tags=["research"])
    async def brief(self, question: str, ctx: RunContext) -> str:
        """Write a short research brief answering a question."""
        seconds_left = ctx.deadline - asyncio.get_running_loop().time()
        caller = ctx.principal.id if ctx.principal else "nobody"
        answer = await super().brief(question, ctx)
        return f"{answer} (for {caller}, {seconds_left:.0f} s left)"


def create_research_app() -> AgentApp:
    dev = dev_settings()
    return AgentApp(
        agent=ReportingResearch(model=BriefModel()),
        host=HostConfig(public_url=dev.public_url, auth=dev.auth),
        router=research.BriefRouter(),
    )


type Next = Remote[Literal["next"]]


@agent(name="Relay", version="0.1.0")
class Relay(Agent):
    """Passes any text on to ``next``; pointed at itself, it never stops."""

    @skill(id="relay", tags=["relay"])
    async def relay(self, text: str, following: Next, ctx: RunContext) -> str:
        """Ask the next agent and return its answer."""
        return await following.ask(text)


class RelayRouter:
    """Routes any free-text message to ``relay``."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall:
        return SkillCall("relay", {"text": get_message_text(conversation[-1])})


def create_relay_app() -> AgentApp:
    dev = dev_settings()
    return AgentApp(
        agent=Relay(),
        host=HostConfig(public_url=dev.public_url, auth=dev.auth),
        router=RelayRouter(),
        directory=dev.directory,
        remotes=dev.remotes,
    )
