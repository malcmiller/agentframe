"""Tests for the tool-calling loop in ``ctx.generate(..., tools=[...])``.

Models are scripted, so no test calls a provider. The hosted test travels over
real localhost HTTP to an ``AgentApp`` served by Uvicorn.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    DefinitionError,
    FinishReason,
    HostConfig,
    IncompleteOutputError,
    Inject,
    Message,
    ModelCallLimitExceededError,
    ModelCapabilities,
    ModelResponse,
    ModelSettings,
    OutputValidationError,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    Services,
    TimeoutLimitExceededError,
    TokenLimitExceededError,
    ToolCall,
    ToolCallLimitExceededError,
    ToolSpec,
    UnsupportedCapabilityError,
    Usage,
    agent,
    skill,
    tool,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

TOOL_MODEL = ModelCapabilities(
    context_window=128_000, structured_output=True, tools=True
)


class OrderStore:
    """Records every lookup, so tests can see which tools ran and in what order."""

    def __init__(self) -> None:
        self.looked_up: list[str] = []
        self.closed = False

    def status(self, order_id: str) -> str:
        self.looked_up.append(order_id)
        return "shipped"


@tool(description="Look up an order's status.")
async def lookup_order(order_id: str, orders: Inject[OrderStore]) -> str:
    return orders.status(order_id)


@tool(description="Count the characters in a text.")
def count_characters(text: str) -> int:
    return len(text)


@tool(description="Report which run is calling.")
def whoami(ctx: RunContext) -> str:
    return ctx.invocation_id


@tool(description="Wait for a slow backend.")
async def wait_for_backend(orders: Inject[OrderStore]) -> str:
    orders.looked_up.append("started")
    await asyncio.sleep(10)
    return "never"


@tool(description="Always fails.")
async def broken_tool() -> str:
    raise RuntimeError("SECRET-DATABASE-PASSWORD")


@tool(description="Returns the wrong type.")
def wrong_type() -> int:
    return "not a number"  # type: ignore[return-value]


class OrderAnswer(BaseModel):
    status: str


@agent(version="1.0.0")
class OrderAssistant(Agent):
    """Answers questions about orders."""

    instructions = "Answer questions about orders."

    @skill("ask", tags=["orders"])
    async def ask(self, question: str, ctx: RunContext) -> str:
        """Answer with the lookup tool."""
        return await ctx.generate(question, tools=[lookup_order])

    @skill("ask_once", tags=["orders"], limits=RunLimits(max_tool_calls=1))
    async def ask_once(self, question: str, ctx: RunContext) -> str:
        """Answer with at most one tool call."""
        return await ctx.generate(question, tools=[lookup_order])

    @skill("ask_typed", tags=["orders"])
    async def ask_typed(self, question: str, ctx: RunContext) -> OrderAnswer:
        """Answer as structured output after using the lookup tool."""
        return await ctx.generate(question, output=OrderAnswer, tools=[lookup_order])

    @skill("chat", tags=["orders"])
    async def chat(self, question: str, ctx: RunContext) -> str:
        """Answer without tools."""
        return await ctx.generate(question)

    @skill("count", tags=["orders"])
    async def count(self, text: str, ctx: RunContext) -> int:
        """Count characters without a model."""
        return len(text)

    @skill("use", tags=["orders"])
    async def use(self, tool_name: str, ctx: RunContext) -> str:
        """Answer with one registered tool, chosen by name."""
        return await ctx.generate("Go.", tools=[self.tools[tool_name].function])

    @skill("twice", tags=["orders"])
    async def twice(self, ctx: RunContext) -> str:
        """Select the same tool twice."""
        return await ctx.generate("Go.", tools=[lookup_order, lookup_order])

    @skill("store_used", tags=["orders"])
    async def store_used(self, orders: Inject[OrderStore], ctx: RunContext) -> bool:
        """Report whether a tool used the same store as this skill."""
        await ctx.generate("Go.", tools=[lookup_order])
        return orders.looked_up == ["A1"]


ALL_TOOLS: list[Callable[..., Any]] = [
    lookup_order,
    count_characters,
    whoami,
    wait_for_backend,
    broken_tool,
    wrong_type,
]


def call(name: str, arguments: str = "{}", call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def lookup(order_id: str, call_id: str = "call_1") -> ToolCall:
    return call("lookup_order", json.dumps({"order_id": order_id}), call_id)


def scripted(*replies: Any) -> ScriptedModel:
    return ScriptedModel(replies, capabilities=TOOL_MODEL)


def assistant(model: ScriptedModel) -> OrderAssistant:
    return OrderAssistant(model=model, tools=ALL_TOOLS)


def runtime_with(store: OrderStore) -> Runtime:
    services = Services()
    services.add(OrderStore, store)
    return Runtime(services=services)


def tool_messages(model: ScriptedModel, request: int) -> list[Message]:
    return [m for m in model.requests[request].messages if m.role == "tool"]


# The loop


async def test_tool_result_is_sent_back_and_the_final_answer_returned() -> None:
    model = scripted(lookup("A1"), "Order A1 has shipped.")

    result = await runtime_with(OrderStore()).run(
        assistant(model), skill="ask", arguments={"question": "Where is A1?"}
    )

    assert result.value == "Order A1 has shipped."
    assert (result.model_calls, result.tool_calls) == (2, 1)
    first, second = model.requests
    assert first.tools == (
        ToolSpec(
            name="lookup_order",
            description="Look up an order's status.",
            parameters=lookup_order_schema(),
        ),
    )
    assert second.tools == first.tools
    assert second.messages == (
        *first.messages,
        Message("assistant", "", tool_calls=(lookup("A1"),)),
        Message("tool", '"shipped"', tool_call_id="call_1", tool_name="lookup_order"),
    )


def lookup_order_schema() -> dict[str, Any]:
    return OrderAssistant(tools=[lookup_order]).tools["lookup_order"].json_schema()


async def test_several_calls_in_one_response_run_in_order() -> None:
    store = OrderStore()
    model = scripted((lookup("A1", "c1"), lookup("B2", "c2")), lookup("C3"), "Done.")

    result = await runtime_with(store).run(
        assistant(model), skill="ask", arguments={"question": "?"}
    )

    assert store.looked_up == ["A1", "B2", "C3"]
    assert result.tool_calls == 3
    assert [m.tool_call_id for m in tool_messages(model, 1)] == ["c1", "c2"]


async def test_provider_items_are_kept_for_replay() -> None:
    reasoning = {"type": "reasoning", "encrypted_content": "opaque"}
    asked = ModelResponse(
        text="",
        usage=Usage(input_tokens=5, output_tokens=5, total_tokens=10, estimated=False),
        finish_reason=FinishReason.TOOL_CALLS,
        tool_calls=(lookup("A1"),),
        provider_items=(reasoning,),
    )
    model = scripted(asked, "Done.")

    await runtime_with(OrderStore()).invoke(
        assistant(model), skill="ask", arguments={"question": "?"}
    )

    replayed = model.requests[1].messages[-2]
    assert replayed.role == "assistant"
    assert replayed.provider_items == (reasoning,)


async def test_tools_with_structured_output_validate_the_final_answer() -> None:
    model = scripted(lookup("A1"), '{"status": "shipped"}')

    answer = await runtime_with(OrderStore()).invoke(
        assistant(model), skill="ask_typed", arguments={"question": "?"}
    )

    assert answer == OrderAnswer(status="shipped")
    assert model.requests[1].output is not None


async def test_sync_tools_run_and_ctx_is_passed_only_when_taken() -> None:
    model = scripted(call("count_characters", '{"text": "abcd"}'), "Four.")
    await runtime_with(OrderStore()).invoke(
        assistant(model), skill="use", arguments={"tool_name": "count_characters"}
    )
    assert tool_messages(model, 1)[0].content == "4"

    model = scripted(call("whoami"), "Done.")
    result = await runtime_with(OrderStore()).run(
        assistant(model), skill="use", arguments={"tool_name": "whoami"}
    )
    assert tool_messages(model, 1)[0].content == json.dumps(result.invocation_id)


async def test_tools_share_the_runs_scoped_services() -> None:
    created: list[OrderStore] = []

    async def open_store() -> AsyncIterator[OrderStore]:
        store = OrderStore()
        created.append(store)
        yield store
        store.closed = True

    services = Services()
    services.add_scoped(OrderStore, open_store)
    model = scripted(lookup("A1"), "Done.")

    shared = await Runtime(services=services).invoke(
        assistant(model), skill="store_used"
    )

    assert shared
    assert len(created) == 1
    assert created[0].closed


async def test_selecting_a_tool_twice_fails_before_sending() -> None:
    model = scripted()

    with pytest.raises(DefinitionError, match="'lookup_order' is selected more"):
        await runtime_with(OrderStore()).invoke(assistant(model), skill="twice")
    assert model.requests == []


async def test_a_model_without_tool_support_fails_before_sending() -> None:
    model = ScriptedModel([])  # Default capabilities: no tools.

    with pytest.raises(UnsupportedCapabilityError, match="tool calling"):
        await runtime_with(OrderStore()).invoke(
            assistant(model), skill="ask", arguments={"question": "?"}
        )
    assert model.requests == []


async def test_tool_free_and_model_free_runs_report_zero_tool_calls() -> None:
    runtime = runtime_with(OrderStore())
    tool_free = await runtime.run(
        assistant(scripted("Hi.")), skill="chat", arguments={"question": "?"}
    )
    model_free = await runtime.run(
        assistant(scripted()), skill="count", arguments={"text": "abc"}
    )

    assert (tool_free.tool_calls, model_free.tool_calls) == (0, 0)


def truncated(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        usage=Usage(input_tokens=5, output_tokens=5, total_tokens=10, estimated=False),
        finish_reason=FinishReason.LENGTH,
    )


async def test_a_truncated_text_answer_after_tool_calls_keeps_its_partial_text() -> (
    None
):
    model = scripted(lookup("A1"), truncated("Order A1 has"))

    with pytest.raises(IncompleteOutputError) as raised:
        await runtime_with(OrderStore()).invoke(
            assistant(model), skill="ask", arguments={"question": "?"}
        )

    assert raised.value.partial_text == "Order A1 has"


async def test_a_truncated_structured_answer_after_tool_calls_has_no_partial_text() -> (
    None
):
    model = scripted(lookup("A1"), truncated('{"status": "sh'))

    with pytest.raises(IncompleteOutputError) as raised:
        await runtime_with(OrderStore()).invoke(
            assistant(model), skill="ask_typed", arguments={"question": "?"}
        )

    assert raised.value.partial_text is None


# Calls that do not run


async def test_invalid_arguments_run_nothing_and_tell_the_model() -> None:
    store = OrderStore()
    model = scripted(call("lookup_order", '{"order_id": 7, "SECRET": 1}'), "Sorry.")

    result = await runtime_with(store).run(
        assistant(model), skill="ask", arguments={"question": "?"}
    )

    assert store.looked_up == []
    assert result.tool_calls == 1
    error = json.loads(tool_messages(model, 1)[0].content)["error"]
    assert error.startswith("Invalid arguments: order_id: ")
    assert "7" not in error


async def test_malformed_json_arguments_run_nothing() -> None:
    store = OrderStore()
    model = scripted(call("lookup_order", '{"order_id": '), "Sorry.")

    await runtime_with(store).invoke(
        assistant(model), skill="ask", arguments={"question": "?"}
    )

    assert store.looked_up == []
    assert "Invalid arguments" in tool_messages(model, 1)[0].content


@pytest.mark.parametrize("skill_id", ["ask", "chat"])
async def test_unknown_or_unoffered_tools_run_nothing(skill_id: str) -> None:
    # "chat" offers no tools at all; "ask" offers only lookup_order.
    model = scripted(call("count_characters", '{"text": "a"}'), "Sorry.")

    result = await runtime_with(OrderStore()).run(
        assistant(model), skill=skill_id, arguments={"question": "?"}
    )

    assert result.tool_calls == 1
    content = json.loads(tool_messages(model, 1)[0].content)
    assert content == {"error": "Unknown tool. Call only the tools you were offered."}


async def test_a_tool_exception_fails_the_run_and_is_not_sent_to_the_model() -> None:
    model = scripted(call("broken_tool"), "Unused.")

    with pytest.raises(RuntimeError, match="SECRET-DATABASE-PASSWORD"):
        await runtime_with(OrderStore()).invoke(
            assistant(model), skill="use", arguments={"tool_name": "broken_tool"}
        )
    assert len(model.requests) == 1


async def test_an_invalid_tool_return_value_fails_the_run() -> None:
    model = scripted(call("wrong_type"), "Unused.")

    with pytest.raises(OutputValidationError, match="Tool 'wrong_type' returned"):
        await runtime_with(OrderStore()).invoke(
            assistant(model), skill="use", arguments={"tool_name": "wrong_type"}
        )
    assert len(model.requests) == 1


# Limits


async def test_the_call_over_max_tool_calls_does_not_run() -> None:
    store = OrderStore()
    model = scripted((lookup("A1", "c1"), lookup("B2", "c2")), "Unused.")

    with pytest.raises(ToolCallLimitExceededError, match="1 tool calls") as raised:
        await runtime_with(store).invoke(
            assistant(model), skill="ask_once", arguments={"question": "?"}
        )

    assert store.looked_up == ["A1"]
    assert raised.value.invocation_id


async def test_run_options_override_the_skills_max_tool_calls() -> None:
    store = OrderStore()
    model = scripted((lookup("A1", "c1"), lookup("B2", "c2")), "Done.")

    result = await runtime_with(store).run(
        assistant(model),
        skill="ask_once",
        arguments={"question": "?"},
        options=RunOptions(limits=RunLimits(max_tool_calls=2)),
    )

    assert result.tool_calls == 2


async def test_invalid_and_unknown_calls_count_toward_max_tool_calls() -> None:
    model = scripted((call("nope", call_id="c1"), lookup("A1", "c2")), "Unused.")

    with pytest.raises(ToolCallLimitExceededError):
        await runtime_with(OrderStore()).invoke(
            assistant(model), skill="ask_once", arguments={"question": "?"}
        )


async def test_continuations_count_toward_max_model_calls() -> None:
    store = OrderStore()
    model = scripted(lookup("A1"), "Unused.")

    with pytest.raises(ModelCallLimitExceededError):
        await runtime_with(store).invoke(
            assistant(model),
            skill="ask",
            arguments={"question": "?"},
            options=RunOptions(limits=RunLimits(max_model_calls=1)),
        )

    assert store.looked_up == ["A1"]
    assert len(model.requests) == 1


async def test_unreported_usage_estimates_tool_call_arguments_as_output() -> None:
    long_id = "A" * 4000
    model = scripted(lookup(long_id), "Done.")

    result = await runtime_with(OrderStore()).run(
        assistant(model), skill="ask", arguments={"question": "?"}
    )

    assert result.usage.estimated
    assert result.usage.output_tokens is not None
    assert result.usage.output_tokens > 4000 // 4


async def test_continuations_count_toward_the_token_budget() -> None:
    used = Usage(input_tokens=100, output_tokens=50, total_tokens=150, estimated=False)
    asked = ModelResponse(
        text="",
        usage=used,
        finish_reason=FinishReason.TOOL_CALLS,
        tool_calls=(lookup("A1"),),
    )
    model = scripted(asked, "Unused.")

    # Each call reserves its input estimate plus 100 output tokens; after the
    # first call's 150 tokens, the continuation no longer fits in 300.
    with pytest.raises(TokenLimitExceededError):
        await runtime_with(OrderStore()).invoke(
            assistant(model),
            skill="ask",
            arguments={"question": "?"},
            options=RunOptions(
                limits=RunLimits(max_total_tokens=300),
                model_settings=ModelSettings(max_output_tokens=100),
            ),
        )
    assert len(model.requests) == 1


async def test_concurrent_resolves_create_a_scoped_service_once() -> None:
    created: list[OrderStore] = []

    async def open_store() -> AsyncIterator[OrderStore]:
        await asyncio.sleep(0.01)  # Lets the other call try to create it too.
        created.append(OrderStore())
        yield created[-1]

    services = Services()
    services.add_scoped(OrderStore, open_store)
    scope = services.scope(AsyncExitStack())

    first, second = await asyncio.gather(
        scope.resolve({"orders": OrderStore}), scope.resolve({"orders": OrderStore})
    )

    assert len(created) == 1
    assert first["orders"] is second["orders"]


# Deadlines and cancellation


async def test_the_deadline_cancels_a_running_tool() -> None:
    store, cleaned_up = OrderStore(), []

    async def open_store() -> AsyncIterator[OrderStore]:
        yield store
        cleaned_up.append(True)

    services = Services()
    services.add_scoped(OrderStore, open_store)
    model = scripted(call("wait_for_backend"), "Unused.")

    with pytest.raises(TimeoutLimitExceededError, match="timeout"):
        await Runtime(services=services).invoke(
            assistant(model),
            skill="use",
            arguments={"tool_name": "wait_for_backend"},
            options=RunOptions(limits=RunLimits(timeout_seconds=0.2)),
        )

    assert store.looked_up == ["started"]
    assert cleaned_up == [True]


async def test_cancelling_the_run_cancels_a_running_tool() -> None:
    store, cleaned_up = OrderStore(), []

    async def open_store() -> AsyncIterator[OrderStore]:
        yield store
        cleaned_up.append(True)

    services = Services()
    services.add_scoped(OrderStore, open_store)
    model = scripted(call("wait_for_backend"), "Unused.")
    run = asyncio.create_task(
        Runtime(services=services).invoke(
            assistant(model), skill="use", arguments={"tool_name": "wait_for_backend"}
        )
    )
    while store.looked_up != ["started"]:
        await asyncio.sleep(0.01)

    run.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run
    assert cleaned_up == [True]


# Hosted over A2A


async def test_hosted_tasks_report_tool_calls(serve: ServeLocally) -> None:
    model = scripted(lookup("A1"), "Order A1 has shipped.")
    services = Services()
    services.add(OrderStore, OrderStore())

    def make_app(public_url: str) -> AgentApp:
        host = HostConfig(public_url=public_url, auth=AllowAnonymous())
        return AgentApp(agent=assistant(model), host=host, services=services)

    message = {
        "messageId": "m-1",
        "role": "ROLE_USER",
        "parts": [{"data": {"question": "Where is A1?"}}],
        "metadata": {"agentframe.skill": "ask"},
    }
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    headers = {"A2A-Version": "1.0", "A2A-Extensions": SKILL_CALL_EXTENSION_URI}
    async with (
        serve(make_app) as base_url,
        httpx.AsyncClient(base_url=base_url) as http,
    ):
        reply = await http.post("/a2a", json=body, headers=headers)

    task = reply.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["metadata"]["usage"]["model_calls"] == 2
    assert task["metadata"]["usage"]["tool_calls"] == 1
