"""Provider-neutral model types and the ``Model`` protocol.

Model adapters translate these AgentFrame-owned types to and from their
provider's types, so skills never see provider SDKs.
"""

from dataclasses import dataclass, fields
from types import TracebackType
from typing import Any, Literal, Protocol, Self

from agentframe.errors import DefinitionError
from agentframe.runtime.usage import FinishReason, Usage

type Role = Literal["system", "user", "assistant"]
"""Who a message is from. ``system`` messages hold trusted instructions only."""


@dataclass(frozen=True)
class Message:
    """One text message sent to or received from a model.

    Attributes:
        role: Who the message is from. Untrusted user text is always a
            ``user`` message, never merged into a ``system`` message.
        content: The message text.
    """

    role: Role
    content: str


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

    Raises:
        DefinitionError: If any field of ``settings`` is unset (``None``).
    """

    messages: tuple[Message, ...]
    settings: ModelSettings
    output: OutputSchema | None = None

    def __post_init__(self) -> None:
        unset = [
            field.name
            for field in fields(self.settings)
            if getattr(self.settings, field.name) is None
        ]
        if unset:
            raise DefinitionError(
                "ModelRequest.settings must be fully resolved; unset: "
                + ", ".join(unset)
            )


@dataclass(frozen=True)
class ModelResponse:
    """The result of one model call.

    A response is returned for every finish reason; deciding whether a
    ``LENGTH`` or ``REFUSED`` finish is an error is the caller's job.

    Attributes:
        text: The generated text; the JSON text for a structured-output call.
        usage: Tokens the call consumed. Unknown counts are ``None``, not zero.
        finish_reason: Why the model stopped.
    """

    text: str
    usage: Usage
    finish_reason: FinishReason


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
