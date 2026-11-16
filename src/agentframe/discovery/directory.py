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
from agentframe.errors import AgentFrameError, DefinitionError, RemoteAgentError

logger = logging.getLogger(__name__)

MAX_CONCURRENT_FETCHES = 8
DEFAULT_MAX_REPLY_BYTES = 8_388_608
"""Twice a host's default ``max_response_bytes``, leaving room for the
task's history and JSON-RPC framing around a result."""


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


# An agent reference and the source that listed it.
type _Listed = tuple[AgentRegistry, AgentReference]


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

    Cards are never fetched with the current caller's identity, so every
    caller sees the same cached card. The only credentials sent are a
    source's own ``card_headers`` (for example ``FoundryAgentRegistry``'s
    bearer token), and only to that card's URL. Card URLs must use https,
    except to loopback hosts or hosts listed in ``allow_http_hosts``.

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
            credentials in its default headers or cookies, nor follow
            redirects when a remote has credentials. When omitted, the
            directory creates one on ``async with`` and closes it on exit.
        max_reply_bytes: The largest A2A reply body a ``Remote`` call reads
            from an agent found here; a larger one fails the call with
            ``RemoteAgentError``. Defaults to 8388608 (8 MiB). Raise it for
            agents whose ``max_response_bytes`` is above the default.

    Raises:
        DefinitionError: ``sources`` is empty or not registries,
            ``card_ttl_seconds`` is negative or not finite,
            ``allow_http_hosts`` is a single string, or ``max_reply_bytes``
            is not a whole number greater than 0.
    """

    def __init__(
        self,
        *,
        sources: Sequence[AgentRegistry],
        card_ttl_seconds: float = 300,
        allow_stale: bool = False,
        allow_http_hosts: Collection[str] = (),
        http_client: httpx.AsyncClient | None = None,
        max_reply_bytes: int = DEFAULT_MAX_REPLY_BYTES,
    ) -> None:
        self._sources = _checked_sources(sources)
        self._card_ttl_seconds = _checked_ttl(card_ttl_seconds)
        self._allow_stale = allow_stale
        self._allow_http_hosts = _checked_hosts(allow_http_hosts)
        self._client = http_client
        self._max_reply_bytes = _checked_max_reply_bytes(max_reply_bytes)
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

    @property
    def is_open(self) -> bool:
        """Whether the directory has an HTTP client to fetch cards with.

        Package-internal: ``AgentApp`` opens a directory that is not open yet.
        A directory given an ``http_client=`` is always open.
        """
        return self._client is not None

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
                source cannot be read or provide its card headers, or the
                directory needs ``async with``.
            RemoteAgentError: The card cannot be fetched or is invalid or
                refused, and no stale card may be served.
        """
        return await self._resolve(*await self._find_reference(name))

    async def find(
        self, *, skill: str | None = None, tags: Collection[str] = ()
    ) -> list[AgentCard]:
        """Return the cards of every listed agent with a matching skill.

        A card matches when one of its advertised skills has the ID ``skill``
        (if given) and carries every tag in ``tags``. Both filters apply to
        the same skill. Every agent of every source is resolved, through the
        cache, with at most eight card fetches at a time, so ``find`` is meant
        for small curated sources; there is no pagination. A match is not
        permission to call the agent.

        Args:
            skill: The skill ID to look for.
            tags: Tags the same skill must all have.

        Returns:
            The matching cards, in source order.

        Raises:
            DefinitionError: No filter is given, ``tags`` is a single string,
                a name is listed by more than one source, a source cannot be
                read, or the directory needs ``async with``.
            RemoteAgentError: Any card cannot be fetched or is invalid or
                refused, and no stale card may be served. A failure is never
                turned into a shorter result.
        """
        wanted_tags = _checked_tags(tags)
        if skill is None and not wanted_tags:
            raise DefinitionError(
                "AgentDirectory.find needs a skill, tags, or both to match on."
            )
        found = await self._resolve_all(await self._list_references())
        return [
            agent.card
            for agent in found
            if _has_matching_skill(agent.card, skill, wanted_tags)
        ]

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
        source, reference = await self._find_reference(name)
        async with self._lock(reference.card_url):
            return await self._fetch(source, reference, previous=None)

    async def _resolve(
        self, source: AgentRegistry, reference: AgentReference
    ) -> DiscoveredAgent:
        async with self._lock(reference.card_url):
            cached = self._cards.get(reference.card_url)
            if cached is not None and self._is_fresh(cached):
                return _discovered(reference, cached.validated)
            return await self._fetch(source, reference, cached)

    async def _resolve_all(self, listed: Sequence[_Listed]) -> list[DiscoveredAgent]:
        slots = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def resolve(
            source: AgentRegistry, reference: AgentReference
        ) -> DiscoveredAgent:
            async with slots:
                return await self._resolve(source, reference)

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(resolve(*entry)) for entry in listed]
        except ExceptionGroup as failures:
            # Callers expect the same single errors as get(); the task group
            # cancels the remaining fetches once one fails.
            raise failures.exceptions[0] from None
        return [task.result() for task in tasks]

    async def resolve_each(
        self, listed: Sequence[_Listed]
    ) -> list[DiscoveredAgent | AgentFrameError]:
        """Resolve every agent, returning each one's card or its failure.

        Package-internal, for the skill catalog, which skips the agents that
        fail instead of failing as a whole. Like ``find``, it resolves
        through the cache with at most eight fetches at a time.

        Returns:
            For each entry of ``listed``, in order: the agent, or the
            ``RemoteAgentError`` (its card could not be fetched or is
            invalid) or ``DefinitionError`` (its source could not provide
            the card headers) it failed with.
        """
        slots = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def resolve(
            source: AgentRegistry, reference: AgentReference
        ) -> DiscoveredAgent | AgentFrameError:
            async with slots:
                try:
                    return await self._resolve(source, reference)
                except (RemoteAgentError, DefinitionError) as error:
                    return error

        self.open_client()  # Not being open is a mistake to raise, not skip.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(resolve(*entry)) for entry in listed]
        return [task.result() for task in tasks]

    async def _list_references(self) -> list[_Listed]:
        return _without_duplicates(
            [(source, await source.list(AgentQuery())) for source in self._sources]
        )

    async def list_available(
        self,
    ) -> tuple[list[_Listed], list[tuple[int, AgentRegistry]]]:
        """List every source's agents, skipping the sources that fail to list.

        Package-internal, for the skill catalog: one failing source must not
        hide the agents of the others.

        Returns:
            The listed agents, in source order, and the sources that failed
            with their 1-based positions.

        Raises:
            DefinitionError: A name is listed by more than one source.
        """
        listed: list[tuple[AgentRegistry, list[AgentReference]]] = []
        failed: list[tuple[int, AgentRegistry]] = []
        for position, source in enumerate(self._sources, start=1):
            try:
                listed.append((source, await source.list(AgentQuery())))
            except Exception:
                failed.append((position, source))
        return _without_duplicates(listed), failed

    async def _find_reference(self, name: str) -> _Listed:
        # Listing by name tells "not listed" (empty) apart from a source
        # failure (raises), which a registry's get cannot.
        query = AgentQuery(names=[name])
        found: list[_Listed] = []
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
        return found[0]

    async def _fetch(
        self,
        source: AgentRegistry,
        reference: AgentReference,
        previous: _CachedCard | None,
    ) -> DiscoveredAgent:
        client = self.open_client()
        headers = await source.card_headers(reference)
        try:
            validated = await fetch_card(
                client, reference, self._allow_http_hosts, headers
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

    @property
    def max_reply_bytes(self) -> int:
        """The largest reply a ``Remote`` reads from an agent, in bytes.

        Package-internal: remote calls bound their reads with it.
        """
        return self._max_reply_bytes

    def open_client(self) -> httpx.AsyncClient:
        """Return the HTTP client the directory fetches cards with.

        Package-internal: remote calls (``Remote.ask``) send their requests
        with it too. It carries no credentials in its defaults.

        Raises:
            DefinitionError: The directory needs ``async with`` first.
        """
        if self._client is None:
            raise DefinitionError(
                "Open the AgentDirectory with 'async with' before using it, "
                "or pass http_client=."
            )
        return self._client

    def _is_fresh(self, cached: _CachedCard) -> bool:
        return monotonic() - cached.fetched_at < self._card_ttl_seconds

    def _lock(self, card_url: str) -> asyncio.Lock:
        return self._locks.setdefault(card_url, asyncio.Lock())


def _without_duplicates(
    listed: Sequence[tuple[AgentRegistry, list[AgentReference]]],
) -> list[_Listed]:
    """Pair each reference with its source, refusing a name listed twice.

    Raises:
        DefinitionError: More than one source lists the same name.
    """
    sources: dict[str, AgentRegistry] = {}
    references: list[_Listed] = []
    for source, source_references in listed:
        for reference in source_references:
            if reference.name in sources:
                raise DefinitionError(
                    f"Agent '{reference.name}' is listed by more than one "
                    f"source: {sources[reference.name]!r} and {source!r}."
                )
            sources[reference.name] = source
            references.append((source, reference))
    return references


def _discovered(
    reference: AgentReference, validated: ValidatedCard, *, stale: bool = False
) -> DiscoveredAgent:
    return DiscoveredAgent(reference, validated.card, validated.interface_url, stale)


def _has_matching_skill(
    card: AgentCard, skill: str | None, tags: frozenset[str]
) -> bool:
    return any(
        (skill is None or advertised.id == skill) and tags <= set(advertised.tags)
        for advertised in card.skills
    )


def _checked_tags(tags: Collection[str]) -> frozenset[str]:
    # A bare string would otherwise be read as one tag per character.
    if isinstance(tags, str):
        raise DefinitionError(
            "AgentDirectory.find tags must be a list of tags, not a single string."
        )
    return frozenset(tags)


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


def _checked_max_reply_bytes(max_reply_bytes: int) -> int:
    # bool is an int subclass, but True is not a byte count.
    if (
        isinstance(max_reply_bytes, bool)
        or not isinstance(max_reply_bytes, int)
        or max_reply_bytes < 1
    ):
        raise DefinitionError(
            "AgentDirectory max_reply_bytes must be a whole number greater than 0."
        )
    return max_reply_bytes
