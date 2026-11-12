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

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
import pytest
from a2a.client import A2ACardResolver
from a2a.helpers import get_artifact_text
from a2a.types import TaskState
from azure.identity.aio import AzureCliCredential

from a2a_client import call_skill, connect
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
)
from agentframe.ollama_model import DEFAULT_ENDPOINT
from local_server import ServeLocally

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


# Hosted: the official A2A client calls the agent with a structured request


async def test_hosted_agent_answers_a_structured_a2a_call(
    serve: ServeLocally, model: Model
) -> None:
    def make_app(public_url: str) -> AgentApp:
        host = HostConfig(public_url=public_url, auth=AllowAnonymous())
        return AgentApp(agent=Assistant(model=model), host=host)

    async with serve(make_app) as base_url:
        async with httpx.AsyncClient() as http:
            card = await A2ACardResolver(http, base_url).get_agent_card()
        async with await connect(base_url) as client:
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
