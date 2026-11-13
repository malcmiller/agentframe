"""``EntraIDAuth``: authenticate callers with Microsoft Entra v2.0 access tokens.

Validation follows Microsoft's access token guidance:
https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#validate-tokens
Needs the optional extra: ``pip install agentframe[entra]``.
"""

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

try:
    import httpx
    import jwt
except ImportError as error:
    raise ImportError(
        "EntraIDAuth needs the optional 'PyJWT' and 'httpx' packages; "
        "install them with: pip install 'agentframe[entra]'"
    ) from error

from a2a.types import OpenIdConnectSecurityScheme, SecurityScheme

from agentframe.errors import AuthenticationError, DefinitionError
from agentframe.security.authentication import (
    Authenticator,
    DeclaresCardSecurity,
    VerifiedCaller,
    find_header,
)

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

    Args:
        tenant_id: The tenant (directory) ID, a GUID.
        audience: The protected API's application (client) ID, a GUID. In v2.0
            access tokens ``aud`` is this client ID, not the ``api://`` App ID
            URI that callers use in the scope they request.
        transport: Optional ``httpx`` transport for fetching the tenant's
            metadata and keys, for example a test double or a proxy.

    Raises:
        DefinitionError: ``tenant_id`` or ``audience`` is not a GUID.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        audience: str,
        transport: httpx.AsyncBaseTransport | None = None,
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
        self._tenant_id = tenant_id.lower()
        self._audience = audience.lower()
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
            return _caller(claims, self._tenant_id)
        except jwt.InvalidTokenError as error:
            # The reason helps operators; the token itself is never logged.
            logger.debug("Rejected an Entra access token: %s", error)
            raise AuthenticationError("Invalid credentials.") from None

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


def _caller(claims: Mapping[str, Any], tenant_id: str) -> VerifiedCaller:
    """Check the Entra-specific claims and build the verified caller."""
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
    return VerifiedCaller(id=object_id, roles=frozenset([*roles, *scopes.split()]))


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
