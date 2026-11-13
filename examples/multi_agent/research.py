"""A research specialist that other agents call over A2A.

It offers two skills: ``find_sources``, a structured call answered without a
model, and ``brief``, answered by local Llama through Ollama. Free text sent
to it, for example with ``Remote.ask``, is routed to ``brief``.

Run it alone (``dev_settings()`` reads the variables ``agentframe dev`` sets)::

    AGENTFRAME_DEV=1 AGENTFRAME_PUBLIC_URL=http://127.0.0.1:8001 \\
        AGENTFRAME_DEV_API_KEYS=planner=dev-only-key \\
        agentframe run research:create_app --port 8001

or both agents with ``agentframe dev`` (see ``agentframe.yaml``).
"""

import os
from collections.abc import Sequence

from a2a.helpers import get_message_text
from a2a.types import Message
from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    HostConfig,
    Model,
    ModelSettings,
    RoutingContext,
    RunContext,
    SkillCall,
    SkillDefinition,
    agent,
    dev_settings,
    skill,
)


class Source(BaseModel):
    """A source worth reading on a topic."""

    title: str
    url: str


# A tiny built-in library, so finding sources needs no search service.
LIBRARY = {
    "tides": [
        Source(title="Tide tables explained", url="https://example.org/tides"),
        Source(title="How the Moon moves the oceans", url="https://example.org/moon"),
    ],
    "weather": [Source(title="Reading a forecast", url="https://example.org/weather")],
    "waves": [Source(title="Wind and waves", url="https://example.org/waves")],
}


@agent(name="Research", version="0.1.0")
class Research(Agent):
    """Finds sources and writes short research briefs."""

    instructions = "Write a research brief of at most three sentences."
    model_settings = ModelSettings(max_output_tokens=512)

    @skill(id="find_sources", tags=["research"])
    async def find_sources(self, topic: str, ctx: RunContext) -> list[Source]:
        """List the library's sources on a topic, such as ``tides``."""
        return LIBRARY.get(topic.lower(), [])

    @skill(id="brief", tags=["research"])
    async def brief(self, question: str, ctx: RunContext) -> str:
        """Write a short research brief answering a question."""
        return await ctx.generate(question)


class BriefRouter:
    """Routes any free-text message to ``brief``, with the text as its question."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall:
        return SkillCall("brief", {"question": get_message_text(conversation[-1])})


def create_model() -> Model:
    """Return local Llama through Ollama; the model tag must already be installed."""
    from agentframe.models.ollama import OllamaModel

    return OllamaModel(
        model=os.environ.get("AGENTFRAME_OLLAMA_MODEL", "llama3.2:3b"),
        context_window=8192,
    )


def create_app(model: Model | None = None) -> AgentApp:
    """Serve the research agent, for the callers ``dev_settings()`` accepts.

    Args:
        model: The model for ``brief``. Defaults to ``create_model()``.
    """
    dev = dev_settings()
    return AgentApp(
        agent=Research(model=model or create_model()),
        host=HostConfig(public_url=dev.public_url, auth=dev.auth),
        router=BriefRouter(),
    )
