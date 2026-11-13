"""A stand-in Microsoft Entra tenant for offline ``EntraIDAuth`` tests.

It signs access tokens with locally generated RSA keys and serves the tenant's
OpenID metadata and key set through an ``httpx`` mock transport, so no test
touches the network.
"""

import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

TENANT_ID = "11111111-2222-3333-4444-555555555555"
API_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CALLER_OID = "99999999-8888-7777-6666-555555555555"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
METADATA_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"


def new_rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeEntra:
    """A tenant with one current signing key, ``"key-1"``.

    Attributes:
        transport: Pass to ``EntraIDAuth(transport=...)``.
        key_fetches: How many times the key set was fetched.
        available: Set to ``False`` to simulate an Entra outage.
    """

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {"key-1": new_rsa_key()}
        self.key_fetches = 0
        self.available = True
        self.transport = httpx.MockTransport(self._handle)

    def rotate_to(self, kid: str) -> None:
        """Publish an additional signing key, as Entra does during rotation."""
        self.keys[kid] = new_rsa_key()

    def claims(self, **overrides: Any) -> dict[str, Any]:
        """Return valid v2.0 access token claims, with ``overrides`` applied."""
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": API_CLIENT_ID,
            "tid": TENANT_ID,
            "oid": CALLER_OID,
            "ver": "2.0",
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "roles": ["Editor"],
            "scp": "Documents.Read Documents.Write",
        }
        claims.update(overrides)
        return {name: value for name, value in claims.items() if value is not None}

    def token(self, kid: str = "key-1", **overrides: Any) -> str:
        """Sign a valid token with the tenant's key ``kid``.

        A claim overridden with ``None`` is left out.
        """
        return sign(self.claims(**overrides), self.keys[kid], kid)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if not self.available:
            return httpx.Response(503)
        if str(request.url) == METADATA_URL:
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
        if str(request.url) == JWKS_URL:
            self.key_fetches += 1
            return httpx.Response(200, json={"keys": self._public_keys()})
        return httpx.Response(404)

    def _public_keys(self) -> list[dict[str, Any]]:
        keys = []
        for kid, private_key in self.keys.items():
            jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
            keys.append({**jwk, "kid": kid, "use": "sig"})
        return keys


def sign(
    claims: dict[str, Any], key: rsa.RSAPrivateKey | str, kid: str, alg: str = "RS256"
) -> str:
    """Sign ``claims`` as a JWT with header ``kid``."""
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})
