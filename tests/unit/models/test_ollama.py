"""OllamaModel against a fake Ollama server built on httpx's mock transport."""

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from functools import partial
from typing import Any

import httpx
import ollama
import pytest
from pydantic import BaseModel

from agentframe import (
    Agent,
    DefinitionError,
    FinishReason,
    Message,
    ModelError,
    ModelRequest,
    ModelSettings,
    OllamaModel,
    OutputSchema,
    OutputValidationError,
    ProviderError,
    RunContext,
    Runtime,
    ToolCall,
    ToolSpec,
    UnsupportedCapabilityError,
    agent,
    skill,
    tool,
)

type Handler = Callable[[httpx.Request], httpx.Response]

SHOW_LLAMA = {
    "model_info": {"general.architecture": "llama", "llama.context_length": 8192},
    "capabilities": ["completion", "tools"],
}
SHOW_WITHOUT_TOOLS = {**SHOW_LLAMA, "capabilities": ["completion"]}
SHOW_OLDER_SERVER = {"model_info": SHOW_LLAMA["model_info"]}


def chat_reply(
    text: str = "Hello!", done_reason: str = "stop", **counts: int
) -> dict[str, Any]:
    return {
        "model": "llama3.2:3b",
        "message": {"role": "assistant", "content": text},
        "done": True,
        "done_reason": done_reason,
        **counts,
    }


class FakeOllama:
    """Answers /api/show and /api/chat, recording every request it receives."""

    def __init__(
        self,
        chat: Handler | dict[str, Any] | None = None,
        show: Handler | dict[str, Any] = SHOW_LLAMA,
    ) -> None:
        self.chat = chat or chat_reply(prompt_eval_count=12, eval_count=3)
        self.show = show
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)  # Let other tasks run, like a real network call.
        self.requests.append((request.url.path, json.loads(request.content)))
        reply = self.show if request.url.path == "/api/show" else self.chat
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def chat_bodies(self) -> list[dict[str, Any]]:
        return [body for path, body in self.requests if path == "/api/chat"]

    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]


@pytest.fixture
def use_server(monkeypatch: pytest.MonkeyPatch) -> Callable[[FakeOllama], None]:
    """Route the HTTP client that OllamaModel creates to a fake server."""

    def use(server: FakeOllama) -> None:
        transport = httpx.MockTransport(server)
        client = partial(ollama.AsyncClient, transport=transport)
        monkeypatch.setattr(ollama, "AsyncClient", client)

    return use


def ask(text: str, max_output_tokens: int = 4096) -> ModelRequest:
    return ModelRequest(
        messages=(
            Message(role="system", content="Be brief."),
            Message(role="user", content=text),
        ),
        settings=ModelSettings(max_output_tokens=max_output_tokens),
    )


def status(code: int) -> Handler:
    return lambda request: httpx.Response(code, json={"error": "details"})


async def test_generate_returns_text_usage_and_finish_reason(
    use_server: Callable[[FakeOllama], None],
) -> None:
    use_server(FakeOllama())

    async with OllamaModel(model="llama3.2:3b") as model:
        response = await model.generate(ask("Say hello"))

    assert response.text == "Hello!"
    assert response.finish_reason is FinishReason.COMPLETED
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens == 15
    assert response.usage.estimated is False


async def test_generate_sends_messages_output_cap_and_context_window(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama()
    use_server(server)

    async with OllamaModel(model="llama3.2:3b") as model:
        await model.generate(ask("Say hello", max_output_tokens=50))

    [body] = server.chat_bodies()
    assert body["model"] == "llama3.2:3b"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Say hello"},
    ]
    assert body["options"] == {"num_ctx": 8192, "num_predict": 50}


async def test_length_finish_means_truncated_output(
    use_server: Callable[[FakeOllama], None],
) -> None:
    use_server(FakeOllama(chat=chat_reply("Hel", done_reason="length")))

    async with OllamaModel(model="llama3.2:3b") as model:
        response = await model.generate(ask("Say hello"))

    assert response.finish_reason is FinishReason.LENGTH


async def test_missing_token_counts_are_unknown_not_zero(
    use_server: Callable[[FakeOllama], None],
) -> None:
    use_server(FakeOllama(chat=chat_reply(eval_count=3)))

    async with OllamaModel(model="llama3.2:3b") as model:
        response = await model.generate(ask("Say hello"))

    assert response.usage.input_tokens is None
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens is None


async def test_context_window_is_read_once_from_model_metadata(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama()
    use_server(server)

    async with OllamaModel(model="llama3.2:3b") as model:
        capabilities = await model.capabilities()
        await model.generate(ask("One"))
        await model.generate(ask("Two"))

    assert capabilities.context_window == 8192
    assert capabilities.structured_output is True
    assert capabilities.tools is True
    assert server.paths() == ["/api/show", "/api/chat", "/api/chat"]


async def test_concurrent_first_calls_read_metadata_once(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama()
    use_server(server)

    async with OllamaModel(model="llama3.2:3b") as model:
        await asyncio.gather(model.generate(ask("One")), model.generate(ask("Two")))

    assert server.paths().count("/api/show") == 1


async def test_configured_context_window_still_reads_metadata_once(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama()
    use_server(server)

    async with OllamaModel(model="llama3.2:3b", context_window=4096) as model:
        capabilities = await model.capabilities()
        await model.generate(ask("One"))
        await model.generate(ask("Two"))

    assert capabilities.context_window == 4096
    assert capabilities.tools is True
    assert server.chat_bodies()[0]["options"]["num_ctx"] == 4096
    assert server.paths() == ["/api/show", "/api/chat", "/api/chat"]


async def test_configured_context_window_needs_no_context_length_in_metadata(
    use_server: Callable[[FakeOllama], None],
) -> None:
    use_server(FakeOllama(show={"model_info": {}, "capabilities": ["tools"]}))

    async with OllamaModel(model="llama3.2:3b", context_window=4096) as model:
        capabilities = await model.capabilities()

    assert capabilities.context_window == 4096


async def test_metadata_without_context_length_fails_explicitly(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama(show={"model_info": {"general.architecture": "llama"}})
    use_server(server)

    async with OllamaModel(model="llama3.2:3b") as model:
        with pytest.raises(ProviderError, match="context_window=") as caught:
            await model.generate(ask("Say hello"))

    assert caught.value.transient is False
    assert server.chat_bodies() == []


async def test_construction_does_no_io(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama()
    use_server(server)

    model = OllamaModel(model="llama3.2:3b")
    await model.close()

    assert server.requests == []


def test_context_window_must_be_positive() -> None:
    with pytest.raises(DefinitionError, match="context_window"):
        OllamaModel(model="llama3.2:3b", context_window=0)


async def test_unknown_model_is_not_transient(
    use_server: Callable[[FakeOllama], None],
) -> None:
    use_server(FakeOllama(show=status(404)))

    async with OllamaModel(model="missing:1b") as model:
        with pytest.raises(ProviderError, match="not installed") as caught:
            await model.generate(ask("Say hello"))

    assert caught.value.transient is False


@pytest.mark.parametrize(
    ("code", "transient"),
    [(400, False), (408, True), (429, True), (500, True), (503, True)],
)
async def test_http_errors_map_to_provider_errors(
    use_server: Callable[[FakeOllama], None], code: int, transient: bool
) -> None:
    use_server(FakeOllama(chat=status(code)))

    async with OllamaModel(model="llama3.2:3b") as model:
        with pytest.raises(ProviderError) as caught:
            await model.generate(ask("Say hello"))

    assert caught.value.transient is transient
    assert "details" not in str(caught.value)


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
    ids=["connection-refused", "timeout"],
)
async def test_unreachable_server_is_transient(
    use_server: Callable[[FakeOllama], None], failure: httpx.TransportError
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise failure

    use_server(FakeOllama(show=fail))

    async with OllamaModel(model="llama3.2:3b") as model:
        with pytest.raises(ProviderError, match="Could not reach") as caught:
            await model.generate(ask("Say hello"))

    assert caught.value.transient is True


async def test_close_closes_the_client_it_created(
    use_server: Callable[[FakeOllama], None],
) -> None:
    use_server(FakeOllama())
    model = OllamaModel(model="llama3.2:3b")

    async with model:
        pass

    with pytest.raises(RuntimeError, match="closed"):
        await model.generate(ask("Say hello"))


def test_missing_extra_names_the_extra_to_install() -> None:
    script = (
        "import sys; sys.modules['ollama'] = None\n"
        "import agentframe\n"
        "from agentframe import OllamaModel"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "agentframe-py[ollama]" in result.stderr


# Tools


TOOL_REQUEST = ModelRequest(
    messages=(Message(role="user", content="Where is order A1?"),),
    settings=ModelSettings(max_output_tokens=100),
    tools=(
        ToolSpec(
            name="lookup_order",
            description="Look up an order.",
            parameters={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        ),
    ),
)


def tool_call_reply(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    reply = chat_reply("", prompt_eval_count=20, eval_count=8)
    reply["message"]["tool_calls"] = [
        {"function": {"name": name, "arguments": arguments}}
        for name, arguments in calls
    ]
    return reply


async def test_tools_are_sent_in_ollama_function_shape(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama()
    use_server(server)

    async with OllamaModel(model="llama3.2:3b") as model:
        await model.generate(TOOL_REQUEST)

    [body] = server.chat_bodies()
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup_order",
                "description": "Look up an order.",
                "parameters": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            },
        }
    ]


async def test_tool_calls_are_parsed_with_generated_ids(
    use_server: Callable[[FakeOllama], None],
) -> None:
    reply = tool_call_reply(
        ("lookup_order", {"order_id": "A1"}), ("lookup_order", {"order_id": "B2"})
    )
    use_server(FakeOllama(chat=reply))

    async with OllamaModel(model="llama3.2:3b") as model:
        response = await model.generate(TOOL_REQUEST)

    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.text == ""
    assert response.tool_calls == (
        ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}'),
        ToolCall(id="call_2", name="lookup_order", arguments='{"order_id": "B2"}'),
    )
    assert response.usage.total_tokens == 28


async def test_tool_calls_and_results_are_replayed_with_tool_name(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama()
    use_server(server)
    call = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')
    request = ModelRequest(
        messages=(
            *TOOL_REQUEST.messages,
            Message(role="assistant", content="", tool_calls=(call,)),
            Message(
                role="tool",
                content='{"status": "shipped"}',
                tool_call_id="call_1",
                tool_name="lookup_order",
            ),
        ),
        settings=TOOL_REQUEST.settings,
        tools=TOOL_REQUEST.tools,
    )

    async with OllamaModel(model="llama3.2:3b") as model:
        await model.generate(request)

    [body] = server.chat_bodies()
    assert body["messages"][1:] == [
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "lookup_order", "arguments": {"order_id": "A1"}}}
            ],
        },
        {
            "role": "tool",
            "content": '{"status": "shipped"}',
            "tool_name": "lookup_order",
        },
    ]


@pytest.mark.parametrize(
    "show", [SHOW_WITHOUT_TOOLS, SHOW_OLDER_SERVER], ids=["no-tools", "older-server"]
)
async def test_models_without_listed_tool_support_fail_before_sending(
    use_server: Callable[[FakeOllama], None], show: dict[str, Any]
) -> None:
    server = FakeOllama(show=show)
    use_server(server)

    async with OllamaModel(model="llama3.2:3b", context_window=8192) as model:
        capabilities = await model.capabilities()
        with pytest.raises(UnsupportedCapabilityError, match="tools"):
            await model.generate(TOOL_REQUEST)

    assert capabilities.tools is False
    assert server.chat_bodies() == []


@tool(description="Look up an order's status.")
def lookup_order(order_id: str) -> str:
    return "shipped"


@agent(version="1.0.0")
class OrderDesk(Agent):
    """Answers questions about orders."""

    @skill("status", tags=["orders"])
    async def status(self, question: str, ctx: RunContext) -> str:
        """Answer a question about an order."""
        return await ctx.generate(question, tools=[lookup_order])


async def test_runtime_completes_a_tool_round_trip(
    use_server: Callable[[FakeOllama], None],
) -> None:
    replies = iter(
        [
            tool_call_reply(("lookup_order", {"order_id": "A1"})),
            chat_reply("Order A1 has shipped.", prompt_eval_count=30, eval_count=6),
        ]
    )
    server = FakeOllama(chat=lambda request: httpx.Response(200, json=next(replies)))
    use_server(server)

    async with OllamaModel(model="llama3.2:3b", context_window=8192) as model:
        result = await Runtime().run(
            OrderDesk(model=model, tools=[lookup_order]),
            skill="status",
            arguments={"question": "Where is order A1?"},
        )

    assert result.value == "Order A1 has shipped."
    assert result.model_calls == 2
    assert result.tool_calls == 1
    assert result.usage.total_tokens == 64
    second = server.chat_bodies()[1]
    assert second["messages"][-1] == {
        "role": "tool",
        "content": '"shipped"',
        "tool_name": "lookup_order",
    }


# Structured output


class Summary(BaseModel):
    text: str
    points: list[str] = []


@agent(version="1.0.0")
class Summarizer(Agent):
    """Summarizes text as a typed summary."""

    @skill("summarize", tags=["text"])
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        """Summarize text."""
        return await ctx.generate(text, output=Summary)

    @skill("failure", tags=["text"])
    async def failure(self, text: str, ctx: RunContext) -> str:
        """Return the name of the error a structured call raises."""
        try:
            await ctx.generate(text, output=Summary)
        except (ModelError, OutputValidationError) as error:
            return type(error).__name__
        return "no error"


def ask_for_summary() -> ModelRequest:
    return ModelRequest(
        messages=(Message(role="user", content="Summarize"),),
        settings=ModelSettings(max_output_tokens=100),
        output=OutputSchema(name="Summary", json_schema=Summary.model_json_schema()),
    )


async def test_structured_requests_send_the_schema_as_format(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama()
    use_server(server)

    async with OllamaModel(model="llama3.2:3b") as model:
        await model.generate(ask_for_summary())

    [body] = server.chat_bodies()
    assert body["format"] == Summary.model_json_schema()
    assert body["messages"] == [{"role": "user", "content": "Summarize"}]


async def test_text_requests_send_no_format(
    use_server: Callable[[FakeOllama], None],
) -> None:
    server = FakeOllama()
    use_server(server)

    async with OllamaModel(model="llama3.2:3b") as model:
        await model.generate(ask("Say hello"))

    [body] = server.chat_bodies()
    assert "format" not in body


async def test_structured_reply_is_validated_into_the_output_type(
    use_server: Callable[[FakeOllama], None],
) -> None:
    reply = chat_reply('{"text": "Short."}', prompt_eval_count=12, eval_count=3)
    use_server(FakeOllama(chat=reply))

    async with OllamaModel(model="llama3.2:3b") as model:
        summary = await Runtime().invoke(
            Summarizer(model=model), skill="summarize", arguments={"text": "Long."}
        )

    assert summary == Summary(text="Short.", points=[])


@pytest.mark.parametrize(
    ("ollama_reply", "error"),
    [
        (chat_reply('{"text": "Sh', done_reason="length"), "IncompleteOutputError"),
        (chat_reply("I would rather not."), "OutputValidationError"),
    ],
    ids=["truncated", "prose-instead-of-json"],
)
async def test_failed_structured_replies_raise_typed_errors_with_usage(
    use_server: Callable[[FakeOllama], None],
    ollama_reply: dict[str, Any],
    error: str,
) -> None:
    use_server(
        FakeOllama(chat={**ollama_reply, "prompt_eval_count": 12, "eval_count": 3})
    )

    async with OllamaModel(model="llama3.2:3b") as model:
        result = await Runtime().run(
            Summarizer(model=model), skill="failure", arguments={"text": "Long."}
        )

    assert result.value == error
    assert result.model_calls == 1
    assert result.usage.total_tokens == 15
