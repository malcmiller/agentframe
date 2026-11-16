"""``examples/dynamic_agents``: a planner that finds skills by tag (#245).

Every agent is served in-process over real localhost HTTP, and a scripted
model stands in for the planner's Ollama model, so nothing leaves localhost.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from a2a.helpers import get_data_parts, get_message_text
from a2a.types import Task, TaskState

from a2a_client import call_skill, connect
from agentframe import (
    Agent,
    AgentApp,
    HostConfig,
    Model,
    ModelCapabilities,
    Plan,
    PlanStep,
    RunContext,
    agent,
    dev_settings,
    skill,
)
from agentframe.testing import ScriptedModel
from examples.dynamic_agents import planner, research, writer
from local_server import ServeLocally

CLIENT_KEY = "client-key"
PLANNER_KEY = "planner-key"
STRUCTURED = ModelCapabilities(context_window=8192, structured_output=True)


@agent(name="Editor", version="0.1.0")
class Editor(Agent):
    """A third specialist, defined only here: the example knows nothing of it."""

    @skill(id="title_case", tags=["content", "editing"])
    async def title_case(self, text: str, ctx: RunContext) -> str:
        """Capitalize every word of a title."""
        return text.title()

    @skill(id="spell_check", tags=["editing"])
    async def spell_check(self, text: str, ctx: RunContext) -> str:
        """Not tagged ``content``, so the planner never sees it."""
        return text


def create_editor_app() -> AgentApp:
    dev = dev_settings()
    return AgentApp(
        agent=Editor(), host=HostConfig(public_url=dev.public_url, auth=dev.auth)
    )


def step(step_id: str, use: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One step of a ``ctx.plan`` reply, as the planning model writes it."""
    return {"id": step_id, "use": use, "arguments": json.dumps(arguments), "after": []}


def planning_model(steps: list[dict[str, Any]], result: str) -> ScriptedModel:
    """A stand-in planning model that answers ``ctx.plan`` with these steps."""
    reply = json.dumps({"steps": steps, "result": result, "reason": None})
    return ScriptedModel([reply], capabilities=STRUCTURED)


@asynccontextmanager
async def planner_over(
    specialists: dict[str, Callable[[], AgentApp]],
    model: Model | None,
    serve: ServeLocally,
    monkeypatch: pytest.MonkeyPatch,
    registry: Path,
) -> AsyncIterator[str]:
    """Serve the specialists and the planner as ``agentframe dev`` would.

    The specialists accept the planner's key; the registry file lists them,
    in the format of the example's ``agents.yaml``. Yields the planner's URL.
    """
    monkeypatch.setenv("AGENTFRAME_DEV", "1")
    monkeypatch.setenv("AGENTFRAME_DEV_API_KEYS", f"planner={PLANNER_KEY}")

    def dev_app(create_app: Callable[[], AgentApp]) -> Callable[[str], AgentApp]:
        def make_app(public_url: str) -> AgentApp:
            monkeypatch.setenv("AGENTFRAME_PUBLIC_URL", public_url)
            return create_app()

        return make_app

    async with AsyncExitStack() as stack:
        entries = []
        for name, create_app in specialists.items():
            url = await stack.enter_async_context(serve(dev_app(create_app)))
            entries.append({"name": name, "url": f"{url}/.well-known/agent-card.json"})
        registry.write_text(yaml.safe_dump(entries, sort_keys=False))

        monkeypatch.setenv("AGENTFRAME_DEV_REGISTRY", str(registry))
        monkeypatch.setenv("AGENTFRAME_DEV_API_KEYS", f"developer={CLIENT_KEY}")
        monkeypatch.setenv(
            "AGENTFRAME_DEV_REMOTE_KEYS",
            ",".join(f"{name}={PLANNER_KEY}" for name in specialists),
        )

        async with serve(dev_app(lambda: planner.create_app(model))) as planner_url:
            yield planner_url


async def run_goal(planner_url: str, goal: str) -> Task:
    client = await connect(planner_url, CLIENT_KEY)
    try:
        return await call_skill(client, "run_goal", {"goal": goal})
    finally:
        await client.close()


def outcome(task: Task) -> planner.PlanOutcome:
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, task.status
    [data] = get_data_parts(task.artifacts[0].parts)
    return planner.PlanOutcome.model_validate(data["value"])


SPECIALISTS = {
    "research-agent": research.create_app,
    "writer-agent": writer.create_app,
}


async def test_the_planner_finds_skills_by_tag_and_runs_a_two_step_plan(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = planning_model(
        [
            step("sources", "research-agent.find_sources", {"topic": "tides"}),
            step(
                "post",
                "writer-agent.write_post",
                {"title": "Tides", "sources": {"$ref": "sources"}},
            ),
        ],
        result="post",
    )
    async with planner_over(
        SPECIALISTS, model, serve, monkeypatch, tmp_path / "agents.yaml"
    ) as planner_url:
        task = await run_goal(planner_url, "A post about tides")

    result = outcome(task)
    assert result.skills == ["research-agent.find_sources", "writer-agent.write_post"]
    assert result.steps == {"sources": "completed", "post": "completed"}
    assert result.complete
    assert result.value == (
        "# Tides\n\n"
        "- [Tide tables explained](https://example.org/tides)\n"
        "- [How the Moon moves the oceans](https://example.org/moon)"
    )


async def test_a_newly_registered_tagged_agent_joins_with_no_code_change(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = planning_model(
        [
            step("title", "editor-agent.title_case", {"text": "tides and the moon"}),
            step(
                "post",
                "writer-agent.write_post",
                {"title": {"$ref": "title"}, "sources": []},
            ),
        ],
        result="post",
    )
    specialists = SPECIALISTS | {"editor-agent": create_editor_app}
    async with planner_over(
        specialists, model, serve, monkeypatch, tmp_path / "agents.yaml"
    ) as planner_url:
        task = await run_goal(planner_url, "A post titled tides and the moon")

    result = outcome(task)
    assert result.skills == [
        "research-agent.find_sources",
        "writer-agent.write_post",
        "editor-agent.title_case",
    ]
    assert result.value == "# Tides And The Moon\n"


async def test_a_failed_step_is_reported_rather_than_failing_the_run(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model = planning_model(
        [
            step("sources", "research-agent.find_sources", {"topic": "tides"}),
            step("post", "writer-agent.write_post", {"title": "Tides", "sources": 3}),
        ],
        result="post",
    )
    async with planner_over(
        SPECIALISTS, model, serve, monkeypatch, tmp_path / "agents.yaml"
    ) as planner_url:
        task = await run_goal(planner_url, "A post about tides")

    result = outcome(task)
    assert result.steps == {"sources": "completed", "post": "failed"}
    assert not result.complete
    assert result.value is None


async def test_the_plan_check_refuses_plans_longer_than_the_limit() -> None:
    run_goal = planner.Planner.definition().skills["run_goal"]

    def plan_of(size: int) -> Plan:
        steps = [
            PlanStep(id=f"s{i}", use="research-agent.find_sources", arguments={})
            for i in range(size)
        ]
        return Plan(steps=steps, result="s0")

    limit = planner.MAX_PLAN_STEPS
    assert await planner.small_plans_only(run_goal, None, plan_of(limit))
    assert not await planner.small_plans_only(run_goal, None, plan_of(limit + 1))


async def test_a_refused_plan_rejects_the_task_before_anything_is_sent(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def refuse_every_plan(*_: object) -> bool:
        return False

    # create_app reads the module's check when it builds the app.
    monkeypatch.setattr(planner, "small_plans_only", refuse_every_plan)
    model = planning_model(
        [step("sources", "research-agent.find_sources", {"topic": "tides"})],
        result="sources",
    )
    async with planner_over(
        SPECIALISTS, model, serve, monkeypatch, tmp_path / "agents.yaml"
    ) as planner_url:
        task = await run_goal(planner_url, "Sources on tides")

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert get_message_text(task.status.message) == (
        "The plan was blocked by an execution policy check"
    )


async def test_the_planner_starts_without_a_model_and_fails_clearly(
    serve: ServeLocally,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AGENTFRAME_OLLAMA_MODEL", raising=False)
    async with planner_over(
        SPECIALISTS, None, serve, monkeypatch, tmp_path / "agents.yaml"
    ) as planner_url:
        task = await run_goal(planner_url, "A post about tides")

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert get_message_text(task.status.message) == (
        "This agent has no model configured"
    )
    assert "AGENTFRAME_OLLAMA_MODEL is not set; run_goal will fail" in caplog.messages
