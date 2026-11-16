"""Human approval of model-made plans: one skill proposes, another runs.

``propose`` asks the model for a plan over the research agent's skills and
returns it without sending anything to another agent. The caller's
application shows the plan to a person; once they approve it, the caller sends
the same plan back to ``execute``, which runs it with ``ctx.run_plan``.
AgentFrame has no built-in pause: the review happens between two ordinary
requests, and the application owns it.

The plan ``execute`` receives is untrusted input, whatever ``propose``
returned. ``run_plan`` checks it again against the catalog and the caller's
permissions, the plan check below sees the whole plan, and tool checks would
see every step, all before anything is sent.

**Open-proxy warning.** ``execute`` runs whatever plan its caller sends, with
this agent's own outbound identity (a ``Service`` API key here). Anyone who
can call it can make this agent call any entry of its catalog. So ``execute``
requires the ``plans:run`` permission, its catalog holds only the declared
research agent, and a production host would prefer ``OnBehalfOf`` so the
research agent authorizes the real user.

Run it next to the research agent of ``examples/multi_agent``, which accepts
this agent's key as the caller ``approvals``::

    AGENTFRAME_DEV=1 AGENTFRAME_PUBLIC_URL=http://127.0.0.1:8001 \\
        AGENTFRAME_DEV_API_KEYS=approvals=research-key \\
        AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \\
        agentframe run examples.multi_agent.research:create_app --port 8001

    CARD=http://127.0.0.1:8001/.well-known/agent-card.json
    AGENTFRAME_AGENTS=research-agent=$CARD RESEARCH_API_KEY=research-key \\
        APPROVALS_REQUESTER_KEY=requester-key APPROVALS_REVIEWER_KEY=reviewer-key \\
        agentframe run examples.plan_approval:create_app --port 8003

The research agent's briefs need local Llama through Ollama, with the
installed model tag ``AGENTFRAME_OLLAMA_MODEL`` names; the planning model is
the one ``AGENTFRAME_MODEL_PROVIDER`` selects, as for the other examples, and
must support structured output.
"""

import os
from typing import Literal

import httpx

from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    APIKey,
    APIKeyAuth,
    APIKeyCredential,
    EnvironmentAgentRegistry,
    ExecutionPolicy,
    HostConfig,
    Model,
    Plan,
    Principal,
    Remote,
    RunContext,
    Service,
    SkillDefinition,
    agent,
    model_from_env,
    skill,
)

type Research = Remote[Literal["research-agent"]]

BRIEF = "research-agent.brief"


@agent(name="Approvals", version="0.1.0")
class Approvals(Agent):
    """Proposes research plans for review and runs reviewed ones."""

    @skill(id="propose", tags=["planning"])
    async def propose(self, goal: str, research: Research, ctx: RunContext) -> Plan:
        """Propose a plan for a goal; nothing is sent to the research agent."""
        skills = await ctx.remote_skills(research)
        return await ctx.plan(goal, skills, max_steps=4)

    # Open-proxy warning: this skill runs caller-supplied plans as this agent.
    @skill(id="execute", tags=["planning"], requires_permissions=["plans:run"])
    async def execute(self, plan: Plan, research: Research, ctx: RunContext) -> str:
        """Run a reviewed plan and return its result as text."""
        skills = await ctx.remote_skills(research)
        result = await ctx.run_plan(plan, skills, max_steps=4)
        return str(result.value)


async def at_most_one_brief(
    skill: SkillDefinition, principal: Principal | None, plan: Plan
) -> bool:
    """Allow at most one brief per plan: each one uses the research agent's model."""
    return sum(step.use == BRIEF for step in plan.steps) <= 1


def create_app(
    model: Model | None = None, directory: AgentDirectory | None = None
) -> AgentApp:
    """Serve the approvals agent.

    Args:
        model: The planning model. Defaults to ``model_from_env()``; it lives
            as long as the server process.
        directory: Where to find ``research-agent``. Defaults to the agents
            listed in ``AGENTFRAME_AGENTS``, over HTTP on localhost only.
    """
    host = HostConfig(
        public_url=os.environ.get("APPROVALS_PUBLIC_URL", "http://127.0.0.1:8003"),
        # Development placeholder: two API keys from the environment. Anyone
        # with a key may propose; only reviewers may run a plan. Production
        # hosts use real identities and roles.
        auth=APIKeyAuth(
            keys={
                os.environ["APPROVALS_REQUESTER_KEY"]: APIKey("requester"),
                os.environ["APPROVALS_REVIEWER_KEY"]: APIKey("reviewer", ["reviewer"]),
            }
        ),
        role_permissions={"reviewer": ["plans:run"]},
    )
    research_key = os.environ.get("RESEARCH_API_KEY")
    remotes = (
        {"research-agent": Service(credential=APIKeyCredential(research_key))}
        if research_key
        else {}
    )
    return AgentApp(
        agent=Approvals(model=model or model_from_env()),
        host=host,
        directory=directory or create_directory(),
        remotes=remotes,
        policy=ExecutionPolicy(plan_checks=[at_most_one_brief]),
    )


def create_directory() -> AgentDirectory:
    """Return a directory of the agents listed in ``AGENTFRAME_AGENTS``."""
    return AgentDirectory(
        sources=[EnvironmentAgentRegistry("AGENTFRAME_AGENTS")],
        allow_http_hosts=["127.0.0.1"],
        # Credentials are never followed through a redirect. The client is
        # never closed, so it keeps no idle connections that would be left open.
        http_client=httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        ),
    )
