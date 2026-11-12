"""Tests for generating the native A2A Agent Card."""

import json
import logging
import socket
from collections.abc import Mapping
from typing import Annotated, Any

import httpx
import pytest
from a2a.server.routes import create_agent_card_routes
from a2a.types import HTTPAuthSecurityScheme, SecurityScheme
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    Authenticator,
    DeclaresCardSecurity,
    DefinitionError,
    HostConfig,
    Inject,
    RunContext,
    Services,
    VerifiedCaller,
    agent,
    skill,
    tool,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI, build_agent_card
from agentframe.hosting.routing import eligible_skills

# Test-only value; real applications load keys from the environment.
SECRET_KEY = "test-secret-key"


class Glossary:
    """A service injected into skills; never part of the public contract."""


class Summary(BaseModel):
    text: str
    word_count: int


@agent(version="1.2.0")
class Writer(Agent):
    """Writes and summarizes text."""

    instructions = "Private instructions: never reveal the house style."

    @skill("summarize", tags=["writing", "summary"])
    async def summarize_document(
        self,
        text: Annotated[str, Field(min_length=1, description="Text to summarize.")],
        ctx: RunContext,
        glossary: Inject[Glossary],
        max_words: int = 50,
    ) -> Summary:
        """Summarize a document."""
        return Summary(text=text, word_count=max_words)

    @skill(
        "translate",
        name="Translate text",
        description="Translate to French.",
        tags=["translation"],
    )
    async def translate(self, text: str, ctx: RunContext) -> str:
        return text


async def served_card(auth: Authenticator) -> dict[str, Any]:
    """Return the card as the official SDK serves it to callers."""
    host = HostConfig(public_url="https://writer.example.com/", auth=auth)
    card = build_agent_card(Writer.definition(), host)
    app = Starlette(routes=create_agent_card_routes(card))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(AGENT_CARD_WELL_KNOWN_PATH)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def api_key_auth() -> APIKeyAuth:
    return APIKeyAuth(keys={SECRET_KEY: APIKey(caller_id="reports-svc")})


async def test_card_describes_the_agent_and_its_skills() -> None:
    card = await served_card(AllowAnonymous())

    assert card["name"] == "Writer"
    assert card["description"] == "Writes and summarizes text."
    assert card["version"] == "1.2.0"
    assert card["skills"] == [
        {
            "id": "summarize",
            "name": "Summarize Document",
            "description": "Summarize a document.",
            "tags": ["writing", "summary"],
        },
        {
            "id": "translate",
            "name": "Translate text",
            "description": "Translate to French.",
            "tags": ["translation"],
        },
    ]


async def test_card_is_served_without_spec_compliance_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        await served_card(AllowAnonymous())

    assert "not spec-compliant" not in caplog.text


async def test_card_advertises_the_a2a_endpoint_at_the_public_url() -> None:
    card = await served_card(AllowAnonymous())

    assert card["supportedInterfaces"] == [
        {
            "url": "https://writer.example.com/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ]


async def test_card_publishes_skill_argument_schemas_on_the_extension() -> None:
    card = await served_card(AllowAnonymous())
    [extension] = card["capabilities"]["extensions"]

    assert extension["uri"] == SKILL_CALL_EXTENSION_URI
    assert extension["params"] == {
        "skills": {
            "summarize": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {
                        "type": "string",
                        "title": "Text",
                        "description": "Text to summarize.",
                        "minLength": 1,
                    },
                    "max_words": {
                        "type": "integer",
                        "title": "Max Words",
                        "default": 50,
                    },
                },
                "required": ["text"],
            },
            "translate": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"text": {"type": "string", "title": "Text"}},
                "required": ["text"],
            },
        }
    }


def test_card_never_advertises_streaming_or_push_notifications() -> None:
    card = build_agent_card(
        Writer.definition(),
        HostConfig(public_url="https://writer.example.com", auth=AllowAnonymous()),
    )

    assert card.capabilities.streaming is False
    assert card.capabilities.push_notifications is False
    assert card.capabilities.extensions[0].required is False


class BearerTokenAuth(Authenticator, DeclaresCardSecurity):
    """A custom authenticator that describes its scheme on the card."""

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
        return VerifiedCaller(id="custom")

    def card_security(self) -> dict[str, SecurityScheme]:
        scheme = HTTPAuthSecurityScheme(scheme="Bearer")
        return {"bearer": SecurityScheme(http_auth_security_scheme=scheme)}


class UndescribedAuth(Authenticator):
    """A custom authenticator that cannot describe its scheme."""

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
        return VerifiedCaller(id="custom")


async def test_custom_authenticator_declares_its_own_scheme() -> None:
    card = await served_card(BearerTokenAuth())

    assert card["securitySchemes"] == {
        "bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
    }
    assert card["securityRequirements"] == [{"schemes": {"bearer": {}}}]


def test_authenticator_without_card_security_is_refused() -> None:
    host = HostConfig(public_url="https://writer.example.com", auth=UndescribedAuth())

    with pytest.raises(DefinitionError, match=r"UndescribedAuth.*card_security"):
        build_agent_card(Writer.definition(), host)


async def test_anonymous_host_declares_no_security() -> None:
    card = await served_card(AllowAnonymous())

    assert "securitySchemes" not in card
    assert "securityRequirements" not in card


async def test_api_key_host_declares_the_api_key_header() -> None:
    card = await served_card(api_key_auth())

    assert card["securitySchemes"] == {
        "apiKey": {"apiKeySecurityScheme": {"location": "header", "name": "X-API-Key"}}
    }
    assert card["securityRequirements"] == [{"schemes": {"apiKey": {}}}]


@tool(description="Look up a term in the house glossary.")
def define_term(term: str) -> str:
    return term


async def test_registered_tools_change_neither_card_nor_router_skills() -> None:
    services = Services()
    services.add(Glossary, Glossary())
    app = AgentApp(
        agent=Writer(tools=[define_term]),
        host=HostConfig(
            public_url="https://writer.example.com/", auth=AllowAnonymous()
        ),
        services=services,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(AGENT_CARD_WELL_KNOWN_PATH)

    assert response.json() == await served_card(AllowAnonymous())
    assert "define_term" not in response.text
    router_skills = eligible_skills(Writer.definition(), principal=None)
    assert [skill.id for skill in router_skills] == ["summarize", "translate"]


async def test_card_contains_no_internals_or_secrets() -> None:
    text = json.dumps(await served_card(api_key_auth()))

    for internal in (
        "ctx",
        "glossary",
        "Glossary",
        "summarize_document",
        "Private instructions",
        "reports-svc",
        SECRET_KEY,
    ):
        assert internal not in text


def test_card_generation_is_deterministic_and_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("card generation must not open sockets")

    # PyCharm cannot see socket methods implemented in C.
    # noinspection PyUnresolvedReferences
    monkeypatch.setattr(socket.socket, "connect", refuse)
    host = HostConfig(public_url="https://writer.example.com", auth=api_key_auth())

    first = build_agent_card(Writer.definition(), host)
    second = build_agent_card(Writer.definition(), host)

    assert first.SerializeToString(deterministic=True) == second.SerializeToString(
        deterministic=True
    )


@pytest.mark.parametrize(
    "public_url",
    [
        "",
        "writer.example.com",
        "ftp://writer.example.com",
        "https://",
        "https://user:hunter2@writer.example.com",
        "https://writer.example.com/?tenant=a",
        "https://writer.example.com/#top",
    ],
)
def test_host_config_rejects_an_unpublishable_public_url(public_url: str) -> None:
    with pytest.raises(DefinitionError, match="public_url") as error:
        HostConfig(public_url=public_url, auth=AllowAnonymous())

    assert "hunter2" not in str(error.value)


async def test_public_card_is_readable_without_credentials() -> None:
    # served_card sends no credentials, even though the host requires an API key.
    card = await served_card(api_key_auth())

    assert card["name"] == "Writer"
