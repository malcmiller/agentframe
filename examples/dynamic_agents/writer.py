"""A model-free writer specialist whose skill is tagged ``content``.

The planner's model can plan to call ``write_post`` after the research
agent, passing the sources it found along.

Run it with the other agents through ``agentframe dev`` (see
``agentframe.yaml``), or alone::

    AGENTFRAME_DEV=1 AGENTFRAME_PUBLIC_URL=http://127.0.0.1:8012 \\
        AGENTFRAME_DEV_API_KEYS=planner=dev-only-key \\
        agentframe run writer:create_app --port 8012
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
    """A source to list under a post."""

    title: str
    url: str


@agent(name="Writer", version="0.1.0")
class Writer(Agent):
    """Writes short posts from sources."""

    @skill(id="write_post", tags=["content", "writing"])
    async def write_post(
        self, title: str, sources: list[Source], ctx: RunContext
    ) -> str:
        """Write a short Markdown post: a title and a list of its sources."""
        lines = [f"# {title}", ""]
        lines += [f"- [{source.title}]({source.url})" for source in sources]
        return "\n".join(lines)


def create_app() -> AgentApp:
    """Serve the writer, for the callers ``dev_settings()`` accepts."""
    dev = dev_settings()
    return AgentApp(
        agent=Writer(), host=HostConfig(public_url=dev.public_url, auth=dev.auth)
    )
