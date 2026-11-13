"""Bounded tool calling end to end, offline: the ``examples/orders.py`` agent.

The order desk is served over real localhost HTTP and called by the official
A2A client with a structured request and a development API key. A scripted
model that supports tools stands in for Ollama and Foundry;
``test_providers.py`` runs the real hosts as opt-in tests.
"""

import json
import logging

import httpx
import pytest
from a2a.helpers import get_artifact_text, get_message_text
from a2a.types import Task, TaskState

from a2a_client import call_skill, connect
from agentframe import (
    AgentApp,
    ModelCapabilities,
    ModelResponse,
    ProviderError,
    ToolCall,
)
from agentframe.runtime import generation
from agentframe.runtime.usage import FinishReason, Usage
from agentframe.testing import ScriptedModel
from examples.orders import Order, OrderStore, create_app
from local_server import ServeLocally

API_KEY = "test-orders-key"
TOOLS = ModelCapabilities(context_window=8192, tools=True)
USAGE = Usage(input_tokens=40, output_tokens=10, total_tokens=50, estimated=False)
# Text that must never reach a caller in a failure message.
SECRET = "db-password-1234"


def lookup(order_id: str = "A1", call_id: str = "call_1") -> ToolCall:
    return ToolCall(
        id=call_id, name="lookup_order", arguments=json.dumps({"order_id": order_id})
    )


def cancel(order_id: str = "A1") -> ToolCall:
    return ToolCall(
        id="call_1", name="cancel_order", arguments=json.dumps({"order_id": order_id})
    )


def answer(text: str) -> ModelResponse:
    return ModelResponse(text, USAGE, FinishReason.COMPLETED)


class FlakyStore(OrderStore):
    """An order store whose database fails, with a secret in the error."""

    def __init__(self, *, fail_owner: bool = False) -> None:
        super().__init__()
        self.fail_owner = fail_owner

    async def owner(self, order_id: str) -> str | None:
        if self.fail_owner:
            raise ConnectionError(f"Cannot connect with {SECRET}")
        return await super().owner(order_id)

    async def cancel(self, order_id: str) -> Order:
        raise ConnectionError(f"Cannot connect with {SECRET}")


class CountingStore(OrderStore):
    """An order store that records every cancellation."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []

    async def cancel(self, order_id: str) -> Order:
        self.cancelled.append(order_id)
        return await super().cancel(order_id)


async def ask_desk(
    serve: ServeLocally,
    monkeypatch: pytest.MonkeyPatch,
    model: ScriptedModel,
    store: OrderStore | None = None,
    request: str = "Where is order A1?",
) -> Task:
    """Serve the example with ``model`` and call ``order_help`` over A2A."""
    monkeypatch.setenv("ORDERS_API_KEY", API_KEY)

    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ORDERS_PUBLIC_URL", public_url)
        return create_app(model, store)

    async with serve(make_app) as base_url, await connect(base_url, API_KEY) as client:
        return await call_skill(client, "order_help", {"request": request})


async def test_a_tool_round_trip_completes_and_reports_tool_usage(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel(
        [
            ModelResponse("", USAGE, FinishReason.TOOL_CALLS, (lookup(),)),
            answer("Shipped."),
        ],
        capabilities=TOOLS,
    )

    task = await ask_desk(serve, monkeypatch, model)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Shipped."
    assert task.metadata["usage"] == {
        "input_tokens": 80,
        "output_tokens": 20,
        "total_tokens": 100,
        "estimated": False,
        "model_calls": 2,
        "tool_calls": 1,
    }
    first, second = model.requests
    # alice holds orders.cancel, so both tools are offered.
    assert [spec.name for spec in first.tools] == ["lookup_order", "cancel_order"]
    result = second.messages[-1]
    assert result.role == "tool"
    assert Order.model_validate_json(result.content) == Order(
        id="A1", customer="alice", status="shipped"
    )


async def test_registered_tools_never_appear_on_the_agent_card(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORDERS_API_KEY", API_KEY)

    def make_app(public_url: str) -> AgentApp:
        return create_app(ScriptedModel(capabilities=TOOLS))

    async with serve(make_app) as base_url, httpx.AsyncClient() as http:
        response = await http.get(f"{base_url}/.well-known/agent-card.json")

    card = response.json()
    assert [skill["id"] for skill in card["skills"]] == ["order_help"]
    assert "lookup_order" not in response.text
    assert "cancel_order" not in response.text


async def test_the_tool_call_limit_fails_the_task(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The example allows 4 tool calls; the model asks for a fifth.
    model = ScriptedModel(
        [lookup(call_id=f"call_{n}") for n in range(5)], capabilities=TOOLS
    )

    task = await ask_desk(serve, monkeypatch, model)

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert get_message_text(task.status.message) == (
        "The run reached its limit of 4 tool calls"
    )
    assert task.metadata["usage"]["tool_calls"] == 4
    assert task.metadata["usage"]["model_calls"] == 5


async def test_a_denied_tool_call_rejects_the_task_without_running_it(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CountingStore()
    # B2 belongs to bob, so the tool policy denies alice's cancellation.
    model = ScriptedModel([cancel("B2")], capabilities=TOOLS)

    task = await ask_desk(serve, monkeypatch, model, store, "Cancel order B2.")

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert get_message_text(task.status.message) == (
        "Tool 'cancel_order' was denied by the tool authorization policy"
    )
    assert store.cancelled == []
    assert task.metadata["usage"]["tool_calls"] == 1


async def test_a_tool_policy_outage_fails_the_task_safely(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel([lookup()], capabilities=TOOLS)

    task = await ask_desk(serve, monkeypatch, model, FlakyStore(fail_owner=True))

    assert task.status.state == TaskState.TASK_STATE_FAILED
    failure = get_message_text(task.status.message)
    assert failure == (
        "Tool authorization policy for tool 'lookup_order' is unavailable"
    )
    assert SECRET not in failure


async def test_a_failing_tool_fails_the_task_with_details_only_in_logs(
    serve: ServeLocally,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    model = ScriptedModel([cancel()], capabilities=TOOLS)

    task = await ask_desk(serve, monkeypatch, model, FlakyStore(), "Cancel A1.")

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert get_message_text(task.status.message) == "The skill failed."
    invocation_id = task.metadata["invocation_id"]
    assert f"Skill failed (invocation_id={invocation_id})" in caplog.text
    assert SECRET in caplog.text


async def test_a_model_without_tool_support_fails_before_sending(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedModel(capabilities=ModelCapabilities(context_window=8192))

    task = await ask_desk(serve, monkeypatch, model)

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert get_message_text(task.status.message) == (
        "The model does not support tool calling"
    )
    assert task.metadata["usage"]["model_calls"] == 0
    assert model.requests == []


async def test_a_model_retry_never_reruns_a_side_effecting_tool(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def skip(seconds: float) -> None:
        pass

    monkeypatch.setattr(generation, "wait_before_retry", skip)
    store = CountingStore()
    model = ScriptedModel(
        [
            cancel(),
            ProviderError("Server error", transient=True),
            "Order A1 is cancelled.",
        ],
        capabilities=TOOLS,
    )

    task = await ask_desk(serve, monkeypatch, model, store, "Cancel order A1.")

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert store.cancelled == ["A1"]
    assert task.metadata["usage"]["tool_calls"] == 1
    _, failed, retried = model.requests
    assert retried == failed
