"""Which configured credentials meet the security an Agent Card declares."""

from collections.abc import Mapping
from typing import Any

import pytest
from a2a.types import (
    AgentCard,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    OAuth2SecurityScheme,
    OpenIdConnectSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from azure.core.credentials_async import AsyncTokenCredential

from agentframe import APIKeyCredential, DefinitionError, EntraAppCredential, Service
from agentframe.discovery.identity import check_identity

API_KEY = SecurityScheme(
    api_key_security_scheme=APIKeySecurityScheme(location="header", name="X-API-Key")
)
QUERY_KEY = SecurityScheme(
    api_key_security_scheme=APIKeySecurityScheme(location="query", name="X-API-Key")
)
OIDC = SecurityScheme(
    open_id_connect_security_scheme=OpenIdConnectSecurityScheme(
        open_id_connect_url="https://login.example.com/.well-known/openid"
    )
)
OAUTH2 = SecurityScheme(oauth2_security_scheme=OAuth2SecurityScheme())
BEARER = SecurityScheme(
    http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer")
)
BASIC = SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Basic"))


# Checked by mypy --strict in CI: any azure.core async credential fits.
def _type_checks(credential: AsyncTokenCredential) -> EntraAppCredential:
    return EntraAppCredential(credential, scope="api://x/.default")


class NoTokens:
    async def get_token(self, *scopes: str) -> Any:
        raise AssertionError("startup checks never ask for a token")


KEY = APIKeyCredential("k")
ENTRA = EntraAppCredential(NoTokens(), scope="api://x/.default")


@pytest.mark.parametrize(
    ("credential", "scheme", "expected"),
    [
        (KEY, API_KEY, True),
        (APIKeyCredential("k", header="x-api-key"), API_KEY, True),
        (APIKeyCredential("k", header="X-Other"), API_KEY, False),
        (KEY, QUERY_KEY, False),
        (KEY, OIDC, False),
        (ENTRA, OIDC, True),
        (ENTRA, OAUTH2, True),
        (ENTRA, BEARER, True),
        (ENTRA, BASIC, False),
        (ENTRA, API_KEY, False),
    ],
)
def test_credentials_satisfy_only_matching_schemes(
    credential: APIKeyCredential | EntraAppCredential,
    scheme: SecurityScheme,
    expected: bool,
) -> None:
    assert credential.satisfies(scheme) is expected


def card(schemes: Mapping[str, SecurityScheme], *requirements: list[str]) -> AgentCard:
    return AgentCard(
        security_schemes=dict(schemes),
        security_requirements=[
            SecurityRequirement(schemes={name: StringList() for name in names})
            for names in requirements
        ],
    )


def test_a_card_without_security_needs_no_identity() -> None:
    check_identity("open", AgentCard(), None)


def test_any_one_requirement_is_enough() -> None:
    either = card({"key": API_KEY, "entra": OIDC}, ["key"], ["entra"])

    check_identity("a", either, Service(credential=ENTRA))
    check_identity("a", either, Service(credential=KEY))


def test_every_scheme_of_a_requirement_must_be_met() -> None:
    both = card({"key": API_KEY, "entra": OIDC}, ["key", "entra"])

    with pytest.raises(DefinitionError, match="cannot provide"):
        check_identity("a", both, Service(credential=KEY))


def test_an_empty_requirement_allows_calls_without_credentials() -> None:
    optional = card({"key": API_KEY}, ["key"], [])

    check_identity("a", optional, None)


def test_a_requirement_naming_an_undefined_scheme_is_never_met() -> None:
    broken = card({"key": API_KEY}, ["missing"])

    with pytest.raises(DefinitionError, match="cannot provide"):
        check_identity("a", broken, Service(credential=KEY))


def test_schemes_without_requirements_are_alternatives() -> None:
    check_identity("a", card({"key": API_KEY, "entra": OIDC}), Service(credential=KEY))


def test_credential_reprs_hide_the_secret() -> None:
    assert "secret" not in repr(APIKeyCredential("secret"))
    assert "api://x" in repr(ENTRA)


@pytest.mark.parametrize(
    "make",
    [lambda: APIKeyCredential(""), lambda: EntraAppCredential(NoTokens(), scope="")],
)
def test_empty_keys_and_scopes_are_refused(make: Any) -> None:
    with pytest.raises(DefinitionError):
        make()
