"""The README success criterion, offline: one agent, two models, no code change.

``examples/assistant.py`` is served over real localhost HTTP and called by the
official A2A client with structured requests, once per model. Two scripted
models stand in for local Llama and a Foundry deployment; ``test_providers.py``
runs the real hosts as opt-in tests.
"""

import httpx
import pytest
from a2a.client import A2ACardResolver
from a2a.helpers import get_artifact_text, get_data_parts
from a2a.types import TaskState

from a2a_client import call_skill, connect
from agentframe import AgentApp, FoundryModel, ModelResponse, OllamaModel
from agentframe.runtime.usage import FinishReason, Usage
from agentframe.testing import ScriptedModel
from examples.assistant import create_app, create_model
from local_server import ServeLocally

QUESTION = "What is the capital of France?"


@pytest.mark.parametrize(
    ("answer", "input_tokens", "output_tokens"),
    [("Paris.", 21, 3), ("The capital of France is Paris.", 34, 9)],
    ids=["first-model", "second-model"],
)
async def test_the_same_agent_serves_either_model_over_a2a(
    serve: ServeLocally,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    usage = Usage(input_tokens, output_tokens, input_tokens + output_tokens, False)
    model = ScriptedModel([ModelResponse(answer, usage, FinishReason.COMPLETED)])

    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ASSISTANT_PUBLIC_URL", public_url)
        return create_app(model)

    async with serve(make_app) as base_url:
        async with httpx.AsyncClient() as http:
            card = await A2ACardResolver(http, base_url).get_agent_card()
        async with await connect(base_url) as client:
            answered = await call_skill(client, "answer", {"question": QUESTION})
            counted = await call_skill(client, "count_words", {"text": "one two"})

    assert card.name == "Assistant"
    assert sorted(s.id for s in card.skills) == ["answer", "count_words", "summarize"]
    assert answered.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(answered.artifacts[0]) == answer
    assert answered.metadata["usage"] == {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated": False,
        "model_calls": 1,
        "tool_calls": 0,
    }
    assert model.requests[0].messages[-1].content == QUESTION
    # The model-free skill runs without inference.
    assert counted.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_data_parts(counted.artifacts[0].parts) == [{"value": 2}]
    assert counted.metadata["usage"]["model_calls"] == 0
    assert len(model.requests) == 1


async def test_ollama_is_chosen_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFRAME_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("AGENTFRAME_OLLAMA_MODEL", "llama3.2:3b")

    model = create_model()

    assert isinstance(model, OllamaModel)
    assert model.model == "llama3.2:3b"
    await model.close()


async def test_foundry_is_chosen_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFRAME_MODEL_PROVIDER", "foundry")
    monkeypatch.setenv(
        "AGENTFRAME_FOUNDRY_ENDPOINT", "https://example.openai.azure.com/openai/v1/"
    )
    monkeypatch.setenv("AGENTFRAME_FOUNDRY_DEPLOYMENT", "gpt-test")

    model = create_model()

    assert isinstance(model, FoundryModel)
    assert model.deployment == "gpt-test"
    await model.close()


def test_an_unknown_provider_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFRAME_MODEL_PROVIDER", "elsewhere")

    with pytest.raises(ValueError, match="elsewhere"):
        create_model()
