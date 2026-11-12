"""Provider-neutral model types and the ``Model`` protocol.

Model adapters translate these AgentFrame-owned types to and from their
provider's types, so skills never see provider SDKs.
"""

from dataclasses import dataclass, field, fields
from types import TracebackType
from typing import Any, Literal, Protocol, Self

from agentframe.errors import DefinitionError
from agentframe.runtime.usage import FinishReason, Usage

type Role = Literal["system", "user", "assistant", "tool"]
"""Who a message is from. ``system`` messages hold trusted instructions only."""


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the model sees it.

    Attributes:
        name: The tool's name, used by the model to call it.
        description: What the tool does, for the model.
        parameters: The JSON schema of the tool's business arguments.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One tool call requested by a model.

    Attributes:
        id: The provider's call ID. Adapters generate one when the provider
            has none, so every result can be linked to its call.
        name: The name of the tool to call.
        arguments: The arguments as raw JSON text. The runtime validates them,
            so invalid JSON and invalid fields fail the same way.
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Message:
    """One message sent to or received from a model.

    Attributes:
        role: Who the message is from. Untrusted user text is always a
            ``user`` message, never merged into a ``system`` message. Tool
            results are ``tool`` messages.
        content: The message text. For a ``tool`` message, the tool's result.
        tool_calls: The tools an ``assistant`` message asked to call.
        tool_call_id: For a ``tool`` message, the ID of the call it answers.
        tool_name: For a ``tool`` message, the name of the tool that ran.
        provider_items: Opaque data a model adapter needs replayed in its
            next request, such as reasoning items. Kept in memory for the run
            only; never inspected, logged, compared or shown in ``repr``.

    Raises:
        DefinitionError: ``tool_calls`` is set on a message that is not from
            the ``assistant``, a ``tool`` message has no ``tool_call_id``, or
            ``tool_call_id`` / ``tool_name`` is set on a non-``tool`` message.
    """

    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None
    provider_items: tuple[object, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.tool_calls and self.role != "assistant":
            raise DefinitionError("Only assistant messages can have tool_calls")
        if self.role == "tool" and not self.tool_call_id:
            raise DefinitionError("A tool message needs the tool_call_id it answers")
        has_tool_fields = self.tool_call_id is not None or self.tool_name is not None
        if self.role != "tool" and has_tool_fields:
            raise DefinitionError(
                "Only tool messages can have tool_call_id or tool_name"
            )


@dataclass(frozen=True, kw_only=True)
class ModelSettings:
    """Generation settings for model calls.

    Every field defaults to ``None``, meaning "unset": the value is inherited
    from the next lower level (library defaults -> agent -> skill -> per-run).
    The runtime fills every unset field from ``DEFAULT_MODEL_SETTINGS``, so a
    ``ModelRequest`` built by the runtime always has every field set.

    Attributes:
        max_output_tokens: Most tokens the model may generate in one call.
            Must be greater than zero when set. Library default: 4096.

    Raises:
        DefinitionError: If ``max_output_tokens`` is set but not greater
            than zero.
    """

    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and not self.max_output_tokens > 0:
            raise DefinitionError(
                "ModelSettings.max_output_tokens must be greater than 0"
            )


DEFAULT_MODEL_SETTINGS = ModelSettings(max_output_tokens=4096)
"""The library defaults: the lowest precedence level, with every field set."""


@dataclass(frozen=True)
class OutputSchema:
    """The JSON shape a model must return for a structured-output call.

    Attributes:
        name: The output type's name, for example ``"Summary"``.
        json_schema: The output type's JSON schema, as produced by Pydantic's
            ``model_json_schema()``. Adapters translate it to their
            provider's format; they never validate the reply themselves.
    """

    name: str
    json_schema: dict[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    """Everything a model needs for one call.

    Attributes:
        messages: The conversation, in order.
        settings: The fully resolved settings for this call. Every field
            must be set, so adapters never have to guess a default.
        output: The JSON shape the reply must have, or ``None`` for plain text.
            When set, the adapter returns the JSON as ``ModelResponse.text``.
        tools: The tools the model may call. Empty means no tools.

    Raises:
        DefinitionError: If any field of ``settings`` is unset (``None``).
    """

    messages: tuple[Message, ...]
    settings: ModelSettings
    output: OutputSchema | None = None
    tools: tuple[ToolSpec, ...] = ()

    def __post_init__(self) -> None:
        unset = [
            setting.name
            for setting in fields(self.settings)
            if getattr(self.settings, setting.name) is None
        ]
        if unset:
            raise DefinitionError(
                "ModelRequest.settings must be fully resolved; unset: "
                + ", ".join(unset)
            )

    def uses_tools(self) -> bool:
        """Return whether the request offers tools or carries tool calls/results."""
        return bool(self.tools) or any(
            message.role == "tool" or message.tool_calls for message in self.messages
        )


@dataclass(frozen=True)
class ModelResponse:
    """The result of one model call.

    A response is returned for every finish reason; deciding whether a
    ``LENGTH`` or ``REFUSED`` finish is an error is the caller's job.

    Attributes:
        text: The generated text; the JSON text for a structured-output call.
            May be empty when the model only asks to call tools.
        usage: Tokens the call consumed. Unknown counts are ``None``, not zero.
        finish_reason: Why the model stopped; ``TOOL_CALLS`` when it asks to
            call tools.
        tool_calls: The tools the model asks to call, in its order.
        provider_items: Opaque data the adapter needs replayed in the next
            request (see ``Message.provider_items``). Never inspected, logged,
            compared or shown in ``repr``.
    """

    text: str
    usage: Usage
    finish_reason: FinishReason
    tool_calls: tuple[ToolCall, ...] = ()
    provider_items: tuple[object, ...] = field(default=(), repr=False, compare=False)


@dataclass(frozen=True)
class ModelCapabilities:
    """What a model actually supports. Unknown support counts as unsupported.

    Attributes:
        context_window: Most tokens of input plus output the model accepts,
            or ``None`` when unknown. Unknown is not unlimited.
        structured_output: Whether the model can return schema-constrained output.
        tools: Whether the model can call tools.
    """

    context_window: int | None = None
    structured_output: bool = False
    tools: bool = False


class Model(Protocol):
    """A language model that AgentFrame can call.

    Any class with these methods is a ``Model``; no base class is needed.
    Constructing a model must not do any I/O. A model closes only the clients
    it created; clients passed in by the caller stay caller-owned.
    """

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a response to ``request``.

        Raises:
            ProviderError: The provider call failed. ``transient`` says whether
                retrying may help.
            UnsupportedCapabilityError: The request needs something the model
                does not support. Raised before anything is sent.
        """
        ...

    async def capabilities(self) -> ModelCapabilities:
        """Return what the model supports.

        Fetched lazily on first call (which may do I/O, so callers bound it with
        their own deadline) and cached afterwards.

        Raises:
            ProviderError: The capabilities could not be fetched.
        """
        ...

    async def close(self) -> None:
        """Release the clients this model created."""
        ...

    async def __aenter__(self) -> Self:
        """Return the model itself."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the model."""
        ...
