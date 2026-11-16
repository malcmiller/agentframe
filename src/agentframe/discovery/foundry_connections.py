"""``FoundryA2AToolRegistry``: the A2A connections in a Microsoft Foundry project.

Reads the Foundry project connections API through the official
``azure-ai-projects`` package. Needs the optional extra:
``pip install agentframe-py[foundry]``.
"""

import builtins
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

try:
    from azure.ai.projects.aio import AIProjectClient
    from azure.ai.projects.models import Connection, ConnectionType
    from azure.core.credentials_async import AsyncTokenCredential
    from azure.core.exceptions import AzureError, ResourceNotFoundError
    from azure.core.pipeline.transport import AsyncHttpTransport
except ImportError as error:
    raise ImportError(
        "FoundryA2AToolRegistry needs the optional 'azure-ai-projects' package; "
        "install it with: pip install 'agentframe-py[foundry]'"
    ) from error

from agentframe.discovery.foundry import (
    REQUEST_TIMEOUTS,
    bearer_headers,
    checked_project_endpoint,
    project_client,
    read_error,
)
from agentframe.discovery.registry import AgentQuery, AgentReference, AgentRegistry
from agentframe.errors import DefinitionError
from agentframe.urls import is_plain_http_url

# The connection metadata key Foundry stores an A2A connection's card path
# under, relative to its target (for example ``/agentCard/v0.3``).
CARD_PATH_KEY = "AgentCardPath"

# The A2A specification's well-known card location, appended to the target
# like Foundry's own A2A tool does when no card path is set.
DEFAULT_CARD_PATH = "/.well-known/agent-card.json"

# Foundry documents a Foundry agent's target as its A2A base path and resolves
# that agent's card itself when no card path is set; it serves the A2A 1.0
# card under this suffix.
FOUNDRY_AGENT_CARD_PATH = "/agentCard/v1.0"
_FOUNDRY_AGENT_TARGET = re.compile(
    r"/api/projects/[^/]+/agents/[^/]+/endpoint/protocols/a2a"
)


class FoundryA2AToolRegistry(AgentRegistry):
    """The remote agents a Foundry project is connected to over A2A.

    Every call reads the project's connections of type ``RemoteA2A`` again
    through the Foundry project connections API, following every page. These
    are the connections Foundry agents use as their A2A tool. Each maps to an
    ``AgentReference`` named after the connection, with the connection's
    target joined with its card path (the connection metadata's
    ``AgentCardPath``) as its Agent Card URL. Without a card path the card is
    at the target plus ``/.well-known/agent-card.json``, except that a target
    that is a Foundry agent's A2A base path
    (``.../agents/{agent}/endpoint/protocols/a2a``) gets that agent's A2A 1.0
    card, ``.../endpoint/protocols/a2a/agentCard/v1.0``.

    The registry is read-only and never reads or uses the credentials stored
    on a connection: discovery grants no authority, so configure calls to
    these agents with ``remotes=`` as usual. Each request waits at most 10
    seconds to connect and 10 seconds between reads.

    ``card_headers`` returns a bearer token from ``credential`` only for card
    URLs inside this project; every other card is fetched without one.

    Args:
        project_endpoint: The project endpoint,
            ``https://{account}.services.ai.azure.com/api/projects/{project}``.
        credential: The Entra credential for listing connections and fetching
            cards inside this project, for example
            ``azure.identity.aio.DefaultAzureCredential()``. It stays yours:
            the registry never closes it.
        transport: The ``azure-core`` transport to send requests with, for
            example a test double or an outbound proxy. Defaults to
            ``azure-core``'s own aiohttp transport. The registry's timeouts
            are passed with every request, overriding the transport's own.

    Raises:
        DefinitionError: ``project_endpoint`` is not an absolute https URL
            without credentials, query or fragment.
    """

    def __init__(
        self,
        *,
        project_endpoint: str,
        credential: AsyncTokenCredential,
        transport: AsyncHttpTransport[Any, Any] | None = None,
    ) -> None:
        self._project_endpoint = checked_project_endpoint(
            project_endpoint, "FoundryA2AToolRegistry"
        )
        self._credential = credential
        self._transport = transport
        self._source = f"Foundry project {self._project_endpoint}"

    def __repr__(self) -> str:
        return f"FoundryA2AToolRegistry(project_endpoint={self._project_endpoint!r})"

    async def get(self, agent_id: str) -> AgentReference:
        """Return the reference for one A2A connection.

        Args:
            agent_id: The connection's name in the Foundry project.

        Returns:
            The connection's reference.

        Raises:
            DefinitionError: The project cannot be read, has no such
                connection, the connection is not an A2A connection, or its
                target is missing or unsafe.
        """
        try:
            async with self._client() as client:
                connection = await client.connections.get(agent_id, **REQUEST_TIMEOUTS)
        except ResourceNotFoundError:
            raise DefinitionError(
                f"{self._source} does not have a connection named '{agent_id}'."
            ) from None
        except AzureError as error:
            raise read_error(self._source, error) from None
        if not _is_a2a(connection):
            raise DefinitionError(
                f"{self._source} connection '{agent_id}' is not an A2A connection."
            )
        return self._reference(connection)

    async def list(self, query: AgentQuery) -> builtins.list[AgentReference]:
        """Return the matching A2A connections, reading every page.

        Args:
            query: Which connections to return, by name.

        Returns:
            The matching references; empty only when the project has none.

        Raises:
            DefinitionError: Any page of the project cannot be read, or a
                matching A2A connection's target is missing or unsafe.
        """
        try:
            async with self._client() as client:
                connections = [
                    connection
                    async for connection in client.connections.list(
                        connection_type=ConnectionType.REMOTE_A2A, **REQUEST_TIMEOUTS
                    )
                ]
        except AzureError as error:
            raise read_error(self._source, error) from None
        return [
            self._reference(connection)
            for connection in connections
            # The service filters by type; the check guards against a
            # service that ignores the filter.
            if _is_a2a(connection)
            and (query.names is None or connection.name in query.names)
        ]

    async def card_headers(self, reference: AgentReference) -> Mapping[str, str]:
        """Return a bearer token for cards inside this project, else nothing.

        Args:
            reference: An agent this registry listed.

        Returns:
            An ``Authorization`` header for ``https://ai.azure.com/.default``
            when the card URL is inside this project; otherwise no headers.

        Raises:
            DefinitionError: The credential cannot provide a token.
        """
        if not reference.card_url.startswith(f"{self._project_endpoint}/agents/"):
            return {}
        return await bearer_headers(self._credential, self._source)

    def _client(self) -> AIProjectClient:
        return project_client(self._project_endpoint, self._credential, self._transport)

    def _reference(self, connection: Connection) -> AgentReference:
        name = connection.name
        target: object = connection.get("target")
        # The message does not echo the target, in case it holds credentials.
        if not isinstance(target, str) or not is_plain_http_url(target):
            raise DefinitionError(
                f"{self._source} A2A connection '{name}' must have a target that "
                "is an absolute http(s) URL without credentials, query or fragment."
            )
        try:
            return AgentReference(
                name=name, card_url=_card_url(target, connection.get("metadata"))
            )
        except DefinitionError as error:
            raise DefinitionError(f"{self._source}: {error}") from None


def _is_a2a(connection: Connection) -> bool:
    return bool(connection.get("type") == ConnectionType.REMOTE_A2A)


def _card_url(target: str, metadata: object) -> str:
    base = target.rstrip("/")
    # Foundry keeps a connection's card path in its metadata, relative to the
    # target; AgentReference then refuses a path that adds a query or fragment.
    card_path = metadata.get(CARD_PATH_KEY) if isinstance(metadata, Mapping) else None
    if isinstance(card_path, str) and card_path.strip("/"):
        return f"{base}/{card_path.lstrip('/')}"
    if _FOUNDRY_AGENT_TARGET.fullmatch(urlsplit(base).path):
        return base + FOUNDRY_AGENT_CARD_PATH
    return base + DEFAULT_CARD_PATH
