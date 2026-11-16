"""``AnthropicModel``: an adapter for Claude models on the Anthropic API.

Calls the Messages API through the official ``anthropic`` package.
Needs the optional extra: ``pip install agentframe-py[anthropic]``.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self, cast

try:
    import anthropic
    from anthropic.types import (
        AssistantMessageParam,
        ContentBlock,
        JSONOutputFormatParam,
        MessageParam,
        ModelInfo,
        OutputConfigParam,
        ToolParam,
        ToolResultBlockParam,
        ToolUseBlock,
        ToolUseBlockParam,
        UserMessageParam,
    )
    from anthropic.types import Message as AnthropicMessage
    from anthropic.types import Usage as AnthropicUsage
except ImportError as error:
    raise ImportError(
        "AnthropicModel needs the optional 'anthropic' package; "
        "install it with: pip install 'agentframe-py[anthropic]'"
    ) from error

from agentframe.errors import (
    DefinitionError,
    IncompleteOutputError,
    ProviderError,
    UnsupportedCapabilityError,
)
from agentframe.models.base import (
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    OutputSchema,
    ToolCall,
    ToolSpec,
)
from agentframe.usage import FinishReason, Usage

# The anthropic client allows 10 minutes per request by default. Connecting
# gets the same 10 seconds as an Agent Card fetch and an Ollama server, so an
# unreachable API fails quickly (and transiently). Reads, writes and pooled
# connections are not bounded here: a non-streaming generation sends nothing
# until it finishes, so the run's deadline, not the HTTP client, decides how
# long a call may take.
CONNECT_TIMEOUT_SECONDS = 10.0
BASE_URL = "https://api.anthropic.com"
_TIMEOUT = anthropic.Timeout(
    connect=CONNECT_TIMEOUT_SECONDS, read=None, write=None, pool=None
)

_FINISH_REASONS: dict[str, FinishReason] = {
    "end_turn": FinishReason.COMPLETED,
    "stop_sequence": FinishReason.COMPLETED,
    "tool_use": FinishReason.TOOL_CALLS,
    "max_tokens": FinishReason.LENGTH,
    "model_context_window_exceeded": FinishReason.LENGTH,
    "refusal": FinishReason.REFUSED,
}
"""Finish reasons for the Messages API stop reasons AgentFrame expects.

Any other stop reason, such as ``pause_turn`` (only for server tools, which
AgentFrame never sends), raises ``IncompleteOutputError``.
"""


@dataclass(frozen=True)
class _Metadata:
    """What the Models API says about a model, read once and cached."""

    context_window: int | None
    structured_output: bool


class AnthropicModel:
    """A Claude model, called through the Anthropic Messages API.

    Give either ``api_key``, in which case the model creates and owns its
    client, or an existing ``client``, which stays caller-owned. A created
    client reads neither the key nor the API's address from the environment.
    Construction does no I/O; the
    model's metadata (its context window and structured-output support) is
    read from the Models API once on first use and cached. SDK retries are
    always disabled, so AgentFrame is the only retry layer.

    Leading ``system`` messages are sent as the ``system`` prompt, the
    Messages API's documented place for instructions; a later ``system``
    message fails before sending.

    Structured output uses the Messages API's JSON-schema output format,
    with the schema converted by the official client's ``transform_schema``:
    constraints the API does not support, such as ``minLength`` or
    ``default``, move into the field's description, and the generation layer
    still validates the reply against the full schema.

    Tools are sent with their parameter schemas unchanged; the runtime
    validates every call's arguments. A tool-calling reply's content blocks,
    such as thinking blocks, are replayed verbatim in the next request; they
    are kept in memory for the run only and never logged or returned.

    Connecting times out after 10 seconds. Nothing else in a call is bounded
    by the HTTP client: the run's deadline (``RunLimits.timeout_seconds``)
    limits how long a generation may take. An injected client keeps its own
    timeouts.

    Credentials are never logged or included in error messages.

    Args:
        model: The model ID, for example ``"claude-sonnet-4-6"``.
        api_key: An Anthropic API key. The model creates and owns a client
            that uses it.
        client: An existing ``anthropic.AsyncAnthropic`` client to use
            instead of ``api_key``. It is used with retries disabled and is
            never closed by the model.
        context_window: Tokens of input plus output the model accepts.
            ``None`` uses the Models API's ``max_input_tokens``; when that is
            missing too, the context window is unknown, never guessed.

    Raises:
        DefinitionError: If neither or both of ``client`` and ``api_key`` are
            given, or ``context_window`` is not greater than zero.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        context_window: int | None = None,
    ) -> None:
        if context_window is not None and context_window <= 0:
            raise DefinitionError(
                "AnthropicModel context_window must be greater than 0"
            )
        self.model = model
        self._configured_context_window = context_window
        self._metadata: _Metadata | None = None
        self._metadata_lock = asyncio.Lock()
        self._owns_client = client is None
        self._client = _resolve_client(api_key, client)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Send ``request`` to the Messages API and return the full reply.

        Returns:
            The reply. A response that calls tools finishes ``TOOL_CALLS``,
            with the calls in the model's order. A refusal finishes
            ``REFUSED`` with empty text; output stopped at the token cap or
            the context window finishes ``LENGTH``, without tool calls.

        Raises:
            UnsupportedCapabilityError: A ``system`` message follows another
                kind of message, the output schema cannot be converted, or
                the request offers hosted tools, which Anthropic models do
                not support here. Raised before sending.
            IncompleteOutputError: The response stopped early for another reason.
            ProviderError: The call failed. Connection failures, timeouts,
                rate limits, overload and server errors are transient;
                rejected credentials, an unknown model and invalid requests,
                including a rejected output or tool schema, are not.
        """
        if request.hosted_tools:
            raise UnsupportedCapabilityError(
                "AnthropicModel does not support hosted tool "
                f"{type(request.hosted_tools[0]).__name__}"
            )
        system, conversation = _split_system(request.messages)
        output_config: OutputConfigParam | anthropic.Omit = (
            {"format": _output_format(request.output)}
            if request.output is not None
            else anthropic.omit
        )
        tools = [_tool(spec) for spec in request.tools]
        max_tokens = request.settings.max_output_tokens
        assert max_tokens is not None  # ModelRequest requires every setting.
        async with _provider_errors(self.model, _rejection_hint(request)):
            response = await self._client.messages.create(
                model=self.model,
                system=system or anthropic.omit,
                messages=conversation,
                max_tokens=max_tokens,
                output_config=output_config,
                tools=tools or anthropic.omit,
            )
        return _model_response(response)

    async def capabilities(self) -> ModelCapabilities:
        """Return the context window, structured-output and tool support.

        Every Claude model calls tools through the Messages API, so tools are
        always reported. Structured output is reported only when the Models
        API says the model supports it; missing information means unsupported.

        Raises:
            ProviderError: The model's metadata could not be read.
        """
        metadata = await self._get_metadata()
        return ModelCapabilities(
            context_window=metadata.context_window,
            structured_output=metadata.structured_output,
            tools=True,
        )

    async def close(self) -> None:
        """Close the client this model created; an injected client stays open."""
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> Self:
        """Return the model itself."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the model."""
        await self.close()

    async def _get_metadata(self) -> _Metadata:
        # The lock makes concurrent first calls share a single metadata fetch.
        async with self._metadata_lock:
            if self._metadata is None:
                self._metadata = await self._fetch_metadata()
            return self._metadata

    async def _fetch_metadata(self) -> _Metadata:
        async with _provider_errors(self.model, features=None):
            info = await self._client.models.retrieve(self.model)
        return _Metadata(
            context_window=self._configured_context_window or info.max_input_tokens,
            structured_output=_supports_structured_output(info),
        )


def _resolve_client(
    api_key: str | None, client: anthropic.AsyncAnthropic | None
) -> anthropic.AsyncAnthropic:
    if client is not None:
        if api_key is not None:
            raise DefinitionError(
                "Give AnthropicModel either client= or api_key=, not both"
            )
        # A copy with retries off; it shares, and never closes, the caller's
        # HTTP connections.
        return client.with_options(max_retries=0)
    if api_key is None:
        raise DefinitionError("AnthropicModel needs api_key= or an existing client=")
    # Passing the base URL stops ANTHROPIC_BASE_URL from redirecting the key.
    return anthropic.AsyncAnthropic(
        api_key=api_key, base_url=BASE_URL, max_retries=0, timeout=_TIMEOUT
    )


def _supports_structured_output(info: ModelInfo) -> bool:
    if info.capabilities is None:
        return False
    return info.capabilities.structured_outputs.supported


def _split_system(
    messages: tuple[Message, ...],
) -> tuple[str | None, list[MessageParam]]:
    """Send leading system messages as the system prompt, the rest as messages.

    Consecutive tool results and other messages of the same Anthropic role
    are merged into one message, because the API expects roles to alternate.

    Raises:
        UnsupportedCapabilityError: A system message follows another message.
    """
    leading = 0
    while leading < len(messages) and messages[leading].role == "system":
        leading += 1
    system = "\n\n".join(message.content for message in messages[:leading])
    conversation: list[MessageParam] = []
    for message in messages[leading:]:
        if message.role == "system":
            raise UnsupportedCapabilityError(
                "Anthropic accepts system instructions only before the conversation"
            )
        role: Literal["user", "assistant"] = (
            "assistant" if message.role == "assistant" else "user"
        )
        blocks = _content_blocks(message)
        if conversation and conversation[-1]["role"] == role:
            cast(list[Any], conversation[-1]["content"]).extend(blocks)
        elif role == "assistant":
            conversation.append(AssistantMessageParam(role=role, content=blocks))
        else:
            conversation.append(UserMessageParam(role=role, content=blocks))
    return system or None, conversation


def _content_blocks(message: Message) -> list[Any]:
    """Return the Messages API content blocks for one message.

    An assistant message from this adapter is replayed as its original
    content blocks, in order, so thinking blocks and call IDs reach the model
    unchanged.
    """
    if message.role == "tool":
        assert message.tool_call_id is not None  # Message requires it for tools.
        result: ToolResultBlockParam = {
            "type": "tool_result",
            "tool_use_id": message.tool_call_id,
            "content": message.content,
        }
        return [result]
    if message.provider_items:
        return list(message.provider_items)
    blocks: list[Any] = []
    if message.content or not message.tool_calls:
        blocks.append({"type": "text", "text": message.content})
    blocks.extend(_tool_use_block(call) for call in message.tool_calls)
    return blocks


def _tool_use_block(call: ToolCall) -> ToolUseBlockParam:
    return {
        "type": "tool_use",
        "id": call.id,
        "name": call.name,
        "input": json.loads(call.arguments),
    }


def _tool(spec: ToolSpec) -> ToolParam:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.parameters,
    }


def _output_format(output: OutputSchema) -> JSONOutputFormatParam:
    """Return the JSON-schema output format for ``output``.

    Raises:
        UnsupportedCapabilityError: The schema cannot be converted, for
            example a value of any type (``Any``).
    """
    try:
        schema = anthropic.transform_schema(output.json_schema)
    except ValueError as error:
        raise UnsupportedCapabilityError(
            f"Output '{output.name}' cannot be sent to Anthropic: {error}"
        ) from None
    return {"type": "json_schema", "schema": schema}


def _model_response(response: AnthropicMessage) -> ModelResponse:
    """Map a Messages API reply to a ``ModelResponse``."""
    usage = _usage(response.usage)
    finish_reason = _FINISH_REASONS.get(response.stop_reason or "")
    if finish_reason is None:
        raise IncompleteOutputError(
            "Anthropic stopped the response early "
            f"(reason {response.stop_reason or 'unknown'})",
            usage=usage,
        )
    if finish_reason is FinishReason.REFUSED:
        return ModelResponse(text="", usage=usage, finish_reason=finish_reason)
    text = "".join(block.text for block in response.content if block.type == "text")
    if finish_reason is not FinishReason.TOOL_CALLS:
        # Tool calls in a response cut short are not run: their arguments
        # may be incomplete.
        return ModelResponse(text=text, usage=usage, finish_reason=finish_reason)
    return ModelResponse(
        text=text,
        usage=usage,
        finish_reason=FinishReason.TOOL_CALLS,
        tool_calls=_tool_calls(response.content),
        provider_items=tuple(block.to_dict(mode="json") for block in response.content),
    )


def _tool_calls(content: list[ContentBlock]) -> tuple[ToolCall, ...]:
    """Return the reply's tool calls, with arguments as JSON text.

    Anthropic returns arguments already parsed; they are turned back into
    JSON text so the runtime validates them like any other provider's.
    """
    return tuple(
        ToolCall(id=block.id, name=block.name, arguments=json.dumps(block.input))
        for block in content
        if isinstance(block, ToolUseBlock)
    )


def _usage(usage: AnthropicUsage) -> Usage:
    """Return the call's usage; input includes tokens written to or read from cache."""
    input_tokens = (
        usage.input_tokens
        + (usage.cache_creation_input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
    )
    return Usage(
        input_tokens=input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=input_tokens + usage.output_tokens,
        estimated=False,
    )


def _rejection_hint(request: ModelRequest) -> str | None:
    """Name the optional features a rejected request used, or ``None``."""
    features = []
    if request.tools:
        features.append("tools")
    if request.output is not None:
        features.append("structured output")
    return " or ".join(features) or None


@asynccontextmanager
async def _provider_errors(model: str, features: str | None) -> AsyncIterator[None]:
    """Turn client errors into ``ProviderError`` with a safe message.

    The service's own error text is not passed on because it may echo request
    content back to the caller. ``features`` names the optional features the
    request used, such as ``"tools"``, for the message when it is rejected.
    """
    try:
        yield
    except anthropic.APIConnectionError as error:  # Includes timeouts.
        raise ProviderError("Could not reach Anthropic", transient=True) from error
    except anthropic.APIStatusError as error:
        raise _status_error(error, model, features) from error
    except anthropic.AnthropicError as error:
        raise ProviderError("The Anthropic call failed", transient=False) from error


def _status_error(
    error: anthropic.APIStatusError, model: str, features: str | None
) -> ProviderError:
    status_code = error.status_code
    if status_code in (401, 403):
        return ProviderError(
            f"Anthropic rejected the credentials (HTTP {status_code})",
            transient=False,
        )
    if status_code == 404:
        return ProviderError(
            f"Anthropic model {model!r} was not found", transient=False
        )
    # 529 means the API is overloaded.
    if status_code in (408, 429) or status_code >= 500:
        return ProviderError(
            f"Anthropic could not handle the call now (HTTP {status_code})",
            transient=True,
            retry_after=_retry_after(error.response.headers),
        )
    if features:
        return ProviderError(
            f"Anthropic rejected the request (HTTP {status_code}); the model "
            f"may not support {features}, or these schemas",
            transient=False,
        )
    return ProviderError(
        f"Anthropic rejected the request (HTTP {status_code})", transient=False
    )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Read a numeric ``Retry-After`` in seconds; HTTP dates are ignored."""
    try:
        return float(headers["retry-after"])
    except (KeyError, ValueError):
        return None
