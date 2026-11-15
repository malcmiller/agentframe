"""A writer specialist that turns sources and notes into a short post, without a model.

The planner's model can plan to call it after the research agent, passing
the research results along (see ``planner.py``'s ``planned`` skill).

Run it alone (``dev_settings()`` reads the variables ``agentframe dev`` sets)::

    AGENTFRAME_DEV=1 AGENTFRAME_PUBLIC_URL=http://127.0.0.1:8004 \\
        AGENTFRAME_DEV_API_KEYS=planner=dev-only-key \\
        agentframe run writer:create_app --port 8004

or with the other agents through ``agentframe dev`` (see ``agentframe.yaml``).
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
    """Writes short posts from sources and notes."""

    @skill(id="write_post", tags=["writing"])
    async def write_post(
        self, title: str, sources: list[Source], ctx: RunContext, notes: str = ""
    ) -> str:
        """Write a short Markdown post: a title, optional notes and its sources."""
        lines = [f"# {title}", ""]
        if notes:
            lines += [notes, ""]
        lines += [f"- [{source.title}]({source.url})" for source in sources]
        return "\n".join(lines)


def create_app() -> AgentApp:
    """Serve the writer, for the callers ``dev_settings()`` accepts."""
    dev = dev_settings()
    return AgentApp(
        agent=Writer(), host=HostConfig(public_url=dev.public_url, auth=dev.auth)
    )
