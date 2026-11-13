"""Outbound identity: how an agent authenticates when it calls a remote agent.

Each remote is configured on its own, in ``remotes=`` on ``Runtime`` or
``AgentApp``, never in a registry or card. ``Service`` sends the calling
agent's own credential; ``OnBehalfOf`` exchanges the signed-in user's token for
one scoped to the remote. An incoming caller's token is never forwarded.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from a2a.types import AgentCard, SecurityScheme

from agentframe.errors import DefinitionError
from agentframe.security.authentication import API_KEY_HEADER


@runtime_checkable
class OutboundCredential(Protocol):
    """A credential an agent sends with its requests to one remote agent.

    Implement it to add another kind of credential; no base class is needed.
    """

    async def headers(self) -> Mapping[str, str]:
        """Return the HTTP headers that carry the credential.

        Called before every request to the remote agent, so a credential can
        refresh an expired token. Never log or return the values elsewhere.
        """
        ...

    def satisfies(self, scheme: SecurityScheme) -> bool:
        """Say whether these headers meet a security scheme on the remote's card.

        Args:
            scheme: One of the remote Agent Card's security schemes.
        """
        ...


class APIKeyCredential:
    """An API key sent in one request header, as ``APIKeyAuth`` expects.

    Args:
        key: The key. It is never shown by ``repr`` or in errors.
        header: The header that carries it. Defaults to ``X-API-Key``.

    Raises:
        DefinitionError: ``key`` or ``header`` is empty.
    """

    def __init__(self, key: str, *, header: str = API_KEY_HEADER) -> None:
        if not key or not header:
            raise DefinitionError("APIKeyCredential needs a non-empty key and header.")
        self._key = key
        self._header = header

    def __repr__(self) -> str:
        return f"APIKeyCredential(header={self._header!r})"

    async def headers(self) -> Mapping[str, str]:
        """Return the key in its header."""
        return {self._header: self._key}

    def satisfies(self, scheme: SecurityScheme) -> bool:
        """Match an ``apiKey`` scheme in a header of the same name (any case)."""
        if scheme.WhichOneof("scheme") != "api_key_security_scheme":
            return False
        api_key = scheme.api_key_security_scheme
        return (
            api_key.location == "header"
            and api_key.name.lower() == self._header.lower()
        )


class _AccessToken(Protocol):
    @property
    def token(self) -> str: ...


class _TokenCredential(Protocol):
    """What ``EntraAppCredential`` needs of an ``azure.core`` async credential."""

    async def get_token(self, *scopes: str) -> _AccessToken: ...


class EntraAppCredential:
    """A Microsoft Entra token for the calling agent's own app identity.

    Each request asks ``credential`` for a token for ``scope`` and sends it
    as ``Authorization: Bearer``. ``azure-identity`` credentials cache tokens
    until they near expiry, so this does not contact Entra on every request.

    Args:
        credential: Any ``azure.core`` async token credential, for example
            ``azure.identity.aio.ClientSecretCredential`` or
            ``ManagedIdentityCredential``. It stays yours: it is never closed.
        scope: The remote agent's scope, for example ``api://<client-id>/.default``
            (``https://ai.azure.com/.default`` for Foundry agents).

    Raises:
        DefinitionError: ``scope`` is empty.
    """

    def __init__(self, credential: _TokenCredential, *, scope: str) -> None:
        if not scope:
            raise DefinitionError("EntraAppCredential needs a scope.")
        self._credential = credential
        self._scope = scope

    def __repr__(self) -> str:
        return f"EntraAppCredential(scope={self._scope!r})"

    async def headers(self) -> Mapping[str, str]:
        """Return a bearer token for the scope."""
        access = await self._credential.get_token(self._scope)
        return {"Authorization": f"Bearer {access.token}"}

    def satisfies(self, scheme: SecurityScheme) -> bool:
        """Match OAuth 2.0, OpenID Connect and HTTP bearer schemes."""
        return _accepts_bearer_token(scheme)


def _accepts_bearer_token(scheme: SecurityScheme) -> bool:
    """Say whether an ``Authorization: Bearer`` token meets ``scheme``."""
    match scheme.WhichOneof("scheme"):
        case "oauth2_security_scheme" | "open_id_connect_security_scheme":
            return True
        case "http_auth_security_scheme":
            return scheme.http_auth_security_scheme.scheme.lower() == "bearer"
        case _:
            return False


@dataclass(frozen=True)
class Service:
    """Call a remote agent with the calling agent's own identity.

    The remote agent authorizes the calling agent, not the caller the run
    serves. It is the only mode for agents whose callers use API keys or no
    authentication.

    Args:
        credential: What to send, such as ``APIKeyCredential`` or
            ``EntraAppCredential``.
    """

    credential: OutboundCredential


@dataclass(frozen=True)
class OnBehalfOf:
    """Call a remote agent on behalf of the signed-in user (Entra on-behalf-of).

    Each run exchanges the user's incoming token for a token scoped to the
    remote, through the standard Microsoft Entra on-behalf-of flow, so the
    remote applies the user's permissions and sees the calling agent in
    ``azp``. The incoming token itself is never forwarded. It is the
    recommended mode for agents whose callers sign in with ``EntraIDAuth``,
    which must then have ``client_id=`` and ``client_credential=``.

    A run without a signed-in user (an app-only token, an API-key or
    anonymous caller, or a local run) fails with ``PermissionDeniedError``;
    a failed exchange fails the call with ``AuthenticationError``. Neither
    falls back to ``Service``.

    Args:
        scope: The remote's scope, for example ``api://<client-id>/.default``.

    Raises:
        DefinitionError: ``scope`` is empty.
    """

    scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or not self.scope:
            raise DefinitionError("OnBehalfOf needs a scope.")

    def satisfies(self, scheme: SecurityScheme) -> bool:
        """Match OAuth 2.0, OpenID Connect and HTTP bearer schemes."""
        return _accepts_bearer_token(scheme)


class UserTokenCredential(Protocol):
    """An ``azure.core`` async token credential that the runtime closes."""

    async def get_token(self, *scopes: str) -> _AccessToken: ...

    async def close(self) -> None: ...


@runtime_checkable
class UserTokenExchange(Protocol):
    """An authenticator that can exchange a user's token on their behalf.

    ``EntraIDAuth`` implements it; ``OnBehalfOf`` remotes need one.
    """

    def check_on_behalf_of(self) -> None:
        """Check that the exchange is configured.

        Raises:
            DefinitionError: It has no client credential to exchange with.
        """
        ...

    def on_behalf_of(self, user_assertion: str) -> UserTokenCredential:
        """Return a new credential that exchanges ``user_assertion``.

        Args:
            user_assertion: The verified incoming user token.
        """
        ...


class UserToken:
    """A run's verified incoming user token and what can exchange it.

    Package-internal. The host creates one per request from the token its
    authenticator verified; ``repr`` never shows the token.
    """

    def __init__(self, assertion: str, exchange: UserTokenExchange) -> None:
        self._assertion = assertion
        self._exchange = exchange

    def __repr__(self) -> str:
        return "UserToken(<hidden>)"

    def credential(self) -> UserTokenCredential:
        """Return a new on-behalf-of credential for this token."""
        return self._exchange.on_behalf_of(self._assertion)


type RemoteIdentity = Service | OnBehalfOf
"""How a run authenticates to one remote agent."""


def checked_remotes(
    remotes: Mapping[str, RemoteIdentity] | None,
) -> dict[str, RemoteIdentity]:
    """Check a ``remotes=`` mapping and return a copy.

    Raises:
        DefinitionError: A name is not a string, or an identity is neither
            ``OnBehalfOf`` nor a ``Service`` with an ``OutboundCredential``.
    """
    checked = dict(remotes or {})
    for name, identity in checked.items():
        if not isinstance(name, str) or not isinstance(identity, Service | OnBehalfOf):
            raise DefinitionError(
                "remotes= maps agent names to Service(credential=...) or "
                "OnBehalfOf(scope=...) identities."
            )
        if isinstance(identity, Service) and not isinstance(
            identity.credential, OutboundCredential
        ):
            raise DefinitionError(
                f"Remote '{name}': Service credential must implement "
                "OutboundCredential (headers() and satisfies())."
            )
    return checked


def on_behalf_of_names(remotes: Mapping[str, RemoteIdentity]) -> list[str]:
    """Return the names of the ``OnBehalfOf`` remotes, sorted."""
    return sorted(n for n, i in remotes.items() if isinstance(i, OnBehalfOf))


def check_identity(name: str, card: AgentCard, identity: RemoteIdentity | None) -> None:
    """Check that the configured identity meets the security the card declares.

    A card that declares no security needs no identity. Otherwise the
    credential must meet every scheme of at least one of the card's
    security requirements (or, when it lists none, one of its schemes).

    Raises:
        DefinitionError: The card requires credentials and the remote has no
            identity, or its credential meets none of the requirements. The
            message names the remote, never the credential.
    """
    alternatives = _security_alternatives(card)
    if not alternatives or any(not schemes for schemes in alternatives):
        return
    if identity is None:
        raise DefinitionError(
            f"Agent '{name}' requires credentials; configure "
            f"remotes={{'{name}': OnBehalfOf(scope=...)}} or "
            "Service(credential=...)."
        )
    credential = identity.credential if isinstance(identity, Service) else identity
    for schemes in alternatives:
        if all(s is not None and credential.satisfies(s) for s in schemes):
            return
    raise DefinitionError(
        f"Agent '{name}' requires credentials that its configured "
        f"{type(credential).__name__} cannot provide; check the security "
        "schemes on its Agent Card."
    )


def _security_alternatives(card: AgentCard) -> list[list[SecurityScheme | None]]:
    """Return the card's security as alternatives, each a list of schemes to meet.

    A requirement naming a scheme the card does not define gives ``None``,
    which no credential meets.
    """
    defined = card.security_schemes
    if card.security_requirements:
        return [
            [defined.get(n) for n in requirement.schemes]
            for requirement in card.security_requirements
        ]
    return [[scheme] for scheme in defined.values()]
