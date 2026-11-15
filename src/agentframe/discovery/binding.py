"""Find a remote agent and bind it to one run as a ``Remote`` parameter."""

from typing import Any, Literal, TypeAliasType, get_args, get_origin

from agentframe.discovery.directory import AgentDirectory, DiscoveredAgent
from agentframe.discovery.identity import (
    OutboundCredential,
    RemoteIdentity,
    check_identity,
)
from agentframe.discovery.remote import Remote
from agentframe.discovery.transport import BoundedJsonRpcTransport
from agentframe.errors import DefinitionError
from agentframe.runtime.accounting import RunAccounting
from agentframe.runtime.correlation import RunCorrelation
from agentframe.security.principal import Principal

# Bounds alias unwrapping, so an alias that refers to itself cannot loop forever.
_MAX_ALIAS_DEPTH = 10


def remote_name(annotation: object) -> str | None:
    """Return the agent name of a ``Remote[Literal["name"]]`` annotation.

    ``type`` aliases of one are read through, including aliases of aliases.

    Returns:
        The name, or ``None`` when ``annotation`` is not a ``Remote``.

    Raises:
        DefinitionError: ``Remote`` is used without exactly one string name,
            or a ``type`` alias nests too deeply or refers to itself.
    """
    for _ in range(_MAX_ALIAS_DEPTH):
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
    if isinstance(annotation, TypeAliasType):
        raise DefinitionError(
            f"type alias nests more than {_MAX_ALIAS_DEPTH} levels or refers to itself"
        )
    if annotation is not Remote and get_origin(annotation) is not Remote:
        return None
    [name_type] = get_args(annotation) or [None]
    names = get_args(name_type) if get_origin(name_type) is Literal else ()
    if len(names) != 1 or not isinstance(names[0], str):
        raise DefinitionError("Name the remote agent as Remote[Literal['agent-name']]")
    return names[0]


async def find_remote(
    directory: AgentDirectory, name: str, identity: RemoteIdentity | None
) -> DiscoveredAgent:
    """Look an agent up and check that a binding can call it.

    Args:
        directory: Where to look the agent up.
        name: The agent's name.
        identity: The agent's entry in ``remotes=``, if any.

    Raises:
        DefinitionError: No source lists the agent, its card requires
            credentials that ``identity`` cannot provide, or credentials
            would be sent with an HTTP client that follows redirects.
        RemoteAgentError: Its card cannot be fetched or is invalid.
    """
    found = await directory.get(name)
    check_identity(name, found.card, identity)
    check_no_redirects(directory, name, identity)
    return found


def check_no_redirects(
    directory: AgentDirectory, name: str, identity: RemoteIdentity | None
) -> None:
    """Check that credentials for ``name`` cannot follow a redirect elsewhere.

    Raises:
        DefinitionError: ``identity`` is set and the directory's HTTP client
            follows redirects, or the directory is not open.
    """
    if identity is not None and directory.open_client().follow_redirects:
        raise DefinitionError(
            f"Remote '{name}' has credentials, but the AgentDirectory's "
            "http_client follows redirects; credentials are only sent to the "
            "card's own origin, so pass a client with follow_redirects=False."
        )


async def bind_remote(
    directory: AgentDirectory,
    name: str,
    *,
    deadline: float,
    correlation: RunCorrelation,
    principal: Principal | None,
    identity: RemoteIdentity | None,
    credential: OutboundCredential | None,
    hops: int,
    max_hops: int,
    accounting: RunAccounting,
    max_total_tokens: int,
) -> Remote[Any]:
    """Create the binding one run passes to a ``Remote`` skill parameter.

    Args:
        identity: The agent's entry in ``remotes=``, if any, checked against
            its card.
        credential: What this run sends for ``identity``: a ``Service``'s
            credential, or the run's on-behalf-of token for ``OnBehalfOf``.

    Raises:
        DefinitionError: As for ``find_remote``, or the directory is not open.
        RemoteAgentError: As for ``find_remote``.
    """
    found = await find_remote(directory, name, identity)
    return new_binding(
        directory,
        found,
        deadline=deadline,
        correlation=correlation,
        principal=principal,
        credential=credential,
        hops=hops,
        max_hops=max_hops,
        accounting=accounting,
        max_total_tokens=max_total_tokens,
    )


def new_binding(
    directory: AgentDirectory,
    found: DiscoveredAgent,
    *,
    deadline: float,
    correlation: RunCorrelation,
    principal: Principal | None,
    credential: OutboundCredential | None,
    hops: int,
    max_hops: int,
    accounting: RunAccounting,
    max_total_tokens: int,
) -> Remote[Any]:
    """Create a run's binding to an agent whose card is already checked.

    The binding sends its requests with the directory's HTTP client. The
    caller must have checked ``credential`` against the card first, as
    ``find_remote`` does.

    Raises:
        DefinitionError: The directory is not open.
    """
    client = directory.open_client()
    transport = BoundedJsonRpcTransport(
        client,
        found.card,
        found.interface_url,
        max_reply_bytes=directory.max_reply_bytes,
    )
    return Remote(
        found,
        transport,
        deadline=deadline,
        correlation=correlation,
        principal=principal,
        credential=credential,
        hops=hops,
        max_hops=max_hops,
        accounting=accounting,
        max_total_tokens=max_total_tokens,
    )
