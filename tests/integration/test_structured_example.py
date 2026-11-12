"""Typed structured output end to end, offline: the example's ``summarize`` skill.

``examples/assistant.py`` is served over real localhost HTTP and called by the
official A2A client with a structured request. A scripted model that supports
structured output stands in for Ollama and Foundry; ``test_providers.py`` runs
the real hosts as opt-in tests.
"""

import json

import pytest
from a2a.helpers import get_data_parts, get_message_text
from a2a.types import Task, TaskState

from a2a_client import call_skill, connect
from agentframe import AgentApp, ModelCapabilities, ModelResponse
from agentframe.runtime.usage import FinishReason, Usage
from agentframe.testing import ScriptedModel
from examples.assistant import create_app
from local_server import ServeLocally

TEXT = "AgentFrame serves typed agents over A2A. Models are injected."
STRUCTURED = ModelCapabilities(context_window=8192, structured_output=True)
USAGE = Usage(input_tokens=40, output_tokens=20, total_tokens=60, estimated=False)
# Model text that must never reach a caller in a failure message.
SECRET = "secret model words"


async def summarize_over_a2a(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch, model: ScriptedModel
) -> Task:
    """Serve the example with ``model`` and call ``summarize`` over A2A."""

    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ASSISTANT_PUBLIC_URL", public_url)
        return create_app(model)

    async with serve(make_app) as base_url, await connect(base_url) as client:
        return await call_skill(client, "summarize", {"text": TEXT})


async def test_summarize_returns_a_typed_summary_as_a_data_part(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = {"text": "AgentFrame hosts agents.", "key_points": ["A2A", "Models"]}
    reply = ModelResponse(json.dumps(summary), USAGE, FinishReason.COMPLETED)
    model = ScriptedModel([reply], capabilities=STRUCTURED)

    task = await summarize_over_a2a(serve, monkeypatch, model)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [artifact] = task.artifacts
    assert get_data_parts(artifact.parts) == [{"value": summary}]
    assert task.metadata["usage"] == {
        "input_tokens": 40,
        "output_tokens": 20,
        "total_tokens": 60,
        "estimated": False,
        "model_calls": 1,
        "tool_calls": 0,
    }
    [request] = model.requests
    assert request.output is not None
    assert request.output.name == "Summary"
    assert request.messages[-1].content == TEXT


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (
            ModelResponse(f'{{"text": "{SECRET}"', USAGE, FinishReason.COMPLETED),
            "The model's output is not a valid 'Summary'",
        ),
        (
            ModelResponse(SECRET, USAGE, FinishReason.REFUSED),
            "The model refused the request",
        ),
        (
            ModelResponse(f'{{"text": "{SECRET}', USAGE, FinishReason.LENGTH),
            "The model's output was cut short at its output token limit",
        ),
    ],
    ids=["invalid-json", "refusal", "truncation"],
)
async def test_bad_model_output_fails_the_task_safely(
    serve: ServeLocally,
    monkeypatch: pytest.MonkeyPatch,
    reply: ModelResponse,
    message: str,
) -> None:
    model = ScriptedModel([reply], capabilities=STRUCTURED)

    task = await summarize_over_a2a(serve, monkeypatch, model)

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert not task.artifacts
    failure = get_message_text(task.status.message)
    assert failure.startswith(message)
    assert SECRET not in failure
    assert task.metadata["usage"]["model_calls"] == 1


async def test_a_model_without_structured_output_fails_before_sending(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel(capabilities=ModelCapabilities(context_window=8192))

    task = await summarize_over_a2a(serve, monkeypatch, model)

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert not task.artifacts
    assert get_message_text(task.status.message) == (
        "The model does not support structured output"
    )
    assert task.metadata["usage"]["model_calls"] == 0
    assert model.requests == []
