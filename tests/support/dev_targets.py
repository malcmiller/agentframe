"""Application code that the ``agentframe dev`` tests point the command at.

Every factory reads its wiring with ``dev_settings()``, as a real project's
would.
"""

import os
import time
from pathlib import Path
from typing import Literal

from agentframe import (
    Agent,
    AgentApp,
    HostConfig,
    Remote,
    RunContext,
    agent,
    dev_settings,
    skill,
)
from agentframe.dev import API_KEYS_VARIABLE, REMOTE_KEYS_VARIABLE

type Research = Remote[Literal["research-agent"]]


@agent(name="Research", description="Says who called it.", version="0.1.0")
class Researcher(Agent):
    @skill(id="who", description="Name the caller.", tags=["research"])
    async def who(self, ctx: RunContext) -> str:
        assert ctx.principal is not None
        return f"Called by {ctx.principal.id}"


@agent(name="Planner", description="Plans with research.", version="0.1.0")
class Planner(Agent):
    @skill(id="plan", description="Ask the researcher who is calling.", tags=["plan"])
    async def plan(self, research: Research, ctx: RunContext) -> str:
        return await research.call("who", output=str)

    @skill(id="dev_keys", description="Test only: reveal its dev keys.", tags=["test"])
    async def dev_keys(self, ctx: RunContext) -> str:
        # Lets the test check that the generated keys never reach the log.
        return f"{os.environ[API_KEYS_VARIABLE]},{os.environ[REMOTE_KEYS_VARIABLE]}"


def _app(agent: Agent) -> AgentApp:
    dev = dev_settings()
    return AgentApp(
        agent=agent,
        host=HostConfig(public_url=dev.public_url, auth=dev.auth),
        directory=dev.directory,
        remotes=dev.remotes,
    )


def create_research_app() -> AgentApp:
    return _app(Researcher())


def create_planner_app() -> AgentApp:
    return _app(Planner())


def create_never_ready_app() -> AgentApp:
    """Never finishes building, after leaving its process ID for the test."""
    Path("never_ready.pid").write_text(str(os.getpid()))
    time.sleep(60)
    return _app(Researcher())
