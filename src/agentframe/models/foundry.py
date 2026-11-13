"""``FoundryModel``: an adapter for GPT deployments in Microsoft Foundry.

Calls the OpenAI v1 Responses API through the official ``openai`` package.
Needs the optional extra: ``pip install agentframe-py[foundry]``.
"""

import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from types import TracebackType
from typing import Any, Literal, Self, cast

try:
    import openai
    from azure.core.credentials_async import AsyncTokenCredential
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity.aio import get_bearer_token_provider
    from openai.types.responses import (
        EasyInputMessageParam,
        FunctionToolParam,
        Response,
        ResponseFunctionToolCall,
        ResponseFunctionToolCallParam,
        ResponseIncludable,
        ResponseInputItemParam,
        ResponseInputParam,
        ResponseTextConfigParam,
        ResponseUsage,
    )
    from openai.types.responses.response_input_param import FunctionCallOutput
except ImportError as error:
    raise ImportError(
        "FoundryModel needs the optional 'openai' and 'azure-identity' packages; "
        "install them with: pip install 'agentframe-py[foundry]'"
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
    Role,
    ToolCall,
    ToolSpec,
)
from agentframe.models.strict_schema import drop_defaulted_nulls, strict_schema
from agentframe.runtime.usage import FinishReason, Usage

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
"""Microsoft Entra scope for Foundry inference tokens."""

_INPUT_ROLES: dict[Role, Literal["developer", "user", "assistant"]] = {
    "system": "developer",
    "user": "user",
    "assistant": "assistant",
}
"""Responses API roles for text messages; tool results are separate input items."""

REASONING_INCLUDE: list[ResponseIncludable] = ["reasoning.encrypted_content"]
"""Asks for encrypted reasoning, so a stateless tool call can replay it."""

TRANSIENT_FAILURE_CODES = frozenset({"server_error", "rate_limit_exceeded"})
"""Codes of ``failed`` responses that retrying may fix."""


class FoundryModel:
    """A GPT deployment in Microsoft Foundry, called through the Responses API.

    Give either ``endpoint`` and ``credential``, in which case the model creates
    and owns its client, or an existing ``client``, which stays caller-owned.
    Construction does no I/O. SDK retries are always disabled, so AgentFrame
    is the only retry layer.

    The API does not report a deployment's context window, so it is unknown
    unless ``context_window`` is given. Unknown is never guessed.

    ``max_output_tokens`` caps visible output plus reasoning tokens, so
    reasoning models need a larger cap than their visible answer.

    Structured output uses the Responses API's strict JSON-schema format. The
    output model's schema is converted to the strict subset by
    ``strict_schema``; constructs it cannot express fail before sending.
    Fields with defaults become nullable, and the model's ``null`` for them
    is dropped so Pydantic applies the default.

    Tools are sent as strict function tools, with parameter schemas converted
    the same way; a tool whose parameters cannot be made strict fails before
    sending, with no non-strict fallback. A ``null`` for a defaulted tool
    parameter is dropped from the call's arguments, so the tool's default
    applies. Every output item of a tool-calling response, including
    reasoning items, is replayed verbatim in the next request. With
    ``store=False``, reasoning is requested in encrypted form for that replay;
    it is kept in memory for the run only and never logged or returned.

    Credentials are never logged or included in error messages.

    Args:
        deployment: Name of the model deployment, for example ``"gpt-5.4"``.
        endpoint: OpenAI v1 base URL, such as
            ``"https://<resource>.openai.azure.com/openai/v1/"``.
        credential: An API key, or an async Azure token credential such as
            ``azure.identity.aio.AzureCliCredential()``. A token credential
            stays caller-owned; the model never closes it. Production hosts
            can use ``DefaultAzureCredential()`` or managed identity; the
            ``foundry`` extra installs the ``aiohttp`` transport they need.
        client: An existing ``openai.AsyncOpenAI`` client to use instead of
            ``endpoint`` and ``credential``. It is used with retries disabled
            and is never closed by the model.
        context_window: Tokens of input plus output the deployment accepts,
            or ``None`` when unknown.
        store: Whether Foundry stores responses server-side. This controls
            response storage only, not every provider retention mechanism.
        structured_output: Whether to report structured-output support. The
            Responses API defines it, so it is on by default; turn it off for
            a deployment that rejects it. A rejection fails the call with a
            non-transient ``ProviderError``.
        tools: Whether to report tool-calling support. The Responses API
            defines function tools, so it is on by default; turn it off for a
            deployment that rejects them. A rejection fails the call with a
            non-transient ``ProviderError``.

    Raises:
        DefinitionError: If neither or both of ``client`` and
            ``endpoint``/``credential`` are given, or ``context_window`` is not
            greater than zero.
    """

    def __init__(
        self,
        *,
        deployment: str,
        endpoint: str | None = None,
        credential: str | AsyncTokenCredential | None = None,
        client: openai.AsyncOpenAI | None = None,
        context_window: int | None = None,
        store: bool = False,
        structured_output: bool = True,
        tools: bool = True,
    ) -> None:
        if context_window is not None and context_window <= 0:
            raise DefinitionError("FoundryModel context_window must be greater than 0")
        self.deployment = deployment
        self.store = store
        self.structured_output = structured_output
        self.tools = tools
        self._context_window = context_window
        self._owns_client = client is None
        self._client = _resolve_client(endpoint, credential, client)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Send ``request`` to the Responses API and return the full reply.

        Returns:
            The reply; for a structured-output request, its JSON text without
            the nulls that stand for defaults. A response that calls tools
            finishes ``TOOL_CALLS``, with the calls in the model's order. A
            refusal finishes ``REFUSED`` with empty text; output stopped at
            the token cap finishes ``LENGTH``, and output stopped by a content
            filter finishes ``CONTENT_FILTER``, without tool calls.

        Raises:
            UnsupportedCapabilityError: The output schema or a tool's
                parameter schema uses a construct strict mode cannot express.
                Raised before sending.
            IncompleteOutputError: The response stopped early for another reason.
            ProviderError: The call or the response failed. Connection failures,
                timeouts, rate limits and server errors are transient; rejected
                credentials, an unknown deployment and invalid requests,
                including a rejected output or tool schema, are not.
        """
        instructions, conversation = _split_instructions(request.messages)
        text = openai.omit if request.output is None else _text_format(request.output)
        tools = [_function_tool(spec) for spec in request.tools]
        # Stateless tool calls lose reasoning between turns unless it comes
        # back encrypted and is replayed.
        include = REASONING_INCLUDE if tools and not self.store else openai.omit
        async with _provider_errors(self.deployment, _rejection_hint(request)):
            response = await self._client.responses.create(
                model=self.deployment,
                instructions=instructions,
                input=conversation,
                max_output_tokens=request.settings.max_output_tokens,
                store=self.store,
                text=text,
                tools=tools or openai.omit,
                include=include,
            )
        model_response = _model_response(response, request.tools)
        if request.output is None:
            return model_response
        return _without_defaulted_nulls(model_response, request.output)

    async def capabilities(self) -> ModelCapabilities:
        """Return the configured context window, structured-output and tool support.

        Does no I/O: the API does not report a deployment's capabilities.
        """
        return ModelCapabilities(
            context_window=self._context_window,
            structured_output=self.structured_output,
            tools=self.tools,
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


def _resolve_client(
    endpoint: str | None,
    credential: str | AsyncTokenCredential | None,
    client: openai.AsyncOpenAI | None,
) -> openai.AsyncOpenAI:
    if client is not None:
        if endpoint is not None or credential is not None:
            raise DefinitionError(
                "Give FoundryModel either client= or endpoint= and credential=, "
                "not both"
            )
        # A copy with retries off; it shares, and never closes, the caller's
        # HTTP connections.
        return client.with_options(max_retries=0)
    if endpoint is None or credential is None:
        raise DefinitionError(
            "FoundryModel needs endpoint= and credential=, or an existing client="
        )
    api_key = (
        credential
        if isinstance(credential, str)
        else get_bearer_token_provider(credential, FOUNDRY_SCOPE)
    )
    return openai.AsyncOpenAI(base_url=endpoint, api_key=api_key, max_retries=0)


def _split_instructions(
    messages: tuple[Message, ...],
) -> tuple[str | None, ResponseInputParam]:
    """Send leading system messages as ``instructions``, the rest as input.

    A later system message keeps its place in the conversation as a
    ``developer`` message, the Responses API role for trusted instructions.
    """
    leading = 0
    while leading < len(messages) and messages[leading].role == "system":
        leading += 1
    instructions = "\n\n".join(message.content for message in messages[:leading])
    conversation: ResponseInputParam = [
        item for message in messages[leading:] for item in _input_items(message)
    ]
    return instructions or None, conversation


def _input_items(message: Message) -> list[ResponseInputItemParam]:
    """Return the Responses API input items for one message.

    An assistant message from this adapter is replayed as its original output
    items, in order, so reasoning items and call IDs reach the model unchanged.
    """
    if message.role == "tool":
        assert message.tool_call_id is not None  # Message requires it for tools.
        return [
            FunctionCallOutput(
                type="function_call_output",
                call_id=message.tool_call_id,
                output=message.content,
            )
        ]
    if message.provider_items:
        return [cast(ResponseInputItemParam, item) for item in message.provider_items]
    items: list[ResponseInputItemParam] = []
    if message.content or not message.tool_calls:
        items.append(
            EasyInputMessageParam(
                role=_INPUT_ROLES[message.role], content=message.content
            )
        )
    items.extend(
        ResponseFunctionToolCallParam(
            type="function_call",
            call_id=call.id,
            name=call.name,
            arguments=call.arguments,
        )
        for call in message.tool_calls
    )
    return items


def _function_tool(spec: ToolSpec) -> FunctionToolParam:
    """Return ``spec`` as a strict function tool.

    Raises:
        UnsupportedCapabilityError: The tool's parameters cannot be made strict.
    """
    try:
        parameters = strict_schema(spec.parameters)
    except UnsupportedCapabilityError as error:
        raise UnsupportedCapabilityError(
            f"Tool '{spec.name}' cannot be offered to Foundry: {error}"
        ) from None
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": parameters,
        "strict": True,
    }


def _text_format(output: OutputSchema) -> ResponseTextConfigParam:
    """Return the strict JSON-schema ``text`` setting for ``output``.

    Raises:
        UnsupportedCapabilityError: The schema cannot be made strict.
    """
    return {
        "format": {
            "type": "json_schema",
            "name": _format_name(output.name),
            "schema": strict_schema(output.json_schema),
            "strict": True,
        }
    }


def _format_name(name: str) -> str:
    """Make ``name`` a valid format name: ``^[a-zA-Z0-9_-]{1,64}$``.

    Generic Pydantic models have names such as ``Page[int]``.
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64] or "output"


def _without_defaulted_nulls(
    response: ModelResponse, output: OutputSchema
) -> ModelResponse:
    """Drop the nulls that stand for defaults from a completed JSON reply.

    Any other reply, including invalid JSON, is returned unchanged for the
    generation layer to reject.
    """
    if response.finish_reason is not FinishReason.COMPLETED:
        return response
    return replace(
        response, text=_drop_nulls_from_json(response.text, output.json_schema)
    )


def _drop_nulls_from_json(text: str, schema: dict[str, Any]) -> str:
    """Drop the nulls that stand for defaults from JSON ``text``.

    Invalid JSON is returned unchanged for validation to reject.
    """
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return json.dumps(drop_defaulted_nulls(value, schema))


def _model_response(response: Response, tools: tuple[ToolSpec, ...]) -> ModelResponse:
    """Map a Responses API result to a ``ModelResponse``, failing on error states."""
    usage = _usage(response.usage)
    if response.status not in ("completed", "incomplete"):
        raise _unfinished_response_error(response, usage)
    if _has_refusal(response):
        return ModelResponse(text="", usage=usage, finish_reason=FinishReason.REFUSED)
    if response.status == "incomplete":
        # Tool calls in a response cut short are not run: their arguments
        # may be incomplete.
        finish_reason = _incomplete_finish_reason(response, usage)
        return ModelResponse(
            text=response.output_text, usage=usage, finish_reason=finish_reason
        )
    tool_calls = _tool_calls(response, tools)
    if not tool_calls:
        return ModelResponse(
            text=response.output_text, usage=usage, finish_reason=FinishReason.COMPLETED
        )
    return ModelResponse(
        text=response.output_text,
        usage=usage,
        finish_reason=FinishReason.TOOL_CALLS,
        tool_calls=tool_calls,
        provider_items=tuple(item.to_dict(mode="json") for item in response.output),
    )


def _tool_calls(
    response: Response, tools: tuple[ToolSpec, ...]
) -> tuple[ToolCall, ...]:
    """Return the response's function calls, without nulls that stand for defaults.

    A call naming an unknown tool keeps its arguments as sent, for the runtime
    to reject.
    """
    schemas = {spec.name: spec.parameters for spec in tools}
    return tuple(
        _tool_call(item, schemas.get(item.name))
        for item in response.output
        if isinstance(item, ResponseFunctionToolCall)
    )


def _tool_call(
    item: ResponseFunctionToolCall, schema: dict[str, Any] | None
) -> ToolCall:
    arguments = item.arguments
    if schema is not None:
        arguments = _drop_nulls_from_json(arguments, schema)
    return ToolCall(id=item.call_id, name=item.name, arguments=arguments)


def _unfinished_response_error(response: Response, usage: Usage) -> ProviderError:
    if response.status == "failed":
        code = response.error.code if response.error else None
        return ProviderError(
            f"Foundry reported a failed response (code {code or 'unknown'})",
            transient=code in TRANSIENT_FAILURE_CODES,
            usage=usage,
        )
    if response.status == "cancelled":
        return ProviderError(
            "Foundry cancelled the response", transient=False, usage=usage
        )
    # queued and in_progress only happen for background responses, never sent.
    return ProviderError(
        f"Foundry returned an unfinished response (status {response.status})",
        transient=False,
        usage=usage,
    )


def _has_refusal(response: Response) -> bool:
    return any(
        content.type == "refusal"
        for item in response.output
        if item.type == "message"
        for content in item.content
    )


def _incomplete_finish_reason(response: Response, usage: Usage) -> FinishReason:
    reason = response.incomplete_details.reason if response.incomplete_details else None
    if reason == "max_output_tokens":
        return FinishReason.LENGTH
    if reason == "content_filter":
        return FinishReason.CONTENT_FILTER
    raise IncompleteOutputError(
        f"Foundry stopped the response early (reason {reason or 'unknown'})",
        usage=usage,
    )


def _usage(usage: ResponseUsage | None) -> Usage:
    if usage is None:
        return Usage(
            input_tokens=None, output_tokens=None, total_tokens=None, estimated=False
        )
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
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
async def _provider_errors(
    deployment: str, features: str | None
) -> AsyncIterator[None]:
    """Turn client and credential errors into ``ProviderError`` with a safe message.

    The service's own error text is not passed on because it may echo request
    content back to the caller. ``features`` names the optional features the
    request used, such as ``"tools"``, for the message when it is rejected.
    """
    try:
        yield
    except openai.APIConnectionError as error:  # Includes timeouts.
        raise ProviderError("Could not reach Foundry", transient=True) from error
    except openai.APIStatusError as error:
        raise _status_error(error, deployment, features) from error
    except ClientAuthenticationError as error:
        raise ProviderError(
            "Could not get a Microsoft Entra token for Foundry", transient=False
        ) from error
    except openai.OpenAIError as error:
        raise ProviderError("The Foundry call failed", transient=False) from error


def _status_error(
    error: openai.APIStatusError, deployment: str, features: str | None
) -> ProviderError:
    status_code = error.status_code
    if status_code in (401, 403):
        return ProviderError(
            f"Foundry rejected the credentials (HTTP {status_code})", transient=False
        )
    if status_code == 404:
        return ProviderError(
            f"Foundry deployment {deployment!r} was not found", transient=False
        )
    if status_code in (408, 429) or status_code >= 500:
        return ProviderError(
            f"Foundry could not handle the call now (HTTP {status_code})",
            transient=True,
            retry_after=_retry_after(error.response.headers),
        )
    if features:
        return ProviderError(
            f"Foundry rejected the request (HTTP {status_code}); the deployment "
            f"may not support {features}, or these schemas",
            transient=False,
        )
    return ProviderError(
        f"Foundry rejected the request (HTTP {status_code})", transient=False
    )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Read the retry hint in seconds, preferring ``retry-after-ms``.

    Azure OpenAI sends ``retry-after-ms`` in milliseconds alongside the
    standard ``Retry-After`` in seconds. HTTP dates are ignored.
    """
    milliseconds = _number(headers.get("retry-after-ms"))
    if milliseconds is not None:
        return milliseconds / 1000
    return _number(headers.get("retry-after"))


def _number(header: str | None) -> float | None:
    try:
        return float(header) if header is not None else None
    except ValueError:
        return None
