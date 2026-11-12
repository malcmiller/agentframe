import logging

import pytest

from agentframe import (
    Agent,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    AuthenticationError,
    DefinitionError,
    PermissionDeniedError,
    Principal,
    RunContext,
    Runtime,
    VerifiedCaller,
    agent,
    skill,
)
from agentframe.auth import authenticate_request, principal_for

# Test-only values; real applications load keys from the environment.
BILLING_KEY = "test-billing-key"
ROTATED_BILLING_KEY = "test-billing-key-rotated"
REPORTS_KEY = "test-reports-key"

ROLE_PERMISSIONS = {
    "Reader": ["documents:read"],
    "Editor": ["documents:read", "documents:write"],
}


@pytest.fixture
def auth() -> APIKeyAuth:
    return APIKeyAuth(
        keys={
            BILLING_KEY: APIKey(caller_id="billing-svc", roles=["Reader"]),
            ROTATED_BILLING_KEY: APIKey(caller_id="billing-svc", roles=["Reader"]),
            REPORTS_KEY: APIKey(caller_id="reports-svc", roles=["Reader", "Editor"]),
        }
    )


async def test_api_key_header_authenticates_the_caller(auth: APIKeyAuth) -> None:
    caller = await auth.authenticate({"X-API-Key": BILLING_KEY})

    assert caller == VerifiedCaller(id="billing-svc", roles=frozenset({"Reader"}))


async def test_header_name_is_case_insensitive(auth: APIKeyAuth) -> None:
    caller = await auth.authenticate({"x-api-key": BILLING_KEY})

    assert caller.id == "billing-svc"


async def test_caller_id_becomes_principal_id_never_the_key(auth: APIKeyAuth) -> None:
    principal = await authenticate_request(
        auth, {"X-API-Key": BILLING_KEY}, ROLE_PERMISSIONS
    )

    assert principal == Principal(
        id="billing-svc", permissions=frozenset({"documents:read"})
    )


async def test_rotated_keys_share_one_caller_id(auth: APIKeyAuth) -> None:
    old = await auth.authenticate({"X-API-Key": BILLING_KEY})
    new = await auth.authenticate({"X-API-Key": ROTATED_BILLING_KEY})

    assert old == new


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": ""}, {"Authorization": f"Bearer {BILLING_KEY}"}],
)
async def test_missing_key_is_rejected(
    auth: APIKeyAuth, headers: dict[str, str]
) -> None:
    with pytest.raises(AuthenticationError, match="Authentication required"):
        await auth.authenticate(headers)


@pytest.mark.parametrize("key", ["wrong-key", BILLING_KEY + "x", "clé-inconnue"])
async def test_invalid_key_is_rejected_without_revealing_it(
    auth: APIKeyAuth, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(AuthenticationError) as error:
        await auth.authenticate({"X-API-Key": key})

    assert error.value.message == "Invalid credentials."
    assert key not in caplog.text


def test_configured_keys_are_not_revealed_by_repr(auth: APIKeyAuth) -> None:
    assert BILLING_KEY not in repr(auth)
    assert repr(auth) == "APIKeyAuth(<3 keys>)"


def test_permissions_are_the_union_of_role_permissions() -> None:
    caller = VerifiedCaller(id="reports-svc", roles=frozenset({"Reader", "Editor"}))

    principal = principal_for(caller, ROLE_PERMISSIONS)

    assert principal.permissions == {"documents:read", "documents:write"}


def test_unknown_roles_grant_nothing() -> None:
    caller = VerifiedCaller(id="guest-svc", roles=frozenset({"Admin"}))

    principal = principal_for(caller, ROLE_PERMISSIONS)

    assert principal == Principal(id="guest-svc")


async def test_allow_anonymous_has_no_principal() -> None:
    principal = await authenticate_request(
        AllowAnonymous(), {"X-API-Key": BILLING_KEY}, ROLE_PERMISSIONS
    )

    assert principal is None


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {"": APIKey(caller_id="billing-svc")},
        {BILLING_KEY: "billing-svc"},
    ],
)
def test_invalid_key_configuration_is_rejected(keys: dict[str, object]) -> None:
    with pytest.raises(DefinitionError):
        APIKeyAuth(keys=keys)  # type: ignore[arg-type]


def test_api_key_requires_caller_id_and_role_list() -> None:
    with pytest.raises(DefinitionError, match="caller_id"):
        APIKey(caller_id="")
    with pytest.raises(DefinitionError, match="roles"):
        APIKey(caller_id="billing-svc", roles="Reader")


@agent(version="1.0.0")
class Documents(Agent):
    """Reads documents."""

    @skill(id="read", requires_permissions=["documents:read"])
    async def read(self, document_id: str, ctx: RunContext) -> str:
        """Read a document."""
        return f"{ctx.principal.id if ctx.principal else None} read {document_id}"


async def test_authenticated_principal_passes_skill_permission_checks(
    auth: APIKeyAuth,
) -> None:
    principal = await authenticate_request(
        auth, {"X-API-Key": BILLING_KEY}, ROLE_PERMISSIONS
    )

    result = await Runtime().invoke(
        Documents(), skill="read", arguments={"document_id": "d1"}, principal=principal
    )

    assert result == "billing-svc read d1"


async def test_anonymous_caller_is_denied_protected_skills() -> None:
    principal = await authenticate_request(AllowAnonymous(), {}, ROLE_PERMISSIONS)

    with pytest.raises(PermissionDeniedError):
        await Runtime().invoke(
            Documents(),
            skill="read",
            arguments={"document_id": "d1"},
            principal=principal,
        )
