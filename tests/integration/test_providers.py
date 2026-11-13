"""Real-provider tests: one agent, run against a real Ollama server and Foundry.

These tests call real model providers, so default ``uv run pytest`` runs and CI
skip them. Enable each provider explicitly; AGENTS.md ("Real-provider tests")
lists every environment variable. For example::

    AGENTFRAME_TEST_OLLAMA=1 AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \\
        uv run pytest -m ollama

Foundry signs in with the Azure CLI credential, so run ``az login`` first. The
agent code is identical for both providers; only the model passed to it
differs. Prompts and output caps are tiny to keep paid calls cheap.
"""

import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
import httpx2
import openai
import pytest
from a2a.client import A2ACardResolver
from a2a.helpers import get_artifact_text, get_data_parts
from a2a.types import TaskState
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from pydantic import BaseModel

from a2a_client import call_skill, connect, say
from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    FoundryModel,
    HostConfig,
    IncompleteOutputError,
    Model,
    ModelSettings,
    OllamaModel,
    ProviderError,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    agent,
    skill,
    tool,
)
from agentframe.models.foundry import FOUNDRY_SCOPE
from agentframe.models.ollama import DEFAULT_ENDPOINT
from examples.assistant import Summary, create_app
from examples.orders import create_app as create_orders_app
from examples.routed_assistant import create_app as create_routed_app
from local_server import ServeLocally

# The SDK's own HTTP client times out after httpx's default 5 seconds, too soon
# for real models. This stays above the agents' 120-second run timeout, so the
# client never gives up before the server's own deadline.
A2A_CLIENT_TIMEOUT_SECONDS = 150

# A name no provider has, to prove an unknown model fails clearly.
MISSING_MODEL = "agentframe-test-missing-model"


@agent(version="1.0.0")
class Assistant(Agent):
    """Answers short questions."""

    instructions = "Answer in as few words as possible."
    model_settings = ModelSettings(max_output_tokens=64)
    # Foundry does not report a deployment's context window, so cap the input.
    limits = RunLimits(max_input_tokens=1000)

    @skill("answer", tags=["questions"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer a question briefly."""
        return await ctx.generate(question)


class Capital(BaseModel):
    country: str
    city: str
    nicknames: list[str] = []


@agent(version="1.0.0")
class Geographer(Agent):
    """Answers geography questions with typed data."""

    # Ollama sees only the schema in format=, so describe the shape here.
    instructions = "Reply with JSON: the country, its capital city and nicknames."

    # Reasoning deployments count reasoning tokens toward the output cap.
    model_settings = ModelSettings(max_output_tokens=256)
    limits = RunLimits(max_input_tokens=1000)

    @skill("capital", tags=["geography"])
    async def capital(self, country: str, ctx: RunContext) -> Capital:
        """Name a country's capital city."""
        return await ctx.generate(f"The capital of {country}?", output=Capital)


@tool(description="Look up the delivery status of an order by its ID.")
def lookup_order(order_id: str) -> dict[str, str]:
    # Small local models use a structured result more reliably than prose.
    return {"order_id": order_id, "status": "delivered", "delivery_day": "Tuesday"}


@agent(version="1.0.0")
class OrderDesk(Agent):
    """Answers questions about orders with a lookup tool."""

    instructions = "Answer in one short sentence."
    model_settings = ModelSettings(max_output_tokens=64)
    limits = RunLimits(max_input_tokens=1000, max_tool_calls=2)

    @skill("status", tags=["orders"])
    async def status(self, question: str, ctx: RunContext) -> str:
        """Answer a question about an order."""
        return await ctx.generate(question, tools=[lookup_order])


# The providers, configured only through environment variables


def required_env(name: str) -> str:
    """Return an environment variable that an enabled provider test needs."""
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"Set {name} to run this real-provider test")
    return value


@asynccontextmanager
async def ollama_model(name: str | None = None) -> AsyncIterator[Model]:
    """Open the configured Ollama model, or the model tag ``name``."""
    async with OllamaModel(
        model=name or required_env("AGENTFRAME_OLLAMA_MODEL"),
        endpoint=os.environ.get("AGENTFRAME_OLLAMA_ENDPOINT", DEFAULT_ENDPOINT),
        context_window=8192,
    ) as model:
        yield model


@asynccontextmanager
async def foundry_model(name: str | None = None) -> AsyncIterator[Model]:
    """Open the configured Foundry deployment, or the deployment ``name``."""
    async with (
        AzureCliCredential() as credential,
        FoundryModel(
            endpoint=required_env("AGENTFRAME_FOUNDRY_ENDPOINT"),
            deployment=name or required_env("AGENTFRAME_FOUNDRY_DEPLOYMENT"),
            credential=credential,
        ) as model,
    ):
        yield model


type OpenModel = Callable[..., AbstractAsyncContextManager[Model]]


def opt_in(marker: pytest.MarkDecorator, variable: str) -> list[pytest.MarkDecorator]:
    """Mark a provider's tests and skip them unless ``variable`` is ``1``."""
    enabled = os.environ.get(variable) == "1"
    return [marker, pytest.mark.skipif(not enabled, reason=f"set {variable}=1")]


@pytest.fixture(
    params=[
        pytest.param(
            ollama_model,
            id="ollama",
            marks=opt_in(pytest.mark.ollama, "AGENTFRAME_TEST_OLLAMA"),
        ),
        pytest.param(
            foundry_model,
            id="foundry",
            marks=opt_in(pytest.mark.foundry, "AGENTFRAME_TEST_FOUNDRY"),
        ),
    ]
)
def open_model(request: pytest.FixtureRequest) -> OpenModel:
    """Open a real model: the configured one, or one named in the call."""
    open_provider_model: OpenModel = request.param
    return open_provider_model


@pytest.fixture
async def model(open_model: OpenModel) -> AsyncIterator[Model]:
    async with open_model() as configured_model:
        yield configured_model


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client for A2A calls that waits as long as a real model run."""
    async with httpx.AsyncClient(timeout=A2A_CLIENT_TIMEOUT_SECONDS) as client:
        yield client


# Local runs through Runtime


async def test_agent_generates_text_and_reports_usage(model: Model) -> None:
    result = await Runtime().run(
        Assistant(model=model),
        skill="answer",
        arguments={"question": "Reply with the word: ready"},
    )

    assert "ready" in result.value.lower()
    assert result.model_calls == 1
    assert result.usage.input_tokens
    assert result.usage.output_tokens
    assert not result.usage.estimated


async def test_tiny_output_limit_cuts_the_answer_short(model: Model) -> None:
    tiny = RunOptions(model_settings=ModelSettings(max_output_tokens=16))

    with pytest.raises(IncompleteOutputError, match="output token limit") as raised:
        await Runtime().run(
            Assistant(model=model),
            skill="answer",
            arguments={"question": "Count from 1 to 100, separated by commas."},
            options=tiny,
        )

    assert raised.value.invocation_id


async def test_unknown_model_fails_with_a_clear_error(open_model: OpenModel) -> None:
    async with open_model(MISSING_MODEL) as missing:
        with pytest.raises(ProviderError, match=MISSING_MODEL) as raised:
            await Runtime().run(
                Assistant(model=missing),
                skill="answer",
                arguments={"question": "Reply with the word: ready"},
            )

    assert not raised.value.transient
    assert raised.value.invocation_id


# Structured output


async def test_agent_returns_a_validated_structured_output(model: Model) -> None:
    result = await Runtime().run(
        Geographer(model=model), skill="capital", arguments={"country": "France"}
    )

    assert isinstance(result.value, Capital)
    assert result.value.city == "Paris"
    assert result.model_calls == 1
    assert result.usage.total_tokens
    assert not result.usage.estimated


async def test_tiny_output_limit_cuts_structured_output_short(model: Model) -> None:
    tiny = RunOptions(model_settings=ModelSettings(max_output_tokens=16))

    with pytest.raises(IncompleteOutputError, match="output token limit") as raised:
        await Runtime().run(
            Geographer(model=model),
            skill="capital",
            arguments={"country": "France, with twenty nicknames for the city"},
            options=tiny,
        )

    assert raised.value.partial_text is None
    assert raised.value.invocation_id


# Hosted: the official A2A client calls the agent with a structured request


async def test_hosted_agent_answers_a_structured_a2a_call(
    serve: ServeLocally, model: Model, http: httpx.AsyncClient
) -> None:
    def make_app(public_url: str) -> AgentApp:
        host = HostConfig(public_url=public_url, auth=AllowAnonymous())
        return AgentApp(agent=Assistant(model=model), host=host)

    async with serve(make_app) as base_url:
        card = await A2ACardResolver(http, base_url).get_agent_card()
        async with await connect(base_url, http=http) as client:
            task = await call_skill(
                client, "answer", {"question": "Reply with the word: ready"}
            )

    assert [s.id for s in card.skills] == ["answer"]
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert "ready" in get_artifact_text(task.artifacts[0]).lower()
    usage = task.metadata["usage"]
    assert usage["input_tokens"]
    assert usage["output_tokens"]
    assert usage["model_calls"] == 1
    assert not usage["estimated"]


async def test_example_summarizes_over_a2a(
    serve: ServeLocally,
    model: Model,
    http: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ASSISTANT_PUBLIC_URL", public_url)
        return create_app(model)

    async with (
        serve(make_app) as base_url,
        await connect(base_url, http=http) as client,
    ):
        task = await call_skill(
            client, "summarize", {"text": "Cats sleep a lot. Cats like fish."}
        )

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [data] = get_data_parts(task.artifacts[0].parts)
    summary = Summary.model_validate(data["value"])
    assert summary.text
    assert summary.key_points
    usage = task.metadata["usage"]
    assert usage["total_tokens"]
    assert usage["model_calls"] == 1
    assert not usage["estimated"]


# Tool calling


async def require_tools(model: Model) -> None:
    """Skip the test when the configured model cannot call tools."""
    if not (await model.capabilities()).tools:
        pytest.skip("the configured model does not support tools")


async def test_agent_completes_a_tool_round_trip(model: Model) -> None:
    await require_tools(model)
    result = await Runtime().run(
        OrderDesk(model=model, tools=[lookup_order]),
        skill="status",
        arguments={"question": "On which day was order A1 delivered?"},
    )

    assert result.tool_calls == 1
    assert "tuesday" in result.value.lower()
    assert result.model_calls == 2
    assert result.usage.total_tokens
    assert not result.usage.estimated


async def test_order_example_uses_a_tool_over_a2a(
    serve: ServeLocally,
    model: Model,
    http: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await require_tools(model)
    monkeypatch.setenv("ORDERS_API_KEY", "test-orders-key")

    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ORDERS_PUBLIC_URL", public_url)
        return create_orders_app(model)

    async with (
        serve(make_app) as base_url,
        await connect(base_url, "test-orders-key", http=http) as client,
    ):
        task = await call_skill(
            client, "order_help", {"request": "What is the status of order A1?"}
        )

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert "shipped" in get_artifact_text(task.artifacts[0]).lower()
    usage = task.metadata["usage"]
    assert usage["tool_calls"] == 1
    assert usage["model_calls"] == 2
    assert usage["total_tokens"]
    assert not usage["estimated"]


# Model routing: free text over A2A, routed by the built-in router


async def test_free_text_is_routed_to_the_skill_it_asks_for(
    serve: ServeLocally,
    model: Model,
    http: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ASSISTANT_PUBLIC_URL", public_url)
        return create_routed_app(model)

    async with (
        serve(make_app) as base_url,
        await connect(base_url, http=http) as client,
    ):
        task = await say(client, "Convert 25 degrees Celsius to Fahrenheit.")

    # Routing is nondeterministic, so only the selected skill and the
    # presence of its argument are checked: only to_fahrenheit returns data.
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [data] = get_data_parts(task.artifacts[0].parts)
    assert set(data["value"]) == {"celsius", "fahrenheit"}
    usage = task.metadata["usage"]
    # Selection and extraction; the skill itself makes no model call.
    assert usage["model_calls"] == 2
    assert usage["total_tokens"]
    assert not usage["estimated"]


# Tool calling (Foundry): reasoning items round-trip through the tool loop


@tool(description="Look up when an order ships.")
def ship_date(order_id: str, units: str = "days") -> str:
    return f"Order {order_id} ships in 3 {units}."


@agent(version="1.0.0")
class ShippingDesk(Agent):
    """Answers questions about orders."""

    model_settings = ModelSettings(max_output_tokens=512)
    limits = RunLimits(max_input_tokens=2000)

    @skill("ask", tags=["orders"])
    async def ask(self, question: str, ctx: RunContext) -> str:
        """Answer with the shipping tool."""
        return await ctx.generate(question, tools=[ship_date])


class ReasoningTransport(httpx2.AsyncBaseTransport):
    """Turns reasoning on and records every request body sent to Foundry.

    AgentFrame has no reasoning-effort setting, and the deployment's default
    may skip reasoning, so this application-supplied transport asks for it to
    make reasoning items appear.
    """

    def __init__(self) -> None:
        self.inner = httpx2.AsyncHTTPTransport()
        self.bodies: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) | {"reasoning": {"effort": "medium"}}
        self.bodies.append(body)
        headers = [
            (name, value)
            for name, value in request.headers.items()
            if name.lower() != "content-length"
        ]
        return await self.inner.handle_async_request(
            httpx2.Request(request.method, request.url, headers=headers, json=body)
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


@pytest.mark.foundry
@pytest.mark.skipif(
    os.environ.get("AGENTFRAME_TEST_FOUNDRY") != "1",
    reason="set AGENTFRAME_TEST_FOUNDRY=1",
)
async def test_foundry_tool_call_replays_encrypted_reasoning() -> None:
    transport = ReasoningTransport()
    async with (
        AzureCliCredential() as credential,
        httpx2.AsyncClient(transport=transport) as http_client,
    ):
        client = openai.AsyncOpenAI(
            base_url=required_env("AGENTFRAME_FOUNDRY_ENDPOINT"),
            api_key=get_bearer_token_provider(credential, FOUNDRY_SCOPE),
            http_client=http_client,
        )
        model = FoundryModel(
            deployment=required_env("AGENTFRAME_FOUNDRY_DEPLOYMENT"), client=client
        )
        result = await Runtime().run(
            ShippingDesk(model=model, tools=[ship_date]),
            skill="ask",
            arguments={
                "question": "Today is Monday. On which weekday does order A1 "
                "ship? Check with the tool."
            },
        )

    assert "thursday" in result.value.lower()
    assert result.model_calls == 2
    assert result.tool_calls == 1
    assert not result.usage.estimated
    first, second = transport.bodies
    assert first["include"] == ["reasoning.encrypted_content"]
    assert first["store"] is False
    replayed = [item.get("type", "message") for item in second["input"]]
    assert replayed == ["message", "reasoning", "function_call", "function_call_output"]
    assert second["input"][1]["encrypted_content"]
