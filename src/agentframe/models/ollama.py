"""``OllamaModel``: an adapter for models served by an Ollama server.

Needs the optional ``ollama`` package: ``pip install agentframe-py[ollama]``.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self, cast

try:
    import httpx
    import ollama
except ImportError as error:
    raise ImportError(
        "OllamaModel needs the optional 'ollama' package; "
        "install it with: pip install 'agentframe-py[ollama]'"
    ) from error

from agentframe.errors import DefinitionError, ProviderError, UnsupportedCapabilityError
from agentframe.models.base import (
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    TextSink,
    ToolCall,
    ToolSpec,
)
from agentframe.usage import FinishReason, Usage

DEFAULT_ENDPOINT = "http://localhost:11434"

# The ollama client waits forever by default. Connecting gets the same 10
# seconds as an Agent Card fetch, so an unreachable server fails quickly (and
# transiently). Reads, writes and pooled connections stay unbounded here: a
# long generation sends nothing until it finishes, so the run's deadline, not
# the HTTP client, decides how long a call may take.
CONNECT_TIMEOUT_SECONDS = 10.0
_TIMEOUT = httpx.Timeout(
    connect=CONNECT_TIMEOUT_SECONDS, read=None, write=None, pool=None
)


@dataclass(frozen=True)
class _Metadata:
    """What the server's show API says about a model, read once and cached."""

    context_window: int
    tools: bool


class OllamaModel:
    """A model served by an Ollama server, such as a locally executed Llama.

    The model must already be installed on the server: AgentFrame never pulls
    models, starts a server or switches to another model. Construction does no
    I/O; the model's metadata (tool support and, unless configured, the context
    window) is read from the server once on first use and cached.

    Every request sends the context window explicitly (Ollama's ``num_ctx``),
    so the server never silently truncates input to a smaller default.

    ``stream`` uses Ollama's streamed chat, passing on each piece of answer
    text; a stream that ends before its final chunk is a transient
    ``ProviderError``.

    For structured output, the request's JSON schema is sent as Ollama's
    ``format``, which constrains generation to that schema. Nothing is added
    to the prompt, so describe the expected format in the agent's trusted
    ``instructions``. Ollama has no refusal signal: a reply in prose instead
    of JSON fails validation in the generation layer.

    Tool support depends on the installed model: it is read from the
    ``capabilities`` the server lists for the model. Ollama returns no tool
    call IDs, so the adapter numbers the calls in each response.

    Prefer an explicit ``context_window`` sized for your inputs, such as
    ``8192``. The metadata size is usually the model's maximum, and Ollama
    allocates memory for the whole window: ``llama3.2:3b`` (131072 tokens)
    loads at about 18 GB instead of about 2 GB at 8192. Ollama also reloads
    the model, taking several seconds, whenever ``num_ctx`` changes.

    Connecting to the server times out after 10 seconds. Nothing else in a
    call is bounded by the HTTP client: the run's deadline
    (``RunLimits.timeout_seconds``) limits how long a generation may take.

    After ``close()`` the model cannot be used again; later calls fail with
    ``RuntimeError``. Create a new ``OllamaModel`` instead.

    Args:
        model: Tag of an installed model, for example ``"llama3.2:3b"``.
        endpoint: URL of the Ollama server.
        context_window: Tokens of input plus output to request from the server.
            ``None`` uses the size reported in the model's metadata (see the
            memory cost above).

    Raises:
        DefinitionError: If ``context_window`` is not greater than zero.
    """

    def __init__(
        self,
        *,
        model: str,
        endpoint: str = DEFAULT_ENDPOINT,
        context_window: int | None = None,
    ) -> None:
        if context_window is not None and context_window <= 0:
            raise DefinitionError("OllamaModel context_window must be greater than 0")
        self.model = model
        self.endpoint = endpoint
        self._configured_context_window = context_window
        self._metadata: _Metadata | None = None
        self._metadata_lock = asyncio.Lock()
        self._client = ollama.AsyncClient(host=endpoint, timeout=_TIMEOUT)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Send ``request`` to the server's chat API and return the full reply.

        Raises:
            UnsupportedCapabilityError: The request uses tools, but the model
                does not support them, or offers hosted tools, which Ollama
                does not run. Raised before sending.
            ProviderError: The call failed. Connection failures, timeouts,
                rate limits and server errors are transient; an unknown model
                or a rejected request is not.
        """
        options = await self._options(request)
        async with _provider_errors(self.model):
            response = await self._client.chat(
                model=self.model,
                messages=[_ollama_message(message) for message in request.messages],
                tools=[_ollama_tool(spec) for spec in request.tools],
                options=options,
                format=request.output.json_schema if request.output else None,
            )
        return _model_response(
            response, response.message.content or "", response.message.tool_calls
        )

    async def stream(self, request: ModelRequest, on_text: TextSink) -> ModelResponse:
        """Like ``generate``, but awaits ``on_text`` with each piece of text.

        Raises:
            UnsupportedCapabilityError: As for ``generate``.
            ProviderError: As for ``generate``, also when the server ends the
                stream without its final chunk (transient).
        """
        options = await self._options(request)
        text: list[str] = []
        tool_calls: list[ollama.Message.ToolCall] = []
        last: ollama.ChatResponse | None = None
        async with _provider_errors(self.model):
            chunks = await self._client.chat(
                model=self.model,
                messages=[_ollama_message(message) for message in request.messages],
                tools=[_ollama_tool(spec) for spec in request.tools],
                options=options,
                format=request.output.json_schema if request.output else None,
                stream=True,
            )
            # The client returns an async generator; closing it ends the HTTP
            # response at once when on_text raises or the run is cancelled.
            async with aclosing(cast(AsyncGenerator[ollama.ChatResponse], chunks)):
                async for chunk in chunks:
                    if chunk.message.content:
                        text.append(chunk.message.content)
                        await on_text(chunk.message.content)
                    tool_calls.extend(chunk.message.tool_calls or ())
                    last = chunk
        if last is None or not last.done:
            raise ProviderError(
                "The Ollama server ended the stream early", transient=True
            )
        return _model_response(last, "".join(text), tool_calls)

    async def _options(self, request: ModelRequest) -> dict[str, Any]:
        """Return the request's Ollama options, checking tool support first.

        Raises:
            UnsupportedCapabilityError: The request offers hosted tools, or
                uses tools the model does not support.
        """
        if request.hosted_tools:
            raise UnsupportedCapabilityError(
                "Ollama does not support hosted tool "
                f"{type(request.hosted_tools[0]).__name__}"
            )
        metadata = await self._get_metadata()
        if request.uses_tools() and not metadata.tools:
            raise UnsupportedCapabilityError(
                f"Ollama model {self.model!r} does not support tools"
            )
        return {
            "num_ctx": metadata.context_window,
            "num_predict": request.settings.max_output_tokens,
        }

    async def capabilities(self) -> ModelCapabilities:
        """Return the context window, structured-output and tool support.

        Structured output is reported for every model, because the server
        constrains generation to the schema rather than the model. Tools are
        reported only when the server lists ``"tools"`` in the model's
        capabilities; a server that lists none means no tools.

        Raises:
            ProviderError: The model's metadata could not be read, or it does
                not report a context length and none was configured.
        """
        metadata = await self._get_metadata()
        return ModelCapabilities(
            context_window=metadata.context_window,
            structured_output=True,
            tools=metadata.tools,
        )

    async def close(self) -> None:
        """Close the HTTP client this model created."""
        # The ollama client leaves close() without type hints.
        await self._client.close()  # type: ignore[no-untyped-call]

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
        async with _provider_errors(self.model):
            details = await self._client.show(self.model)
        context_window = self._configured_context_window or _context_length(
            details, self.model
        )
        return _Metadata(
            context_window=context_window,
            tools="tools" in (details.capabilities or []),
        )


def _context_length(details: ollama.ShowResponse, model: str) -> int:
    """Return the context length in a model's metadata.

    Raises:
        ProviderError: The metadata reports no valid context length.
    """
    model_info = details.modelinfo or {}
    architecture = model_info.get("general.architecture")
    context_length = model_info.get(f"{architecture}.context_length")
    if not isinstance(context_length, int) or context_length <= 0:
        raise ProviderError(
            f"Ollama did not report a context length for model {model!r}; "
            "pass context_window= to OllamaModel",
            transient=False,
        )
    return context_length


def _ollama_tool(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def _ollama_message(message: Message) -> dict[str, Any]:
    """Return ``message`` in the chat API's shape, including tool calls/results.

    Ollama links a tool result to its call by the tool's name, not an ID.
    """
    if message.role == "tool":
        return {
            "role": "tool",
            "content": message.content,
            "tool_name": message.tool_name,
        }
    ollama_message: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }
    if message.tool_calls:
        ollama_message["tool_calls"] = [
            {"function": {"name": call.name, "arguments": json.loads(call.arguments)}}
            for call in message.tool_calls
        ]
    return ollama_message


def _model_response(
    response: ollama.ChatResponse,
    text: str,
    calls: Sequence[ollama.Message.ToolCall] | None,
) -> ModelResponse:
    """Return the reply, given its final chunk (or whole reply) and its text."""
    tool_calls = _tool_calls(calls or ())
    return ModelResponse(
        text=text,
        usage=_usage(response),
        finish_reason=_finish_reason(response.done_reason, tool_calls),
        tool_calls=tool_calls,
    )


def _tool_calls(calls: Sequence[ollama.Message.ToolCall]) -> tuple[ToolCall, ...]:
    """Return the reply's tool calls, numbered because Ollama has no call IDs.

    Ollama returns arguments already parsed; they are turned back into JSON
    text so the runtime validates them like any other provider's.
    """
    return tuple(
        ToolCall(
            id=f"call_{number}",
            name=call.function.name,
            arguments=json.dumps(call.function.arguments),
        )
        for number, call in enumerate(calls, start=1)
    )


@asynccontextmanager
async def _provider_errors(model: str) -> AsyncIterator[None]:
    """Turn Ollama client errors into ``ProviderError`` with a safe message.

    The server's own error text is not passed on because it may echo request
    content back to the caller.
    """
    try:
        yield
    # The client raises the built-in ConnectionError when it cannot connect
    # and lets other httpx transport errors, such as timeouts, through.
    except (ConnectionError, httpx.TransportError) as error:
        raise ProviderError(
            "Could not reach the Ollama server", transient=True
        ) from error
    except ollama.ResponseError as error:
        raise _response_error(error.status_code, model) from error
    except ollama.RequestError as error:
        raise ProviderError("Ollama rejected the request", transient=False) from error


def _response_error(status_code: int, model: str) -> ProviderError:
    if status_code == 404:
        return ProviderError(
            f"Ollama model {model!r} is not installed on the server",
            transient=False,
        )
    if status_code in (408, 429) or status_code >= 500:
        return ProviderError(
            f"Ollama server error (HTTP {status_code})", transient=True
        )
    return ProviderError(
        f"Ollama rejected the request (HTTP {status_code})", transient=False
    )


def _usage(response: ollama.ChatResponse) -> Usage:
    input_tokens = response.prompt_eval_count
    output_tokens = response.eval_count
    total_tokens = None
    if input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated=False,
    )


def _finish_reason(
    done_reason: str | None, tool_calls: tuple[ToolCall, ...]
) -> FinishReason:
    # Ollama reports "stop" when the model asks to call tools.
    if tool_calls:
        return FinishReason.TOOL_CALLS
    if done_reason == "length":
        return FinishReason.LENGTH
    return FinishReason.COMPLETED
