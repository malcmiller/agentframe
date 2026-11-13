"""Reading ``agentframe.yaml``: the agents, their apps and ports."""

from pathlib import Path

import pytest

from agentframe import DefinitionError
from agentframe.project import ProjectAgent, load_project


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agentframe.yaml"
    path.write_text(text)
    return path


def test_lists_the_agents_in_file_order(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
agents:
  research-agent:
    app: myapp.research:create_app
    port: 8001
  planner:
    app: myapp.planner:create_app
    port: 8002
""",
    )

    assert load_project(path) == [
        ProjectAgent("research-agent", "myapp.research:create_app", 8001),
        ProjectAgent("planner", "myapp.planner:create_app", 8002),
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("agents: {}", "must list at least one agent"),
        ("[]", "must be a mapping with one key, 'agents'"),
        ("agents: {a: {app: m:f, port: 1}}\nextra: 1", "one key, 'agents'"),
        ("agents: {Research: {app: m:f, port: 1}}", "lowercase letters"),
        ("agents: {a: {app: m:f, port: 1, url: x}}", "exactly the keys"),
        ("agents: {a: {app: m.Class, port: 1}}", "must be module:function"),
        ("agents: {a: {app: m:f, port: true}}", "from 1 to 65535"),
        ("agents: {a: {app: m:f, port: 70000}}", "from 1 to 65535"),
        (
            "agents: {a: {app: m:f, port: 1}, b: {app: m:g, port: 1}}",
            "agents 'a' and 'b' both use port 1",
        ),
        ("agents: [", "is not valid YAML at line"),
    ],
)
def test_refuses_an_invalid_project(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(DefinitionError, match=message):
        load_project(write(tmp_path, text))


def test_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DefinitionError, match="cannot be read"):
        load_project(tmp_path / "agentframe.yaml")
