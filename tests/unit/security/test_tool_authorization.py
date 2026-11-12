"""Tests for tool authorization: ``@tool(requires_permissions=...)`` and
``Runtime(tool_authorization=...)``.

Models are scripted, so no test calls a provider. The hosted tests travel over
real localhost HTTP to an ``AgentApp`` served by Uvicorn.
"""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    HostConfig,
    Inject,
    ModelCapabilities,
    PermissionDeniedError,
    PolicyUnavailableError,
    Principal,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    Services,
    ToolAuthorizationPolicy,
    ToolCall,
    ToolDefinition,
    agent,
    skill,
    tool,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

TOOL_MODEL = ModelCapabilities(context_window=128_000, tools=True)

ALICE = Principal(id="alice", permissions=frozenset({"orders:read", "orders:write"}))
READER = Principal(id="reader", permissions=frozenset({"orders:read"}))

ORDER_OWNERS = {"A1": "alice", "B2": "bob"}


class OrderStore:
    """Records the orders each tool acted on, so tests can see what ran."""

    def __init__(self) -> None:
        self.looked_up: list[str] = []
        self.cancelled: list[str] = []
        self.closed = False


@tool(description="Look up an order's status.", requires_permissions=["orders:read"])
async def lookup_order(order_id: str, orders: Inject[OrderStore]) -> str:
    orders.looked_up.append(order_id)
    return "shipped"


@tool(
    description="Cancel an order.",
    side_effects=True,
    requires_permissions=["orders:write"],
)
async def cancel_order(
    order_id: str, orders: Inject[OrderStore], ctx: RunContext
) -> None:
    orders.cancelled.append(order_id)


@agent(version="1.0.0")
class OrderAssistant(Agent):
    """Answers questions about orders and cancels them."""

    @skill("ask", tags=["orders"])
    async def ask(self, question: str, ctx: RunContext) -> str:
        """Answer with the order tools."""
        return await ctx.generate(question, tools=[lookup_order, cancel_order])


class OwnerOnly(ToolAuthorizationPolicy):
    """Lets callers act only on orders they own."""

    def __init__(self) -> None:
        self.calls: list[tuple[Principal | None, str, Mapping[str, object]]] = []

    async def check_tool(
        self,
        principal: Principal | None,
        tool: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        self.calls.append((principal, tool.name, arguments))
        if principal is None:
            return False
        return ORDER_OWNERS.get(str(arguments["order_id"])) == principal.id


class Answers:
    """A policy that always returns ``answer``, whatever it is."""

    def __init__(self, answer: object) -> None:
        self.answer = answer

    async def check_tool(
        self,
        principal: Principal | None,
        tool: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> Any:
        return self.answer


class Broken:
    """A policy whose backend is down."""

    async def check_tool(
        self,
        principal: Principal | None,
        tool: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        raise ConnectionError("policy-db.internal refused the connection")


class Hangs:
    """A policy that never answers."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def check_tool(
        self,
        principal: Principal | None,
        tool: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        self.started.set()
        await asyncio.sleep(10)
        return True


def call(name: str, order_id: object, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=json.dumps({"order_id": order_id}))


def scripted(*replies: Any) -> ScriptedModel:
    return ScriptedModel(replies, capabilities=TOOL_MODEL)


def runtime_with(
    store: OrderStore, policy: ToolAuthorizationPolicy | None = None
) -> Runtime:
    services = Services()
    services.add(OrderStore, store)
    return Runtime(services=services, tool_authorization=policy)


async def ask(
    runtime: Runtime, model: ScriptedModel, principal: Principal | None
) -> Any:
    return await runtime.invoke(
        OrderAssistant(model=model, tools=[lookup_order, cancel_order]),
        skill="ask",
        arguments={"question": "?"},
        principal=principal,
    )


def offered(model: ScriptedModel) -> list[str]:
    return [spec.name for spec in model.requests[0].tools]


# requires_permissions


async def test_only_tools_the_caller_is_permitted_to_use_are_offered() -> None:
    for principal, expected in [
        (ALICE, ["lookup_order", "cancel_order"]),
        (READER, ["lookup_order"]),
        (None, []),
    ]:
        model = scripted("Hello.")
        await ask(runtime_with(OrderStore()), model, principal)
        assert offered(model) == expected


@pytest.mark.parametrize("arguments", ['{"order_id": "A1"}', '{"bad": 1}'])
async def test_an_unpermitted_tool_fails_the_run_even_if_the_model_names_it(
    arguments: str,
) -> None:
    store = OrderStore()
    model = scripted(ToolCall(id="c1", name="cancel_order", arguments=arguments))

    with pytest.raises(PermissionDeniedError, match="'cancel_order' requires"):
        await ask(runtime_with(store), model, READER)

    assert store.cancelled == []
    assert len(model.requests) == 1  # The denial is never sent to the model.


# ToolAuthorizationPolicy


async def test_the_policy_sees_the_principal_tool_and_read_only_arguments() -> None:
    policy, store = OwnerOnly(), OrderStore()
    model = scripted(call("cancel_order", "A1"), "Cancelled.")

    assert await ask(runtime_with(store, policy), model, ALICE) == "Cancelled."

    assert store.cancelled == ["A1"]
    [(principal, tool_name, arguments)] = policy.calls
    assert (principal, tool_name) == (ALICE, "cancel_order")
    assert dict(arguments) == {"order_id": "A1"}  # Never ctx or services.
    with pytest.raises(TypeError):
        arguments["order_id"] = "B2"  # type: ignore[index]


async def test_a_denied_call_fails_the_run_without_revealing_the_resource() -> None:
    store = OrderStore()
    model = scripted(call("cancel_order", "B2"), "Unused.")

    with pytest.raises(PermissionDeniedError) as raised:
        await ask(runtime_with(store, OwnerOnly()), model, ALICE)

    assert raised.value.message == (
        "Tool 'cancel_order' was denied by the tool authorization policy"
    )
    assert raised.value.invocation_id
    assert store.cancelled == []
    assert len(model.requests) == 1


@pytest.mark.parametrize("answer", [False, None, 1, "yes", object()])
async def test_only_an_explicit_true_allows(answer: object) -> None:
    store = OrderStore()
    model = scripted(call("lookup_order", "A1"), "Unused.")

    with pytest.raises(PermissionDeniedError):
        await ask(runtime_with(store, Answers(answer)), model, ALICE)

    assert store.looked_up == []


async def test_invalid_arguments_are_rejected_before_the_policy() -> None:
    policy = OwnerOnly()
    model = scripted(call("lookup_order", ["not", "a", "string"]), "Sorry.")

    assert await ask(runtime_with(OrderStore(), policy), model, ALICE) == "Sorry."

    assert policy.calls == []


async def test_a_policy_error_fails_closed() -> None:
    store = OrderStore()
    model = scripted(call("lookup_order", "A1"), "Unused.")

    with pytest.raises(PolicyUnavailableError) as raised:
        await ask(runtime_with(store, Broken()), model, ALICE)

    assert "policy-db" not in raised.value.message
    assert isinstance(raised.value.__cause__, ConnectionError)
    assert store.looked_up == []


async def test_a_policy_that_misses_the_deadline_fails_closed() -> None:
    store = OrderStore()
    model = scripted(call("lookup_order", "A1"), "Unused.")

    with pytest.raises(PolicyUnavailableError, match="did not answer"):
        await runtime_with(store, Hangs()).invoke(
            OrderAssistant(model=model, tools=[lookup_order, cancel_order]),
            skill="ask",
            arguments={"question": "?"},
            principal=ALICE,
            options=RunOptions(limits=RunLimits(timeout_seconds=0.2)),
        )

    assert store.looked_up == []


async def test_cancelling_the_run_while_the_policy_decides_cancels_it() -> None:
    policy = Hangs()
    model = scripted(call("lookup_order", "A1"), "Unused.")
    run = asyncio.create_task(ask(runtime_with(OrderStore(), policy), model, ALICE))
    await policy.started.wait()

    run.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run


# Hosted over A2A


@pytest.mark.parametrize(
    ("policy", "state"),
    [
        (Answers(False), "TASK_STATE_REJECTED"),
        (Broken(), "TASK_STATE_FAILED"),
    ],
)
async def test_hosted_denial_and_outage_map_to_task_states(
    serve: ServeLocally, policy: ToolAuthorizationPolicy, state: str
) -> None:
    store = OrderStore()
    services = Services()
    services.add(OrderStore, store)

    @tool(description="Look up an order's status.")
    async def public_lookup(order_id: str, orders: Inject[OrderStore]) -> str:
        orders.looked_up.append(order_id)
        return "shipped"

    @agent(version="1.0.0")
    class PublicOrders(Agent):
        """Answers questions about orders for anyone."""

        @skill("ask", tags=["orders"])
        async def ask(self, question: str, ctx: RunContext) -> str:
            """Answer with the lookup tool."""
            return await ctx.generate(question, tools=[public_lookup])

    model = scripted(call("public_lookup", "A1"), "Unused.")

    def make_app(public_url: str) -> AgentApp:
        host = HostConfig(public_url=public_url, auth=AllowAnonymous())
        runtime = Runtime(services=services, tool_authorization=policy)
        return AgentApp(
            agent=PublicOrders(model=model, tools=[public_lookup]),
            host=host,
            runtime=runtime,
        )

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
    assert task["status"]["state"] == state
    assert "policy-db" not in json.dumps(task)
    assert store.looked_up == []
