"""``FoundryAgentRegistry``: the A2A-enabled agents in a Microsoft Foundry project.

Reads the Foundry project agents API through the official ``azure-ai-projects``
package. Needs the optional extra: ``pip install agentframe-py[foundry]``.
"""

import builtins
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit

try:
    from azure.ai.projects.aio import AIProjectClient
    from azure.ai.projects.models import AgentDetails
    from azure.core.credentials_async import AsyncTokenCredential
    from azure.core.exceptions import AzureError, ResourceNotFoundError
    from azure.core.pipeline.transport import AsyncHttpTransport
except ImportError as error:
    raise ImportError(
        "FoundryAgentRegistry needs the optional 'azure-ai-projects' package; "
        "install it with: pip install 'agentframe-py[foundry]'"
    ) from error

from agentframe.discovery.registry import AgentQuery, AgentReference, AgentRegistry
from agentframe.errors import DefinitionError
from agentframe.hosting.host import is_plain_http_url

# The Entra scope for Foundry project and agent endpoint access.
FOUNDRY_SCOPE = "https://ai.azure.com/.default"

# Foundry publishes the GA A2A 1.0 card here for every agent with incoming A2A
# enabled; the same agents also serve the 0.3 preview, which is never used.
CARD_PATH = "endpoint/protocols/a2a/agentCard/v1.0"

# The largest page the agents API allows, so large projects need fewer calls.
_PAGE_SIZE = 100

# azure-core otherwise waits 300 seconds to connect and between reads. Each
# attempt gets the same 10 seconds as a card fetch. With azure-core's 3 default
# retries and their backoff, an unreachable project fails a request in about
# 45 seconds rather than minutes, and ``list`` sends its follow-up ``get``
# calls one after another. The read timeout is the longest wait between bytes,
# not the whole reply.
REQUEST_TIMEOUT_SECONDS = 10.0
_TIMEOUTS: dict[str, Any] = {
    "connection_timeout": REQUEST_TIMEOUT_SECONDS,
    "read_timeout": REQUEST_TIMEOUT_SECONDS,
}


class FoundryAgentRegistry(AgentRegistry):
    """The agents in a Foundry project that have incoming A2A enabled.

    Every call reads the project's agents again through the Foundry project
    agents API, following every page, then reads each queried agent's
    details, since listed agents omit their endpoint. Only agents whose
    endpoint enables the A2A protocol are listed; each maps to its A2A 1.0
    Agent Card URL,
    ``{project_endpoint}/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0``.
    The registry is read-only: it never enables A2A, publishes or deploys.
    Each request waits at most 10 seconds to connect and 10 seconds between
    reads, whichever transport sends it.

    Foundry cards require Microsoft Entra ID, so ``card_headers`` returns a
    bearer token from ``credential`` for ``AgentDirectory`` to send with card
    requests to this project. The token is this registry's own identity,
    never the current caller's.

    Args:
        project_endpoint: The project endpoint,
            ``https://{account}.services.ai.azure.com/api/projects/{project}``.
            This is not a model inference URL.
        credential: The Entra credential for listing agents and fetching their
            cards, for example ``azure.identity.aio.DefaultAzureCredential()``.
            It stays yours: the registry never closes it.
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
        self._project_endpoint = _checked_endpoint(project_endpoint)
        self._credential = credential
        self._transport = transport
        self._source = f"Foundry project {self._project_endpoint}"

    def __repr__(self) -> str:
        return f"FoundryAgentRegistry(project_endpoint={self._project_endpoint!r})"

    async def get(self, agent_id: str) -> AgentReference:
        """Return the reference for one A2A-enabled agent.

        Args:
            agent_id: The agent's name in the Foundry project.

        Returns:
            The agent's reference.

        Raises:
            DefinitionError: The project cannot be read, has no such agent, or
                the agent does not have incoming A2A enabled.
        """
        try:
            async with self._client() as client:
                details = await client.agents.get(agent_id, **_TIMEOUTS)
        except ResourceNotFoundError:
            raise DefinitionError(
                f"{self._source} does not list an agent named '{agent_id}'."
            ) from None
        except AzureError as error:
            raise self._read_error(error) from None
        if not _a2a_enabled(details):
            raise DefinitionError(
                f"{self._source} agent '{agent_id}' does not have incoming A2A enabled."
            )
        return self._reference(details.name)

    async def list(self, query: AgentQuery) -> builtins.list[AgentReference]:
        """Return the matching A2A-enabled agents, reading every page.

        Args:
            query: Which agents to return.

        Returns:
            The matching references; empty only when the project has none.

        Raises:
            DefinitionError: Any page of the project cannot be read.
        """
        references = []
        try:
            async with self._client() as client:
                # Listed agents omit their endpoint, so each wanted agent's
                # details are read with one more call.
                names = [
                    summary.name
                    async for summary in client.agents.list(
                        limit=_PAGE_SIZE, **_TIMEOUTS
                    )
                    if _wanted(summary.name, query)
                ]
                for name in names:
                    try:
                        details = await client.agents.get(name, **_TIMEOUTS)
                    except ResourceNotFoundError:
                        continue  # deleted since the listing
                    if _a2a_enabled(details):
                        references.append(self._reference(details.name))
        except AzureError as error:
            raise self._read_error(error) from None
        return references

    async def card_headers(self, reference: AgentReference) -> Mapping[str, str]:
        """Return a bearer token from this registry's credential for the card.

        Args:
            reference: An agent this registry listed.

        Returns:
            An ``Authorization`` header for ``https://ai.azure.com/.default``.

        Raises:
            DefinitionError: ``reference`` is not a card in this project, or
                the credential cannot provide a token.
        """
        if not reference.card_url.startswith(f"{self._project_endpoint}/agents/"):
            raise DefinitionError(
                f"Agent '{reference.name}' is not a card in {self._source}, so "
                "its credential is not sent there."
            )
        try:
            token = await self._credential.get_token(FOUNDRY_SCOPE)
        except AzureError as error:
            raise DefinitionError(
                f"{self._source} credential could not get a token: "
                f"{type(error).__name__}."
            ) from None
        return {"Authorization": f"Bearer {token.token}"}

    def _client(self) -> AIProjectClient:
        # A client per call keeps no connections open between calls, so the
        # registry has nothing to close.
        if self._transport is None:
            return AIProjectClient(
                endpoint=self._project_endpoint, credential=self._credential
            )
        return AIProjectClient(
            endpoint=self._project_endpoint,
            credential=self._credential,
            transport=self._transport,
        )

    def _reference(self, name: str) -> AgentReference:
        card_url = f"{self._project_endpoint}/agents/{quote(name, safe='')}/{CARD_PATH}"
        try:
            return AgentReference(name=name, card_url=card_url)
        except DefinitionError as error:
            raise DefinitionError(f"{self._source}: {error}") from None

    def _read_error(self, error: AzureError) -> DefinitionError:
        # Only the status and class are kept: service messages can quote
        # request details.
        status = getattr(error, "status_code", None)
        reason = f"HTTP {status}" if status is not None else type(error).__name__
        return DefinitionError(f"{self._source} cannot be read: {reason}.")


def _checked_endpoint(project_endpoint: str) -> str:
    endpoint: object = project_endpoint
    if (
        not isinstance(endpoint, str)
        or not is_plain_http_url(endpoint)
        or urlsplit(endpoint).scheme != "https"
    ):
        raise DefinitionError(
            "FoundryAgentRegistry project_endpoint must be an absolute https URL "
            "without credentials, query or fragment."
        )
    return endpoint.rstrip("/")


def _a2a_enabled(details: AgentDetails) -> bool:
    endpoint = details.agent_endpoint
    if endpoint is None:
        return False
    # The live API lists enabled protocols as ``protocols: ["a2a", ...]``, a
    # field azure-ai-projects 2.11.0 does not model; SDK models are mappings
    # over the raw JSON, so it is read from there. ``protocol_configuration``
    # is the shape the SDK models and is still accepted.
    protocols = endpoint.get("protocols")
    if isinstance(protocols, list) and "a2a" in protocols:
        return True
    configuration = endpoint.protocol_configuration
    return configuration is not None and configuration.a2a is not None


def _wanted(name: str, query: AgentQuery) -> bool:
    return query.names is None or name in query.names
