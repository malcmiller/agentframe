"""Fetch and validate another agent's Agent Card.

Used by ``AgentDirectory``; not a public API. The official SDK parses the card,
but AgentFrame downloads it itself so it can bound the size and time and refuse
redirects, which the SDK's ``A2ACardResolver`` does not allow.
"""

import asyncio
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from a2a.client.card_resolver import parse_agent_card
from a2a.types import AgentCard
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol

# protobuf ships no type stubs; ParseError is what the SDK's card parser raises.
from google.protobuf.json_format import ParseError  # type: ignore[import-untyped]

from agentframe.discovery.registry import AgentReference
from agentframe.errors import RemoteAgentError
from agentframe.hosting.host import is_loopback_host, is_plain_http_url

MAX_CARD_BYTES = 1_048_576
FETCH_TIMEOUT_SECONDS = 10.0

_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class ValidatedCard:
    """An Agent Card that passed every check, and the A2A URL to call.

    Args:
        card: The parsed official SDK card.
        interface_url: The card's JSON-RPC A2A 1.0 interface URL, on the same
            origin as the card URL.
    """

    card: AgentCard
    interface_url: str


async def fetch_card(
    client: httpx.AsyncClient,
    reference: AgentReference,
    allow_http_hosts: Collection[str],
    headers: Mapping[str, str] | None = None,
) -> ValidatedCard:
    """Download, parse and validate the Agent Card ``reference`` points to.

    The request carries only ``headers``: the client's own ``auth`` is
    disabled and nothing about the current caller is added. ``headers`` go
    only to the card URL itself, since redirects are refused.

    Args:
        client: The HTTP client to send the request with. It must carry no
            credentials in its default headers or cookies.
        reference: The agent whose card to fetch.
        allow_http_hosts: Non-loopback hosts that may be reached over plain http.
        headers: Extra headers from the agent's source, such as the source's
            own bearer token. Defaults to none.

    Returns:
        The validated card and its interface URL.

    Raises:
        RemoteAgentError: The destination is refused, the card cannot be
            fetched in time, is too large, is not a valid Agent Card or offers
            no same-origin JSON-RPC A2A 1.0 interface.
    """
    agent = f"Agent '{reference.name}'"
    _check_destination(reference.card_url, allow_http_hosts, agent)
    body = await _download(client, reference.card_url, headers or {}, agent)
    card = _parse(body, agent)
    return ValidatedCard(card, _interface_url(card, reference.card_url, agent))


def _check_destination(
    card_url: str, allow_http_hosts: Collection[str], agent: str
) -> None:
    parts = urlsplit(card_url)
    if parts.scheme == "https":
        return
    host = parts.hostname or ""
    if is_loopback_host(host) or host in allow_http_hosts:
        return
    raise RemoteAgentError(
        f"{agent} card URL uses plain http to a non-loopback host; use https, "
        "or list the host in allow_http_hosts for development."
    )


async def _download(
    client: httpx.AsyncClient, url: str, headers: Mapping[str, str], agent: str
) -> bytes:
    try:
        async with (
            asyncio.timeout(FETCH_TIMEOUT_SECONDS),
            client.stream(
                "GET",
                url,
                headers={**headers, "Accept": "application/json"},
                auth=None,
                follow_redirects=False,
            ) as response,
        ):
            if response.is_redirect:
                raise RemoteAgentError(
                    f"{agent} card URL redirected; redirects are not followed."
                )
            if not response.is_success:
                raise RemoteAgentError(
                    f"{agent} card request failed with HTTP {response.status_code}."
                )
            return await _read_limited(response, agent)
    except (TimeoutError, httpx.TimeoutException):
        raise RemoteAgentError(
            f"{agent} card was not received within {FETCH_TIMEOUT_SECONDS:g} seconds."
        ) from None
    except httpx.HTTPError as error:
        raise RemoteAgentError(
            f"{agent} card could not be fetched: {type(error).__name__}."
        ) from None


async def _read_limited(response: httpx.Response, agent: str) -> bytes:
    too_large = RemoteAgentError(f"{agent} card is larger than {MAX_CARD_BYTES} bytes.")
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > MAX_CARD_BYTES:
        raise too_large
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_CARD_BYTES:
            raise too_large
    return bytes(body)


def _parse(body: bytes, agent: str) -> AgentCard:
    try:
        data = json.loads(body)
    except ValueError:
        raise RemoteAgentError(f"{agent} card is not valid JSON.") from None
    try:
        card = parse_agent_card(data)
    except (ParseError, TypeError, ValueError):
        # The parser's message can quote card content, so it is not kept.
        raise RemoteAgentError(f"{agent} card is not a valid Agent Card.") from None
    if not card.name:
        raise RemoteAgentError(f"{agent} card is not a valid Agent Card: no name.")
    return card


def _interface_url(card: AgentCard, card_url: str, agent: str) -> str:
    for interface in card.supported_interfaces:
        if (
            interface.protocol_binding == TransportProtocol.JSONRPC
            and interface.protocol_version == PROTOCOL_VERSION_1_0
        ):
            if not _same_origin(interface.url, card_url):
                raise RemoteAgentError(
                    f"{agent} card's A2A interface is not on the card URL's origin."
                )
            return str(interface.url)
    raise RemoteAgentError(f"{agent} card offers no JSON-RPC A2A 1.0 interface.")


def _same_origin(url: str, card_url: str) -> bool:
    # The card URL comes from the trusted registry; the interface URL comes
    # from the remote card, so it must also be a clean http(s) URL.
    return is_plain_http_url(url) and _origin(url) == _origin(card_url)


def _origin(url: str) -> tuple[str, str, int]:
    # Only called on URLs that passed is_plain_http_url, so the port is valid.
    parts = urlsplit(url)
    port = parts.port or _DEFAULT_PORTS[parts.scheme]
    return parts.scheme, parts.hostname or "", port
