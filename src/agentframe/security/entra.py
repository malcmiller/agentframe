"""``EntraIDAuth``: authenticate callers with Microsoft Entra v2.0 access tokens.

Validation follows Microsoft's access token guidance:
https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#validate-tokens
and the on-behalf-of exchange follows
https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow
Needs the optional extra: ``pip install agentframe-py[entra]``.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

try:
    import httpx
    import jwt
except ImportError as error:
    raise ImportError(
        "EntraIDAuth needs the optional 'PyJWT' and 'httpx' packages; "
        "install them with: pip install 'agentframe-py[entra]'"
    ) from error

from a2a.types import OpenIdConnectSecurityScheme, SecurityScheme

from agentframe.errors import AuthenticationError, DefinitionError
from agentframe.security.authentication import (
    Authenticator,
    DeclaresCardSecurity,
    VerifiedCaller,
    find_header,
)

if TYPE_CHECKING:
    from azure.core.pipeline.transport import AsyncHttpTransport
    from azure.identity.aio import OnBehalfOfCredential

logger = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com"
"""The Microsoft Entra authority for the global Azure cloud."""

CLOCK_SKEW_SECONDS = 60
"""Leeway when checking a token's ``exp`` and ``nbf`` times."""

KEY_CACHE_SECONDS = 24 * 60 * 60
"""How long fetched signing keys are used before they are fetched again."""

MIN_REFETCH_SECONDS = 5 * 60
"""Minimum time between fetches caused by a token with an unknown key ID."""

FETCH_TIMEOUT_SECONDS = 10.0

_GUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


class EntraIDAuth(Authenticator, DeclaresCardSecurity):
    """Authenticate callers by a Microsoft Entra v2.0 access token.

    Callers send ``Authorization: Bearer <token>``. A token is accepted only if
    it is signed with ``RS256`` by one of the tenant's current signing keys, was
    issued by the tenant's v2.0 endpoint for this API, and is within its
    validity period. The caller's ID is the token's ``oid`` claim and its roles
    are the ``roles`` claim plus the ``scp`` scopes, so ``role_permissions`` can
    map both.

    Signing keys are fetched on first use, never at construction, and cached
    for 24 hours. A token whose key ID is unknown triggers at most one fetch
    every 5 minutes, which picks up rotated keys. Tokens are never logged or
    included in error messages.

    With ``client_id`` and ``client_credential`` it can also act for signed-in
    users: ``OnBehalfOf`` remotes exchange a delegated user token (one with
    an ``scp`` claim) for a token to the remote through the Entra
    on-behalf-of flow. App-only tokens are never exchanged.

    Args:
        tenant_id: The tenant (directory) ID, a GUID.
        audience: The protected API's application (client) ID, a GUID. In v2.0
            access tokens ``aud`` is this client ID, not the ``api://`` App ID
            URI that callers use in the scope they request.
        client_id: The API's own client ID, for on-behalf-of exchanges. It
            must equal ``audience``: Entra only exchanges a token whose
            ``aud`` is the app asking.
        client_credential: How the API proves its identity for on-behalf-of
            exchanges: a client secret (``str``), a PEM or PKCS12 certificate
            with its private key and no password (``bytes``), or a function
            returning a client assertion (``Callable[[], str]``), for example
            a federated managed identity token. Never shown by ``repr``.
        transport: Optional ``httpx`` transport for fetching the tenant's
            metadata and keys, for example a test double or a proxy.
        token_transport: Optional ``azure-core`` async transport for the
            on-behalf-of token requests, for example a test double or a proxy.

    Raises:
        DefinitionError: ``tenant_id`` or ``audience`` is not a GUID, only one
            of ``client_id`` and ``client_credential`` is given, or
            ``client_id`` is not ``audience``.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        audience: str,
        client_id: str | None = None,
        client_credential: str | bytes | Callable[[], str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        token_transport: "AsyncHttpTransport[Any, Any] | None" = None,
    ) -> None:
        if not _is_guid(tenant_id):
            raise DefinitionError(
                "EntraIDAuth tenant_id must be the tenant (directory) ID, a GUID."
            )
        if not _is_guid(audience):
            raise DefinitionError(
                "EntraIDAuth audience must be the API's application (client) ID, "
                "a GUID: v2.0 access tokens carry the client ID in 'aud', not an "
                "api:// App ID URI."
            )
        if (client_id is None) != (client_credential is None):
            raise DefinitionError(
                "EntraIDAuth needs both client_id and client_credential to act "
                "on behalf of users, or neither."
            )
        if client_id is not None and client_id.lower() != audience.lower():
            raise DefinitionError(
                "EntraIDAuth client_id must equal audience: Entra only exchanges "
                "a user token on behalf of the app it was issued to."
            )
        self._tenant_id = tenant_id.lower()
        self._audience = audience.lower()
        self._client_credential = client_credential
        self._token_transport = token_transport
        self._issuer = f"{AUTHORITY}/{self._tenant_id}/v2.0"
        self._metadata_url = f"{self._issuer}/.well-known/openid-configuration"
        self._transport = transport
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._refetched_for_unknown_kid_at: float | None = None
        self._fetch_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return (
            f"EntraIDAuth(tenant_id={self._tenant_id!r}, audience={self._audience!r})"
        )

    def card_security(self) -> dict[str, SecurityScheme]:
        """Return an OpenID Connect scheme pointing at the tenant's v2.0 metadata."""
        scheme = OpenIdConnectSecurityScheme(open_id_connect_url=self._metadata_url)
        return {"entraId": SecurityScheme(open_id_connect_security_scheme=scheme)}

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
        """Verify the bearer token in the request's ``Authorization`` header.

        Args:
            headers: The request's HTTP headers; names match case-insensitively.

        Returns:
            The caller identified by the token's ``oid`` claim, with its
            ``roles`` and ``scp`` values as roles.

        Raises:
            AuthenticationError: The header is missing or not a bearer token,
                the token is invalid, or the tenant's signing keys cannot be
                fetched.
        """
        token = _bearer_token(headers)
        try:
            key = await self._signing_key(token)
            claims = self._decode(token, key)
            return _caller(claims, self._tenant_id, token)
        except jwt.InvalidTokenError as error:
            # The reason helps operators; the token itself is never logged.
            logger.debug("Rejected an Entra access token: %s", error)
            raise AuthenticationError("Invalid credentials.") from None

    def check_on_behalf_of(self) -> None:
        """Check that this authenticator can act on behalf of users.

        ``AgentApp`` calls it when a remote is configured ``OnBehalfOf``.

        Raises:
            DefinitionError: ``client_id`` and ``client_credential`` were
                not given.
        """
        if self._client_credential is None:
            raise DefinitionError(
                "OnBehalfOf remotes need EntraIDAuth(client_id=..., "
                "client_credential=...) to exchange the user's token."
            )

    def on_behalf_of(self, user_assertion: str) -> "OnBehalfOfCredential":
        """Return a new credential that exchanges a user's token for others.

        Each ``get_token(scope)`` call asks Entra for a token to ``scope`` on
        behalf of the user, sending ``user_assertion``; tokens are cached
        until they near expiry. Close the credential when done.

        Args:
            user_assertion: A delegated user token this authenticator verified.

        Raises:
            DefinitionError: Not configured for on-behalf-of.
            ImportError: ``azure-identity`` is not installed.
        """
        self.check_on_behalf_of()
        try:
            from azure.identity.aio import OnBehalfOfCredential
        except ImportError as error:
            raise ImportError(
                "OnBehalfOf needs the optional 'azure-identity' package; "
                "install it with: pip install 'agentframe-py[entra]'"
            ) from error
        credential = self._client_credential
        options: dict[str, Any] = {}
        if self._token_transport is not None:
            options["transport"] = self._token_transport
        if isinstance(credential, str):
            options["client_secret"] = credential
        elif isinstance(credential, bytes):
            options["client_certificate"] = credential
        else:
            options["client_assertion_func"] = credential
        return OnBehalfOfCredential(
            self._tenant_id,
            self._audience,
            user_assertion=user_assertion,
            **options,
        )

    def _decode(self, token: str, key: jwt.PyJWK) -> dict[str, Any]:
        """Check the signature, issuer, audience and validity period."""
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self._audience,
            issuer=self._issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss", "aud"], "strict_aud": True},
        )

    async def _signing_key(self, token: str) -> jwt.PyJWK:
        """Return the tenant key named by the token's ``kid`` header."""
        kid = jwt.get_unverified_header(token).get("kid")
        if not isinstance(kid, str):
            raise jwt.InvalidTokenError("The token has no key ID.")
        async with self._fetch_lock:
            if self._keys_are_stale():
                await self._fetch_keys()
            elif kid not in self._keys and self._may_refetch_for_unknown_kid():
                self._refetched_for_unknown_kid_at = time.monotonic()
                await self._fetch_keys()
        key = self._keys.get(kid)
        if key is None:
            raise jwt.InvalidTokenError("The token's key ID is not a tenant key.")
        return key

    def _keys_are_stale(self) -> bool:
        if self._fetched_at is None:
            return True
        return time.monotonic() - self._fetched_at >= KEY_CACHE_SECONDS

    def _may_refetch_for_unknown_kid(self) -> bool:
        # Throttled so tokens with made-up key IDs cannot make every request
        # fetch keys from Entra.
        last = self._refetched_for_unknown_kid_at
        return last is None or time.monotonic() - last >= MIN_REFETCH_SECONDS

    async def _fetch_keys(self) -> None:
        """Fetch the tenant's signing keys, failing closed when Entra is down."""
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=FETCH_TIMEOUT_SECONDS
            ) as client:
                metadata = await _get_json(client, self._metadata_url)
                jwks_uri = metadata.get("jwks_uri")
                if not isinstance(jwks_uri, str):
                    raise ValueError("The tenant metadata has no jwks_uri.")
                keys = _signing_keys(await _get_json(client, jwks_uri))
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as error:
            logger.warning(
                "Microsoft Entra signing keys for tenant %s are unavailable "
                "(%s); rejecting bearer tokens until they can be fetched.",
                self._tenant_id,
                error,
            )
            raise AuthenticationError("Invalid credentials.") from None
        self._keys = keys
        self._fetched_at = time.monotonic()


def _is_guid(value: object) -> bool:
    return isinstance(value, str) and _GUID.fullmatch(value) is not None


def _bearer_token(headers: Mapping[str, str]) -> str:
    """Return the token from an ``Authorization: Bearer <token>`` header."""
    value = find_header(headers, "Authorization")
    if not value:
        raise AuthenticationError("Authentication required.")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("Invalid credentials.")
    return token.strip()


def _caller(claims: Mapping[str, Any], tenant_id: str, token: str) -> VerifiedCaller:
    """Check the Entra-specific claims and build the verified caller.

    Only a delegated user token is kept as ``user_assertion``: it has the
    ``scp`` claim, which app-only tokens never carry, and is not marked
    app-only by the optional ``idtyp`` claim.
    """
    if claims.get("ver") != "2.0":
        raise jwt.InvalidTokenError("Only v2.0 access tokens are accepted.")
    if claims.get("tid") != tenant_id:
        raise jwt.InvalidTokenError("The token is from another tenant.")
    object_id = claims.get("oid")
    if not isinstance(object_id, str) or not object_id:
        raise jwt.InvalidTokenError("The token has no 'oid' claim.")
    roles = claims.get("roles", [])
    scopes = claims.get("scp", "")
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise jwt.InvalidTokenError("The 'roles' claim is not a list of names.")
    if not isinstance(scopes, str):
        raise jwt.InvalidTokenError("The 'scp' claim is not a string.")
    is_user = bool(scopes.split()) and claims.get("idtyp") != "app"
    return VerifiedCaller(
        id=object_id,
        roles=frozenset([*roles, *scopes.split()]),
        user_assertion=token if is_user else None,
    )


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    response = await client.get(url)
    response.raise_for_status()
    document = response.json()
    if not isinstance(document, dict):
        raise ValueError(f"{url} did not return a JSON object.")
    return document


def _signing_keys(jwks: Mapping[str, Any]) -> dict[str, jwt.PyJWK]:
    """Map key ID to RSA signing key; other key types and uses are skipped."""
    keys = {
        entry["kid"]: jwt.PyJWK(entry, algorithm="RS256")
        for entry in jwks.get("keys", [])
        if isinstance(entry, dict)
        and entry.get("kty") == "RSA"
        and entry.get("use", "sig") == "sig"
        and isinstance(entry.get("kid"), str)
    }
    if not keys:
        raise ValueError("The tenant's key set has no RSA signing keys.")
    return keys
