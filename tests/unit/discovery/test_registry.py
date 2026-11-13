from pathlib import Path

import pytest

from agentframe import (
    AgentQuery,
    AgentReference,
    AgentRegistry,
    DefinitionError,
    EnvironmentAgentRegistry,
    FileAgentRegistry,
)

RESEARCH_CARD = "https://research.example.com/.well-known/agent-card.json"
REVIEW_CARD = "http://127.0.0.1:8002/.well-known/agent-card.json"

AGENTS_YAML = f"""
- name: research-agent
  url: {RESEARCH_CARD}
- name: review-agent
  url: {REVIEW_CARD}
"""


def write_registry(tmp_path: Path, text: str) -> FileAgentRegistry:
    path = tmp_path / "agents.yaml"
    path.write_text(text)
    return FileAgentRegistry(path)


# References and queries


def test_reference_keeps_name_and_card_url() -> None:
    reference = AgentReference("research-agent", RESEARCH_CARD)

    assert reference.name == "research-agent"
    assert reference.card_url == RESEARCH_CARD


@pytest.mark.parametrize(
    "url",
    [
        "research.example.com/card.json",
        "ftp://research.example.com/card.json",
        "https://user:secret@research.example.com/card.json",
        "https://research.example.com/card.json?key=secret",
        "https://research.example.com/card.json#card",
        "http://[::1",
    ],
)
def test_reference_refuses_unsafe_card_urls_without_echoing_them(url: str) -> None:
    with pytest.raises(DefinitionError, match="research-agent") as raised:
        AgentReference("research-agent", url)

    assert "secret" not in str(raised.value)


def test_reference_refuses_an_empty_name() -> None:
    with pytest.raises(DefinitionError, match="non-empty"):
        AgentReference(" ", RESEARCH_CARD)


def test_query_refuses_a_single_string_of_names() -> None:
    with pytest.raises(DefinitionError, match="not a single string"):
        AgentQuery(names="research-agent")


# FileAgentRegistry


async def test_file_registry_gets_and_lists_agents(tmp_path: Path) -> None:
    registry = write_registry(tmp_path, AGENTS_YAML)

    research = await registry.get("research-agent")
    everyone = await registry.list(AgentQuery())

    assert research == AgentReference("research-agent", RESEARCH_CARD)
    assert [reference.name for reference in everyone] == [
        "research-agent",
        "review-agent",
    ]


async def test_file_registry_filters_by_name(tmp_path: Path) -> None:
    registry = write_registry(tmp_path, AGENTS_YAML)

    found = await registry.list(AgentQuery(names=["review-agent", "missing"]))

    assert found == [AgentReference("review-agent", REVIEW_CARD)]


async def test_file_registry_reads_the_file_again_on_each_call(
    tmp_path: Path,
) -> None:
    registry = write_registry(tmp_path, "[]")
    assert await registry.list(AgentQuery()) == []

    (tmp_path / "agents.yaml").write_text(AGENTS_YAML)

    assert len(await registry.list(AgentQuery())) == 2


async def test_file_registry_does_no_io_until_first_use(tmp_path: Path) -> None:
    registry = FileAgentRegistry(tmp_path / "missing.yaml")

    with pytest.raises(DefinitionError, match=r"missing\.yaml.*cannot be read"):
        await registry.list(AgentQuery())


async def test_file_registry_get_of_unknown_agent_names_source_and_agent(
    tmp_path: Path,
) -> None:
    registry = write_registry(tmp_path, AGENTS_YAML)

    with pytest.raises(
        DefinitionError, match=r"agents\.yaml' does not list an agent named 'writer'"
    ):
        await registry.get("writer")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- name: [unclosed", "is not valid YAML at line 1"),
        ("", "must be a YAML list"),
        ("name: research-agent", "must be a YAML list"),
        ("- research-agent", "entry 1 must have exactly the keys"),
        (f"- name: a\n  card_url: {RESEARCH_CARD}", "entry 1 must have exactly"),
        ("- name: a\n  url: 8002", "entry 1 'name' and 'url' must be strings"),
        ('- name: ""\n  url: https://a.example.com', "non-empty"),
        ("- name: a\n  url: https://a.example.com?k=v", "Agent 'a' card URL"),
        (
            f"- name: a\n  url: {RESEARCH_CARD}\n- name: a\n  url: {REVIEW_CARD}",
            "agent 'a' is listed more than once",
        ),
    ],
)
async def test_file_registry_refuses_invalid_files(
    tmp_path: Path, text: str, message: str
) -> None:
    registry = write_registry(tmp_path, text)

    with pytest.raises(DefinitionError, match=message) as raised:
        await registry.list(AgentQuery())

    assert "agents.yaml" in str(raised.value)


async def test_file_registry_refuses_yaml_tags(tmp_path: Path) -> None:
    registry = write_registry(tmp_path, "- !!python/object/apply:os.system [ls]")

    with pytest.raises(DefinitionError, match="not valid YAML"):
        await registry.list(AgentQuery())


async def test_file_registry_refuses_files_over_one_mebibyte(tmp_path: Path) -> None:
    registry = write_registry(tmp_path, "#" * (1_048_576 + 1))

    with pytest.raises(DefinitionError, match="larger than 1048576 bytes"):
        await registry.list(AgentQuery())


# EnvironmentAgentRegistry


async def test_environment_registry_reads_name_url_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AGENTFRAME_AGENTS",
        f"research-agent={RESEARCH_CARD}, review-agent={REVIEW_CARD}",
    )
    registry = EnvironmentAgentRegistry("AGENTFRAME_AGENTS")

    assert await registry.get("review-agent") == AgentReference(
        "review-agent", REVIEW_CARD
    )
    assert await registry.list(AgentQuery(names={"research-agent"})) == [
        AgentReference("research-agent", RESEARCH_CARD)
    ]


async def test_environment_registry_with_an_empty_value_lists_no_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFRAME_AGENTS", "")

    assert await EnvironmentAgentRegistry("AGENTFRAME_AGENTS").list(AgentQuery()) == []


async def test_environment_registry_does_no_io_until_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTFRAME_AGENTS", raising=False)
    registry = EnvironmentAgentRegistry("AGENTFRAME_AGENTS")
    monkeypatch.setenv("AGENTFRAME_AGENTS", f"research-agent={RESEARCH_CARD}")

    assert len(await registry.list(AgentQuery())) == 1


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "AGENTFRAME_AGENTS is not set"),
        ("research-agent", "an entry without '='"),
        (f"={RESEARCH_CARD}", "non-empty"),
        ("research-agent=not-a-url", "Agent 'research-agent' card URL"),
        (
            f"a={RESEARCH_CARD},a={REVIEW_CARD}",
            "agent 'a' is listed more than once",
        ),
    ],
)
async def test_environment_registry_refuses_invalid_values(
    monkeypatch: pytest.MonkeyPatch, value: str | None, message: str
) -> None:
    if value is None:
        monkeypatch.delenv("AGENTFRAME_AGENTS", raising=False)
    else:
        monkeypatch.setenv("AGENTFRAME_AGENTS", value)
    registry = EnvironmentAgentRegistry("AGENTFRAME_AGENTS")

    with pytest.raises(DefinitionError, match=message) as raised:
        await registry.list(AgentQuery())

    assert "AGENTFRAME_AGENTS" in str(raised.value)


async def test_environment_registry_get_of_unknown_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFRAME_AGENTS", f"research-agent={RESEARCH_CARD}")

    with pytest.raises(
        DefinitionError,
        match="AGENTFRAME_AGENTS does not list an agent named 'writer'",
    ):
        await EnvironmentAgentRegistry("AGENTFRAME_AGENTS").get("writer")


# Custom registries


class InMemoryAgentRegistry(AgentRegistry):
    """An application's own source, here a fixed list."""

    def __init__(self, references: list[AgentReference]) -> None:
        self._references = references

    async def get(self, agent_id: str) -> AgentReference:
        for reference in self._references:
            if reference.name == agent_id:
                return reference
        raise DefinitionError(f"No agent named '{agent_id}'.")

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        if query.names is None:
            return list(self._references)
        return [ref for ref in self._references if ref.name in query.names]


async def test_a_custom_registry_follows_the_same_contract() -> None:
    research = AgentReference("research-agent", RESEARCH_CARD)
    review = AgentReference("review-agent", REVIEW_CARD)
    registry: AgentRegistry = InMemoryAgentRegistry([research, review])

    assert await registry.get("review-agent") == review
    assert await registry.list(AgentQuery(names=["research-agent"])) == [research]
    with pytest.raises(DefinitionError, match="writer"):
        await registry.get("writer")


def test_a_registry_must_implement_get_and_list() -> None:
    class Incomplete(AgentRegistry):
        async def get(self, agent_id: str) -> AgentReference:
            return AgentReference(agent_id, RESEARCH_CARD)

    with pytest.raises(TypeError, match="list"):
        Incomplete()  # type: ignore[abstract]
