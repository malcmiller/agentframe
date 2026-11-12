"""Provider-neutral model types and the ``Model`` protocol.

Model adapters translate these AgentFrame-owned types to and from their
provider's types, so skills never see provider SDKs.
"""

from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Protocol, Self

from agentframe.errors import DefinitionError
from agentframe.usage import FinishReason, Usage

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


@dataclass(frozen=True)
class ModelSettings:
    """Generation settings for one model call.

    Attributes:
        max_output_tokens: Most tokens the model may generate in one call.
            Must be greater than zero.

    Raises:
        DefinitionError: If ``max_output_tokens`` is not greater than zero.
    """

    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        if not self.max_output_tokens > 0:
            raise DefinitionError(
                "ModelSettings.max_output_tokens must be greater than 0"
            )


@dataclass(frozen=True)
class ModelRequest:
    """Everything a model needs for one call.

    Attributes:
        messages: The conversation, in order.
        settings: The effective settings for this call.
    """

    messages: tuple[Message, ...]
    settings: ModelSettings


@dataclass(frozen=True)
class ModelResponse:
    """The result of one model call.

    A response is returned for every finish reason; deciding whether a
    ``LENGTH`` or ``REFUSED`` finish is an error is the caller's job.

    Attributes:
        text: The generated text.
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
