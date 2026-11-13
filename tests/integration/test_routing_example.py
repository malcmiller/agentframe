"""Built-in model routing end to end, offline: ``examples/routed_assistant.py``.

The example is served over real localhost HTTP and called by the official A2A
client with free text, the way any other framework's A2A caller would. A
scripted model with structured output stands in for Ollama and Foundry, both
as the router's model and the agent's; ``test_providers.py`` runs the real
hosts as opt-in tests.
"""

import json
import re
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from a2a.helpers import get_artifact_text, get_data_parts, get_message_text
from a2a.types import Message, Role, Task, TaskState

from a2a_client import call_skill, connect, say
from agentframe import (
    AgentApp,
    AllowAnonymous,
    Clarification,
    HostConfig,
    ModelCapabilities,
    ModelResponse,
    ModelRouter,
    RoutingContext,
    RunLimits,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    agent,
)
from agentframe.runtime.usage import FinishReason, Usage
from agentframe.testing import ScriptedModel
from examples.routed_assistant import RoutedAssistant, Temperature, create_app
from local_server import ServeLocally

USAGE = Usage(input_tokens=40, output_tokens=10, total_tokens=50, estimated=False)


def scripted_model(*replies: object) -> ScriptedModel:
    """A model with structured output whose replies all report ``USAGE``.

    A ``dict`` reply is a JSON answer for a routing call; a ``str`` is text.
    """
    responses = [
        ModelResponse(
            reply if isinstance(reply, str) else json.dumps(reply),
            USAGE,
            FinishReason.COMPLETED,
        )
        for reply in replies
    ]
    return ScriptedModel(
        responses,
        capabilities=ModelCapabilities(context_window=8192, structured_output=True),
    )


def select(skill: str) -> dict[str, Any]:
    return {"skill": skill, "question": None}


def extract(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"arguments": arguments, "question": None}


def ask(question: str) -> dict[str, Any]:
    return {"arguments": None, "question": question}


def temperature(task: Task) -> Temperature:
    [data] = get_data_parts(task.artifacts[0].parts)
    return Temperature.model_validate(data["value"])


def serve_example(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch, model: ScriptedModel
) -> AbstractAsyncContextManager[str]:
    """Serve the example with ``model`` for both its skills and its router."""

    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ASSISTANT_PUBLIC_URL", public_url)
        return create_app(model)

    return serve(make_app)


async def test_free_text_selects_a_skill_and_reports_routing_plus_skill_usage(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = scripted_model(
        select("answer"),
        extract({"question": "What is the capital of France?"}),
        "Paris.",
    )

    async with (
        serve_example(serve, monkeypatch, model) as base_url,
        await connect(base_url) as client,
    ):
        task = await say(client, "What is the capital of France?")

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Paris."
    # Two routing calls (select, extract) plus the skill's own call.
    assert task.metadata["usage"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "estimated": False,
        "model_calls": 3,
        "tool_calls": 0,
    }
    skill_request = model.requests[-1]
    assert skill_request.messages[-1].content == "What is the capital of France?"


async def test_a_missing_argument_is_asked_for_and_answered_in_the_same_task(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = scripted_model(
        select("to_fahrenheit"),
        ask("Which temperature in Celsius?"),
        select("to_fahrenheit"),
        extract({"celsius": 25}),
    )

    async with (
        serve_example(serve, monkeypatch, model) as base_url,
        await connect(base_url) as client,
    ):
        asked = await say(client, "Convert a temperature to Fahrenheit.")
        answered = await say(client, "25 degrees", task_id=asked.id)

    assert asked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert get_message_text(asked.status.message) == "Which temperature in Celsius?"
    assert answered.id == asked.id
    assert answered.status.state == TaskState.TASK_STATE_COMPLETED
    assert temperature(answered) == Temperature(celsius=25, fahrenheit=77)
    # The answer is routed again with the whole conversation.
    assert [(m.role, m.content) for m in model.requests[-1].messages[1:]] == [
        ("user", "Convert a temperature to Fahrenheit."),
        ("assistant", "Which temperature in Celsius?"),
        ("user", "25 degrees"),
    ]


async def test_a_structured_skill_call_never_calls_the_router_model(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = scripted_model()

    async with (
        serve_example(serve, monkeypatch, model) as base_url,
        await connect(base_url) as client,
    ):
        task = await call_skill(client, "to_fahrenheit", {"celsius": 100})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert temperature(task) == Temperature(celsius=100, fahrenheit=212)
    assert task.metadata["usage"]["model_calls"] == 0
    assert model.requests == []


@agent(version="1.0.0")
class OneCallAssistant(RoutedAssistant):
    """The example agent, allowed fewer model calls than routing needs."""

    limits = RunLimits(max_input_tokens=4000, max_model_calls=1)


async def test_too_few_model_calls_for_routing_fail_before_the_skill_runs(
    serve: ServeLocally,
) -> None:
    model = scripted_model(select("answer"))

    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=OneCallAssistant(model=model),
            host=HostConfig(public_url=public_url, auth=AllowAnonymous()),
            router=ModelRouter(model),
        )

    async with serve(make_app) as base_url, await connect(base_url) as client:
        task = await say(client, "What is the capital of France?")

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert get_message_text(task.status.message) == (
        "The run reached its limit of 1 model calls"
    )
    # Selection used the only call; extraction and the skill never ran.
    assert len(model.requests) == 1
    assert task.metadata["usage"]["model_calls"] == 1


class CelsiusRouter(SkillRouter):
    """Routes any message with a number to ``to_fahrenheit``, without a model."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        text = " ".join(
            get_message_text(m) for m in conversation if m.role == Role.ROLE_USER
        )
        numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
        if not numbers:
            return Clarification("Which temperature in Celsius?")
        return SkillCall("to_fahrenheit", {"celsius": float(numbers[-1])})


async def test_a_custom_router_still_routes_on_another_app(
    serve: ServeLocally,
) -> None:
    model = scripted_model()

    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=RoutedAssistant(model=model),
            host=HostConfig(public_url=public_url, auth=AllowAnonymous()),
            router=CelsiusRouter(),
        )

    async with serve(make_app) as base_url, await connect(base_url) as client:
        asked = await say(client, "How warm is it in Fahrenheit?")
        answered = await say(client, "It is 30 degrees", task_id=asked.id)

    assert asked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert answered.status.state == TaskState.TASK_STATE_COMPLETED
    assert temperature(answered) == Temperature(celsius=30, fahrenheit=86)
    assert model.requests == []
