"""A stand-in Microsoft Entra tenant for offline ``EntraIDAuth`` tests.

It signs access tokens with locally generated RSA keys and serves the tenant's
OpenID metadata and key set through an ``httpx`` mock transport, so no test
touches the network. ``FakeTokenEndpoint`` stands in for the tenant's token
endpoint, which ``azure-identity`` calls for on-behalf-of exchanges.
"""

import json
import time
from typing import Any

import httpx
import jwt
from azure.core.pipeline.transport import AsyncHttpTransport
from azure.core.rest import HttpRequest

# azure-core has no public in-memory response; this is the class its own
# transports build, so the client parses it exactly like a real one.
from azure.core.rest._http_response_impl_async import AsyncHttpResponseImpl
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

TENANT_ID = "11111111-2222-3333-4444-555555555555"
API_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TARGET_CLIENT_ID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
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


class FakeTokenEndpoint(AsyncHttpTransport):  # type: ignore[type-arg]
    """The tenant's token endpoint, answering on-behalf-of exchanges.

    Pass it to ``EntraIDAuth(token_transport=...)``. Each exchange issues a
    token signed by ``entra`` for ``TARGET_CLIENT_ID``, for the same user,
    with ``azp`` naming the exchanging app.

    Attributes:
        exchanges: The form fields of every token request.
        issued: Every token issued.
        failing: Set to ``True`` to refuse exchanges as Entra does.
    """

    def __init__(self, entra: FakeEntra) -> None:
        self.entra = entra
        self.exchanges: list[dict[str, str]] = []
        self.issued: list[str] = []
        self.failing = False

    async def send(self, request: HttpRequest, **kwargs: Any) -> AsyncHttpResponseImpl:
        assert request.url == TOKEN_URL, request.url
        # azure-identity sends the form as a dict, encoded by the transport.
        fields = {str(k): str(v) for k, v in dict(request.content).items()}
        self.exchanges.append(fields)
        if self.failing:
            return _reply(request, 400, {"error": "invalid_grant"})
        user = jwt.decode(fields["assertion"], options={"verify_signature": False})
        token = self.entra.token(
            aud=TARGET_CLIENT_ID, oid=user["oid"], azp=fields["client_id"]
        )
        self.issued.append(token)
        body = {"access_token": token, "token_type": "Bearer", "expires_in": 3600}
        return _reply(request, 200, body)

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "FakeTokenEndpoint":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass


def _reply(request: HttpRequest, status: int, body: object) -> AsyncHttpResponseImpl:
    response = AsyncHttpResponseImpl(
        request=request,
        internal_response=None,
        status_code=status,
        reason="",
        content_type="application/json",
        headers={"Content-Type": "application/json"},
        stream_download_generator=None,
    )
    response._content = json.dumps(body).encode()
    response._is_closed = True
    response._is_stream_consumed = True
    return response
