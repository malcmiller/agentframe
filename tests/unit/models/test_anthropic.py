"""AnthropicModel against a fake Messages API built on a mock HTTP transport."""

import json
import subprocess
import sys
from collections.abc import Callable
from functools import partial
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from agentframe import (
    Agent,
    AnthropicModel,
    DefinitionError,
    FinishReason,
    IncompleteOutputError,
    Message,
    ModelError,
    ModelRequest,
    ModelSettings,
    OutputSchema,
    ProviderError,
    RemoteSkill,
    RunContext,
    Runtime,
    ToolCall,
    ToolSpec,
    UnsupportedCapabilityError,
    agent,
    skill,
    tool,
)
from agentframe.orchestration.planning import plan_reply

API_KEY = "test-key-not-real"
MODEL = "claude-sonnet-4-6"

type Handler = Callable[[httpx2.Request], httpx2.Response]


def model_info(
    max_input_tokens: int | None = 200000, structured_outputs: bool | None = True
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": MODEL,
        "type": "model",
        "display_name": "Claude Sonnet 4.6",
        "created_at": "2025-09-29T00:00:00Z",
        "lifecycle": "active",
        "max_input_tokens": max_input_tokens,
    }
    if structured_outputs is not None:
        support = {"supported": structured_outputs}
        # The client validates nothing it does not read, so only the field
        # the adapter reads needs a realistic shape.
        info["capabilities"] = dict.fromkeys(
            [
                "batch",
                "citations",
                "code_execution",
                "image_input",
                "pdf_input",
                "structured_outputs",
            ],
            support,
        )
    return info


USAGE = {"input_tokens": 12, "output_tokens": 3}


def text_block(text: str = "Hello!") -> dict[str, Any]:
    return {"type": "text", "text": text}


def tool_use(
    call_id: str, arguments: dict[str, Any], name: str = "lookup_order"
) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments}


def reply(
    *content: dict[str, Any], stop_reason: str = "end_turn", **fields: Any
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": list(content) or [text_block()],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": USAGE,
        **fields,
    }


def status(code: int, headers: dict[str, str] | None = None) -> Handler:
    body = {"type": "error", "error": {"type": "x", "message": "secret details"}}
    return lambda request: httpx2.Response(code, json=body, headers=headers)


class FakeAnthropic:
    """Answers GET /v1/models/<id> and POST /v1/messages, recording requests."""

    def __init__(
        self,
        *messages: Handler | dict[str, Any],
        models: Handler | dict[str, Any] | None = None,
    ) -> None:
        self.messages = list(messages) or [reply()]
        self.models = models or model_info()
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path.startswith("/v1/models/"):
            answer = self.models
        else:
            # Repeat the last reply once the scripted ones run out.
            answer = (
                self.messages.pop(0) if len(self.messages) > 1 else self.messages[0]
            )
        if callable(answer):
            return answer(request)
        return httpx2.Response(200, json=answer)

    def bodies(self) -> list[dict[str, Any]]:
        return [
            json.loads(request.content)
            for request in self.requests
            if request.url.path == "/v1/messages"
        ]

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def caller_client(server: FakeAnthropic) -> anthropic.AsyncAnthropic:
    """A client the application creates and owns, with default SDK retries."""
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(server))
    return anthropic.AsyncAnthropic(api_key=API_KEY, http_client=http_client)


@pytest.fixture
def use_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[FakeAnthropic], httpx2.AsyncClient]:
    """Route the client that AnthropicModel creates to a fake server."""

    def use(server: FakeAnthropic) -> httpx2.AsyncClient:
        http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(server))
        client = partial(anthropic.AsyncAnthropic, http_client=http_client)
        monkeypatch.setattr(anthropic, "AsyncAnthropic", client)
        return http_client

    return use


def claude(**options: Any) -> AnthropicModel:
    return AnthropicModel(model=MODEL, api_key=API_KEY, **options)


def ask(text: str = "Say hello", max_output_tokens: int = 100) -> ModelRequest:
    return ModelRequest(
        messages=(
            Message(role="system", content="Be brief."),
            Message(role="system", content="Be kind."),
            Message(role="user", content=text),
        ),
        settings=ModelSettings(max_output_tokens=max_output_tokens),
    )


async def test_generate_returns_text_usage_and_finish_reason(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    use_server(FakeAnthropic(reply(text_block("Hello"), text_block(" there"))))

    async with claude() as model:
        response = await model.generate(ask())

    assert response.text == "Hello there"
    assert response.finish_reason is FinishReason.COMPLETED
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens == 15
    assert response.usage.estimated is False


async def test_generate_sends_the_system_prompt_messages_and_token_cap(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic()
    use_server(server)

    async with claude() as model:
        await model.generate(ask("Say hello", max_output_tokens=42))

    [body] = server.bodies()
    assert body == {
        "model": MODEL,
        "system": "Be brief.\n\nBe kind.",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Say hello"}]}
        ],
        "max_tokens": 42,
    }
    assert server.requests[-1].headers["authorization"] == f"Bearer {API_KEY}"


async def test_a_system_message_inside_the_conversation_fails_before_sending(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic()
    use_server(server)
    request = ModelRequest(
        messages=(
            Message(role="user", content="Hi"),
            Message(role="system", content="Be brief."),
        ),
        settings=ModelSettings(max_output_tokens=100),
    )

    async with claude() as model:
        with pytest.raises(UnsupportedCapabilityError, match="system"):
            await model.generate(request)

    assert server.requests == []


async def test_cached_input_tokens_count_as_input(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    usage = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 10,
        "cache_read_input_tokens": 30,
        "output_tokens": 3,
    }
    use_server(FakeAnthropic(reply(usage=usage)))

    async with claude() as model:
        response = await model.generate(ask())

    assert response.usage.input_tokens == 42
    assert response.usage.total_tokens == 45


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [
        ("stop_sequence", FinishReason.COMPLETED),
        ("max_tokens", FinishReason.LENGTH),
        ("model_context_window_exceeded", FinishReason.LENGTH),
    ],
)
async def test_stop_reasons_map_to_finish_reasons(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
    stop_reason: str,
    finish_reason: FinishReason,
) -> None:
    use_server(FakeAnthropic(reply(text_block("Hel"), stop_reason=stop_reason)))

    async with claude() as model:
        response = await model.generate(ask())

    assert response.finish_reason is finish_reason
    assert response.text == "Hel"


async def test_a_refusal_finishes_refused_with_empty_text(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    use_server(FakeAnthropic(reply(text_block("I can't"), stop_reason="refusal")))

    async with claude() as model:
        response = await model.generate(ask())

    assert response.finish_reason is FinishReason.REFUSED
    assert response.text == ""
    assert response.usage.total_tokens == 15


async def test_other_stop_reasons_raise_incomplete_output_with_usage(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    use_server(FakeAnthropic(reply(stop_reason="pause_turn")))

    async with claude() as model:
        with pytest.raises(IncompleteOutputError, match="pause_turn") as caught:
            await model.generate(ask())

    assert caught.value.usage is not None
    assert caught.value.usage.total_tokens == 15


@pytest.mark.parametrize(
    ("code", "transient", "message"),
    [
        (400, False, "rejected the request"),
        (401, False, "credentials"),
        (403, False, "credentials"),
        (404, False, "not found"),
        (408, True, "could not handle"),
        (429, True, "could not handle"),
        (500, True, "could not handle"),
        (529, True, "could not handle"),
    ],
)
async def test_http_errors_map_to_safe_provider_errors(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
    code: int,
    transient: bool,
    message: str,
) -> None:
    use_server(FakeAnthropic(status(code)))

    async with claude() as model:
        with pytest.raises(ProviderError, match=message) as caught:
            await model.generate(ask())

    assert caught.value.transient is transient
    assert "secret details" not in str(caught.value)
    assert API_KEY not in str(caught.value)


async def test_rate_limit_passes_on_the_retry_after_hint(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    use_server(FakeAnthropic(status(429, {"retry-after": "7"})))

    async with claude() as model:
        with pytest.raises(ProviderError) as caught:
            await model.generate(ask())

    assert caught.value.retry_after == 7.0


@pytest.mark.parametrize(
    "failure",
    [httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow")],
    ids=["connection-refused", "timeout"],
)
async def test_unreachable_service_is_transient(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
    failure: httpx2.TransportError,
) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise failure

    use_server(FakeAnthropic(fail))

    async with claude() as model:
        with pytest.raises(ProviderError, match="Could not reach") as caught:
            await model.generate(ask())

    assert caught.value.transient is True


async def test_sdk_retries_are_disabled(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic(status(503))
    use_server(server)

    async with claude() as model:
        with pytest.raises(ProviderError):
            await model.generate(ask())

    assert len(server.requests) == 1


async def test_injected_client_is_used_without_retries_and_stays_open() -> None:
    server = FakeAnthropic(status(503))
    client = caller_client(server)

    async with AnthropicModel(model=MODEL, client=client) as model:
        with pytest.raises(ProviderError):
            await model.generate(ask())

    assert len(server.requests) == 1
    assert client.max_retries == 2
    assert not client.is_closed()
    await client.close()


async def test_close_closes_the_client_it_created(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    http_client = use_server(FakeAnthropic())

    async with claude():
        pass

    assert http_client.is_closed


def test_the_created_client_connects_within_10_seconds_and_never_reads_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []
    create = anthropic.AsyncAnthropic

    def record(**options: Any) -> anthropic.AsyncAnthropic:
        created.append(options)
        return create(**options)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", record)

    claude()

    # The anthropic client's default allows 10 minutes for every request.
    assert created == [
        {
            "api_key": API_KEY,
            "base_url": "https://api.anthropic.com",
            "max_retries": 0,
            "timeout": httpx2.Timeout(connect=10.0, read=None, write=None, pool=None),
        }
    ]


async def test_construction_does_no_io(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic()
    use_server(server)

    async with claude():
        pass

    assert server.requests == []


async def test_capabilities_come_from_the_models_api_once(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic()
    use_server(server)

    async with claude() as model:
        first = await model.capabilities()
        second = await model.capabilities()

    assert first == second
    assert first.context_window == 200000
    assert first.structured_output is True
    assert first.tools is True
    assert server.paths() == [f"/v1/models/{MODEL}"]


async def test_a_configured_context_window_wins(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    use_server(FakeAnthropic())

    async with claude(context_window=32000) as model:
        capabilities = await model.capabilities()

    assert capabilities.context_window == 32000


async def test_missing_metadata_means_unknown_window_and_no_structured_output(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    info = model_info(max_input_tokens=None, structured_outputs=None)
    use_server(FakeAnthropic(models=info))

    async with claude() as model:
        capabilities = await model.capabilities()

    assert capabilities.context_window is None
    assert capabilities.structured_output is False


async def test_a_model_without_structured_outputs_reports_none(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    use_server(FakeAnthropic(models=model_info(structured_outputs=False)))

    async with claude() as model:
        capabilities = await model.capabilities()

    assert capabilities.structured_output is False


async def test_an_unknown_model_fails_capabilities_without_being_transient(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    use_server(FakeAnthropic(models=status(404)))

    async with claude() as model:
        with pytest.raises(ProviderError, match="was not found") as caught:
            await model.capabilities()

    assert caught.value.transient is False


def test_context_window_must_be_positive() -> None:
    with pytest.raises(DefinitionError, match="context_window"):
        claude(context_window=0)


@pytest.mark.parametrize("both", [False, True], ids=["nothing", "client-and-key"])
async def test_needs_either_a_client_or_an_api_key(both: bool) -> None:
    client = anthropic.AsyncAnthropic(api_key=API_KEY) if both else None
    key = API_KEY if both else None

    with pytest.raises(DefinitionError, match="client="):
        AnthropicModel(model=MODEL, api_key=key, client=client)

    if client is not None:
        await client.close()


def test_missing_extra_names_the_extra_to_install() -> None:
    script = (
        "import sys; sys.modules['anthropic'] = None\n"
        "import agentframe\n"
        "from agentframe import AnthropicModel"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "agentframe-py[anthropic]" in result.stderr


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


def ask_for(output: type[BaseModel]) -> ModelRequest:
    return ModelRequest(
        messages=(Message(role="user", content="Summarize"),),
        settings=ModelSettings(max_output_tokens=100),
        output=OutputSchema(
            name=output.__name__, json_schema=output.model_json_schema()
        ),
    )


async def test_structured_requests_send_the_json_schema_output_format(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic()
    use_server(server)

    async with claude() as model:
        await model.generate(ask_for(Summary))

    [body] = server.bodies()
    output_format = body["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    schema = output_format["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["text"]
    # Unsupported keywords, such as defaults, move into the description.
    assert "default: 5" in schema["properties"]["rating"]["description"]


async def test_a_typed_summary_is_validated_by_the_generation_layer(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    answer = json.dumps({"text": "Short.", "points": ["a"]})
    use_server(FakeAnthropic(reply(text_block(answer))))

    async with claude() as model:
        summary = await Runtime().invoke(
            Summarizer(model=model), skill="summarize", arguments={"text": "Long."}
        )

    assert summary == Summary(text="Short.", points=["a"], rating=5)


@pytest.mark.parametrize(
    ("stop_reason", "error"),
    [("refusal", "ModelRefusalError"), ("max_tokens", "IncompleteOutputError")],
)
async def test_unfinished_structured_replies_raise_typed_errors(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
    stop_reason: str,
    error: str,
) -> None:
    use_server(FakeAnthropic(reply(text_block('{"te'), stop_reason=stop_reason)))

    async with claude() as model:
        result = await Runtime().invoke(
            Summarizer(model=model), skill="failure", arguments={"text": "Long."}
        )

    assert result == error


async def test_a_model_without_structured_output_fails_before_sending(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic(models=model_info(structured_outputs=False))
    use_server(server)

    async with claude() as model:
        result = await Runtime().invoke(
            Summarizer(model=model), skill="failure", arguments={"text": "Long."}
        )

    assert result == "UnsupportedCapabilityError"
    assert server.bodies() == []


async def test_an_unconvertible_schema_fails_before_sending(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    class Anything(BaseModel):
        value: Any

    server = FakeAnthropic()
    use_server(server)

    async with claude() as model:
        with pytest.raises(UnsupportedCapabilityError, match="Anything"):
            await model.generate(ask_for(Anything))

    assert server.bodies() == []


async def test_a_rejected_structured_request_is_a_safe_non_transient_error(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    use_server(FakeAnthropic(status(400)))

    async with claude() as model:
        with pytest.raises(ProviderError, match="structured output") as caught:
            await model.generate(ask_for(Summary))

    assert caught.value.transient is False
    assert "secret details" not in str(caught.value)


async def test_the_planning_reply_schema_converts(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic()
    use_server(server)
    entries = (
        RemoteSkill("research-agent.find_sources", "research-agent", "x", "", ()),
        RemoteSkill("writer-agent", "writer-agent", None, "", ()),
    )

    async with claude() as model:
        await model.generate(ask_for(plan_reply(entries)))

    [body] = server.bodies()
    step = body["output_config"]["format"]["schema"]["$defs"]["PlannedStep"]
    assert step["additionalProperties"] is False
    assert step["properties"]["use"]["enum"] == [
        "research-agent.find_sources",
        "writer-agent",
    ]


# Tools


TOOL_SPEC = ToolSpec(
    name="lookup_order",
    description="Look up an order.",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string", "minLength": 2}},
        "required": ["order_id"],
    },
)
TOOL_REQUEST = ModelRequest(
    messages=(Message(role="user", content="Where is order A1?"),),
    settings=ModelSettings(max_output_tokens=100),
    tools=(TOOL_SPEC,),
)


async def test_tools_are_sent_with_their_schemas_unchanged(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic()
    use_server(server)

    async with claude() as model:
        await model.generate(TOOL_REQUEST)

    [body] = server.bodies()
    assert body["tools"] == [
        {
            "name": "lookup_order",
            "description": "Look up an order.",
            "input_schema": TOOL_SPEC.parameters,
        }
    ]


async def test_requests_without_tools_send_none(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic()
    use_server(server)

    async with claude() as model:
        await model.generate(ask())

    [body] = server.bodies()
    assert "tools" not in body
    assert "output_config" not in body


async def test_tool_use_blocks_become_tool_calls_in_order(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    content = (
        text_block("Checking."),
        tool_use("toolu_1", {"order_id": "A1"}),
        tool_use("toolu_2", {"order_id": "B2"}),
    )
    use_server(FakeAnthropic(reply(*content, stop_reason="tool_use")))

    async with claude() as model:
        response = await model.generate(TOOL_REQUEST)

    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.text == "Checking."
    assert response.tool_calls == (
        ToolCall(id="toolu_1", name="lookup_order", arguments='{"order_id": "A1"}'),
        ToolCall(id="toolu_2", name="lookup_order", arguments='{"order_id": "B2"}'),
    )


async def test_calls_in_a_response_cut_short_are_not_run(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    cut = reply(tool_use("toolu_1", {"order_id": "A"}), stop_reason="max_tokens")
    use_server(FakeAnthropic(cut))

    async with claude() as model:
        response = await model.generate(TOOL_REQUEST)

    assert response.finish_reason is FinishReason.LENGTH
    assert response.tool_calls == ()


THINKING = {"type": "thinking", "thinking": "hmm", "signature": "sig-secret"}


async def test_reply_blocks_are_replayed_and_results_merged_into_one_message(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    content = (
        THINKING,
        tool_use("toolu_1", {"order_id": "A1"}),
        tool_use("toolu_2", {"order_id": "B2"}),
    )
    server = FakeAnthropic(reply(*content, stop_reason="tool_use"))
    use_server(server)

    async with claude() as model:
        first = await model.generate(TOOL_REQUEST)
        follow_up = ModelRequest(
            messages=(
                *TOOL_REQUEST.messages,
                Message(
                    role="assistant",
                    content=first.text,
                    tool_calls=first.tool_calls,
                    provider_items=first.provider_items,
                ),
                Message(
                    role="tool",
                    content="Shipped",
                    tool_call_id="toolu_1",
                    tool_name="lookup_order",
                ),
                Message(
                    role="tool",
                    content="Packed",
                    tool_call_id="toolu_2",
                    tool_name="lookup_order",
                ),
            ),
            settings=TOOL_REQUEST.settings,
            tools=TOOL_REQUEST.tools,
        )
        await model.generate(follow_up)

    messages = server.bodies()[1]["messages"]
    assert messages[1] == {"role": "assistant", "content": list(content)}
    assert messages[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Shipped"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "Packed"},
        ],
    }


async def test_assistant_messages_from_elsewhere_are_sent_as_tool_use_blocks(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    server = FakeAnthropic()
    use_server(server)
    call = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')
    request = ModelRequest(
        messages=(
            Message(role="user", content="Where is A1?"),
            Message(role="assistant", content="", tool_calls=(call,)),
            Message(
                role="tool",
                content="Shipped",
                tool_call_id="call_1",
                tool_name="lookup_order",
            ),
        ),
        settings=ModelSettings(max_output_tokens=100),
        tools=(TOOL_SPEC,),
    )

    async with claude() as model:
        await model.generate(request)

    [body] = server.bodies()
    assert body["messages"][1] == {
        "role": "assistant",
        "content": [tool_use("call_1", {"order_id": "A1"})],
    }


async def test_a_rejected_tool_request_is_a_safe_non_transient_error(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
) -> None:
    use_server(FakeAnthropic(status(400)))

    async with claude() as model:
        with pytest.raises(ProviderError, match="tools") as caught:
            await model.generate(TOOL_REQUEST)

    assert caught.value.transient is False


@tool(description="Look up an order's status.")
async def lookup_order(order_id: str) -> str:
    return f"Order {order_id} has shipped."


@agent(version="1.0.0")
class OrderAssistant(Agent):
    """Answers questions about orders."""

    @skill("ask", tags=["orders"])
    async def ask(self, question: str, ctx: RunContext) -> str:
        """Answer a question about an order."""
        return await ctx.generate(question, tools=[lookup_order])


async def test_a_tool_round_trip_through_the_runtime_keeps_thinking_private(
    use_server: Callable[[FakeAnthropic], httpx2.AsyncClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = reply(
        THINKING, tool_use("toolu_1", {"order_id": "A1"}), stop_reason="tool_use"
    )
    server = FakeAnthropic(first, reply(text_block("A1 has shipped.")))
    use_server(server)

    async with claude() as model:
        result = await Runtime().run(
            OrderAssistant(model=model, tools=[lookup_order]),
            skill="ask",
            arguments={"question": "Where is A1?"},
        )

    assert result.value == "A1 has shipped."
    assert result.tool_calls == 1
    [_, second] = server.bodies()
    assert second["messages"][-1]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            # The runtime sends every tool result JSON-encoded.
            "content": '"Order A1 has shipped."',
        }
    ]
    assert "sig-secret" not in repr(result)
    assert "sig-secret" not in caplog.text
