"""Read-only registries that map agent names to Agent Card URLs."""

import asyncio
import builtins
import os
from abc import ABC, abstractmethod
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentframe.errors import DefinitionError
from agentframe.hosting.host import is_plain_http_url

_MAX_FILE_BYTES = 1_048_576


@dataclass(frozen=True)
class AgentReference:
    """One agent a registry lists: its name and where its Agent Card is.

    A reference only says where to look. It does not mean the agent is
    reachable, trusted or that a caller may use it.

    Args:
        name: The agent's name, unique within its registry, for example
            ``"research-agent"``.
        card_url: The absolute http(s) URL of the agent's Agent Card (not its
            A2A endpoint), for example
            ``"https://research.example.com/.well-known/agent-card.json"``.

    Raises:
        DefinitionError: ``name`` is empty, or ``card_url`` is not an absolute
            http(s) URL or contains credentials, a query or a fragment.
    """

    name: str
    card_url: str

    def __post_init__(self) -> None:
        name: object = self.name
        if not isinstance(name, str) or not name.strip():
            raise DefinitionError("An agent name must be a non-empty string.")
        card_url: object = self.card_url
        # The message does not echo the URL, in case it holds credentials.
        if not isinstance(card_url, str) or not is_plain_http_url(card_url):
            raise DefinitionError(
                f"Agent '{name}' card URL must be an absolute http(s) URL "
                "without credentials, query or fragment."
            )


@dataclass(frozen=True, kw_only=True)
class AgentQuery:
    """Which agents to list from a registry.

    Matching on skills and tags happens later, on the agents' cards, so a
    query only filters by name.

    Args:
        names: The agent names to keep. ``None`` (the default) means every
            agent in the source. Names the source does not list are ignored.

    Raises:
        DefinitionError: ``names`` is a single string instead of a collection.
    """

    names: Collection[str] | None = None

    def __post_init__(self) -> None:
        if self.names is None:
            return
        # A bare string would otherwise be read as one name per character.
        if isinstance(self.names, str):
            raise DefinitionError(
                "AgentQuery names must be a list of agent names, not a single string."
            )
        object.__setattr__(self, "names", frozenset(self.names))


class AgentRegistry(ABC):
    """A read-only source of agent references.

    Subclass it to read references from your own source. Implementations
    should do no I/O when constructed and raise on a failed or invalid read
    rather than return an empty result.
    """

    @abstractmethod
    async def get(self, agent_id: str) -> AgentReference:
        """Return the reference for one agent.

        Args:
            agent_id: The agent's name in this registry.

        Returns:
            The agent's reference.

        Raises:
            DefinitionError: The source cannot be read, is invalid or does not
                list the agent.
        """

    @abstractmethod
    async def list(self, query: AgentQuery) -> list[AgentReference]:
        """Return the references matching ``query``, in source order.

        Args:
            query: Which agents to return.

        Returns:
            The matching references; empty only when the source has none.

        Raises:
            DefinitionError: The source cannot be read or is invalid.
        """

    async def card_headers(self, reference: AgentReference) -> Mapping[str, str]:
        """Return extra headers to send when fetching ``reference``'s Agent Card.

        Override it for sources whose cards need the source's own credentials,
        such as a bearer token. ``AgentDirectory`` sends these headers only on
        the request for this card URL, never on redirects, other requests or
        A2A calls, and never adds the current caller's identity. The default
        returns no headers.

        Args:
            reference: An agent this registry listed.

        Returns:
            The header names and values to add to the card request.

        Raises:
            DefinitionError: The source's credentials cannot provide them.
        """
        return {}


class FileAgentRegistry(AgentRegistry):
    """Agents listed in a YAML file of names and Agent Card URLs.

    The file is read again on every call, so edits take effect without a
    restart. It must be a list of ``name`` / ``url`` entries::

        - name: research-agent
          url: https://research.example.com/.well-known/agent-card.json

    Args:
        path: The YAML file. It is not read until the first call.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._source = f"Agent registry file '{self._path}'"

    def __repr__(self) -> str:
        return f"FileAgentRegistry({str(self._path)!r})"

    async def get(self, agent_id: str) -> AgentReference:
        """Return the reference for one agent; see ``AgentRegistry.get``."""
        return _find(await self._read(), agent_id, self._source)

    async def list(self, query: AgentQuery) -> builtins.list[AgentReference]:
        """Return the matching references; see ``AgentRegistry.list``."""
        return _matching(await self._read(), query)

    async def _read(self) -> builtins.list[AgentReference]:
        text = await asyncio.to_thread(self._read_text)
        try:
            entries = yaml.safe_load(text)
        except yaml.YAMLError as error:
            # The parser's message quotes file content, so only the line is kept.
            mark = getattr(error, "problem_mark", None)
            where = f" at line {mark.line + 1}" if mark is not None else ""
            raise DefinitionError(f"{self._source} is not valid YAML{where}.") from None
        return _references(_file_entries(entries, self._source), self._source)

    def _read_text(self) -> str:
        try:
            with self._path.open("rb") as file:
                data = file.read(_MAX_FILE_BYTES + 1)
        except OSError as error:
            reason = error.strerror or type(error).__name__
            raise DefinitionError(f"{self._source} cannot be read: {reason}.") from None
        if len(data) > _MAX_FILE_BYTES:
            raise DefinitionError(
                f"{self._source} is larger than {_MAX_FILE_BYTES} bytes."
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise DefinitionError(f"{self._source} is not UTF-8 text.") from None


class EnvironmentAgentRegistry(AgentRegistry):
    """Agents listed in an environment variable as comma-separated pairs.

    The variable holds ``name=url`` pairs, where each URL is an Agent Card
    URL, for example
    ``research-agent=https://research.example.com/.well-known/agent-card.json,
    review-agent=http://127.0.0.1:8002/.well-known/agent-card.json``. An empty
    value lists no agents. The variable is read again on every call.

    Args:
        variable: The environment variable's name, for example
            ``"AGENTFRAME_AGENTS"``. It is not read until the first call.
    """

    def __init__(self, variable: str) -> None:
        self._variable = variable
        self._source = f"Environment variable {variable}"

    def __repr__(self) -> str:
        return f"EnvironmentAgentRegistry({self._variable!r})"

    async def get(self, agent_id: str) -> AgentReference:
        """Return the reference for one agent; see ``AgentRegistry.get``."""
        return _find(self._read(), agent_id, self._source)

    async def list(self, query: AgentQuery) -> builtins.list[AgentReference]:
        """Return the matching references; see ``AgentRegistry.list``."""
        return _matching(self._read(), query)

    def _read(self) -> builtins.list[AgentReference]:
        value = os.environ.get(self._variable)
        if value is None:
            raise DefinitionError(f"{self._source} is not set.")
        if not value.strip():
            return []
        pairs = []
        for entry in value.split(","):
            name, separator, url = entry.partition("=")
            if not separator:
                raise DefinitionError(
                    f"{self._source} must hold comma-separated name=url pairs; "
                    "found an entry without '='."
                )
            pairs.append((name.strip(), url.strip()))
        return _references(pairs, self._source)


def _file_entries(entries: object, source: str) -> list[tuple[str, str]]:
    if not isinstance(entries, list):
        raise DefinitionError(f"{source} must be a YAML list of name and url entries.")
    pairs = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or set(entry) != {"name", "url"}:
            raise DefinitionError(
                f"{source} entry {position} must have exactly the keys "
                "'name' and 'url'."
            )
        name, url = entry["name"], entry["url"]
        if not isinstance(name, str) or not isinstance(url, str):
            raise DefinitionError(
                f"{source} entry {position} 'name' and 'url' must be strings."
            )
        pairs.append((name.strip(), url.strip()))
    return pairs


def _references(pairs: Iterable[tuple[str, str]], source: str) -> list[AgentReference]:
    references: list[AgentReference] = []
    seen: set[str] = set()
    for name, url in pairs:
        try:
            reference = AgentReference(name=name, card_url=url)
        except DefinitionError as error:
            raise DefinitionError(f"{source}: {error}") from None
        if reference.name in seen:
            raise DefinitionError(
                f"{source}: agent '{reference.name}' is listed more than once."
            )
        seen.add(reference.name)
        references.append(reference)
    return references


def _find(references: list[AgentReference], name: str, source: str) -> AgentReference:
    for reference in references:
        if reference.name == name:
            return reference
    raise DefinitionError(f"{source} does not list an agent named '{name}'.")


def _matching(
    references: list[AgentReference], query: AgentQuery
) -> list[AgentReference]:
    if query.names is None:
        return references
    return [reference for reference in references if reference.name in query.names]
