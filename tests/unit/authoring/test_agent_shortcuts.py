"""Agent.invoke and Agent.ask: run an agent without building a Runtime."""

import asyncio
import json
from collections.abc import Sequence
from typing import Annotated, Any

import pytest
from a2a.types import Message
from pydantic import Field

from agentframe import (
    Agent,
    ClarificationNeededError,
    DefinitionError,
    Inject,
    InputValidationError,
    ModelCallLimitExceededError,
    ModelCapabilities,
    ModelRouter,
    PermissionDeniedError,
    Principal,
    RoutingContext,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    Services,
    SkillCall,
    SkillDefinition,
    TimeoutLimitExceededError,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel


class OrderStore:
    def status(self, order_id: str) -> str:
        return f"Order {order_id} has shipped."


@agent(version="1.0.0")
class HelpDesk(Agent):
    """Answers order questions."""

    @skill(tags=["orders"])
    async def help(self, request: Annotated[str, Field()], ctx: RunContext) -> str:
        """Answer an order question."""
        return f"You asked: {request}"

    @skill(tags=["orders"])
    async def status(self, order_id: str, count: int, ctx: RunContext) -> str:
        """Report on several orders."""
        return f"{count} x {order_id}"


@agent(version="1.0.0")
class Orders(Agent):
    """Tracks and cancels orders."""

    limits = RunLimits(timeout_seconds=2)

    @skill(tags=["orders"])
    async def track(
        self, order_id: str, ctx: RunContext, orders: Inject[OrderStore]
    ) -> str:
        """Track an order."""
        return orders.status(order_id)

    @skill(tags=["orders"], requires_permissions=["orders.cancel"])
    async def cancel(self, order_id: str, ctx: RunContext) -> str:
        """Cancel an order."""
        return f"Cancelled {order_id}."

    @skill(tags=["orders"])
    async def summarize(self, order_id: str, ctx: RunContext) -> str:
        """Summarize an order with the agent's model."""
        return await ctx.generate(f"Summarize order {order_id}")

    @skill(tags=["orders"])
    async def wait(self, seconds: float, ctx: RunContext) -> None:
        """Wait for a while."""
        await asyncio.sleep(seconds)


def orders_runtime() -> Runtime:
    services = Services()
    services.add(OrderStore, OrderStore())
    return Runtime(services=services)


def router_model(*replies: dict[str, Any]) -> ScriptedModel:
    """A router model that answers with ``replies`` as JSON."""
    return ScriptedModel(
        [json.dumps(reply) for reply in replies],
        capabilities=ModelCapabilities(context_window=8192, structured_output=True),
    )


def select(skill_id: str) -> dict[str, Any]:
    return {"skill": skill_id, "question": None}


def extract(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"arguments": arguments, "question": None}


# invoke


async def test_invoke_runs_the_skill_with_keyword_arguments() -> None:
    assert await HelpDesk().invoke("status", order_id="A1", count=2) == "2 x A1"


async def test_invoke_rejects_an_unknown_skill() -> None:
    with pytest.raises(InputValidationError, match="no skill with the requested id"):
        await HelpDesk().invoke("missing")


async def test_invoke_validates_arguments() -> None:
    with pytest.raises(InputValidationError):
        await HelpDesk().invoke("status", order_id="A1", count="many")


async def test_invoke_uses_the_given_runtime() -> None:
    with pytest.raises(DefinitionError, match="no service registered"):
        await Orders().invoke("track", order_id="A1")

    value = await Orders().invoke("track", runtime=orders_runtime(), order_id="A1")

    assert value == "Order A1 has shipped."


async def test_invoke_checks_permissions() -> None:
    with pytest.raises(PermissionDeniedError):
        await Orders().invoke("cancel", order_id="A1")

    clerk = Principal(id="clerk", permissions=frozenset({"orders.cancel"}))
    assert await Orders().invoke("cancel", principal=clerk, order_id="A1") == (
        "Cancelled A1."
    )


async def test_invoke_applies_run_options_limits() -> None:
    options = RunOptions(limits=RunLimits(timeout_seconds=0.01))

    with pytest.raises(TimeoutLimitExceededError):
        await Orders().invoke("wait", options=options, seconds=10)


# ask


async def test_ask_sends_the_prompt_to_the_only_text_skill_without_a_model() -> None:
    model = router_model()

    answer = await HelpDesk().ask("Where is A1?", router=ModelRouter(model))

    assert answer == "You asked: Where is A1?"
    assert model.requests == []


async def test_ask_rejects_a_blank_prompt() -> None:
    with pytest.raises(InputValidationError, match="no text"):
        await HelpDesk().ask("  ")


async def test_ask_lets_the_router_choose_among_several_skills() -> None:
    model = router_model(select("track"), extract({"order_id": "A1"}))

    answer = await Orders().ask(
        "Where is order A1?", router=ModelRouter(model), runtime=orders_runtime()
    )

    assert answer == "Order A1 has shipped."
    assert len(model.requests) == 2


async def test_ask_uses_the_agents_own_router() -> None:
    model = router_model(select("track"), extract({"order_id": "A1"}))
    orders = Orders(router=ModelRouter(model))

    assert await orders.ask("Where is A1?", runtime=orders_runtime()) == (
        "Order A1 has shipped."
    )


async def test_ask_charges_routing_model_calls_to_the_run() -> None:
    model = router_model(select("summarize"), extract({"order_id": "A1"}))
    agent_model = ScriptedModel(["A summary."])
    # Routing takes both calls this run allows, so the skill's own call fails.
    options = RunOptions(limits=RunLimits(max_model_calls=2))

    with pytest.raises(ModelCallLimitExceededError):
        await Orders(model=agent_model).ask(
            "Summarize A1", router=ModelRouter(model), options=options
        )
    assert agent_model.requests == []


async def test_ask_routes_only_to_skills_the_principal_may_run() -> None:
    offered: list[str] = []

    class CancelRouter:
        async def route(
            self,
            conversation: Sequence[Message],
            skills: Sequence[SkillDefinition],
            ctx: RoutingContext,
        ) -> SkillCall:
            offered.extend(skill.id for skill in skills)
            return SkillCall("cancel", {"order_id": "A1"})

    with pytest.raises(InputValidationError, match="not available"):
        await Orders().ask("Cancel A1", router=CancelRouter())
    assert offered == ["track", "summarize", "wait"]


async def test_ask_raises_the_routers_question() -> None:
    model = router_model({"skill": None, "question": "Which order?"})

    with pytest.raises(ClarificationNeededError) as raised:
        await Orders().ask("Help with my order", router=ModelRouter(model))

    assert raised.value.question == "Which order?"
    assert raised.value.task_id is None
    assert raised.value.invocation_id is not None


async def test_ask_without_a_router_lists_the_skills() -> None:
    with pytest.raises(DefinitionError, match="cancel, summarize, track, wait"):
        await Orders().ask("Where is A1?")


async def test_ask_checks_permissions_on_the_text_skill() -> None:
    @agent(version="1.0.0")
    class Vault(Agent):
        """Answers questions about secrets."""

        @skill(tags=["secrets"], requires_permissions=["vault.read"])
        async def reveal(self, question: str, ctx: RunContext) -> str:
            """Reveal a secret."""
            return "secret"

    with pytest.raises(PermissionDeniedError):
        await Vault().ask("What is the secret?")


# Names


def test_a_skill_method_cannot_be_named_ask() -> None:
    with pytest.raises(DefinitionError, match="skill method 'ask' hides"):

        @agent(version="1.0.0")
        class Desk(Agent):
            """Answers questions."""

            @skill(tags=["questions"])
            async def ask(self, question: str, ctx: RunContext) -> str:  # type: ignore[override]
                """Answer a question."""
                return question


def test_a_skill_method_cannot_be_named_invoke() -> None:
    with pytest.raises(DefinitionError, match="skill method 'invoke' hides"):

        @agent(version="1.0.0")
        class Desk(Agent):
            """Runs tasks."""

            @skill(tags=["tasks"])
            async def invoke(self, task: str, ctx: RunContext) -> str:  # type: ignore[override]
                """Run a task."""
                return task


async def test_a_skill_can_still_have_the_id_ask() -> None:
    @agent(version="1.0.0")
    class Desk(Agent):
        """Answers questions."""

        @skill("ask", tags=["questions"])
        async def answer(self, question: str, ctx: RunContext) -> str:
            """Answer a question."""
            return question.upper()

    assert await Desk().invoke("ask", question="hi") == "HI"
