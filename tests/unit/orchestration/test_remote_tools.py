"""``ctx.generate(tools=[*catalog])`` offers remote skills to the model as tools.

These tests send no A2A message: an ``httpx.MockTransport`` serves the cards
and fails any other request. Calls that reach real agents are in
``tests/integration/test_remote_tools_hosted.py``.
"""

from collections.abc import Iterable
from typing import Any

import httpx
import httpx2
import openai
import pytest

from agentframe import (
    Agent,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    DefinitionError,
    FoundryModel,
    Model,
    ModelCapabilities,
    PermissionDeniedError,
    Remote,
    RemoteSkill,
    RunContext,
    Runtime,
    SkillCatalog,
    ToolCall,
    ToolSpec,
    UnsupportedCapabilityError,
    agent,
    skill,
    tool,
)
from agentframe.hosting.skill_call import SKILL_CALL_EXTENSION_URI
from agentframe.orchestration.remote_tools import ASK_SCHEMA
from agentframe.testing import ScriptedModel

TOOL_MODEL = ModelCapabilities(context_window=128_000, tools=True)
TOPIC_SCHEMA = {
    "type": "object",
    "properties": {"topic": {"type": "string"}},
    "required": ["topic"],
}


def card(name: str, skills: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """An Agent Card; ``skills`` maps skill IDs to argument schemas and
    declares the skill-call extension."""
    extensions = []
    if skills is not None:
        extensions = [{"uri": SKILL_CALL_EXTENSION_URI, "params": {"skills": skills}}]
    return {
        "name": name,
        "description": f"The {name} agent.",
        "version": "0.1.0",
        "supportedInterfaces": [
            {
                "url": f"https://{name}.example.com/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {"extensions": extensions},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {"id": skill_id, "name": skill_id, "description": f"Run {skill_id}."}
            for skill_id in skills or {}
        ],
    }


class Cards(AgentRegistry):
    """Lists agents by name; their hosts serve only their cards."""

    def __init__(self, cards: dict[str, dict[str, Any]]) -> None:
        self.cards = cards
        self.posts = 0

    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return [
            AgentReference(name, f"https://{name}.example.com/agent-card.json")
            for name in self.cards
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            self.posts += 1
            raise AssertionError("no A2A message should be sent")
        return httpx.Response(200, json=self.cards[request.url.host.split(".")[0]])

    def directory(self) -> AgentDirectory:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return AgentDirectory(sources=[self], http_client=client)


RESEARCH = card("research-agent", {"find_sources": TOPIC_SCHEMA})
PLAIN = card("plain")


@tool(description="Look something up.")
def lookup(text: str) -> str:
    return text


@agent(name="Orchestrator", description="Delegates.", version="0.1.0")
class Orchestrator(Agent):
    """Offers its catalog (plus ``extra`` tools) to its model."""

    def __init__(
        self, model: Model, *extra: Any, twice: bool = False, **options: Any
    ) -> None:
        super().__init__(model=model, tools=[lookup])
        self.extra = extra
        self.twice = twice
        self.options = options
        self.catalog: SkillCatalog | None = None

    @skill(id="answer", description="Answer a goal.", tags=["orchestration"])
    async def answer(self, goal: str, ctx: RunContext) -> str:
        self.catalog = await ctx.remote_skills(**self.options)
        entries: list[Any] = list(self.catalog)
        if self.twice:
            entries += entries
        # A one-shot iterator, as any Iterable may be.
        return await ctx.generate(goal, tools=iter([*self.extra, *entries]))


async def answer(cards: Cards, orchestrator: Orchestrator) -> str:
    async with cards.directory() as directory:
        result: str = await Runtime(directory=directory).invoke(
            orchestrator, skill="answer", arguments={"goal": "Research tides"}
        )
    return result


def scripted(*replies: Any) -> ScriptedModel:
    return ScriptedModel(replies, capabilities=TOOL_MODEL)


async def test_entries_become_tools_with_safe_names_card_text_and_schemas() -> None:
    model = scripted("done")

    await answer(
        Cards({"research-agent": RESEARCH, "plain": PLAIN}), Orchestrator(model)
    )

    assert model.requests[0].tools == (
        ToolSpec("research-agent_find_sources", "Run find_sources.", TOPIC_SCHEMA),
        ToolSpec("plain", "The plain agent.", ASK_SCHEMA),
    )
    # Card text is never a trusted instruction.
    assert [m.role for m in model.requests[0].messages] == ["user"]


async def test_the_model_gets_a_copy_of_the_entrys_schema() -> None:
    model = scripted("done")
    orchestrator = Orchestrator(model)

    await answer(Cards({"research-agent": RESEARCH}), orchestrator)
    model.requests[0].tools[0].parameters["required"].append("injected")

    assert orchestrator.catalog is not None
    assert orchestrator.catalog.entries[0].arguments_schema == TOPIC_SCHEMA


async def test_invalid_arguments_and_unknown_names_are_told_to_the_model() -> None:
    cards = Cards({"research-agent": RESEARCH, "plain": PLAIN})
    model = scripted(
        (
            ToolCall("1", "plain", '{"txt": "hi"}'),
            ToolCall("2", "research-agent_find_sources", "[1]"),
            ToolCall("3", "research-agent.find_sources", '{"topic": "tides"}'),
        ),
        "done",
    )

    await answer(cards, Orchestrator(model))

    results = [m.content for m in model.requests[1].messages if m.role == "tool"]
    assert results == [
        '{"error": "Invalid arguments: expected one string \'text\'."}',
        '{"error": "Invalid arguments: expected a JSON object."}',
        '{"error": "Unknown tool. Call only the tools you were offered."}',
    ]
    assert cards.posts == 0


async def test_a_permission_error_fails_the_run_instead_of_reaching_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied(*args: Any) -> Any:
        raise PermissionDeniedError("The remote call was denied")

    monkeypatch.setattr(Remote, "_call_with", denied)
    model = scripted(ToolCall("1", "research-agent_find_sources", '{"topic": "x"}'))

    with pytest.raises(PermissionDeniedError):
        await answer(Cards({"research-agent": RESEARCH}), Orchestrator(model))
    assert len(model.requests) == 1


# Refused before any model call


@pytest.mark.parametrize(
    ("cards", "extra"),
    [
        # Agent "a"'s skill "b" and agent "a_b"'s ask entry are both "a_b".
        ({"a": card("a", {"b": TOPIC_SCHEMA}), "a_b": card("a_b")}, ()),
        # Agent "lookup"'s ask entry and the @tool lookup.
        ({"lookup": card("lookup")}, (lookup,)),
    ],
)
async def test_tool_name_collisions_fail_before_any_model_call(
    cards: dict[str, dict[str, Any]], extra: Iterable[Any]
) -> None:
    model = scripted()

    with pytest.raises(DefinitionError, match="both become the model tool"):
        await answer(Cards(cards), Orchestrator(model, *extra))
    assert model.requests == []


async def test_an_entry_selected_twice_fails_before_any_model_call() -> None:
    model = scripted()

    with pytest.raises(DefinitionError, match="more than once"):
        await answer(Cards({"plain": PLAIN}), Orchestrator(model, twice=True))
    assert model.requests == []


async def test_an_entry_from_no_catalog_of_this_run_is_refused() -> None:
    hand_made = RemoteSkill(
        name="plain", agent="plain", skill=None, description="", tags=()
    )
    model = scripted()

    with pytest.raises(DefinitionError, match="not from a catalog this run built"):
        await answer(Cards({}), Orchestrator(model, hand_made))
    assert model.requests == []


async def test_an_overlong_tool_name_is_refused() -> None:
    long_name = "a" * 60
    model = scripted()

    with pytest.raises(DefinitionError, match="longer than 64"):
        await answer(
            Cards({long_name: card(long_name, {"find_sources": TOPIC_SCHEMA})}),
            Orchestrator(model),
        )


async def test_a_schema_foundry_cannot_use_fails_before_sending() -> None:
    def no_requests(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("nothing should reach Foundry")

    client = openai.AsyncOpenAI(
        base_url="https://foundry.example.com/openai/v1/",
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(no_requests)),
    )
    free_form = {
        "type": "object",
        "properties": {"counts": {"type": "object", "additionalProperties": True}},
    }
    model = FoundryModel(deployment="gpt-5.4", client=client, context_window=128_000)

    with pytest.raises(UnsupportedCapabilityError, match="research-agent_count"):
        await answer(
            Cards({"research-agent": card("research-agent", {"count": free_form})}),
            Orchestrator(model),
        )


@pytest.mark.parametrize("ceiling", [0, -1, True, 1.5])
async def test_max_tokens_per_call_must_be_a_positive_whole_number(
    ceiling: object,
) -> None:
    model = scripted()

    with pytest.raises(DefinitionError, match="max_tokens_per_call"):
        await answer(Cards({}), Orchestrator(model, max_tokens_per_call=ceiling))
