"""Authenticate remote callers and map their roles to permissions.

These units are framework-neutral: they read a mapping of HTTP request headers
and produce a verified ``Principal``. The HTTP host turns ``AuthenticationError``
into a 401 response before any task is created.
"""

import hmac
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from a2a.types import APIKeySecurityScheme, SecurityScheme

from agentframe.errors import AuthenticationError, DefinitionError
from agentframe.principal import Principal

API_KEY_HEADER = "X-API-Key"


@dataclass(frozen=True)
class VerifiedCaller:
    """A caller whose credentials an ``Authenticator`` has verified.

    Attributes:
        id: Stable caller identifier; becomes ``Principal.id``.
        roles: Verified roles, mapped to permissions by ``role_permissions``.
    """

    id: str
    roles: frozenset[str] = frozenset()


class Authenticator(Protocol):
    """Verifies the credentials sent with a remote request."""

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller | None:
        """Verify the caller of a request.

        Args:
            headers: The request's HTTP headers.

        Returns:
            The verified caller, or ``None`` for an explicitly allowed
            anonymous caller.

        Raises:
            AuthenticationError: Credentials are missing or invalid.
        """
        ...


@runtime_checkable
class DeclaresCardSecurity(Protocol):
    """An authenticator that can describe itself on the public Agent Card.

    Hosting requires it: a card without security schemes tells A2A clients that
    no credentials are needed, so an authenticator that cannot describe its
    scheme must not produce a card that looks open.
    """

    def card_security(self) -> dict[str, SecurityScheme]:
        """Return the security schemes the card advertises, keyed by name.

        Callers must satisfy all returned schemes. An empty mapping declares
        that the host accepts callers without credentials.
        """
        ...


# The roles field is a frozenset so grants cannot change after configuration,
# while the constructor accepts any iterable such as a list of role names.
@dataclass(frozen=True, init=False)
class APIKey:
    """The identity behind one API key.

    Args:
        caller_id: Stable caller identifier; becomes ``Principal.id``. Several
            keys may share one ``caller_id``, for example during key rotation.
        roles: Roles granted to the caller.

    Raises:
        DefinitionError: ``caller_id`` is empty or ``roles`` is a single string.
    """

    caller_id: str
    roles: frozenset[str]

    def __init__(self, caller_id: str, roles: Iterable[str] = ()) -> None:
        if not isinstance(caller_id, str) or not caller_id:
            raise DefinitionError("APIKey caller_id must be a non-empty string.")
        if isinstance(roles, str):
            raise DefinitionError("APIKey roles must be a list of role names.")
        object.__setattr__(self, "caller_id", caller_id)
        object.__setattr__(self, "roles", frozenset(roles))


class APIKeyAuth:
    """Authenticate callers by the API key sent in the ``X-API-Key`` header.

    Load keys from secure configuration such as environment variables, never
    from source code. Keys are not logged or included in error messages.

    Args:
        keys: Maps each accepted key to the identity it authenticates.

    Raises:
        DefinitionError: ``keys`` is empty, a key is empty or not a string, or
            an identity is not an ``APIKey``.
    """

    def __init__(self, *, keys: Mapping[str, APIKey]) -> None:
        if not keys:
            raise DefinitionError("APIKeyAuth requires at least one key.")
        for key, identity in keys.items():
            if not isinstance(key, str) or not key:
                raise DefinitionError("APIKeyAuth keys must be non-empty strings.")
            if not isinstance(identity, APIKey):
                raise DefinitionError("APIKeyAuth values must be APIKey instances.")
        self._keys = [(key.encode(), identity) for key, identity in keys.items()]

    def __repr__(self) -> str:
        return f"APIKeyAuth(<{len(self._keys)} keys>)"

    def card_security(self) -> dict[str, SecurityScheme]:
        """Return the ``apiKey`` scheme: an API key in the ``X-API-Key`` header."""
        scheme = APIKeySecurityScheme(location="header", name=API_KEY_HEADER)
        return {"apiKey": SecurityScheme(api_key_security_scheme=scheme)}

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
        """Verify the API key in the request's ``X-API-Key`` header.

        Args:
            headers: The request's HTTP headers; names match case-insensitively.

        Returns:
            The caller that owns the key.

        Raises:
            AuthenticationError: The header is missing or the key is unknown.
        """
        sent_key = _header(headers, API_KEY_HEADER)
        if not sent_key:
            raise AuthenticationError("Authentication required.")
        identity = self._find(sent_key.encode())
        if identity is None:
            raise AuthenticationError("Invalid credentials.")
        return VerifiedCaller(id=identity.caller_id, roles=identity.roles)

    def _find(self, sent_key: bytes) -> APIKey | None:
        # Compare against every key in constant time so response timing does
        # not reveal how much of a guessed key is correct.
        found = None
        for key, identity in self._keys:
            if hmac.compare_digest(key, sent_key):
                found = identity
        return found


class AllowAnonymous:
    """Explicitly allow anonymous remote callers. Never a default.

    Anonymous callers have no principal, so skills that require permissions
    still deny them.
    """

    def __repr__(self) -> str:
        return "AllowAnonymous()"

    def card_security(self) -> dict[str, SecurityScheme]:
        """Return no schemes: callers need no credentials."""
        return {}

    async def authenticate(self, headers: Mapping[str, str]) -> None:
        """Accept every request as anonymous, ignoring any credentials sent.

        Args:
            headers: The request's HTTP headers (unused).

        Returns:
            ``None``, meaning no verified caller.
        """
        return None


def principal_for(
    caller: VerifiedCaller, role_permissions: Mapping[str, Iterable[str]]
) -> Principal:
    """Build the principal for a verified caller.

    Args:
        caller: The verified caller.
        role_permissions: Maps role names to the permissions they grant.

    Returns:
        A principal whose permissions are the union of the permissions of the
        caller's roles. Roles missing from ``role_permissions`` grant nothing.
    """
    permissions: set[str] = set()
    for role in caller.roles:
        permissions.update(role_permissions.get(role, ()))
    return Principal(id=caller.id, permissions=frozenset(permissions))


async def authenticate_request(
    auth: Authenticator,
    headers: Mapping[str, str],
    role_permissions: Mapping[str, Iterable[str]],
) -> Principal | None:
    """Authenticate a request and resolve the caller's principal.

    Args:
        auth: The host's authenticator.
        headers: The request's HTTP headers.
        role_permissions: Maps role names to the permissions they grant.

    Returns:
        The caller's principal, or ``None`` for an allowed anonymous caller.

    Raises:
        AuthenticationError: Credentials are missing or invalid.
    """
    caller = await auth.authenticate(headers)
    if caller is None:
        return None
    return principal_for(caller, role_permissions)


def www_authenticate(schemes: Mapping[str, SecurityScheme]) -> str:
    """Build a 401 response's ``WWW-Authenticate`` value (RFC 9110, 11.6.1).

    Lists one challenge per security scheme the Agent Card declares, so the
    response names the same scheme callers find on the card. Mutual TLS is
    negotiated below HTTP and has no challenge.

    Args:
        schemes: The authenticator's ``card_security()`` schemes.

    Returns:
        Comma-separated challenges, for example
        ``APIKey location="header", name="X-API-Key"``; empty when no scheme
        has an HTTP challenge.
    """
    challenges = []
    for scheme in schemes.values():
        match scheme.WhichOneof("scheme"):
            case "api_key_security_scheme":
                api_key = scheme.api_key_security_scheme
                challenges.append(
                    f'APIKey location="{api_key.location}", name="{api_key.name}"'
                )
            case "http_auth_security_scheme":
                challenges.append(scheme.http_auth_security_scheme.scheme)
            case "oauth2_security_scheme" | "open_id_connect_security_scheme":
                challenges.append("Bearer")
    return ", ".join(challenges)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for header_name, value in headers.items():
        if header_name.lower() == wanted:
            return value
    return None
