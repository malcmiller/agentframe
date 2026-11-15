"""Application code that the release tests serve with ``agentframe run``.

Both release tests serve ``examples/multi_agent``'s planner with
``GoalModel``, a stand-in planning model, so no test needs Ollama. The
research agent is the example's, with a stand-in model and one addition so
the test can see what reached it. The
relay is a deliberately recursive agent: it asks its ``next`` agent whatever
it is asked.

The orchestration release test (M10) also serves the coordinator, which
offers every registered agent's skills to that model, as tools or in a plan.

Every factory reads its wiring with ``dev_settings()``, as the example's do.
"""

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from typing import Any, Literal

from a2a.helpers import get_message_text
from a2a.types import Message

from agentframe import (
    Agent,
    AgentApp,
    HostConfig,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Remote,
    RoutingContext,
    RunContext,
    SkillCall,
    SkillDefinition,
    ToolCall,
    Usage,
    agent,
    dev_settings,
    skill,
)
from agentframe.testing import ScriptedModel
from agentframe.usage import FinishReason
from examples.multi_agent import planner, research

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


# Model-planned orchestration (M10)

PLANNING_TOKENS = 200
PLAN_LOG_VARIABLE = "AGENTFRAME_TEST_PLAN_LOG"
LOGGED_FIELDS = (
    "invocation_id",
    "root_invocation_id",
    "step_count",
    "uses",
    "plan_step",
    "use",
    "status",
    "error_type",
    "remote_task_id",
    "duration_ms",
    "total_tokens",
)

# Goals the stand-in planning model knows, so tests can choose its plan.
POST_GOAL = "A post about tides"
TWO_BRIEFS_GOAL = "Brief me twice"
PRIVATE_GOAL = "Publish the salaries"
PEER_GOAL = "Research tides and tell the peer, then fail"
TOOLS_GOAL = "Find tide sources and greet the peer"
PRIVATE_URL = "https://intranet.example/salaries"


def planned_step(
    step_id: str, use: str, arguments: dict[str, Any], after: Sequence[str] = ()
) -> dict[str, Any]:
    """One step of a ``ctx.plan`` reply, as a planning model writes it."""
    arguments_json = json.dumps(arguments)
    return {"id": step_id, "use": use, "arguments": arguments_json, "after": [*after]}


def plan_reply(steps: list[dict[str, Any]], result: str) -> str:
    """A ``ctx.plan`` reply: the steps and the step whose value is the result."""
    return json.dumps({"steps": steps, "result": result, "reason": None})


PLANS = {
    # Sources and a brief at once, then a post from both.
    POST_GOAL: plan_reply(
        [
            planned_step("tides", "research-agent.find_sources", {"topic": "tides"}),
            planned_step("brief", "research-agent.brief", {"question": "Tides?"}),
            planned_step(
                "post",
                "writer-agent.write_post",
                {
                    "title": "Tides",
                    "sources": {"$ref": "tides"},
                    "notes": {"$ref": "brief"},
                },
            ),
        ],
        result="post",
    ),
    # Refused by the planner's plan check: at most one brief per plan.
    TWO_BRIEFS_GOAL: plan_reply(
        [
            planned_step("first", "research-agent.brief", {"question": "Tides?"}),
            planned_step("second", "research-agent.brief", {"question": "Waves?"}),
        ],
        result="second",
    ),
    # Refused by the planner's tool check: a private source for a public post.
    PRIVATE_GOAL: plan_reply(
        [
            planned_step("tides", "research-agent.find_sources", {"topic": "tides"}),
            planned_step(
                "post",
                "writer-agent.write_post",
                {"title": "Salaries", "sources": [{"title": "HR", "url": PRIVATE_URL}]},
                after=["tides"],
            ),
        ],
        result="post",
    ),
    # The coordinator's plan: the peer's "fail" step fails, the step after it
    # is skipped, and the rest completes.
    PEER_GOAL: plan_reply(
        [
            planned_step("tides", "research-agent.find_sources", {"topic": "tides"}),
            planned_step("tell", "peer", {"text": "Sources: {tides}"}),
            planned_step("fail", "peer", {"text": "fail"}),
            planned_step(
                "post",
                "writer-agent.write_post",
                {"title": "Tides", "sources": {"$ref": "tides"}},
                after=["fail"],
            ),
        ],
        result="tell",
    ),
}

TOOL_CALLS = {
    TOOLS_GOAL: (
        ToolCall("call_1", "research-agent_find_sources", '{"topic": "tides"}'),
        ToolCall("call_2", "peer", '{"text": "hello"}'),
    )
}


class GoalModel(ScriptedModel):
    """A stand-in planning model with a fixed answer for each goal it knows.

    ``ctx.plan`` gets the plan in ``PLANS``; a tool loop gets the calls in
    ``TOOL_CALLS``, then an answer joining the tools' results. Every call
    reports ``PLANNING_TOKENS`` used.
    """

    def __init__(self) -> None:
        capabilities = ModelCapabilities(
            context_window=8192, structured_output=True, tools=True
        )
        super().__init__(capabilities=capabilities)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        last = request.messages[-1]
        if last.role == "tool":
            results = [m.content for m in request.messages if m.role == "tool"]
            return self._reply("Answer: " + " | ".join(results))
        if request.output is not None:
            return self._reply(PLANS[json.loads(last.content)["goal"]])
        return self._reply("", tool_calls=TOOL_CALLS[last.content])

    @staticmethod
    def _reply(text: str, tool_calls: tuple[ToolCall, ...] = ()) -> ModelResponse:
        usage = Usage(
            input_tokens=PLANNING_TOKENS - 20,
            output_tokens=20,
            total_tokens=PLANNING_TOKENS,
            estimated=False,
        )
        finish = FinishReason.TOOL_CALLS if tool_calls else FinishReason.COMPLETED
        return ModelResponse(
            text=text, usage=usage, finish_reason=finish, tool_calls=tool_calls
        )


class PlanLogFile(logging.Handler):
    """Writes each orchestration log record's message and fields as a JSON line.

    ``agentframe run`` prints only messages, so this is how a test sees the
    records' ``extra`` fields from another process.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def emit(self, record: logging.LogRecord) -> None:
        line: dict[str, Any] = {"message": record.getMessage()}
        line |= {name: getattr(record, name, None) for name in LOGGED_FIELDS}
        with open(self.path, "a") as file:
            file.write(json.dumps(line) + "\n")


def log_plans() -> None:
    """Log orchestration records to the file ``AGENTFRAME_TEST_PLAN_LOG`` names."""
    path = os.environ.get(PLAN_LOG_VARIABLE)
    if path:
        logging.getLogger("agentframe.orchestration").addHandler(PlanLogFile(path))


def create_planner_app() -> AgentApp:
    """The example's planner, planning with ``GoalModel`` instead of Ollama."""
    log_plans()
    return planner.create_app(GoalModel())


@agent(name="Coordinator", version="0.1.0")
class Coordinator(Agent):
    """Offers every registered agent's skills to its model, as tools or a plan."""

    @skill(id="answer", tags=["orchestration"])
    async def answer(self, goal: str, ctx: RunContext) -> str:
        """Answer a goal by letting the model call registered agents' skills."""
        skills = await ctx.remote_skills(max_tokens_per_call=1_000)
        return await ctx.generate(goal, tools=[*skills])

    @skill(id="run_goal", tags=["orchestration"])
    async def run_goal(self, goal: str, ctx: RunContext) -> dict[str, Any]:
        """Plan a goal over registered agents' skills, run it, report each step."""
        skills = await ctx.remote_skills()
        plan = await ctx.plan(goal, skills)
        result = await ctx.run_plan(
            plan, skills, max_tokens_per_step=1_000, on_failure="continue"
        )
        return {
            "value": result.value,
            "complete": result.complete,
            "steps": {step_id: step.status for step_id, step in result.steps.items()},
            "usage": None if result.usage is None else result.usage.total_tokens,
        }


def create_coordinator_app() -> AgentApp:
    log_plans()
    dev = dev_settings()
    return AgentApp(
        agent=Coordinator(model=GoalModel()),
        host=HostConfig(public_url=dev.public_url, auth=dev.auth),
        directory=dev.directory,
        remotes=dev.remotes,
    )
