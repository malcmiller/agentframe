"""Authenticate remote callers and map their roles to permissions.

These units are framework-neutral: they read a mapping of HTTP request headers
and produce a verified ``Principal``. The HTTP host turns ``AuthenticationError``
into a 401 response before any task is created.
"""

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from a2a.types import APIKeySecurityScheme, SecurityScheme

from agentframe.errors import AuthenticationError, DefinitionError
from agentframe.security.principal import Principal

API_KEY_HEADER = "X-API-Key"

# An HTTP field name is an RFC 9110 token. Keeping to it also keeps the name
# safe inside the quoted ``WWW-Authenticate`` challenge.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")


@dataclass(frozen=True)
class VerifiedCaller:
    """A caller whose credentials an ``Authenticator`` has verified.

    Attributes:
        id: Stable caller identifier; becomes ``Principal.id``.
        roles: Verified roles, mapped to permissions by ``role_permissions``.
        user_assertion: The verified token of a signed-in user, which the
            host may exchange on their behalf for ``OnBehalfOf`` remotes.
            ``EntraIDAuth`` sets it for delegated user tokens only. It never
            reaches ``Principal`` or ``RunContext``, is never forwarded, and
            ``repr`` hides it.
    """

    id: str
    roles: frozenset[str] = frozenset()
    user_assertion: str | None = field(default=None, repr=False, compare=False)


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

        Callers must satisfy all returned schemes (``AnyOf`` instead needs
        only one member's). An empty mapping declares that the host accepts
        callers without credentials.
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


class APIKeyAuth(Authenticator, DeclaresCardSecurity):
    """Authenticate callers by the API key sent in one request header.

    Load keys from secure configuration such as environment variables, never
    from source code. Keys are not logged or included in error messages.

    Args:
        keys: Maps each accepted key to the identity it authenticates. With
            ``hashed=True``, each mapping key is instead the lowercase or
            uppercase hex SHA-256 digest of an accepted key.
        header: The request header carrying the key, for example
            ``Ocp-Apim-Subscription-Key`` behind Azure API Management. The
            Agent Card and 401 challenge name the same header.
        hashed: Whether ``keys`` holds SHA-256 hex digests rather than keys,
            so configuration need not contain plaintext keys. Use only with
            long random keys: an unsalted digest does not protect guessable ones.

    Raises:
        DefinitionError: ``keys`` is empty, a key is empty or not a string (or
            not a SHA-256 hex digest when ``hashed``), two digests name the same
            key, an identity is not an
            ``APIKey``, or ``header`` is not a valid HTTP header name.
    """

    def __init__(
        self,
        *,
        keys: Mapping[str, APIKey],
        header: str = API_KEY_HEADER,
        hashed: bool = False,
    ) -> None:
        if not isinstance(header, str) or not _HEADER_NAME.fullmatch(header):
            raise DefinitionError("APIKeyAuth header must be a valid HTTP header name.")
        if not keys:
            raise DefinitionError("APIKeyAuth requires at least one key.")
        for key, identity in keys.items():
            if not isinstance(key, str) or not key:
                raise DefinitionError("APIKeyAuth keys must be non-empty strings.")
            if hashed and not _SHA256_HEX.fullmatch(key):
                raise DefinitionError(
                    "APIKeyAuth keys must be SHA-256 hex digests when hashed=True."
                )
            if not isinstance(identity, APIKey):
                raise DefinitionError("APIKeyAuth values must be APIKey instances.")
        self._header = header
        self._hashed = hashed
        self._keys = [
            (bytes.fromhex(key) if hashed else key.encode(), identity)
            for key, identity in keys.items()
        ]
        if len({key for key, _ in self._keys}) != len(self._keys):
            # Digests differing only in letter case are the same key.
            raise DefinitionError("APIKeyAuth keys must be distinct.")

    def __repr__(self) -> str:
        return f"APIKeyAuth(<{len(self._keys)} keys>, header={self._header!r})"

    def card_security(self) -> dict[str, SecurityScheme]:
        """Return the ``apiKey`` scheme: an API key in the configured header."""
        scheme = APIKeySecurityScheme(location="header", name=self._header)
        return {"apiKey": SecurityScheme(api_key_security_scheme=scheme)}

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
        """Verify the API key in the request's configured header.

        Args:
            headers: The request's HTTP headers; names match case-insensitively.

        Returns:
            The caller that owns the key.

        Raises:
            AuthenticationError: The header is missing or the key is unknown.
        """
        sent_key = find_header(headers, self._header)
        if not sent_key:
            raise AuthenticationError("Authentication required.")
        sent = sent_key.encode()
        identity = self._find(hashlib.sha256(sent).digest() if self._hashed else sent)
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


class AllowAnonymous(Authenticator, DeclaresCardSecurity):
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


class AnyOf(Authenticator, DeclaresCardSecurity):
    """Accept callers that any one of several authenticators verifies.

    For example ``AnyOf(EntraIDAuth(...), APIKeyAuth(...))`` lets users sign
    in with Entra while automation sends API keys. Each request goes to the
    first member, in the order given, whose credential header it carries:
    ``Authorization`` for bearer, OAuth 2.0 and OpenID Connect schemes, or the
    named header for an API key. That member's answer is final, so a wrong
    credential is never retried with another member. A request carrying no
    member's header is refused.

    The Agent Card lists every member's schemes and one security requirement
    per member, which A2A clients read as alternatives. ``OnBehalfOf``
    remotes use the member that can exchange user tokens, such as an
    ``EntraIDAuth`` with ``client_credential=``. All members share one
    ``role_permissions`` mapping, so keep their role names distinct.

    Args:
        *members: Two or more authenticators, tried in this order.

    Raises:
        DefinitionError: Fewer than two members are given; a member is an
            ``AnyOf``, does not implement ``DeclaresCardSecurity``, declares
            no schemes (anonymous access cannot be combined) or a scheme whose
            credential header is unknown; or two members read the same header
            or declare a scheme with the same name.
    """

    def __init__(self, *members: Authenticator) -> None:
        if len(members) < 2:
            raise DefinitionError("AnyOf needs at least two authenticators.")
        self._members = members
        self._headers: list[frozenset[str]] = []
        self._scheme_names: list[list[str]] = []
        self._schemes: dict[str, SecurityScheme] = {}
        for member in members:
            schemes = _member_schemes(member)
            headers = frozenset(_credential_header(s) for s in schemes.values())
            if any(headers & seen for seen in self._headers):
                raise DefinitionError(
                    "AnyOf members must read different credential headers; "
                    "a later member would never be tried."
                )
            if self._schemes.keys() & schemes.keys():
                raise DefinitionError(
                    "AnyOf members must declare security schemes with different names."
                )
            self._headers.append(headers)
            self._scheme_names.append(list(schemes))
            self._schemes.update(schemes)

    def __repr__(self) -> str:
        return f"AnyOf({', '.join(repr(member) for member in self._members)})"

    @property
    def members(self) -> tuple[Authenticator, ...]:
        """The member authenticators, in the order they are tried."""
        return self._members

    def card_security(self) -> dict[str, SecurityScheme]:
        """Return every member's schemes; a caller needs only one member's."""
        return dict(self._schemes)

    def card_security_requirements(self) -> list[list[str]]:
        """Return each member's scheme names: the card's alternative requirements."""
        return [list(names) for names in self._scheme_names]

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller | None:
        """Verify the request with the first member whose header it carries.

        Args:
            headers: The request's HTTP headers; names match case-insensitively.

        Returns:
            The caller that member verified. Its ``user_assertion`` is kept
            only when that member can exchange user tokens itself.

        Raises:
            AuthenticationError: No member's header is present, or that
                member rejects the credentials.
        """
        sent = {name.lower() for name in headers}
        for member, member_headers in zip(self._members, self._headers, strict=True):
            if member_headers & sent:
                caller = await member.authenticate(headers)
                if caller is not None and not _exchanges_user_tokens(member):
                    # The host exchanges user tokens with the member that can,
                    # so it must only ever see tokens that member verified.
                    caller = replace(caller, user_assertion=None)
                return caller
        raise AuthenticationError("Authentication required.")


def _exchanges_user_tokens(member: Authenticator) -> bool:
    """Return whether ``member`` can exchange user tokens, like ``EntraIDAuth``.

    This is ``discovery.identity.UserTokenExchange``, checked by name because
    importing it here would be circular.
    """
    return callable(getattr(member, "on_behalf_of", None))


def _member_schemes(member: Authenticator) -> dict[str, SecurityScheme]:
    """Return an ``AnyOf`` member's card schemes, checking it can be combined."""
    if isinstance(member, AnyOf):
        raise DefinitionError("AnyOf members cannot be AnyOf; list them all in one.")
    if not isinstance(member, DeclaresCardSecurity):
        raise DefinitionError(
            f"AnyOf member {type(member).__name__} must implement card_security() "
            "so AnyOf knows which requests carry its credentials."
        )
    schemes = member.card_security()
    if not schemes:
        raise DefinitionError(
            "AnyOf members must require credentials; anonymous access "
            "cannot be combined with authentication."
        )
    return schemes


def _credential_header(scheme: SecurityScheme) -> str:
    """Return the lowercase request header that carries a scheme's credential.

    Raises:
        DefinitionError: The credential is not sent in a request header.
    """
    match scheme.WhichOneof("scheme"):
        case "api_key_security_scheme" if scheme.api_key_security_scheme.location == (
            "header"
        ):
            return scheme.api_key_security_scheme.name.lower()
        case (
            "http_auth_security_scheme"
            | "oauth2_security_scheme"
            | "open_id_connect_security_scheme"
        ):
            return "authorization"
        case _:
            raise DefinitionError(
                "AnyOf members must send credentials in a request header, such "
                "as an API key header or Authorization."
            )


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


def find_header(headers: Mapping[str, str], name: str) -> str | None:
    """Return the value of header ``name``, matching names case-insensitively."""
    wanted = name.lower()
    for header_name, value in headers.items():
        if header_name.lower() == wanted:
            return value
    return None
