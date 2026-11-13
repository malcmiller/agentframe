"""FoundryModel against a fake Responses API built on a mock HTTP transport."""

import json
import subprocess
import sys
from collections.abc import Callable
from functools import partial
from typing import Any

import httpx2
import openai
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.transport import AioHttpTransport
from azure.identity.aio import DefaultAzureCredential
from pydantic import BaseModel

from agentframe import (
    Agent,
    DefinitionError,
    FinishReason,
    FoundryModel,
    IncompleteOutputError,
    Inject,
    Message,
    ModelError,
    ModelRequest,
    ModelSettings,
    OutputSchema,
    ProviderError,
    RunContext,
    RunResult,
    Runtime,
    Services,
    ToolCall,
    ToolSpec,
    UnsupportedCapabilityError,
    agent,
    skill,
    tool,
)
from agentframe.authoring.definitions import tool_definition
from agentframe.runtime.tools import tool_spec

ENDPOINT = "https://example.openai.azure.com/openai/v1/"
API_KEY = "test-key-not-real"

type Handler = Callable[[httpx2.Request], httpx2.Response]


def text_output(text: str = "Hello!") -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def refusal_output() -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "refusal", "refusal": "I can't help with that."}],
    }


USAGE = {
    "input_tokens": 12,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 3,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 15,
}


def reply(
    *output: dict[str, Any], status: str = "completed", **fields: Any
) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-5.4",
        "status": status,
        "output": list(output) or [text_output()],
        "usage": USAGE,
        **fields,
    }


def incomplete(reason: str | None) -> dict[str, Any]:
    return reply(status="incomplete", incomplete_details={"reason": reason})


def status(code: int, headers: dict[str, str] | None = None) -> Handler:
    body = {"error": {"message": "secret details", "code": "x"}}
    return lambda request: httpx2.Response(code, json=body, headers=headers)


class FakeFoundry:
    """Answers POST /openai/v1/responses, recording every request it receives."""

    def __init__(self, response: Handler | dict[str, Any] | None = None) -> None:
        self.response = response or reply()
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if callable(self.response):
            return self.response(request)
        return httpx2.Response(200, json=self.response)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
def use_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[FakeFoundry], httpx2.AsyncClient]:
    """Route the client that FoundryModel creates to a fake server."""

    def use(server: FakeFoundry) -> httpx2.AsyncClient:
        http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(server))
        client = partial(openai.AsyncOpenAI, http_client=http_client)
        monkeypatch.setattr(openai, "AsyncOpenAI", client)
        return http_client

    return use


def caller_client(server: FakeFoundry) -> openai.AsyncOpenAI:
    """A client the application creates and owns, with default SDK retries."""
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(server))
    return openai.AsyncOpenAI(
        base_url=ENDPOINT, api_key=API_KEY, http_client=http_client
    )


def foundry(**options: Any) -> FoundryModel:
    return FoundryModel(
        endpoint=ENDPOINT, deployment="gpt-5.4", credential=API_KEY, **options
    )


def ask(*messages: Message, max_output_tokens: int = 4096) -> ModelRequest:
    return ModelRequest(
        messages=messages
        or (
            Message(role="system", content="Be brief."),
            Message(role="user", content="Say hello"),
        ),
        settings=ModelSettings(max_output_tokens=max_output_tokens),
    )


class FakeTokenCredential:
    """An async Azure token credential that records the scopes it was asked for."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.scopes: list[str] = []

    async def get_token(self, *scopes: str, **options: Any) -> AccessToken:
        if self.error:
            raise self.error
        self.scopes.extend(scopes)
        return AccessToken("entra-token", 4102444800)

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "FakeTokenCredential":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass


async def test_generate_returns_text_usage_and_finish_reason(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    use_server(FakeFoundry())

    async with foundry() as model:
        response = await model.generate(ask())

    assert response.text == "Hello!"
    assert response.finish_reason is FinishReason.COMPLETED
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens == 15
    assert response.usage.estimated is False


async def test_generate_sends_instructions_input_cap_and_no_storage(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)

    async with foundry() as model:
        await model.generate(ask(max_output_tokens=50))

    [request] = server.requests
    assert str(request.url) == f"{ENDPOINT}responses"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert server.bodies() == [
        {
            "model": "gpt-5.4",
            "instructions": "Be brief.",
            "input": [{"role": "user", "content": "Say hello"}],
            "max_output_tokens": 50,
            "store": False,
        }
    ]


async def test_later_system_messages_keep_their_place_as_developer_messages(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)
    request = ask(
        Message(role="system", content="Agent instructions."),
        Message(role="system", content="Skill instructions."),
        Message(role="user", content="Hi"),
        Message(role="assistant", content="Hello"),
        Message(role="system", content="Now be formal."),
    )

    async with foundry() as model:
        await model.generate(request)

    [body] = server.bodies()
    assert body["instructions"] == "Agent instructions.\n\nSkill instructions."
    assert body["input"] == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "developer", "content": "Now be formal."},
    ]


async def test_response_storage_can_be_enabled(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)

    async with foundry(store=True) as model:
        await model.generate(ask())

    assert server.bodies()[0]["store"] is True


async def test_token_credential_sends_an_entra_token_for_the_foundry_scope(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)
    credential = FakeTokenCredential()

    async with FoundryModel(
        endpoint=ENDPOINT, deployment="gpt-5.4", credential=credential
    ) as model:
        await model.generate(ask())

    assert credential.scopes == ["https://ai.azure.com/.default"]
    assert server.requests[0].headers["authorization"] == "Bearer entra-token"


async def test_entra_token_failure_is_not_transient(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)
    credential = FakeTokenCredential(ClientAuthenticationError("az login expired"))

    async with FoundryModel(
        endpoint=ENDPOINT, deployment="gpt-5.4", credential=credential
    ) as model:
        with pytest.raises(ProviderError, match="Entra token") as caught:
            await model.generate(ask())

    assert caught.value.transient is False
    assert server.requests == []


async def test_the_foundry_extra_supports_default_azure_credential() -> None:
    # The async credential chain needs azure-core's aiohttp transport, which the
    # foundry extra installs. Nothing here touches the network.
    async with (
        AioHttpTransport(),
        DefaultAzureCredential() as credential,
        FoundryModel(
            endpoint="https://example.openai.azure.com/openai/v1/",
            deployment="gpt-test",
            credential=credential,
        ),
    ):
        pass


async def test_refusal_content_means_refused_even_beside_text(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    use_server(FakeFoundry(reply(text_output("Sure"), refusal_output())))

    async with foundry() as model:
        response = await model.generate(ask())

    assert response.finish_reason is FinishReason.REFUSED
    assert response.text == ""
    assert response.usage.total_tokens == 15


@pytest.mark.parametrize(
    ("reason", "finish_reason"),
    [
        ("max_output_tokens", FinishReason.LENGTH),
        ("content_filter", FinishReason.CONTENT_FILTER),
    ],
)
async def test_incomplete_responses_map_to_finish_reasons(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
    reason: str,
    finish_reason: FinishReason,
) -> None:
    use_server(FakeFoundry(incomplete(reason)))

    async with foundry() as model:
        response = await model.generate(ask())

    assert response.finish_reason is finish_reason
    assert response.usage.output_tokens == 3


@pytest.mark.parametrize("reason", ["max_messages", None])
async def test_other_incomplete_responses_raise_incomplete_output(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient], reason: str | None
) -> None:
    use_server(FakeFoundry(incomplete(reason)))

    async with foundry() as model:
        with pytest.raises(
            IncompleteOutputError, match="stopped the response early"
        ) as caught:
            await model.generate(ask())

    assert caught.value.usage is not None
    assert caught.value.usage.output_tokens == 3


@pytest.mark.parametrize(
    ("code", "transient"),
    [("server_error", True), ("rate_limit_exceeded", True), ("invalid_prompt", False)],
)
async def test_failed_responses_raise_provider_errors_with_usage(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
    code: str,
    transient: bool,
) -> None:
    error = {"code": code, "message": "secret details"}
    use_server(FakeFoundry(reply(status="failed", error=error)))

    async with foundry() as model:
        with pytest.raises(ProviderError, match="failed response") as caught:
            await model.generate(ask())

    assert caught.value.transient is transient
    assert caught.value.usage is not None
    assert caught.value.usage.total_tokens == 15
    assert "secret details" not in str(caught.value)


@pytest.mark.parametrize("state", ["cancelled", "queued", "in_progress"])
async def test_unfinished_responses_are_not_transient(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient], state: str
) -> None:
    use_server(FakeFoundry(reply(status=state)))

    async with foundry() as model:
        with pytest.raises(ProviderError) as caught:
            await model.generate(ask())

    assert caught.value.transient is False


async def test_missing_usage_is_unknown_not_zero(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    use_server(FakeFoundry(reply(usage=None)))

    async with foundry() as model:
        response = await model.generate(ask())

    assert response.usage.input_tokens is None
    assert response.usage.output_tokens is None
    assert response.usage.total_tokens is None


@pytest.mark.parametrize(
    ("code", "transient", "message"),
    [
        (400, False, "rejected the request"),
        (401, False, "rejected the credentials"),
        (403, False, "rejected the credentials"),
        (404, False, "'gpt-5.4' was not found"),
        (408, True, "HTTP 408"),
        (429, True, "HTTP 429"),
        (500, True, "HTTP 500"),
        (503, True, "HTTP 503"),
    ],
)
async def test_http_errors_map_to_safe_provider_errors(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
    code: int,
    transient: bool,
    message: str,
) -> None:
    use_server(FakeFoundry(status(code)))

    async with foundry() as model:
        with pytest.raises(ProviderError, match=message) as caught:
            await model.generate(ask())

    assert caught.value.transient is transient
    assert "secret details" not in str(caught.value)
    assert API_KEY not in str(caught.value)


async def test_rate_limit_passes_on_the_retry_after_hint(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    use_server(FakeFoundry(status(429, headers={"retry-after": "7"})))

    async with foundry() as model:
        with pytest.raises(ProviderError) as caught:
            await model.generate(ask())

    assert caught.value.retry_after == 7.0


async def test_retry_after_ms_is_preferred_over_retry_after(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    headers = {"retry-after-ms": "1500", "retry-after": "2"}
    use_server(FakeFoundry(status(429, headers=headers)))

    async with foundry() as model:
        with pytest.raises(ProviderError) as caught:
            await model.generate(ask())

    assert caught.value.retry_after == 1.5


@pytest.mark.parametrize(
    "failure",
    [httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow")],
    ids=["connection-refused", "timeout"],
)
async def test_unreachable_service_is_transient(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
    failure: httpx2.TransportError,
) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise failure

    use_server(FakeFoundry(fail))

    async with foundry() as model:
        with pytest.raises(ProviderError, match="Could not reach") as caught:
            await model.generate(ask())

    assert caught.value.transient is True


async def test_sdk_retries_are_disabled(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry(status(503))
    use_server(server)

    async with foundry() as model:
        with pytest.raises(ProviderError):
            await model.generate(ask())

    assert len(server.requests) == 1


async def test_injected_client_is_used_without_retries_and_stays_open() -> None:
    server = FakeFoundry(status(503))
    client = caller_client(server)

    async with FoundryModel(deployment="gpt-5.4", client=client) as model:
        with pytest.raises(ProviderError):
            await model.generate(ask())

    assert len(server.requests) == 1
    assert client.max_retries == 2
    assert not client.is_closed()
    await client.close()


async def test_close_closes_the_client_it_created(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    http_client = use_server(FakeFoundry())

    async with foundry():
        pass

    assert http_client.is_closed


async def test_construction_does_no_io(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)
    credential = FakeTokenCredential()

    async with FoundryModel(
        endpoint=ENDPOINT, deployment="gpt-5.4", credential=credential
    ) as model:
        await model.capabilities()

    assert server.requests == []
    assert credential.scopes == []


async def test_capabilities_report_the_configured_context_window() -> None:
    async with foundry(context_window=128000) as model:
        capabilities = await model.capabilities()

    assert capabilities.context_window == 128000
    assert capabilities.structured_output is True
    assert capabilities.tools is True


async def test_structured_output_and_tool_support_can_be_turned_off() -> None:
    async with foundry(structured_output=False, tools=False) as model:
        capabilities = await model.capabilities()

    assert capabilities.structured_output is False
    assert capabilities.tools is False


async def test_unconfigured_context_window_is_unknown() -> None:
    async with foundry() as model:
        capabilities = await model.capabilities()

    assert capabilities.context_window is None


def test_context_window_must_be_positive() -> None:
    with pytest.raises(DefinitionError, match="context_window"):
        foundry(context_window=0)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"endpoint": ENDPOINT},
        {"credential": API_KEY},
        {"endpoint": ENDPOINT, "credential": API_KEY, "client": "client"},
    ],
    ids=["nothing", "no-credential", "no-endpoint", "client-and-credential"],
)
def test_needs_either_a_client_or_endpoint_and_credential(
    options: dict[str, Any],
) -> None:
    if options.get("client"):
        options["client"] = openai.AsyncOpenAI(base_url=ENDPOINT, api_key=API_KEY)

    with pytest.raises(DefinitionError, match="client="):
        FoundryModel(deployment="gpt-5.4", **options)


def test_missing_extra_names_the_extra_to_install() -> None:
    script = (
        "import sys; sys.modules['openai'] = None\n"
        "import agentframe\n"
        "from agentframe import FoundryModel"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "agentframe-py[foundry]" in result.stderr


# Structured output


class Summary(BaseModel):
    text: str
    points: list[str] = []
    rating: int | None = 5


@agent(version="1.0.0")
class Summarizer(Agent):
    """Summarizes text as a typed summary."""

    @skill("summarize", tags=["text"])
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        """Summarize text."""
        return await ctx.generate(text, output=Summary)

    @skill("failure", tags=["text"])
    async def failure(self, text: str, ctx: RunContext) -> str:
        """Return the name of the model error a structured call raises."""
        try:
            await ctx.generate(text, output=Summary)
        except ModelError as error:
            return type(error).__name__
        return "no error"


def ask_for(output: type[BaseModel], name: str | None = None) -> ModelRequest:
    schema = OutputSchema(
        name=output.__name__ if name is None else name,
        json_schema=output.model_json_schema(),
    )
    return ModelRequest(
        messages=(Message(role="user", content="Summarize"),),
        settings=ModelSettings(max_output_tokens=100),
        output=schema,
    )


async def test_structured_requests_send_a_strict_json_schema_format(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)

    async with foundry() as model:
        await model.generate(ask_for(Summary))

    [body] = server.bodies()
    assert body["text"] == {
        "format": {
            "type": "json_schema",
            "name": "Summary",
            "schema": {
                "type": "object",
                "title": "Summary",
                "properties": {
                    "text": {"type": "string", "title": "Text"},
                    "points": {
                        "anyOf": [
                            {
                                "type": "array",
                                "items": {"type": "string"},
                                "title": "Points",
                            },
                            {"type": "null"},
                        ]
                    },
                    "rating": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}],
                        "title": "Rating",
                    },
                },
                "required": ["text", "points", "rating"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    }


@pytest.mark.parametrize(
    ("name", "format_name"),
    [
        ("Page[int]", "Page_int_"),
        ("Pair[Summary, list[str]]", "Pair_Summary__list_str__"),
        ("x" * 70, "x" * 64),
        ("", "output"),
    ],
)
async def test_format_names_are_made_valid(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
    name: str,
    format_name: str,
) -> None:
    server = FakeFoundry()
    use_server(server)

    async with foundry() as model:
        await model.generate(ask_for(Summary, name=name))

    assert server.bodies()[0]["text"]["format"]["name"] == format_name


async def test_nulls_for_defaults_are_dropped_so_defaults_apply(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    reply_json = '{"text": "Short.", "points": null, "rating": null}'
    use_server(FakeFoundry(reply(text_output(reply_json))))

    async with foundry(context_window=128000) as model:
        summary = await Runtime().invoke(
            Summarizer(model=model), skill="summarize", arguments={"text": "Long."}
        )

    assert summary == Summary(text="Short.", points=[], rating=None)


async def test_an_unsupported_schema_fails_before_the_client_is_called(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    class Counts(BaseModel):
        counts: dict[str, int]

    server = FakeFoundry()
    use_server(server)

    async with foundry() as model:
        with pytest.raises(UnsupportedCapabilityError, match="free-form object"):
            await model.generate(ask_for(Counts))

    assert server.requests == []


async def test_a_rejected_structured_request_is_a_safe_non_transient_error(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    use_server(FakeFoundry(status(400)))

    async with foundry() as model:
        with pytest.raises(ProviderError, match="structured output") as caught:
            await model.generate(ask_for(Summary))

    assert caught.value.transient is False
    assert "secret details" not in str(caught.value)
    assert "Summary" not in str(caught.value)


@pytest.mark.parametrize(
    ("foundry_reply", "error"),
    [
        (reply(refusal_output()), "ModelRefusalError"),
        (incomplete("max_output_tokens"), "IncompleteOutputError"),
        (incomplete("content_filter"), "ModelRefusalError"),
    ],
    ids=["refusal", "max-output-tokens", "content-filter"],
)
async def test_unfinished_structured_replies_raise_typed_errors_with_usage(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
    foundry_reply: dict[str, Any],
    error: str,
) -> None:
    use_server(FakeFoundry(foundry_reply))

    async with foundry(context_window=128000) as model:
        result = await Runtime().run(
            Summarizer(model=model), skill="failure", arguments={"text": "Long."}
        )

    assert result.value == error
    assert result.model_calls == 1
    assert result.usage.total_tokens == 15


# Tools

ENCRYPTED = "encrypted-reasoning-not-for-logs"


def reasoning_output() -> dict[str, Any]:
    return {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": ENCRYPTED,
    }


def function_call(
    call_id: str, arguments: str, name: str = "lookup_order"
) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


class Replies:
    """Answers each request with the next reply in order."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=self.replies.pop(0))


class OrderStore:
    def __init__(self) -> None:
        self.lookups: list[tuple[str, int]] = []


@tool(description="Look up an order's status.")
async def lookup_order(
    order_id: str, orders: Inject[OrderStore], limit: int = 5
) -> str:
    orders.lookups.append((order_id, limit))
    return "shipped"


@tool(description="Count words.")
def count_words(counts: dict[str, int]) -> int:
    return len(counts)


@agent(version="1.0.0")
class OrderAssistant(Agent):
    """Answers questions about orders."""

    @skill("ask", tags=["orders"])
    async def ask(self, question: str, ctx: RunContext) -> str:
        """Answer with the lookup tool."""
        return await ctx.generate(question, tools=[lookup_order])

    @skill("count", tags=["orders"])
    async def count(self, question: str, ctx: RunContext) -> str:
        """Answer with a tool whose parameters strict mode cannot express."""
        return await ctx.generate(question, tools=[count_words])


def lookup_spec() -> ToolSpec:
    return tool_spec(tool_definition(lookup_order))


def ask_with_tools(*messages: Message) -> ModelRequest:
    return ModelRequest(
        messages=messages or (Message(role="user", content="Where is order A1?"),),
        settings=ModelSettings(max_output_tokens=100),
        tools=(lookup_spec(),),
    )


async def run_orders(
    model: FoundryModel, skill_id: str = "ask"
) -> tuple[RunResult[Any], OrderStore]:
    orders = OrderStore()
    assistant = OrderAssistant(model=model, tools=[lookup_order, count_words])
    services = Services()
    services.add(OrderStore, orders)
    result = await Runtime(services=services).run(
        assistant, skill=skill_id, arguments={"question": "Where is order A1?"}
    )
    return result, orders


async def test_tools_are_sent_as_strict_functions_with_encrypted_reasoning(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)

    async with foundry() as model:
        await model.generate(ask_with_tools())

    [body] = server.bodies()
    assert body["tools"] == [
        {
            "type": "function",
            "name": "lookup_order",
            "description": "Look up an order's status.",
            "parameters": {
                "type": "object",
                "title": "lookup_order_arguments",
                "properties": {
                    "order_id": {"type": "string", "title": "Order Id"},
                    "limit": {
                        "anyOf": [
                            {"type": "integer", "title": "Limit"},
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["order_id", "limit"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    ]
    assert body["include"] == ["reasoning.encrypted_content"]
    assert "parallel_tool_calls" not in body
    assert "max_tool_calls" not in body


async def test_stored_responses_do_not_ask_for_encrypted_reasoning(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)

    async with foundry(store=True) as model:
        await model.generate(ask_with_tools())

    assert "include" not in server.bodies()[0]


async def test_requests_without_tools_are_unchanged(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)

    async with foundry() as model:
        await model.generate(ask())

    [body] = server.bodies()
    assert set(body) == {"model", "instructions", "input", "max_output_tokens", "store"}


async def test_function_calls_become_tool_calls_in_order(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    calls = (
        function_call("call_1", '{"order_id": "A1", "limit": 2}'),
        function_call("call_2", '{"order_id": "B2", "limit": 3}'),
    )
    use_server(FakeFoundry(reply(reasoning_output(), *calls)))

    async with foundry() as model:
        response = await model.generate(ask_with_tools())

    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls == (
        ToolCall(
            id="call_1", name="lookup_order", arguments='{"order_id": "A1", "limit": 2}'
        ),
        ToolCall(
            id="call_2", name="lookup_order", arguments='{"order_id": "B2", "limit": 3}'
        ),
    )
    assert [item["type"] for item in response.provider_items] == [  # type: ignore[index]
        "reasoning",
        "function_call",
        "function_call",
    ]
    assert ENCRYPTED not in repr(response)


async def test_reasoning_and_calls_are_replayed_before_their_outputs(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    first = reply(
        reasoning_output(),
        function_call("call_1", '{"order_id": "A1", "limit": 1}'),
        function_call("call_2", '{"order_id": "B2", "limit": 2}'),
    )
    server = FakeFoundry(Replies(first, reply(text_output("Both shipped."))))
    use_server(server)

    async with foundry(context_window=128000) as model:
        result, _ = await run_orders(model)

    assert result.value == "Both shipped."
    assert result.tool_calls == 2
    second_input = server.bodies()[1]["input"]
    assert [item.get("type", "message") for item in second_input] == [
        "message",
        "reasoning",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert second_input[1] == reasoning_output()
    assert second_input[2] == function_call("call_1", '{"order_id": "A1", "limit": 1}')
    assert second_input[4] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '"shipped"',
    }
    assert second_input[5]["call_id"] == "call_2"


async def test_nulls_for_defaulted_tool_parameters_reach_the_tool_as_defaults(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    call = function_call("call_1", '{"order_id": "A1", "limit": null}')
    use_server(FakeFoundry(Replies(reply(call), reply(text_output("Shipped.")))))

    async with foundry(context_window=128000) as model:
        _, orders = await run_orders(model)

    assert orders.lookups == [("A1", 5)]


async def test_assistant_messages_from_elsewhere_are_sent_as_function_calls(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)
    call = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')

    async with foundry() as model:
        await model.generate(
            ask_with_tools(
                Message(role="user", content="Where is A1?"),
                Message(role="assistant", content="", tool_calls=(call,)),
                Message(role="tool", content='"shipped"', tool_call_id="call_1"),
            )
        )

    assert server.bodies()[0]["input"][1:] == [
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup_order",
            "arguments": '{"order_id": "A1"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '"shipped"'},
    ]


async def test_calls_in_a_response_cut_short_are_not_run(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    cut_short = reply(
        function_call("call_1", '{"order_id": "A'),
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    use_server(FakeFoundry(cut_short))

    async with foundry() as model:
        response = await model.generate(ask_with_tools())

    assert response.finish_reason is FinishReason.LENGTH
    assert response.tool_calls == ()


async def test_a_tool_schema_strict_mode_cannot_express_fails_before_sending(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    server = FakeFoundry()
    use_server(server)

    async with foundry(context_window=128000) as model:
        with pytest.raises(UnsupportedCapabilityError, match=r"count_words.*free-form"):
            await run_orders(model, skill_id="count")

    assert server.requests == []


async def test_a_deployment_that_rejects_tools_is_a_non_transient_error(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
) -> None:
    use_server(FakeFoundry(status(400)))

    async with foundry() as model:
        with pytest.raises(ProviderError, match="tools") as caught:
            await model.generate(ask_with_tools())

    assert caught.value.transient is False
    assert "secret details" not in str(caught.value)


@tool(description="Always fails.")
def broken(order_id: str) -> str:
    raise RuntimeError("backend down")


@agent(version="1.0.0")
class BrokenAssistant(Agent):
    """Uses a tool that fails."""

    @skill("ask", tags=["orders"])
    async def ask(self, question: str, ctx: RunContext) -> str:
        """Answer with the broken tool."""
        return await ctx.generate(question, tools=[broken])


async def test_encrypted_reasoning_stays_out_of_results_errors_and_logs(
    use_server: Callable[[FakeFoundry], httpx2.AsyncClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = reply(reasoning_output(), function_call("call_1", '{"order_id": "A1"}'))
    use_server(FakeFoundry(Replies(first, reply(text_output("Shipped.")))))

    async with foundry(context_window=128000) as model:
        result, _ = await run_orders(model)
    failure = reply(
        reasoning_output(), function_call("c", '{"order_id": "A1"}', "broken")
    )
    use_server(FakeFoundry(failure))
    async with foundry(context_window=128000) as model:
        with pytest.raises(RuntimeError) as caught:
            await Runtime().run(
                BrokenAssistant(model=model, tools=[broken]),
                skill="ask",
                arguments={"question": "Where is A1?"},
            )

    assert ENCRYPTED not in repr(result)
    assert ENCRYPTED not in str(caught.value)
    assert ENCRYPTED not in caplog.text
