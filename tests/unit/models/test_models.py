"""Tests for the provider-neutral model types, protocol and errors."""

import subprocess
import sys
from types import TracebackType
from typing import Any, Self

import pytest

from agentframe import (
    DefinitionError,
    FinishReason,
    Message,
    Model,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    ProviderError,
    ToolCall,
    ToolSpec,
    Usage,
)

UNKNOWN_USAGE = Usage(
    input_tokens=None, output_tokens=None, total_tokens=None, estimated=False
)


class EchoModel:
    """A plain class, with no AgentFrame base, that satisfies ``Model``."""

    def __init__(self) -> None:
        self.capability_fetches = 0
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text=request.messages[-1].content,
            usage=Usage(
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                estimated=False,
            ),
            finish_reason=FinishReason.COMPLETED,
        )

    async def capabilities(self) -> ModelCapabilities:
        self.capability_fetches += 1
        return ModelCapabilities(context_window=8192)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


async def test_plain_class_is_a_model_usable_with_async_with() -> None:
    echo = EchoModel()
    model: Model = echo  # checked by mypy: no inheritance needed
    request = ModelRequest(
        messages=(
            Message(role="system", content="Repeat the user."),
            Message(role="user", content="Hello"),
        ),
        settings=ModelSettings(max_output_tokens=100),
    )

    async with model as entered:
        response = await entered.generate(request)
        capabilities = await entered.capabilities()

    assert response.text == "Hello"
    assert response.usage.total_tokens is None
    assert capabilities.context_window == 8192
    assert echo.closed


def test_max_output_tokens_must_be_positive() -> None:
    with pytest.raises(DefinitionError, match="max_output_tokens"):
        ModelSettings(max_output_tokens=0)


def test_model_request_rejects_unset_settings() -> None:
    # Adapters rely on fully resolved settings, e.g. an output-token cap.
    with pytest.raises(DefinitionError, match="unset: max_output_tokens"):
        ModelRequest(
            messages=(Message(role="user", content="Hi"),), settings=ModelSettings()
        )


def test_provider_error_carries_retry_and_accounting_details() -> None:
    partial = Usage(
        input_tokens=12, output_tokens=None, total_tokens=None, estimated=False
    )

    error = ProviderError(
        "The model provider is rate limiting requests.",
        transient=True,
        retry_after=2.5,
        usage=partial,
    )

    assert error.transient
    assert error.retry_after == 2.5
    assert error.usage == partial
    assert error.invocation_id is None
    assert error.message == "The model provider is rate limiting requests."


def test_importing_agentframe_loads_no_provider_sdk() -> None:
    check = (
        "import sys, agentframe; "
        "roots = {name.split('.')[0] for name in sys.modules}; "
        "loaded = {'openai', 'ollama', 'azure'} & roots; "
        "assert not loaded, loaded"
    )
    subprocess.run([sys.executable, "-c", check], check=True)


# Tool-call messages, requests and responses

LOOKUP = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')


def test_tool_call_conversation_shapes_are_valid() -> None:
    asked = Message(role="assistant", content="", tool_calls=(LOOKUP,))
    answered = Message(
        role="tool",
        content='{"status": "shipped"}',
        tool_call_id="call_1",
        tool_name="lookup_order",
    )

    assert asked.tool_calls == (LOOKUP,)
    assert answered.tool_call_id == asked.tool_calls[0].id


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        pytest.param(
            {"role": "user", "tool_calls": (LOOKUP,)},
            "Only assistant messages can have tool_calls",
            id="tool-calls-on-user",
        ),
        pytest.param(
            {"role": "tool", "tool_name": "lookup_order"},
            "needs the tool_call_id",
            id="tool-without-call-id",
        ),
        pytest.param(
            {"role": "assistant", "tool_call_id": "call_1"},
            "Only tool messages can have tool_call_id",
            id="call-id-on-assistant",
        ),
        pytest.param(
            {"role": "user", "tool_name": "lookup_order"},
            "Only tool messages can have tool_call_id or tool_name",
            id="tool-name-on-user",
        ),
    ],
)
def test_inconsistent_message_shapes_are_rejected(
    fields: dict[str, Any], error: str
) -> None:
    with pytest.raises(DefinitionError, match=error):
        Message(content="", **fields)


def test_text_requests_and_responses_have_no_tools_by_default() -> None:
    request = ModelRequest(
        messages=(Message(role="user", content="Hi"),),
        settings=ModelSettings(max_output_tokens=100),
    )
    response = ModelResponse(
        text="Hello", usage=UNKNOWN_USAGE, finish_reason=FinishReason.COMPLETED
    )

    assert request.tools == ()
    assert not request.uses_tools()
    assert response.tool_calls == ()
    assert response.provider_items == ()


@pytest.mark.parametrize(
    "request_",
    [
        pytest.param(
            ModelRequest(
                messages=(Message(role="user", content="Where is A1?"),),
                settings=ModelSettings(max_output_tokens=100),
                tools=(ToolSpec("lookup_order", "Look up an order.", {}),),
            ),
            id="offers-tools",
        ),
        pytest.param(
            ModelRequest(
                messages=(Message(role="assistant", content="", tool_calls=(LOOKUP,)),),
                settings=ModelSettings(max_output_tokens=100),
            ),
            id="replays-a-tool-call",
        ),
        pytest.param(
            ModelRequest(
                messages=(Message(role="tool", content="{}", tool_call_id="call_1"),),
                settings=ModelSettings(max_output_tokens=100),
            ),
            id="sends-a-tool-result",
        ),
    ],
)
def test_requests_with_tool_content_use_tools(request_: ModelRequest) -> None:
    assert request_.uses_tools()


def test_provider_items_are_hidden_from_repr_and_equality() -> None:
    secret = {"type": "reasoning", "encrypted_content": "SECRET-REASONING"}
    message = Message(
        role="assistant", content="", tool_calls=(LOOKUP,), provider_items=(secret,)
    )
    response = ModelResponse(
        text="",
        usage=UNKNOWN_USAGE,
        finish_reason=FinishReason.TOOL_CALLS,
        tool_calls=(LOOKUP,),
        provider_items=(secret,),
    )

    assert "SECRET-REASONING" not in repr(message)
    assert "SECRET-REASONING" not in repr(response)
    assert message == Message(role="assistant", content="", tool_calls=(LOOKUP,))
    assert response == ModelResponse(
        text="",
        usage=UNKNOWN_USAGE,
        finish_reason=FinishReason.TOOL_CALLS,
        tool_calls=(LOOKUP,),
    )
