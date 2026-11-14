"""A model calls remote skills as tools, over real localhost A2A.

The specialist is an ``AgentApp`` with a scripted model, the plain peer is a
bare official-SDK server (``tests/support/a2a_peer.py``) and both are listed
in one registry. The orchestrator's model is scripted too; one test hosts it
and calls it with the official SDK client.
"""

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from a2a.helpers import get_text_parts
from a2a.types import TaskState

from a2a_client import call_skill, connect
from a2a_peer import PEER_QUESTION, PEER_SECRET, PeerExecutor, a2a_peer
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    AllowAnonymous,
    FinishReason,
    HostConfig,
    ModelCapabilities,
    ModelResponse,
    ModelSettings,
    ProviderError,
    RunContext,
    RunLimits,
    RunOptions,
    RunResult,
    Runtime,
    TokenLimitExceededError,
    ToolCall,
    ToolCallLimitExceededError,
    Usage,
    agent,
    skill,
)
from agentframe.runtime import generation
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

TOOL_MODEL = ModelCapabilities(context_window=128_000, tools=True)
FIND = "research_find_sources"


def used(total: int) -> Usage:
    return Usage(
        input_tokens=total - 10, output_tokens=10, total_tokens=total, estimated=False
    )


def reply(*calls: ToolCall, text: str = "", total: int = 100) -> ModelResponse:
    """A model response reporting ``total`` tokens: tool calls, or text."""
    finish = FinishReason.TOOL_CALLS if calls else FinishReason.COMPLETED
    return ModelResponse(
        text=text, usage=used(total), finish_reason=finish, tool_calls=calls
    )


def find(topic: str, call_id: str = "1") -> ToolCall:
    return ToolCall(call_id, FIND, json.dumps({"topic": topic}))


def ask_peer(text: str, call_id: str = "2") -> ToolCall:
    return ToolCall(call_id, "peer", json.dumps({"text": text}))


@agent(name="Research", description="Finds sources.", version="0.1.0")
class Research(Agent):
    """Uses one 50-token model call per request, then answers."""

    def __init__(self) -> None:
        super().__init__(
            model=ScriptedModel(reply(text="ok", total=50) for _ in range(9))
        )
        self.topics: list[str] = []

    @skill(id="find_sources", description="Find sources on a topic.", tags=["research"])
    async def find_sources(self, topic: str, ctx: RunContext) -> list[str]:
        self.topics.append(topic)
        await ctx.generate(topic)
        return [f"{topic} source"]


@agent(name="Orchestrator", description="Delegates.", version="0.1.0")
class Orchestrator(Agent):
    """Offers every registered agent's skills to its model."""

    def __init__(self, *replies: Any, **catalog_options: Any) -> None:
        self.scripted = ScriptedModel(replies, capabilities=TOOL_MODEL)
        super().__init__(model=self.scripted)
        self.catalog_options = catalog_options

    @skill(id="answer", description="Answer a goal.", tags=["orchestration"])
    async def answer(self, goal: str, ctx: RunContext) -> str:
        skills = await ctx.remote_skills(**self.catalog_options)
        return await ctx.generate(goal, tools=[*skills])

    def tool_results(self) -> list[str]:
        """What the model was told by its tool calls, in order."""
        last = self.scripted.requests[-1].messages
        return [message.content for message in last if message.role == "tool"]


class Registered(AgentRegistry):
    """Lists the agents served for one test by name."""

    def __init__(self, **urls: str) -> None:
        self.urls = urls

    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return [
            AgentReference(name, f"{url}/.well-known/agent-card.json")
            for name, url in self.urls.items()
        ]


def anonymous(public_url: str) -> HostConfig:
    return HostConfig(public_url=public_url, auth=AllowAnonymous())


type Agents = Callable[[], AbstractAsyncContextManager[AgentDirectory]]


@pytest.fixture
def research() -> Research:
    return Research()


@pytest.fixture
def peer() -> PeerExecutor:
    return PeerExecutor()


@pytest.fixture
def agents(serve: ServeLocally, research: Research, peer: PeerExecutor) -> Agents:
    """Serve the specialist and the plain peer; yield a directory listing both."""

    @asynccontextmanager
    async def serving() -> AsyncIterator[AgentDirectory]:
        def research_app(public_url: str) -> AgentApp:
            return AgentApp(agent=research, host=anonymous(public_url))

        async with (
            serve(research_app) as research_url,
            serve(a2a_peer(peer)) as peer_url,
            AgentDirectory(
                sources=[Registered(research=research_url, peer=peer_url)]
            ) as directory,
        ):
            yield directory

    return serving


async def run(
    agents: Agents, orchestrator: Orchestrator, limits: RunLimits | None = None
) -> RunResult[str]:
    """Run the orchestrator locally, with 100-token model answers."""
    async with agents() as directory:
        return await Runtime(directory=directory).run(
            orchestrator,
            skill="answer",
            arguments={"goal": "Write about tides"},
            options=RunOptions(
                limits=limits, model_settings=ModelSettings(max_output_tokens=100)
            ),
        )


async def test_the_model_calls_a_structured_skill_and_an_ask_entry_and_uses_both(
    agents: Agents, research: Research, peer: PeerExecutor
) -> None:
    orchestrator = Orchestrator(
        reply(find("tides"), ask_peer("Summarize: tides source")),
        reply(text="Tides: tides source. Echo: Summarize: tides source"),
    )

    result = await run(agents, orchestrator)

    assert result.value.startswith("Tides: tides source")
    assert orchestrator.tool_results() == [
        '["tides source"]',
        "Echo: Summarize: tides source",
    ]
    assert result.tool_calls == 2
    assert research.topics == ["tides"]
    assert [r.message.parts[0].text for r in peer.requests] == [
        "Summarize: tides source"
    ]


async def test_remote_calls_count_toward_max_tool_calls(
    agents: Agents, research: Research
) -> None:
    orchestrator = Orchestrator(reply(find("tides"), find("waves", "2")))

    with pytest.raises(ToolCallLimitExceededError):
        await run(agents, orchestrator, RunLimits(max_tool_calls=1))
    assert research.topics == ["tides"]


async def test_the_peers_reported_usage_is_charged_once(agents: Agents) -> None:
    orchestrator = Orchestrator(reply(find("tides")), reply(text="done"))

    result = await run(agents, orchestrator)

    # Two orchestrator model calls of 100 tokens, one research call of 50.
    assert result.usage.total_tokens == 250


async def test_a_peer_that_reports_no_usage_makes_usage_unknown(
    agents: Agents,
) -> None:
    orchestrator = Orchestrator(reply(ask_peer("hello")), reply(text="done"))

    result = await run(agents, orchestrator)

    assert result.usage.total_tokens is None


@pytest.mark.parametrize(
    ("text", "error"),
    [("fail", "RemoteAgentError"), ("question", "ClarificationNeededError")],
)
async def test_a_remote_failure_is_a_safe_tool_error_the_model_sees(
    agents: Agents, text: str, error: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="agentframe.orchestration.remote_tools")
    orchestrator = Orchestrator(reply(ask_peer(text)), reply(text="I tried."))

    result = await run(agents, orchestrator)

    assert result.value == "I tried."
    [told] = orchestrator.tool_results()
    assert json.loads(told) == {"error": f"The remote call failed: {error}"}
    assert PEER_SECRET not in caplog.text and PEER_QUESTION not in caplog.text
    [record] = [r for r in caplog.records if r.name.startswith("agentframe")]
    assert record.getMessage().startswith(
        f"Remote tool call 'peer' failed with {error}"
    )


async def test_a_token_limit_fails_the_run_before_sending(
    agents: Agents, research: Research
) -> None:
    orchestrator = Orchestrator(reply(find("tides")), max_tokens_per_call=5000)

    with pytest.raises(TokenLimitExceededError):
        await run(agents, orchestrator, RunLimits(max_total_tokens=3000))
    assert research.topics == []


async def test_a_remote_call_is_not_retried_after_a_transient_model_failure(
    agents: Agents, research: Research, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_wait(seconds: float) -> None:
        pass

    monkeypatch.setattr(generation, "wait_before_retry", no_wait)
    orchestrator = Orchestrator(
        reply(find("tides")),
        ProviderError("Server error", transient=True),
        reply(text="done"),
    )

    result = await run(agents, orchestrator)

    assert result.value == "done"
    assert research.topics == ["tides"]
    assert result.tool_calls == 1


async def test_a_hosted_orchestrator_calls_remote_skills_for_an_a2a_caller(
    serve: ServeLocally, agents: Agents, research: Research
) -> None:
    orchestrator = Orchestrator(reply(find("tides")), reply(text="Found tides."))

    async with agents() as directory:

        def orchestrator_app(public_url: str) -> AgentApp:
            return AgentApp(
                agent=orchestrator, host=anonymous(public_url), directory=directory
            )

        async with (
            serve(orchestrator_app) as url,
            await connect(url) as client,
        ):
            task = await call_skill(client, "answer", {"goal": "Write about tides"})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_text_parts(task.artifacts[0].parts) == ["Found tides."]
    assert research.topics == ["tides"]
