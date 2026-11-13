"""Tests that model retries never replay tool executions, and tools never retry.

A model retry re-sends the same request, whose history already holds the
results of the tools that ran, so no tool runs again. No tool is retried
automatically, whatever its ``side_effects`` and ``idempotent`` flags. Models
are scripted, and ``wait_before_retry`` is replaced, so nothing waits.
"""

import json

import pytest

from agentframe import (
    Agent,
    Inject,
    Message,
    ModelCallLimitExceededError,
    ModelCapabilities,
    ProviderError,
    RunContext,
    RunLimits,
    RunOptions,
    RunResult,
    Runtime,
    Services,
    ToolCall,
    agent,
    skill,
    tool,
)
from agentframe.runtime import generation
from agentframe.testing import ScriptedModel

TOOL_MODEL = ModelCapabilities(
    context_window=128_000, structured_output=True, tools=True
)


class OrderBackend:
    """Records every cancellation, so tests can count how often a tool ran."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel(self, order_id: str) -> None:
        self.cancelled.append(order_id)


@tool(description="Cancel an order.", side_effects=True)
async def cancel_order(order_id: str, orders: Inject[OrderBackend]) -> str:
    orders.cancel(order_id)
    return "cancelled"


@tool(description="Cancel an order; fails.", side_effects=True)
async def failing_cancel(order_id: str, orders: Inject[OrderBackend]) -> str:
    orders.cancel(order_id)
    raise ConnectionError("Backend unavailable")


@tool(description="Cancel an order; fails.", idempotent=True)
async def failing_idempotent_cancel(order_id: str, orders: Inject[OrderBackend]) -> str:
    orders.cancel(order_id)
    raise ConnectionError("Backend unavailable")


@tool(description="Cancel an order; fails.", side_effects=True, idempotent=True)
async def failing_idempotent_side_effect_cancel(
    order_id: str, orders: Inject[OrderBackend]
) -> str:
    orders.cancel(order_id)
    raise ConnectionError("Backend unavailable")


ALL_TOOLS = [
    cancel_order,
    failing_cancel,
    failing_idempotent_cancel,
    failing_idempotent_side_effect_cancel,
]


@agent(version="1.0.0")
class OrderDesk(Agent):
    """Handles order requests."""

    @skill("handle", tags=["orders"])
    async def handle(self, tool_name: str, ctx: RunContext) -> str:
        """Handle a request with one registered tool, chosen by name."""
        return await ctx.generate("Cancel A1.", tools=[self.tools[tool_name].function])


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def skip(seconds: float) -> None:
        pass

    monkeypatch.setattr(generation, "wait_before_retry", skip)


def transient() -> ProviderError:
    return ProviderError("Server error", transient=True)


def cancel(tool_name: str = "cancel_order", call_id: str = "call_1") -> ToolCall:
    return ToolCall(
        id=call_id, name=tool_name, arguments=json.dumps({"order_id": "A1"})
    )


async def run_desk(
    model: ScriptedModel,
    backend: OrderBackend,
    tool_name: str = "cancel_order",
    limits: RunLimits | None = None,
) -> RunResult[str]:
    services = Services()
    services.add(OrderBackend, backend)
    return await Runtime(services=services).run(
        OrderDesk(model=model, tools=ALL_TOOLS),
        skill="handle",
        arguments={"tool_name": tool_name},
        options=RunOptions(limits=limits),
    )


async def test_a_retried_continuation_does_not_rerun_the_tool() -> None:
    backend = OrderBackend()
    model = ScriptedModel(
        [cancel(), transient(), "Order A1 is cancelled."], capabilities=TOOL_MODEL
    )

    await run_desk(model, backend)

    assert backend.cancelled == ["A1"]
    first, failed, retried = model.requests
    assert retried == failed
    assert retried.messages[len(first.messages) :] == (
        Message("assistant", "", tool_calls=(cancel(),)),
        Message("tool", '"cancelled"', tool_call_id="call_1", tool_name="cancel_order"),
    )


async def test_a_retry_before_any_tool_ran_is_safe() -> None:
    backend = OrderBackend()
    model = ScriptedModel([transient(), cancel(), "Done."], capabilities=TOOL_MODEL)

    await run_desk(model, backend)

    assert backend.cancelled == ["A1"]
    assert len(model.requests) == 3


@pytest.mark.parametrize(
    "tool_name",
    [
        "failing_cancel",
        "failing_idempotent_cancel",
        "failing_idempotent_side_effect_cancel",
    ],
)
async def test_a_tool_that_raises_is_not_run_again(tool_name: str) -> None:
    backend = OrderBackend()
    model = ScriptedModel(
        [cancel(tool_name), cancel(tool_name), "Unused."], capabilities=TOOL_MODEL
    )

    with pytest.raises(ConnectionError):
        await run_desk(model, backend, tool_name)

    assert backend.cancelled == ["A1"]
    assert len(model.requests) == 1


async def test_running_out_of_model_calls_while_retrying_does_not_rerun_the_tool() -> (
    None
):
    backend = OrderBackend()
    model = ScriptedModel([cancel(), transient(), "Unused."], capabilities=TOOL_MODEL)

    with pytest.raises(ModelCallLimitExceededError):
        await run_desk(model, backend, limits=RunLimits(max_model_calls=2))

    assert backend.cancelled == ["A1"]
    assert len(model.requests) == 2


async def test_a_repeated_call_in_a_later_turn_is_a_new_counted_call() -> None:
    backend = OrderBackend()
    model = ScriptedModel(
        [cancel(call_id="c1"), cancel(call_id="c2"), "Done."],
        capabilities=TOOL_MODEL,
    )

    result = await run_desk(model, backend)

    assert backend.cancelled == ["A1", "A1"]
    assert result.tool_calls == 2
