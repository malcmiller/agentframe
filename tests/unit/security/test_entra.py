"""``EntraIDAuth``: Microsoft Entra v2.0 access tokens become verified callers.

Tokens are signed with locally generated keys and the tenant's metadata and key
set come from ``FakeEntra``'s mock transport, so nothing leaves the process.
"""

import logging
import subprocess
import sys
from typing import Any

import pytest

from agentframe import AuthenticationError, DefinitionError, EntraIDAuth, Principal
from agentframe.security.authentication import authenticate_request, www_authenticate
from fake_entra import (
    API_CLIENT_ID,
    CALLER_OID,
    METADATA_URL,
    TENANT_ID,
    FakeEntra,
    new_rsa_key,
    sign,
)


@pytest.fixture
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def auth(entra: FakeEntra) -> EntraIDAuth:
    return EntraIDAuth(
        tenant_id=TENANT_ID, audience=API_CLIENT_ID, transport=entra.transport
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_valid_token_becomes_a_principal_with_mapped_permissions(
    entra: FakeEntra, auth: EntraIDAuth
) -> None:
    role_permissions = {
        "Editor": ["documents:write"],
        "Documents.Read": ["documents:read"],
    }

    principal = await authenticate_request(
        auth, bearer(entra.token()), role_permissions
    )

    assert principal == Principal(
        id=CALLER_OID, permissions=frozenset({"documents:write", "documents:read"})
    )


async def test_roles_and_scopes_are_both_caller_roles(
    entra: FakeEntra, auth: EntraIDAuth
) -> None:
    caller = await auth.authenticate(bearer(entra.token()))

    assert caller.roles == {"Editor", "Documents.Read", "Documents.Write"}


async def test_an_application_token_without_scopes_is_accepted(
    entra: FakeEntra, auth: EntraIDAuth
) -> None:
    token = entra.token(scp=None, roles=["Reader"])

    caller = await auth.authenticate(bearer(token))

    assert caller.roles == {"Reader"}


async def test_expiry_allows_sixty_seconds_of_clock_skew(
    entra: FakeEntra, auth: EntraIDAuth
) -> None:
    just_expired = entra.claims()["iat"] - 30

    caller = await auth.authenticate(bearer(entra.token(exp=just_expired)))

    assert caller.id == CALLER_OID


@pytest.mark.parametrize(
    "audience",
    ["api://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "https://writer.example.com", ""],
)
def test_an_audience_that_is_not_a_client_id_is_refused(audience: str) -> None:
    with pytest.raises(DefinitionError, match=r"client\) ID.*not an api://"):
        EntraIDAuth(tenant_id=TENANT_ID, audience=audience)


@pytest.mark.parametrize("tenant_id", ["contoso.onmicrosoft.com", "common", ""])
def test_a_tenant_that_is_not_a_tenant_id_is_refused(tenant_id: str) -> None:
    with pytest.raises(DefinitionError, match="tenant_id"):
        EntraIDAuth(tenant_id=tenant_id, audience=API_CLIENT_ID)


async def test_construction_does_no_io(entra: FakeEntra, auth: EntraIDAuth) -> None:
    assert entra.key_fetches == 0

    await auth.authenticate(bearer(entra.token()))

    assert entra.key_fetches == 1


async def test_a_missing_header_requires_authentication(auth: EntraIDAuth) -> None:
    with pytest.raises(AuthenticationError, match="Authentication required"):
        await auth.authenticate({})


@pytest.mark.parametrize(
    "value",
    ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "Bearer not-a-jwt", "not-a-jwt"],
)
async def test_anything_but_a_bearer_token_is_rejected(
    auth: EntraIDAuth, value: str
) -> None:
    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        await auth.authenticate({"Authorization": value})


def unsigned_token(entra: FakeEntra) -> str:
    return sign(entra.claims(), "", "key-1", alg="none")


def hmac_token(entra: FakeEntra) -> str:
    return sign(
        entra.claims(), "a-shared-secret-of-at-least-32-bytes!", "key-1", "HS256"
    )


def forged_token(entra: FakeEntra) -> str:
    return sign(entra.claims(), new_rsa_key(), "key-1")


INVALID_CLAIMS: dict[str, dict[str, Any]] = {
    "wrong issuer": {"iss": "https://login.microsoftonline.com/other/v2.0"},
    "v1.0 issuer": {"iss": f"https://sts.windows.net/{TENANT_ID}/"},
    "wrong tenant": {"tid": "00000000-0000-0000-0000-000000000000"},
    "wrong audience": {"aud": "00000000-0000-0000-0000-000000000000"},
    "App ID URI audience": {"aud": f"api://{API_CLIENT_ID}"},
    "audience list": {"aud": [API_CLIENT_ID]},
    "expired": {"exp": 1},
    "not yet valid": {"nbf": 4_000_000_000},
    "v1.0 token": {"ver": "1.0"},
    "missing oid": {"oid": None},
    "missing exp": {"exp": None},
    "roles not a list": {"roles": "Editor"},
}


@pytest.mark.parametrize("overrides", INVALID_CLAIMS.values(), ids=INVALID_CLAIMS)
async def test_invalid_claims_are_rejected(
    entra: FakeEntra, auth: EntraIDAuth, overrides: dict[str, Any]
) -> None:
    token = entra.token(**overrides)

    with pytest.raises(AuthenticationError, match="Invalid credentials") as raised:
        await auth.authenticate(bearer(token))

    assert token not in str(raised.value)


@pytest.mark.parametrize("make_token", [unsigned_token, hmac_token, forged_token])
async def test_tokens_not_signed_by_the_tenant_are_rejected(
    entra: FakeEntra, auth: EntraIDAuth, make_token: Any
) -> None:
    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        await auth.authenticate(bearer(make_token(entra)))


async def test_rejected_tokens_are_never_logged(
    entra: FakeEntra, auth: EntraIDAuth, caplog: pytest.LogCaptureFixture
) -> None:
    token = entra.token(aud="00000000-0000-0000-0000-000000000000")

    with caplog.at_level(logging.DEBUG), pytest.raises(AuthenticationError):
        await auth.authenticate(bearer(token))

    assert "audience" in caplog.text.lower()
    assert token not in caplog.text


async def test_a_rotated_key_is_fetched_once_then_validates(
    entra: FakeEntra, auth: EntraIDAuth
) -> None:
    await auth.authenticate(bearer(entra.token()))
    entra.rotate_to("key-2")

    caller = await auth.authenticate(bearer(entra.token(kid="key-2")))

    assert caller.id == CALLER_OID
    assert entra.key_fetches == 2


async def test_an_unknown_key_is_rejected_after_one_refetch(
    entra: FakeEntra, auth: EntraIDAuth
) -> None:
    unknown_kid = sign(entra.claims(), new_rsa_key(), "key-unknown")

    for _ in range(3):
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await auth.authenticate(bearer(unknown_kid))

    # The first fetch loads the keys, the second looks for a rotated key and
    # later unknown key IDs wait 5 minutes before fetching again.
    assert entra.key_fetches == 2


async def test_unavailable_signing_keys_fail_closed_with_a_warning(
    entra: FakeEntra, auth: EntraIDAuth, caplog: pytest.LogCaptureFixture
) -> None:
    entra.available = False
    token = entra.token()

    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        await auth.authenticate(bearer(token))

    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "signing keys" in warning.getMessage()
    assert TENANT_ID in warning.getMessage()
    assert token not in caplog.text


async def test_keys_are_fetched_once_entra_recovers(
    entra: FakeEntra, auth: EntraIDAuth
) -> None:
    entra.available = False
    with pytest.raises(AuthenticationError):
        await auth.authenticate(bearer(entra.token()))
    entra.available = True

    caller = await auth.authenticate(bearer(entra.token()))

    assert caller.id == CALLER_OID


def test_the_card_declares_the_tenants_openid_connect_metadata(
    auth: EntraIDAuth,
) -> None:
    schemes = auth.card_security()

    [scheme] = schemes.values()
    assert scheme.open_id_connect_security_scheme.open_id_connect_url == (METADATA_URL)
    assert www_authenticate(schemes) == "Bearer"


def test_repr_shows_configuration_only(auth: EntraIDAuth) -> None:
    assert repr(auth) == (
        f"EntraIDAuth(tenant_id='{TENANT_ID}', audience='{API_CLIENT_ID}')"
    )


def test_missing_extra_names_the_extra_to_install() -> None:
    script = (
        "import sys; sys.modules['jwt'] = None\n"
        "import agentframe\n"
        "from agentframe import EntraIDAuth"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "agentframe-py[entra]" in result.stderr
