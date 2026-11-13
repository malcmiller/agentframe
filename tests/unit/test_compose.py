"""``agentframe compose``: one development container per agent."""

import re
from pathlib import Path

import httpx
import pytest
import yaml

from agentframe import (
    AgentDirectory,
    DefinitionError,
    EnvironmentAgentRegistry,
    RemoteAgentError,
    dev_settings,
)
from agentframe.cli import main
from agentframe.compose import compose_document, write_compose
from agentframe.project import ProjectAgent

GOLDEN = Path(__file__).parent / "golden"
PROJECT = """\
agents:
  research-agent:
    app: myapp.research:create_app
    port: 8001
  planner:
    app: myapp.planner:create_app
    port: 8002
"""
# What a user's .env file would hold.
KEYS = {
    "RESEARCH_AGENT_API_KEY": "research-secret",
    "PLANNER_API_KEY": "planner-secret",
    "AGENTFRAME_DEV_CLIENT_KEY": "client-secret",
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "agentframe.yaml"
    path.write_text(PROJECT)
    return path


def test_generated_files_match_the_golden_files(project: Path) -> None:
    result = write_compose(project)

    compose = project.parent / "compose.yaml"
    dockerfile = project.parent / "Dockerfile"
    assert result.written == [compose.resolve(), dockerfile.resolve()]
    assert compose.read_text() == (GOLDEN / "compose.yaml").read_text()
    assert dockerfile.read_text() == (GOLDEN / "Dockerfile").read_text()
    assert result.variables == list(KEYS)


def test_keys_are_only_variable_references() -> None:
    compose = compose_document(_agents())

    references = set(re.findall(r"\$\{(\w+):\?", compose))
    assert references == set(KEYS)


def test_an_existing_compose_file_is_kept_without_force(project: Path) -> None:
    compose = project.parent / "compose.yaml"
    compose.write_text("mine")

    with pytest.raises(DefinitionError, match="pass --force"):
        write_compose(project)

    assert compose.read_text() == "mine"
    assert not (project.parent / "Dockerfile").exists()


def test_force_overwrites_compose_but_never_an_existing_dockerfile(
    project: Path,
) -> None:
    (project.parent / "compose.yaml").write_text("mine")
    dockerfile = project.parent / "Dockerfile"
    dockerfile.write_text("FROM my-own-image\n")

    result = write_compose(project, force=True)

    assert result.written == [(project.parent / "compose.yaml").resolve()]
    assert result.kept == dockerfile.resolve()
    assert dockerfile.read_text() == "FROM my-own-image\n"
    assert (project.parent / "compose.yaml").read_text().startswith("# Generated")


def test_an_agent_name_starting_with_a_digit_is_refused() -> None:
    agent = ProjectAgent("1st-agent", "myapp.first:create_app", 8001)

    with pytest.raises(DefinitionError, match="Start the name with a letter"):
        compose_document([agent])


def test_the_command_reports_the_files_and_the_variables_to_set(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["compose", "--config", str(project)]) == 0
    assert main(["compose", "--config", str(project)]) == 1

    output = capsys.readouterr()
    assert "wrote" in output.out
    assert "development only" in output.out
    assert "RESEARCH_AGENT_API_KEY, PLANNER_API_KEY" in output.out
    assert "compose.yaml already exists; pass --force" in output.err


def test_the_compose_environment_wires_dev_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_service_environment(monkeypatch, "planner")

    dev = dev_settings()

    assert dev.public_url == "http://planner:8002"
    assert dev.directory is not None
    # noinspection PyProtectedMember
    assert dev.directory._allow_http_hosts == frozenset({"research-agent", "planner"})
    assert list(dev.remotes) == ["research-agent"]


async def test_the_dev_opt_in_lets_the_directory_fetch_service_name_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_service_environment(monkeypatch, "planner")
    requested: list[httpx.URL] = []
    directory = _directory(requested, allow_http_hosts=["research-agent", "planner"])

    with pytest.raises(RemoteAgentError):
        await directory.get("research-agent")

    assert requested == [
        httpx.URL("http://research-agent:8001/.well-known/agent-card.json")
    ]


async def test_without_the_dev_opt_in_the_compose_urls_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_service_environment(monkeypatch, "planner")
    monkeypatch.delenv("AGENTFRAME_DEV")
    monkeypatch.delenv("AGENTFRAME_ALLOW_HTTP_HOSTS")
    requested: list[httpx.URL] = []
    directory = _directory(requested, allow_http_hosts=[])

    with pytest.raises(DefinitionError, match="needs AGENTFRAME_DEV=1"):
        dev_settings()
    with pytest.raises(RemoteAgentError, match="allow_http_hosts"):
        await directory.get("research-agent")
    assert requested == []


def _agents() -> list[ProjectAgent]:
    return [
        ProjectAgent("research-agent", "myapp.research:create_app", 8001),
        ProjectAgent("planner", "myapp.planner:create_app", 8002),
    ]


def _use_service_environment(monkeypatch: pytest.MonkeyPatch, service: str) -> None:
    """Set the environment Compose would give ``service``, keys filled in."""
    document = yaml.safe_load(compose_document(_agents()))
    for name, value in document["services"][service]["environment"].items():
        for variable, key in KEYS.items():
            value = value.replace(f"${{{variable}:?set {variable} in .env}}", key)
        monkeypatch.setenv(name, value)


def _directory(
    requested: list[httpx.URL], *, allow_http_hosts: list[str]
) -> AgentDirectory:
    def record(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        return httpx.Response(404)

    return AgentDirectory(
        sources=[EnvironmentAgentRegistry("AGENTFRAME_AGENTS")],
        allow_http_hosts=allow_http_hosts,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )
