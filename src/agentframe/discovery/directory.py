"""A directory that combines agent sources and returns validated Agent Cards."""

import asyncio
import logging
import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from time import monotonic
from types import TracebackType
from typing import Self

import httpx
from a2a.types import AgentCard

from agentframe.discovery.cards import ValidatedCard, fetch_card
from agentframe.discovery.registry import AgentQuery, AgentReference, AgentRegistry
from agentframe.errors import DefinitionError, RemoteAgentError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredAgent:
    """An agent found in an ``AgentDirectory``, with its validated card.

    A discovered agent is not proof that it is healthy, trusted, or that a
    caller may use it.

    Args:
        reference: The source entry the agent was found through.
        card: The agent's validated official SDK Agent Card.
        interface_url: The card's JSON-RPC A2A 1.0 interface URL. It always
            has the same origin (scheme, host and port) as ``reference.card_url``.
        stale: ``True`` only when ``allow_stale=True`` served this card after
            a failed refresh.
    """

    reference: AgentReference
    card: AgentCard
    interface_url: str
    stale: bool = False


@dataclass(frozen=True)
class _CachedCard:
    validated: ValidatedCard
    fetched_at: float


class AgentDirectory:
    """Agents from one or more sources, with their Agent Cards fetched and cached.

    Sources are read on every ``get``, so their edits apply at once; cards are
    cached per card URL for ``card_ttl_seconds`` and refetched on the next
    ``get`` after that. Nothing is read or fetched when the directory is
    constructed, and nothing runs in the background.

    Cards are fetched without credentials and never with the current caller's
    identity, so every caller sees the same cached card. Card URLs must use
    https, except to loopback hosts or hosts listed in ``allow_http_hosts``.

    Use it as an async context manager::

        async with AgentDirectory(sources=[FileAgentRegistry("agents.yaml")]) as d:
            found = await d.get("research-agent")

    Args:
        sources: The registries to look agents up in. A name listed by more
            than one source is a configuration error.
        card_ttl_seconds: How long a fetched card stays fresh; ``0`` refetches
            on every ``get``. Defaults to 300.
        allow_stale: Serve the previous card, marked ``stale``, when a refresh
            fails, instead of raising. Defaults to ``False``.
        allow_http_hosts: Non-loopback hosts whose cards may be fetched over
            plain http, for development only.
        http_client: The client to fetch cards with. When given, it stays
            yours: the directory never closes it, and it must not carry
            credentials in its default headers or cookies. When omitted, the
            directory creates one on ``async with`` and closes it on exit.

    Raises:
        DefinitionError: ``sources`` is empty or not registries,
            ``card_ttl_seconds`` is negative or not finite, or
            ``allow_http_hosts`` is a single string.
    """

    def __init__(
        self,
        *,
        sources: Sequence[AgentRegistry],
        card_ttl_seconds: float = 300,
        allow_stale: bool = False,
        allow_http_hosts: Collection[str] = (),
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._sources = _checked_sources(sources)
        self._card_ttl_seconds = _checked_ttl(card_ttl_seconds)
        self._allow_stale = allow_stale
        self._allow_http_hosts = _checked_hosts(allow_http_hosts)
        self._client = http_client
        self._owns_client = False
        self._cards: dict[str, _CachedCard] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> Self:
        """Create the HTTP client, unless one was supplied.

        Raises:
            DefinitionError: The directory is already open.
        """
        if self._owns_client:
            raise DefinitionError("This AgentDirectory is already open.")
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False)
            self._owns_client = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the HTTP client if the directory created it; keep the cache."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    async def get(self, name: str) -> DiscoveredAgent:
        """Return an agent and its validated card, from the cache while fresh.

        A stale or missing card is fetched again. Concurrent calls for the
        same card share one fetch.

        Args:
            name: The agent's name in one of the sources.

        Returns:
            The agent's reference, card and interface URL.

        Raises:
            DefinitionError: No source lists the agent, more than one does, a
                source cannot be read, or the directory needs ``async with``.
            RemoteAgentError: The card cannot be fetched or is invalid or
                refused, and no stale card may be served.
        """
        reference = await self._find_reference(name)
        async with self._lock(reference.card_url):
            cached = self._cards.get(reference.card_url)
            if cached is not None and self._is_fresh(cached):
                return _discovered(reference, cached.validated)
            return await self._fetch(reference, cached)

    async def refresh(self, name: str) -> DiscoveredAgent:
        """Fetch an agent's card again, even if the cached one is fresh.

        A failed refresh always raises and leaves the cached card unchanged;
        ``allow_stale`` does not apply.

        Args:
            name: The agent's name in one of the sources.

        Returns:
            The agent's reference and newly fetched card.

        Raises:
            DefinitionError: As for ``get``.
            RemoteAgentError: The card cannot be fetched or is invalid or refused.
        """
        reference = await self._find_reference(name)
        async with self._lock(reference.card_url):
            return await self._fetch(reference, previous=None)

    async def _find_reference(self, name: str) -> AgentReference:
        # Listing by name tells "not listed" (empty) apart from a source
        # failure (raises), which a registry's get cannot.
        query = AgentQuery(names=[name])
        found: list[tuple[AgentRegistry, AgentReference]] = []
        for source in self._sources:
            found += [(source, reference) for reference in await source.list(query)]
        if not found:
            raise DefinitionError(f"No agent source lists an agent named '{name}'.")
        if len(found) > 1:
            first, second = found[0][0], found[1][0]
            raise DefinitionError(
                f"Agent '{name}' is listed by more than one source: "
                f"{first!r} and {second!r}."
            )
        return found[0][1]

    async def _fetch(
        self, reference: AgentReference, previous: _CachedCard | None
    ) -> DiscoveredAgent:
        if self._client is None:
            raise DefinitionError(
                "Open the AgentDirectory with 'async with' before using it, "
                "or pass http_client=."
            )
        try:
            validated = await fetch_card(
                self._client, reference, self._allow_http_hosts
            )
        except RemoteAgentError as error:
            if previous is None or not self._allow_stale:
                raise
            logger.warning(
                "Serving a stale card for agent '%s': %s", reference.name, error
            )
            return _discovered(reference, previous.validated, stale=True)
        self._cards[reference.card_url] = _CachedCard(validated, monotonic())
        return _discovered(reference, validated)

    def _is_fresh(self, cached: _CachedCard) -> bool:
        return monotonic() - cached.fetched_at < self._card_ttl_seconds

    def _lock(self, card_url: str) -> asyncio.Lock:
        return self._locks.setdefault(card_url, asyncio.Lock())


def _discovered(
    reference: AgentReference, validated: ValidatedCard, *, stale: bool = False
) -> DiscoveredAgent:
    return DiscoveredAgent(reference, validated.card, validated.interface_url, stale)


def _checked_sources(sources: Sequence[AgentRegistry]) -> tuple[AgentRegistry, ...]:
    checked = tuple(sources)
    if not checked or not all(isinstance(s, AgentRegistry) for s in checked):
        raise DefinitionError(
            "AgentDirectory sources must be a non-empty list of AgentRegistry."
        )
    return checked


def _checked_ttl(card_ttl_seconds: float) -> float:
    if (
        isinstance(card_ttl_seconds, bool)
        or not isinstance(card_ttl_seconds, int | float)
        or not math.isfinite(card_ttl_seconds)
        or card_ttl_seconds < 0
    ):
        raise DefinitionError(
            "AgentDirectory card_ttl_seconds must be a finite number of "
            "seconds, zero or more."
        )
    return float(card_ttl_seconds)


def _checked_hosts(allow_http_hosts: Collection[str]) -> frozenset[str]:
    # A bare string would otherwise be read as one host per character.
    if isinstance(allow_http_hosts, str):
        raise DefinitionError(
            "AgentDirectory allow_http_hosts must be a list of host names, "
            "not a single string."
        )
    return frozenset(host.lower() for host in allow_http_hosts)
