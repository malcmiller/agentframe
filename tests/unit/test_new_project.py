"""``agentframe new``: names, generated files, conflicts and the command."""

from pathlib import Path

import pytest

from agentframe import (
    AgentQuery,
    AgentReference,
    DefinitionError,
    FileAgentRegistry,
)
from agentframe.cli import main
from agentframe.new_project import (
    MAX_NAME_LENGTH,
    agent_project,
    fleet_project,
    names,
    write_project,
)
from agentframe.project import load_project


def test_a_name_converts_to_package_class_and_variable_forms() -> None:
    forms = names("customer-support2", kind="project")

    assert forms.package == "customer_support2"
    assert forms.class_name == "CustomerSupport2"
    assert forms.env_prefix == "CUSTOMER_SUPPORT2"


@pytest.mark.parametrize(
    "name",
    [
        "Support",
        "2support",
        "support-",
        "support--bot",
        "support_bot",
        "",
        "a" * (MAX_NAME_LENGTH + 1),
    ],
)
def test_a_name_must_be_lowercase_words_joined_by_hyphens(name: str) -> None:
    with pytest.raises(DefinitionError, match="lowercase letters and digits"):
        names(name, kind="project")


@pytest.mark.parametrize("name", ["class", "match"])
def test_a_name_cannot_be_a_python_keyword(name: str) -> None:
    with pytest.raises(DefinitionError, match="Python keyword"):
        names(name, kind="agent")


@pytest.mark.parametrize("name", ["json", "agentframe", "pytest", "tests"])
def test_a_project_name_cannot_hide_a_package_it_needs(name: str) -> None:
    with pytest.raises(DefinitionError, match="would hide the Python package"):
        agent_project(name)


@pytest.mark.parametrize("name", ["ask", "invoke", "agent", "skill", "service"])
def test_an_agent_name_cannot_clash_with_an_agentframe_name(name: str) -> None:
    with pytest.raises(DefinitionError, match="clashes with an AgentFrame name"):
        fleet_project("newsroom", [name])


@pytest.mark.parametrize("name", ["agent", "agent-app", "model", "scripted-model"])
def test_an_agent_project_cannot_hide_an_agentframe_name(name: str) -> None:
    with pytest.raises(DefinitionError, match="clashes with the AgentFrame name"):
        agent_project(name)


def test_the_planner_name_is_taken_only_with_the_planner() -> None:
    assert "newsroom/planner.py" in fleet_project("newsroom", ["planner"])

    with pytest.raises(DefinitionError, match="taken by the planner"):
        fleet_project("newsroom", ["planner"], planner=True)


@pytest.mark.parametrize(
    ("specialists", "message"),
    [([], "at least one"), (["research", "research"], "must be different")],
)
def test_specialists_must_be_named_once_each(
    specialists: list[str], message: str
) -> None:
    with pytest.raises(DefinitionError, match=message):
        fleet_project("newsroom", specialists)


def test_an_agent_project_has_its_agent_tests_and_readme() -> None:
    files = agent_project("support-bot")

    assert sorted(files) == [
        ".gitignore",
        "README.md",
        "pyproject.toml",
        "support_bot/__init__.py",
        "support_bot/app.py",
        "tests/test_app.py",
    ]
    assert "class SupportBot(Agent):" in files["support_bot/app.py"]
    assert 'API_KEY_VARIABLE = "SUPPORT_BOT_API_KEY"' in files["support_bot/app.py"]


def test_a_fleet_lists_its_agents_for_agentframe_dev(tmp_path: Path) -> None:
    files = fleet_project("newsroom", ["research", "fact-check"], planner=True)
    write_project(tmp_path, files)

    agents = load_project(tmp_path / "agentframe.yaml")

    assert [(agent.name, agent.app, agent.port) for agent in agents] == [
        ("research", "newsroom.research:create_app", 8001),
        ("fact-check", "newsroom.fact_check:create_app", 8002),
        ("planner", "newsroom.planner:create_app", 8000),
    ]


async def test_a_fleet_registry_lists_its_specialists(tmp_path: Path) -> None:
    write_project(tmp_path, fleet_project("newsroom", ["research"], planner=True))

    registry = FileAgentRegistry(tmp_path / "agents.yaml")

    references = await registry.list(AgentQuery())

    assert references == [
        AgentReference(
            name="research",
            card_url="http://127.0.0.1:8001/.well-known/agent-card.json",
        )
    ]


def test_a_fleet_without_a_planner_has_no_planner_files() -> None:
    files = fleet_project("newsroom")

    assert "newsroom/planner.py" not in files
    assert "tests/test_planner.py" not in files
    assert {"newsroom/research.py", "newsroom/writer.py"} <= set(files)
    assert "planner" not in files["agentframe.yaml"]


def test_existing_files_are_listed_and_nothing_is_written(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("mine")

    with pytest.raises(DefinitionError, match=r"README\.md") as raised:
        write_project(tmp_path, agent_project("support-bot"))

    assert "--force" in str(raised.value)
    assert (tmp_path / "README.md").read_text() == "mine"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["README.md"]


def test_force_overwrites_existing_files_and_keeps_others(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("mine")
    (tmp_path / "notes.txt").write_text("kept")

    write_project(tmp_path, agent_project("support-bot"), force=True)

    assert (tmp_path / "README.md").read_text().startswith("# support-bot")
    assert (tmp_path / "notes.txt").read_text() == "kept"


def test_a_file_in_place_of_the_folder_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "support-bot"
    target.write_text("")

    with pytest.raises(DefinitionError, match="is not a folder"):
        write_project(target, agent_project("support-bot"))


def test_a_file_in_place_of_a_subfolder_is_refused_before_writing(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").write_text("")

    with pytest.raises(DefinitionError, match="is not a folder"):
        write_project(tmp_path, agent_project("support-bot"))

    assert sorted(path.name for path in tmp_path.iterdir()) == ["tests"]


def test_the_command_creates_a_project_in_a_folder_named_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(["new", "fleet", "newsroom", "--planner", "--agents", "a,b"])

    assert code == 0
    assert (tmp_path / "newsroom" / "newsroom" / "planner.py").is_file()
    assert (tmp_path / "newsroom" / "newsroom" / "b.py").is_file()
    assert "uv sync" in capsys.readouterr().out


def test_the_command_reports_conflicts_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = str(tmp_path)
    assert main(["new", "agent", "support-bot", "--directory", directory]) == 0

    code = main(["new", "agent", "support-bot", "--directory", directory])

    assert code == 1
    assert "Not overwriting existing files" in capsys.readouterr().err
