"""A model-free research specialist whose skill is tagged ``content``.

The planner finds ``find_sources`` by that tag, so it never names this agent.

Run it with the other agents through ``agentframe dev`` (see
``agentframe.yaml``), or alone::

    AGENTFRAME_DEV=1 AGENTFRAME_PUBLIC_URL=http://127.0.0.1:8011 \\
        AGENTFRAME_DEV_API_KEYS=planner=dev-only-key \\
        agentframe run research:create_app --port 8011
"""

from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    HostConfig,
    RunContext,
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
    "waves": [Source(title="Wind and waves", url="https://example.org/waves")],
}


@agent(name="Research", version="0.1.0")
class Research(Agent):
    """Finds sources on a topic."""

    @skill(id="find_sources", tags=["content", "research"])
    async def find_sources(self, topic: str, ctx: RunContext) -> list[Source]:
        """List the library's sources on a topic, such as ``tides``."""
        return LIBRARY.get(topic.lower(), [])


def create_app() -> AgentApp:
    """Serve the research agent, for the callers ``dev_settings()`` accepts."""
    dev = dev_settings()
    return AgentApp(
        agent=Research(), host=HostConfig(public_url=dev.public_url, auth=dev.auth)
    )
