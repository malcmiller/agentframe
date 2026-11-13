"""An order desk agent whose model looks up and cancels orders with tools.

``lookup_order`` only reads; ``cancel_order`` changes an order, so it declares
``side_effects=True`` and needs the ``orders.cancel`` permission. Every tool
call is also checked by ``OwnOrdersOnly``, so callers can only touch their own
orders, whatever the model (or a prompt injection) asks for. Tools are never
advertised on the Agent Card: callers see only the ``order_help`` skill.

The model comes from the same environment variables as ``examples.assistant``,
and one development API key, read from ``ORDERS_API_KEY``, signs in as the
customer ``alice``::

    ORDERS_API_KEY=<any-dev-key> AGENTFRAME_MODEL_PROVIDER=ollama \\
        AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \\
        agentframe run examples.orders:create_app
"""

import os
from collections.abc import Mapping

from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    APIKey,
    APIKeyAuth,
    HostConfig,
    Inject,
    Model,
    ModelSettings,
    Principal,
    RunContext,
    RunLimits,
    Runtime,
    Services,
    ToolDefinition,
    agent,
    skill,
    tool,
)
from examples.assistant import create_model


class Order(BaseModel):
    """One customer's order."""

    id: str
    customer: str
    status: str


class OrderStore:
    """In-memory orders: a stand-in for the application's database."""

    def __init__(self) -> None:
        self.orders = {
            "A1": Order(id="A1", customer="alice", status="shipped"),
            "B2": Order(id="B2", customer="bob", status="processing"),
        }

    async def owner(self, order_id: str) -> str | None:
        """Return the customer who owns an order, or ``None`` if there is none."""
        order = self.orders.get(order_id)
        return order.customer if order else None

    async def get(self, order_id: str) -> Order:
        """Return an order. Raises ``KeyError`` for an unknown ID."""
        return self.orders[order_id]

    async def cancel(self, order_id: str) -> Order:
        """Cancel an order and return it. Raises ``KeyError`` for an unknown ID."""
        order = self.orders[order_id].model_copy(update={"status": "cancelled"})
        self.orders[order_id] = order
        return order


@tool(description="Look up an order's status by its ID.", idempotent=True)
async def lookup_order(order_id: str, orders: Inject[OrderStore]) -> Order:
    return await orders.get(order_id)


@tool(
    description="Cancel an order by its ID.",
    side_effects=True,
    requires_permissions=["orders.cancel"],
)
async def cancel_order(order_id: str, orders: Inject[OrderStore]) -> Order:
    return await orders.cancel(order_id)


class OwnOrdersOnly:
    """Development placeholder tool policy: callers touch only their own orders.

    Runs before every tool call, so an unknown order, or another customer's,
    stops the run before the tool runs. Applications supply their own policy.
    """

    def __init__(self, store: OrderStore) -> None:
        self.store = store

    async def check_tool(
        self,
        principal: Principal | None,
        tool: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        if principal is None:
            return False
        return await self.store.owner(str(arguments["order_id"])) == principal.id


@agent(version="1.0.0")
class OrderDesk(Agent):
    """Answers questions about orders and cancels them on request."""

    instructions = "Help with orders using the tools. Answer in one short sentence."
    model_settings = ModelSettings(max_output_tokens=512)
    # Foundry does not report a deployment's context window, so cap the input.
    limits = RunLimits(max_input_tokens=4000, max_tool_calls=4)

    @skill("order_help", tags=["orders"])
    async def order_help(self, request: str, ctx: RunContext) -> str:
        """Answer a question about an order, or cancel one."""
        return await ctx.generate(request, tools=[lookup_order, cancel_order])


def create_app(model: Model | None = None, store: OrderStore | None = None) -> AgentApp:
    """Serve the order desk over A2A.

    Args:
        model: The model to use. Defaults to ``create_model()``.
        store: The orders. Defaults to a new ``OrderStore``.
    """
    store = store or OrderStore()
    services = Services()
    services.add(OrderStore, store)
    host = HostConfig(
        public_url=os.environ.get("ORDERS_PUBLIC_URL", "http://127.0.0.1:8000"),
        # Development placeholder: one API key from the environment, for the
        # customer alice. Production hosts use real identities and roles.
        auth=APIKeyAuth(
            keys={os.environ["ORDERS_API_KEY"]: APIKey("alice", ["customer"])}
        ),
        role_permissions={"customer": ["orders.cancel"]},
    )
    agent = OrderDesk(model=model or create_model(), tools=[lookup_order, cancel_order])
    runtime = Runtime(services=services, tool_authorization=OwnOrdersOnly(store))
    return AgentApp(agent=agent, host=host, runtime=runtime)
