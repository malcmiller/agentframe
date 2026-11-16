"""``agentframe new``: create a project from the templates shipped in the wheel.

Generation runs once, from templates to ordinary code the user owns and edits:
nothing records that a file was generated, and nothing reads it back. The
templates live in ``agentframe/project_templates`` and use ``string.Template``
placeholders such as ``$package``.

Two kinds of project exist:

- ``agent``: one agent with a skill, ``create_app()``, tests and a README.
- ``fleet``: specialist agents with skills tagged ``content``, optionally a
  planner that finds those skills by tag, the ``agentframe.yaml`` that
  ``agentframe dev`` reads, an ``agents.yaml`` registry and tests.
"""

import keyword
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from string import Template

import agentframe
from agentframe.authoring.agent import Agent
from agentframe.errors import DefinitionError

# Lowercase words joined by single hyphens, starting with a letter, so each
# name converts to a valid Python package, module, class and agentframe.yaml
# name. The length limit keeps every generated line within 88 characters.
_NAME = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
MAX_NAME_LENGTH = 30

DEFAULT_SPECIALISTS = ("research", "writer")
PLANNER = "planner"
PLANNER_PORT = 8000
FIRST_SPECIALIST_PORT = 8001

# Packages a generated project imports, directly or through its tools, which
# a project package of the same name would hide.
_TAKEN_PACKAGES = frozenset(
    {"agentframe", "a2a", "httpx", "mypy", "pydantic", "pytest", "ruff"}
    | {"starlette", "test", "tests", "uvicorn", "yaml"}
)

# AgentFrame names that generated modules import, which a generated agent
# class or module of the same name would hide.
_AGENTFRAME_NAMES = frozenset(agentframe.__all__) | {"ScriptedModel"}


@dataclass(frozen=True)
class Names:
    """The forms of one name that generated code uses.

    Args:
        name: The name as given, such as ``customer-support``.
        package: A Python identifier, such as ``customer_support``.
        class_name: A Python class name, such as ``CustomerSupport``.
        env_prefix: An environment variable prefix, such as
            ``CUSTOMER_SUPPORT``.
    """

    name: str
    package: str
    class_name: str
    env_prefix: str


def names(name: str, *, kind: str) -> Names:
    """Check a project or agent name and return its forms.

    Args:
        name: The name to check.
        kind: ``"project"`` or ``"agent"``, for the error message.

    Returns:
        The name's package, class and environment variable forms.

    Raises:
        DefinitionError: The name is not lowercase words joined by hyphens,
            is too long, or is a Python keyword.
    """
    if len(name) > MAX_NAME_LENGTH or not _NAME.fullmatch(name):
        raise DefinitionError(
            f"The {kind} name '{name}' must be lowercase letters and digits, "
            "words joined by single hyphens, starting with a letter and at "
            f"most {MAX_NAME_LENGTH} characters, such as 'customer-support'."
        )
    package = name.replace("-", "_")
    if keyword.iskeyword(package) or keyword.issoftkeyword(package):
        raise DefinitionError(f"The {kind} name '{name}' is a Python keyword.")
    return Names(
        name=name,
        package=package,
        class_name="".join(word.capitalize() for word in name.split("-")),
        env_prefix=package.upper(),
    )


def agent_project(name: str) -> dict[str, str]:
    """Return the files of an ``agent`` project, by relative path.

    Args:
        name: The project name, which also names its package and agent class.

    Raises:
        DefinitionError: The name is not usable (see ``names``), or the agent
            class it names would hide an AgentFrame name.
    """
    project = _project_names(name)
    # The agent class is named after the project and imported next to
    # AgentFrame's own names, so it cannot reuse one of those.
    if project.class_name in _AGENTFRAME_NAMES:
        raise DefinitionError(
            f"The project name '{name}' clashes with the AgentFrame name "
            f"'{project.class_name}'; choose another name."
        )
    values = {
        "project": project.name,
        "package": project.package,
        "class_name": project.class_name,
        "env_prefix": project.env_prefix,
        "description": "One AgentFrame agent.",
        "agentframe_range": _agentframe_range(),
    }
    package = project.package
    return {
        "README.md": _render("agent/README.md", values),
        "pyproject.toml": _render("common/pyproject.toml", values),
        ".gitignore": _render("common/gitignore", values),
        f"{package}/__init__.py": _package_docstring(project.name),
        f"{package}/app.py": _render("agent/app.py", values),
        "tests/test_app.py": _render("agent/test_app.py", values),
    }


def fleet_project(
    name: str,
    specialists: Sequence[str] = DEFAULT_SPECIALISTS,
    *,
    planner: bool = False,
) -> dict[str, str]:
    """Return the files of a ``fleet`` project, by relative path.

    Args:
        name: The project name, which also names its package.
        specialists: The specialist agents' names, at least one. Each gets a
            module, a skill tagged ``content`` and a port from 8001.
        planner: Add a planner on port 8000 that plans over every registered
            skill tagged ``content``.

    Raises:
        DefinitionError: A name is not usable or is repeated, no specialist is
            named, or a specialist is named ``planner`` alongside the planner.
    """
    project = _project_names(name)
    agents = _specialist_names(specialists, planner=planner)
    values = {
        "project": project.name,
        "package": project.package,
        "description": "A fleet of AgentFrame agents.",
        "agentframe_range": _agentframe_range(),
        "planner_port": str(PLANNER_PORT),
        "first_agent": agents[0].name,
        "first_module": agents[0].package,
    }
    package = project.package
    files = {
        "README.md": _fleet_readme(project, agents, planner=planner),
        "pyproject.toml": _render("common/pyproject.toml", values),
        ".gitignore": _render("common/gitignore", values),
        "agentframe.yaml": _project_file(project, agents, planner=planner),
        "agents.yaml": _registry_file(agents),
        f"{package}/__init__.py": _package_docstring(project.name),
    }
    for port, agent in enumerate(agents, start=FIRST_SPECIALIST_PORT):
        agent_values = values | {
            "name": agent.name,
            "module": agent.package,
            "class_name": agent.class_name,
            "port": str(port),
        }
        module = agent.package
        files[f"{package}/{module}.py"] = _render("fleet/specialist.py", agent_values)
        files[f"tests/test_{module}.py"] = _render(
            "fleet/test_specialist.py", agent_values
        )
    if planner:
        files[f"{package}/planner.py"] = _render("fleet/planner.py", values)
        files["tests/test_planner.py"] = _render("fleet/test_planner.py", values)
    return files


def write_project(
    directory: Path, files: Mapping[str, str], *, force: bool = False
) -> list[Path]:
    """Write a project's files under ``directory``, creating folders as needed.

    Nothing is written when any file already exists, unless ``force`` is set.
    Other files in ``directory`` are left alone.

    Args:
        directory: The project's folder, which may already exist.
        files: The contents of each file, by path relative to ``directory``.
        force: Overwrite files that already exist.

    Returns:
        The written files' paths, in the order of ``files``.

    Raises:
        DefinitionError: ``directory`` or a folder to create is a file, or a
            file exists and ``force`` is not set. The message lists every
            existing file.
    """
    paths = [directory / relative for relative in files]
    folders = {directory} | {path.parent for path in paths}
    for folder in sorted(folders):
        if folder.exists() and not folder.is_dir():
            raise DefinitionError(f"{folder} exists and is not a folder.")
    existing = [path for path in paths if path.exists()]
    if existing and not force:
        listed = ", ".join(str(path) for path in existing)
        raise DefinitionError(
            f"Not overwriting existing files: {listed}. Choose another folder, "
            "or pass --force to overwrite them."
        )
    for path, content in zip(paths, files.values(), strict=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return paths


def _project_names(name: str) -> Names:
    project = names(name, kind="project")
    package = project.package
    if package in _TAKEN_PACKAGES or package in sys.stdlib_module_names:
        raise DefinitionError(
            f"The project name '{name}' would hide the Python package "
            f"'{package}'; choose another name."
        )
    return project


def _specialist_names(specialists: Sequence[str], *, planner: bool) -> list[Names]:
    if not specialists:
        raise DefinitionError("Name at least one specialist agent.")
    if len(set(specialists)) != len(specialists):
        raise DefinitionError("Each specialist agent name must be different.")
    agents = [names(name, kind="agent") for name in specialists]
    for agent in agents:
        _check_specialist(agent, planner=planner)
    return agents


def _check_specialist(agent: Names, *, planner: bool) -> None:
    # The module name is also the skill's method name, which cannot reuse an
    # Agent attribute such as ``ask``, and the module and class are imported
    # next to AgentFrame's own names, so they cannot reuse those either.
    clashes = {agent.package, agent.class_name} & _AGENTFRAME_NAMES
    if hasattr(Agent, agent.package) or clashes:
        raise DefinitionError(
            f"The agent name '{agent.name}' clashes with an AgentFrame name; "
            "choose another name."
        )
    if planner and agent.name == PLANNER:
        raise DefinitionError(
            f"The agent name '{PLANNER}' is taken by the planner; choose "
            "another name, or leave out --planner."
        )


def _agentframe_range() -> str:
    """Return the compatible range of this AgentFrame version, such as
    ``>=0.5.0,<0.6``: before 1.0, a minor release may break the API."""
    major, minor = (int(part) for part in agentframe.__version__.split(".")[:2])
    upper = f"{major}.{minor + 1}" if major == 0 else str(major + 1)
    return f">={agentframe.__version__},<{upper}"


def _render(template: str, values: Mapping[str, str]) -> str:
    source = resources.files("agentframe").joinpath(
        "project_templates", f"{template}.tmpl"
    )
    return Template(source.read_text(encoding="utf-8")).substitute(values)


def _package_docstring(project: str) -> str:
    return f'"""The {project} project\'s agents."""\n'


def _project_file(project: Names, agents: list[Names], *, planner: bool) -> str:
    lines = [
        "# The agents `agentframe dev` starts, in this order: each waits for",
        "# the previous one to be ready. See README.md.",
        "agents:",
    ]
    entries = [
        (agent.name, agent.package, port)
        for port, agent in enumerate(agents, start=FIRST_SPECIALIST_PORT)
    ]
    if planner:
        entries.append((PLANNER, PLANNER, PLANNER_PORT))
    for name, module, port in entries:
        lines += [
            f"  {name}:",
            f"    app: {project.package}.{module}:create_app",
            f"    port: {port}",
        ]
    return "\n".join(lines) + "\n"


def _registry_file(agents: list[Names]) -> str:
    lines = [
        "# The specialists' Agent Cards, for agents you start yourself with",
        "# `agentframe run`. `agentframe dev` writes its own registry instead.",
    ]
    for port, agent in enumerate(agents, start=FIRST_SPECIALIST_PORT):
        lines += [
            f"- name: {agent.name}",
            f"  url: http://127.0.0.1:{port}/.well-known/agent-card.json",
        ]
    return "\n".join(lines) + "\n"


def _fleet_readme(project: Names, agents: list[Names], *, planner: bool) -> str:
    rows = [
        f"| `{project.package}/{agent.package}.py` | The {agent.name} "
        f"specialist, on port {port}. |\n"
        for port, agent in enumerate(agents, start=FIRST_SPECIALIST_PORT)
    ]
    planner_section = ""
    dev_command = "AGENTFRAME_DEV_CLIENT_KEY=dev-only-key uv run agentframe dev\n"
    if planner:
        rows.append(
            f"| `{project.package}/planner.py` | The planner, on port "
            f"{PLANNER_PORT}. |\n"
        )
        planner_section = _render(
            "fleet/planner_section.md",
            {"package": project.package, "planner_port": str(PLANNER_PORT)},
        )
        dev_command = (
            "AGENTFRAME_DEV_CLIENT_KEY=dev-only-key "
            "AGENTFRAME_MODEL_PROVIDER=ollama \\\n"
            "    AGENTFRAME_OLLAMA_MODEL=llama3.2:3b uv run agentframe dev\n"
        )
    return _render(
        "fleet/README.md",
        {
            "project": project.name,
            "agent_rows": "".join(rows),
            "planner_section": planner_section,
            "dev_command": dev_command,
            "env_prefix": project.env_prefix,
        },
    )
