"""``SkillCatalog.check`` validates a ``Plan`` against the catalog, without I/O,
and references are substituted when a step runs."""

from typing import Any

import httpx
import pytest

from agentframe import (
    Agent,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    DefinitionError,
    InputValidationError,
    Plan,
    PlanStep,
    RemoteSkill,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    SkillCatalog,
    agent,
    skill,
)
from agentframe.orchestration.plan import substituted_arguments, substituted_text
from agentframe.skill_call import SKILL_CALL_EXTENSION_URI
from agentframe.testing import ScriptedModel

FIND_SOURCES = RemoteSkill(
    name="research-agent.find_sources",
    agent="research-agent",
    skill="find_sources",
    description="Find sources.",
    tags=(),
    arguments_schema={
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": ["topic"],
        "additionalProperties": False,
    },
)
BRIEF = RemoteSkill(
    name="research-agent.brief",
    agent="research-agent",
    skill="brief",
    description="Write a brief.",
    tags=(),
    arguments_schema={"type": "object", "required": ["sources"]},
)
WRITER = RemoteSkill(
    name="writer-agent", agent="writer-agent", skill=None, description="", tags=()
)
CATALOG = SkillCatalog([FIND_SOURCES, BRIEF, WRITER], bindings={})


def step(id: str, use: str = "research-agent.find_sources", **fields: Any) -> PlanStep:
    if use == "research-agent.find_sources" and "arguments" not in fields:
        fields["arguments"] = {"topic": "tides"}
    return PlanStep(id=id, use=use, **fields)


def ask(id: str, text: str, **fields: Any) -> PlanStep:
    return PlanStep(id=id, use="writer-agent", text=text, **fields)


def ids(groups: tuple[tuple[PlanStep, ...], ...]) -> list[list[str]]:
    return [[s.id for s in group] for group in groups]


def test_a_valid_plan_returns_parallel_groups_in_topological_order() -> None:
    plan = Plan(
        steps=[
            ask("post", "Write a post from this brief: {brief} {{not a ref}}"),
            step(
                "brief",
                "research-agent.brief",
                arguments={"sources": [{"$ref": "tides"}, {"$ref": "waves.items"}]},
            ),
            step("waves"),
            step("tides"),
            ask("notify", "Tell the team.", after=["post"]),
        ],
        result="post",
    )

    groups = CATALOG.check(plan)

    assert ids(groups) == [["waves", "tides"], ["brief"], ["post"], ["notify"]]
    assert CATALOG.check(plan) == groups


def test_the_catalog_finds_entries_by_name() -> None:
    assert CATALOG.get("writer-agent") is WRITER
    assert CATALOG.get("missing") is None


INVALID_PLANS = [
    pytest.param(
        [step("Bad-ID")], "x", "Plan step 1 field 'id' must match", id="malformed-id"
    ),
    pytest.param(
        [step("a"), step("a")],
        "a",
        "step 'a' field 'id' is used by more than one step",
        id="duplicate-id",
    ),
    pytest.param(
        [step("a", "billing-agent.refund")],
        "a",
        "step 'a' field 'use' names no entry of the skill catalog",
        id="entry-outside-catalog",
    ),
    pytest.param(
        [step("a", text="hi")],
        "a",
        "step 'a' field 'text' is only for ask entries",
        id="text-on-structured-entry",
    ),
    pytest.param(
        [ask("a", "hi", arguments={"topic": "tides"})],
        "a",
        "step 'a' field 'arguments' is only for structured entries",
        id="arguments-on-ask-entry",
    ),
    pytest.param(
        [step("a", "writer-agent")],
        "a",
        "step 'a' field 'text' is required for the ask entry 'writer-agent'",
        id="ask-without-text",
    ),
    pytest.param(
        [step("a", arguments={})],
        "a",
        "step 'a' field 'arguments.topic' is required by 'research-agent.find_sources'",
        id="missing-required-argument",
    ),
    pytest.param(
        [step("a", arguments={"topic": "tides", "depth": 3})],
        "a",
        "step 'a' field 'arguments.depth' is not an argument",
        id="unknown-argument",
    ),
    pytest.param(
        [step("a", arguments={"topic": "tides", "secret value!": 3})],
        "a",
        "step 'a' field 'arguments' is not an argument",
        id="unknown-argument-with-unsafe-name",
    ),
    pytest.param(
        [step("a", arguments={"topic": {"$ref": "missing"}})],
        "a",
        "step 'a' field 'arguments.topic' refers to no step of the plan",
        id="dangling-reference",
    ),
    pytest.param(
        [step("a", "research-agent.brief", arguments={"$ref": "nope"})],
        "a",
        "step 'a' field 'arguments' must name arguments",
        id="top-level-dangling-reference",
    ),
    pytest.param(
        [step("a", "research-agent.brief", arguments={"$ref": "b"}), step("b")],
        "a",
        "step 'a' field 'arguments' must name arguments",
        id="top-level-reference",
    ),
    pytest.param(
        [step("a", arguments={"topic": {"$ref": "b.c.d"}}), step("b")],
        "a",
        "step 'a' field 'arguments.topic' has a malformed reference",
        id="nested-field-reference",
    ),
    pytest.param(
        [step("a", arguments={"topic": {"$ref": "b", "default": 1}}), step("b")],
        "a",
        "step 'a' field 'arguments.topic' has a malformed reference",
        id="reference-with-extra-keys",
    ),
    pytest.param(
        [step("a", arguments={"topic": {"$ref": "b.title"}}), ask("b", "hi")],
        "a",
        "step 'a' field 'arguments.topic' refers to a field of 'b', an ask step",
        id="field-of-ask-step",
    ),
    pytest.param(
        [ask("a", "Use {missing}")],
        "a",
        "step 'a' field 'text' refers to no step of the plan",
        id="dangling-placeholder",
    ),
    pytest.param(
        [ask("a", "Use {b!r}"), step("b")],
        "a",
        "step 'a' field 'text' has a malformed placeholder",
        id="placeholder-expression",
    ),
    pytest.param(
        [ask("a", "Use {b")],
        "a",
        "step 'a' field 'text' has a malformed placeholder",
        id="unmatched-brace",
    ),
    pytest.param(
        [ask("a", "hi", after=["missing"])],
        "a",
        "step 'a' field 'after' names no step of the plan",
        id="dangling-after",
    ),
    pytest.param(
        [ask("a", "hi", after=["b"]), ask("b", "hi", after=["a"])],
        "a",
        "step 'a' field 'after' is part of a dependency cycle",
        id="cycle-through-after",
    ),
    pytest.param(
        [step("a", arguments={"topic": {"$ref": "a"}})],
        "a",
        "step 'a' field 'arguments' is part of a dependency cycle",
        id="self-reference",
    ),
    pytest.param(
        [ask("first", "{c}"), ask("b", "{c}"), ask("c", "{b}")],
        "first",
        "step 'c' field 'text' is part of a dependency cycle",
        id="cycle-behind-a-dependent",
    ),
    pytest.param(
        [step("a")],
        "missing",
        "Plan field 'result' names no step of the plan",
        id="dangling-result",
    ),
    pytest.param(
        [step(f"s{n}") for n in range(9)],
        "s0",
        "The plan has 9 steps, more than the 8 this run allows",
        id="too-many-steps",
    ),
    pytest.param(
        [ask("a", "x" * 70_000)],
        "a",
        "The plan is larger than 64 KiB serialized",
        id="too-large",
    ),
]


@pytest.mark.parametrize(("steps", "result", "message"), INVALID_PLANS)
def test_an_invalid_plan_names_the_step_and_field(
    steps: list[PlanStep], result: str, message: str
) -> None:
    with pytest.raises(InputValidationError, match=message):
        CATALOG.check(Plan(steps=steps, result=result))


def test_errors_never_echo_values_from_the_plan() -> None:
    secret = "sk secret value"
    plans = [
        Plan(steps=[step(secret)], result="a"),
        Plan(steps=[step("a", secret)], result="a"),
        Plan(steps=[step("a", arguments={"topic": {"$ref": secret}})], result="a"),
        Plan(steps=[step("a", arguments={"topic": "x", secret: 1})], result="a"),
        Plan(steps=[step("a")], result=secret),
    ]
    for plan in plans:
        with pytest.raises(InputValidationError) as raised:
            CATALOG.check(plan)
        assert secret not in raised.value.message


def test_max_steps_can_be_raised_or_lowered() -> None:
    plan = Plan(steps=[step(f"s{n}") for n in range(9)], result="s0")

    assert len(CATALOG.check(plan, max_steps=9)) == 1
    with pytest.raises(InputValidationError, match="more than the 2"):
        CATALOG.check(plan, max_steps=2)


@pytest.mark.parametrize("max_steps", [0, -1, True, 2.5])
def test_max_steps_must_be_a_positive_whole_number(max_steps: Any) -> None:
    plan = Plan(steps=[step("a")], result="a")

    with pytest.raises(DefinitionError, match="max_steps="):
        CATALOG.check(plan, max_steps=max_steps)


# A run's catalog, built from served cards, then checked with every request
# failing: checking a plan must not touch the network or the model.

RESEARCH_CARD = {
    "name": "research-agent",
    "description": "Finds sources.",
    "version": "0.1.0",
    "supportedInterfaces": [
        {
            "url": "https://research-agent.example.com/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ],
    "capabilities": {
        "extensions": [
            {
                "uri": SKILL_CALL_EXTENSION_URI,
                "params": {"skills": {"find_sources": FIND_SOURCES.arguments_schema}},
            }
        ]
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [{"id": "find_sources", "name": "find_sources", "description": "Find."}],
}


class OneAgent(AgentRegistry):
    """Lists the research agent; its host serves the card until ``offline``."""

    def __init__(self) -> None:
        self.offline = False
        self.requests = 0

    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        if self.offline:
            raise AssertionError("checking a plan must not list agents")
        url = "https://research-agent.example.com/agent-card.json"
        return [AgentReference("research-agent", url)]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.offline or request.method != "GET":
            raise httpx.ConnectError("no network while checking a plan")
        return httpx.Response(200, json=RESEARCH_CARD)


@agent(name="Planner", description="Checks plans.", version="0.1.0")
class Planner(Agent):
    def __init__(self, registry: OneAgent, plan: Plan) -> None:
        self.scripted = ScriptedModel([])
        super().__init__(model=self.scripted)
        self.registry = registry
        self.plan = plan

    @skill(id="check", description="Check a plan.", tags=["planning"])
    async def check(self, ctx: RunContext) -> list[list[str]]:
        catalog = await ctx.remote_skills(all_registered=True)
        self.registry.offline = True
        return ids(catalog.check(self.plan))


async def check_in_a_run(plan: Plan, limits: RunLimits | None = None) -> Any:
    registry = OneAgent()
    planner = Planner(registry, plan)
    client = httpx.AsyncClient(transport=httpx.MockTransport(registry.handle))
    async with AgentDirectory(sources=[registry], http_client=client) as directory:
        result = await Runtime(directory=directory).invoke(
            planner, skill="check", options=RunOptions(limits=limits)
        )
    assert registry.requests == 1  # The card, before going offline.
    assert planner.scripted.requests == []
    return result


async def test_checking_makes_no_network_or_model_call() -> None:
    plan = Plan(steps=[step("tides"), step("waves")], result="tides")

    assert await check_in_a_run(plan) == [["tides", "waves"]]


async def test_steps_never_exceed_the_runs_remaining_tool_calls() -> None:
    plan = Plan(steps=[step("tides"), step("waves"), step("wind")], result="tides")

    with pytest.raises(InputValidationError, match="more than the 2 this run"):
        await check_in_a_run(plan, RunLimits(max_tool_calls=2))


@pytest.mark.parametrize("depth", [300, 3000])
def test_arguments_nested_too_deeply_are_refused(depth: int) -> None:
    nested: Any = "tides"
    for _ in range(depth):
        nested = [nested]
    # Validation refuses this depth, so only model_construct can build it.
    deep = PlanStep.model_construct(
        id="a",
        use="research-agent.find_sources",
        arguments={"topic": nested},
        text=None,
        after=[],
    )
    plan = Plan.model_construct(steps=[deep], result="a")

    with pytest.raises(InputValidationError, match="nested too deeply"):
        CATALOG.check(plan)


def test_references_at_any_depth_become_copies_of_earlier_values() -> None:
    step = PlanStep(
        id="brief",
        use="research-agent.brief",
        arguments={
            "all": {"$ref": "found"},
            "nested": [{"topic": {"$ref": "found.topic"}}, 3],
            "literal": "kept",
        },
    )
    found = {"topic": "tides", "sources": ["a"]}

    arguments = substituted_arguments(step, {"found": found})

    assert arguments == {
        "all": found,
        "nested": [{"topic": "tides"}, 3],
        "literal": "kept",
    }
    arguments["all"]["sources"].append("changed")
    assert found["sources"] == ["a"]


@pytest.mark.parametrize("value", ["text", ["a"], {"other": 1}])
def test_a_field_of_a_value_without_it_fails_without_naming_the_value(
    value: Any,
) -> None:
    step = PlanStep(id="brief", use="writer-agent", text="Use {found.topic}")

    with pytest.raises(InputValidationError) as raised:
        substituted_text(step, {"found": value})

    assert raised.value.message == (
        "Plan step 'brief' field 'text' refers to field 'topic' of 'found', "
        "whose result has no such field"
    )


def test_placeholders_insert_strings_as_they_are_and_other_values_as_json() -> None:
    step = PlanStep(
        id="post",
        use="writer-agent",
        text="{title}: {found} {count} {{literal}}",
    )
    values = {"title": "Tides", "found": {"b": "é", "a": None}, "count": 2}

    assert substituted_text(step, values) == 'Tides: {"a":null,"b":"é"} 2 {literal}'


def test_an_upstream_value_that_looks_like_a_reference_is_inserted_literally() -> None:
    found = {"$ref": "secret"}
    values = {"found": found, "title": "{secret}", "secret": "never inserted"}
    reference = PlanStep(
        id="brief", use="research-agent.brief", arguments={"all": {"$ref": "found"}}
    )
    placeholder = PlanStep(id="post", use="writer-agent", text="{title} {found}")

    assert substituted_arguments(reference, values) == {"all": {"$ref": "secret"}}
    assert substituted_text(placeholder, values) == '{secret} {"$ref":"secret"}'
