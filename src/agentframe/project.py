"""``agentframe.yaml``: the named agents of a project, their apps and ports.

The file lists each agent's app factory, in the same ``module:function`` form
``agentframe run`` takes, and the port it listens on::

    agents:
      research-agent:
        app: myapp.research:create_app
        port: 8001
      planner:
        app: myapp.planner:create_app
        port: 8002

``agentframe dev`` starts the agents in the order listed.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentframe.errors import DefinitionError

PROJECT_FILE = "agentframe.yaml"

_MAX_FILE_BYTES = 1_048_576

# Names become registry names, environment-variable pairs ("name=key") and,
# later, container service names, so they stay simple.
_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_APP = re.compile(r"[\w.]+:\w+")


@dataclass(frozen=True)
class ProjectAgent:
    """One agent listed in ``agentframe.yaml``.

    Args:
        name: The agent's name, which other agents find it by: lowercase
            letters, digits and hyphens.
        app: The ``module:function`` that returns its ``AgentApp``.
        port: The port it listens on.
    """

    name: str
    app: str
    port: int


def load_project(path: str | os.PathLike[str]) -> list[ProjectAgent]:
    """Read the agents listed in an ``agentframe.yaml`` file, in file order.

    Args:
        path: The file to read.

    Returns:
        The listed agents.

    Raises:
        DefinitionError: The file cannot be read, is not valid YAML, or does
            not list at least one agent with a valid name, ``app`` and
            ``port``, or two agents share a port.
    """
    source = f"Project file '{path}'"
    document = _read_yaml(Path(path), source)
    if not isinstance(document, dict) or set(document) != {"agents"}:
        raise DefinitionError(f"{source} must be a mapping with one key, 'agents'.")
    entries = document["agents"]
    if not isinstance(entries, dict) or not entries:
        raise DefinitionError(f"{source} must list at least one agent under 'agents'.")
    agents = [_agent(name, entry, source) for name, entry in entries.items()]
    _check_unique_ports(agents, source)
    return agents


def _read_yaml(path: Path, source: str) -> object:
    try:
        with path.open("rb") as file:
            data = file.read(_MAX_FILE_BYTES + 1)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise DefinitionError(f"{source} cannot be read: {reason}.") from None
    if len(data) > _MAX_FILE_BYTES:
        raise DefinitionError(f"{source} is larger than {_MAX_FILE_BYTES} bytes.")
    try:
        return yaml.safe_load(data)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise DefinitionError(f"{source} is not valid YAML{where}.") from None


def _agent(name: object, entry: object, source: str) -> ProjectAgent:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise DefinitionError(
            f"{source}: agent name {name!r} must use lowercase letters, digits "
            "and hyphens, for example research-agent."
        )
    if not isinstance(entry, dict) or set(entry) != {"app", "port"}:
        raise DefinitionError(
            f"{source}: agent '{name}' must have exactly the keys 'app' and 'port'."
        )
    app, port = entry["app"], entry["port"]
    if not isinstance(app, str) or not _APP.fullmatch(app):
        raise DefinitionError(
            f"{source}: agent '{name}' app must be module:function, for "
            "example myapp.research:create_app."
        )
    # bool is an int subclass, so "port: true" would otherwise pass as 1.
    if type(port) is not int or not 1 <= port <= 65535:
        raise DefinitionError(
            f"{source}: agent '{name}' port must be a whole number from 1 to 65535."
        )
    return ProjectAgent(name=name, app=app, port=port)


def _check_unique_ports(agents: list[ProjectAgent], source: str) -> None:
    used: dict[int, str] = {}
    for agent in agents:
        if agent.port in used:
            raise DefinitionError(
                f"{source}: agents '{used[agent.port]}' and '{agent.name}' "
                f"both use port {agent.port}."
            )
        used[agent.port] = agent.name
