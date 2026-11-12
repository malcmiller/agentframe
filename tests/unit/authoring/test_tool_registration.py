"""Registering tools on an agent and selecting them per ``ctx.generate`` call."""

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    DefinitionError,
    HostConfig,
    Inject,
    RunContext,
    Runtime,
    Services,
    agent,
    skill,
    tool,
)
from agentframe.testing import ScriptedModel


class OrderStore:
    async def status(self, order_id: str) -> str:
        return "shipped"


@tool(description="Look up an order's status.", idempotent=True)
async def lookup_order(order_id: str, orders: Inject[OrderStore]) -> str:
    return await orders.status(order_id)


@tool(description="Cancel an order.", side_effects=True)
async def cancel_order(order_id: str) -> str:
    return f"{order_id} cancelled"


class OrderAnswer(BaseModel):
    status: str


@agent(version="0.1.0")
class OrderAssistant(Agent):
    """Answers questions about orders."""

    @skill("ask", tags=["orders"])
    async def ask(self, question: str, ctx: RunContext) -> str:
        """Answer with the lookup tool."""
        return await ctx.generate(question, tools=[lookup_order])

    @skill("cancel", tags=["orders"])
    async def cancel(self, question: str, ctx: RunContext) -> str:
        """Answer with the cancel tool."""
        return await ctx.generate(question, tools=[cancel_order])

    @skill("status", tags=["orders"])
    async def status(self, question: str, ctx: RunContext) -> OrderAnswer:
        """Answer as structured output with the cancel tool."""
        return await ctx.generate(question, output=OrderAnswer, tools=[cancel_order])

    @skill("chat", tags=["orders"])
    async def chat(self, question: str, ctx: RunContext) -> str:
        """Answer without tools."""
        return await ctx.generate(question, tools=[])


def order_services() -> Services:
    services = Services()
    services.add(OrderStore, OrderStore())
    return services


def test_registered_tools_are_available_by_name() -> None:
    assistant = OrderAssistant(tools=[lookup_order, cancel_order])

    assert list(assistant.tools) == ["lookup_order", "cancel_order"]
    assert assistant.tools["cancel_order"].side_effects
    with pytest.raises(TypeError):
        assistant.tools["other"] = assistant.tools["cancel_order"]  # type: ignore[index]


def test_agents_without_tools_have_none() -> None:
    @agent(version="0.1.0")
    class CustomInit(Agent):
        """Sets up its own state without calling Agent.__init__."""

        def __init__(self) -> None:
            self.greeting = "Hello"

        @skill("greet", tags=["greeting"])
        async def greet(self, ctx: RunContext) -> str:
            """Greet."""
            return self.greeting

    assert dict(OrderAssistant().tools) == {}
    assert dict(CustomInit().tools) == {}


def test_rejects_undecorated_functions() -> None:
    async def undecorated(order_id: str) -> str:
        return order_id

    with pytest.raises(DefinitionError, match="not decorated with @tool"):
        OrderAssistant(tools=[undecorated])


def test_rejects_two_tools_with_the_same_name() -> None:
    def make_tool() -> Callable[..., Any]:
        @tool(description="Another lookup.")
        def lookup_order(order_id: str) -> str:
            return order_id

        return lookup_order

    with pytest.raises(DefinitionError, match="'lookup_order' is registered more"):
        OrderAssistant(tools=[lookup_order, make_tool()])


async def test_selecting_an_unregistered_tool_fails_before_sending() -> None:
    model = ScriptedModel([])
    assistant = OrderAssistant(model=model, tools=[lookup_order])
    runtime = Runtime(services=order_services())

    for skill_id in ("cancel", "status"):
        with pytest.raises(DefinitionError, match="'cancel_order' is not registered"):
            await runtime.invoke(assistant, skill=skill_id, arguments={"question": "?"})
    assert model.requests == []


async def test_an_empty_selection_offers_no_tools() -> None:
    model = ScriptedModel(["No tools needed."])
    assistant = OrderAssistant(model=model, tools=[lookup_order])

    answer = await Runtime(services=order_services()).invoke(
        assistant, skill="chat", arguments={"question": "Hi"}
    )

    assert answer == "No tools needed."
    assert model.requests[0].tools == ()


def test_validate_requires_services_of_registered_tools() -> None:
    assistant = OrderAssistant(tools=[lookup_order])

    with pytest.raises(DefinitionError, match="Tool 'lookup_order': no service"):
        Runtime().validate(assistant)
    Runtime(services=order_services()).validate(assistant)


async def test_local_runs_require_services_of_registered_tools() -> None:
    model = ScriptedModel([])
    assistant = OrderAssistant(model=model, tools=[lookup_order])

    with pytest.raises(DefinitionError, match="Tool 'lookup_order': no service"):
        await Runtime().invoke(assistant, skill="chat", arguments={"question": "?"})
    assert model.requests == []


def test_app_startup_requires_services_of_registered_tools() -> None:
    host = HostConfig(public_url="http://localhost:8000/", auth=AllowAnonymous())

    with pytest.raises(DefinitionError, match="Tool 'lookup_order': no service"):
        AgentApp(agent=OrderAssistant(tools=[lookup_order]), host=host)
