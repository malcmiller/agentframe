"""Tests for host limit ceilings, ``HostConfig(limits_ceiling=...)``.

A ceiling caps every hosted run and routing. Library defaults above it are
lowered to it; agent or skill limits set explicitly above it fail startup.
Hosted requests travel over real localhost HTTP.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx
import pytest
from a2a.types import Message

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    Clarification,
    DefinitionError,
    HostConfig,
    ModelCapabilities,
    ModelRouter,
    RoutingContext,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    SkillCall,
    SkillDefinition,
    TimeoutLimitExceededError,
    agent,
    skill,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

UNKNOWN_WINDOW = ModelCapabilities(context_window=None, structured_output=True)


@agent(version="1.0.0")
class Worker(Agent):
    """Sets no limits of its own, so the library defaults apply."""

    @skill("sleep", tags=["test"])
    async def sleep(self, seconds: float, ctx: RunContext) -> str:
        """Sleep for ``seconds``."""
        await asyncio.sleep(seconds)
        return "awake"

    @skill("write", tags=["test"])
    async def write(self, text: str, ctx: RunContext) -> str:
        """Send ``text`` to the model."""
        return await ctx.generate(text)


@agent(version="1.0.0")
class GreedyAgent(Agent):
    """Asks for more model calls than the test ceiling allows."""

    limits = RunLimits(max_model_calls=50)

    @skill("work", tags=["test"])
    async def work(self, ctx: RunContext) -> str:
        """Do nothing."""
        return "done"


@agent(version="1.0.0")
class GreedySkill(Agent):
    """One skill asks for more model calls than the test ceiling allows."""

    @skill("modest", tags=["test"])
    async def modest(self, ctx: RunContext) -> str:
        """Do nothing."""
        return "done"

    @skill("work", limits=RunLimits(max_model_calls=50), tags=["test"])
    async def work(self, ctx: RunContext) -> str:
        """Do nothing."""
        return "done"


@agent(version="1.0.0")
class LoweredBySkill(Agent):
    """The agent asks for too much, but its only skill lowers it again."""

    limits = RunLimits(max_model_calls=50)

    @skill("work", limits=RunLimits(max_model_calls=5), tags=["test"])
    async def work(self, ctx: RunContext) -> str:
        """Do nothing."""
        return "done"


class SlowRouter:
    """Takes longer to route than any test ceiling allows."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        await asyncio.sleep(30)
        return SkillCall("sleep", {"seconds": 0})


def host(public_url: str, ceiling: RunLimits) -> HostConfig:
    # Anonymous access is for these local tests only.
    return HostConfig(
        public_url=public_url, auth=AllowAnonymous(), limits_ceiling=ceiling
    )


@contextlib.asynccontextmanager
async def serve_worker(
    serve: ServeLocally,
    ceiling: RunLimits,
    model: ScriptedModel | None = None,
    router: Any = None,
) -> AsyncIterator[httpx.AsyncClient]:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Worker(model=model), host=host(public_url, ceiling), router=router
        )

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        yield h


async def send(
    http: httpx.AsyncClient, part: Mapping[str, Any], skill_id: str | None = None
) -> dict[str, Any]:
    """Send one message and return its task: a skill call when ``skill_id`` is set."""
    message: dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [part],
    }
    headers = {"A2A-Version": "1.0"}
    if skill_id is not None:
        message["metadata"] = {"agentframe.skill": skill_id}
        headers["A2A-Extensions"] = SKILL_CALL_EXTENSION_URI
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    response = await http.post("/a2a", json=body, headers=headers, timeout=30)
    task: dict[str, Any] = response.json()["result"]["task"]
    return task


def failure_text(task: Mapping[str, Any]) -> str:
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    return " ".join(part["text"] for part in task["status"]["message"]["parts"])


async def lifespan_startup(app: AgentApp) -> dict[str, Any]:
    """Run the ASGI lifespan startup and return the server's reply."""
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await events.put({"type": "lifespan.startup"})
    replies: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return await events.get()

    async def send_event(message: Any) -> None:
        replies.append(message)
        await events.put({"type": "lifespan.shutdown"})

    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    # Starlette re-raises a startup error after reporting it.
    with contextlib.suppress(DefinitionError):
        await app(scope, receive, send_event)
    return replies[0]


# Startup checks of explicit limits


@pytest.mark.parametrize(
    "greedy", [GreedyAgent, GreedySkill], ids=["agent-level", "skill-level"]
)
def test_an_explicit_limit_above_the_ceiling_fails_construction(
    greedy: type[Agent],
) -> None:
    ceiling = RunLimits(max_model_calls=6)

    with pytest.raises(
        DefinitionError,
        match=r"Skill 'work': RunLimits.max_model_calls of 50 exceeds the host "
        r"ceiling of 6",
    ):
        AgentApp(agent=greedy(), host=host("http://127.0.0.1:8000", ceiling))


def test_a_skill_may_lower_an_agent_limit_below_the_ceiling() -> None:
    ceiling = RunLimits(max_model_calls=6)

    AgentApp(agent=LoweredBySkill(), host=host("http://127.0.0.1:8000", ceiling))


# Hosted runs use the capped limits


async def test_a_library_default_above_the_ceiling_is_lowered_to_it(
    serve: ServeLocally,
) -> None:
    async with serve_worker(serve, RunLimits(timeout_seconds=1)) as h:
        started = asyncio.get_running_loop().time()
        task = await send(h, {"data": {"seconds": 30}}, skill_id="sleep")
        elapsed = asyncio.get_running_loop().time() - started

    assert "exceeded its timeout of 1 seconds" in failure_text(task)
    assert elapsed < 5  # Not the 120-second library default.


async def test_free_text_routing_is_bounded_by_the_capped_timeout(
    serve: ServeLocally,
) -> None:
    ceiling = RunLimits(timeout_seconds=0.2)

    async with serve_worker(serve, ceiling, router=SlowRouter()) as h:
        task = await send(h, {"text": "sleep a little"})

    assert "Routing the request exceeded the run's timeout" in failure_text(task)


async def test_the_ceiling_caps_the_models_context_window_as_input_limit(
    serve: ServeLocally,
) -> None:
    model = ScriptedModel(["unused"])

    async with serve_worker(serve, RunLimits(max_input_tokens=10), model) as h:
        task = await send(h, {"data": {"text": "x" * 100}}, skill_id="write")

    assert "over the limit of 10 input tokens" in failure_text(task)
    assert model.requests == []


async def test_a_total_token_ceiling_also_bounds_the_output_cap(
    serve: ServeLocally,
) -> None:
    model = ScriptedModel(["unused"])

    async with serve_worker(serve, RunLimits(max_total_tokens=1000), model) as h:
        task = await send(h, {"data": {"text": "hi"}}, skill_id="write")

    # The default 4096-token output cap does not fit and is never shrunk.
    assert "but only 1000 of the run's 1000 remain" in failure_text(task)
    assert model.requests == []


async def test_per_run_options_are_capped_too() -> None:
    # Hosted callers cannot send options, but the cap applies after every level.
    options = RunOptions(limits=RunLimits(timeout_seconds=60))

    with pytest.raises(TimeoutLimitExceededError, match=r"timeout of 0.05 seconds"):
        # noinspection PyProtectedMember
        await Runtime()._run_invocation(
            "run-1",
            Worker(),
            "sleep",
            {"seconds": 5},
            None,
            options,
            limits_ceiling=RunLimits(timeout_seconds=0.05),
        )


# Startup model checks use the capped input limit


async def test_startup_accepts_unknown_context_windows_under_an_input_ceiling() -> None:
    model = ScriptedModel(capabilities=UNKNOWN_WINDOW)

    def make_app(ceiling: RunLimits) -> AgentApp:
        return AgentApp(
            agent=Worker(model=model),
            host=host("http://127.0.0.1:8000", ceiling),
            router=ModelRouter(model),
        )

    uncapped = await lifespan_startup(make_app(RunLimits()))
    capped = await lifespan_startup(make_app(RunLimits(max_input_tokens=1000)))

    assert uncapped["type"] == "lifespan.startup.failed"
    assert capped["type"] == "lifespan.startup.complete"
