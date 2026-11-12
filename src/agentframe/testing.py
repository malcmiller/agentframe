"""Test helpers for agents that use models, without calling a provider.

Example::

    from agentframe import Message, ModelRequest, ModelSettings
    from agentframe.testing import ScriptedModel

    model = ScriptedModel(["Hello!"])
    request = ModelRequest(
        messages=(Message(role="user", content="Hi"),),
        settings=ModelSettings(max_output_tokens=100),
    )
    response = await model.generate(request)
    assert response.text == "Hello!"
    assert model.requests == [request]
"""

from collections import deque
from collections.abc import Iterable
from types import TracebackType
from typing import Self

from agentframe.models import ModelCapabilities, ModelRequest, ModelResponse
from agentframe.usage import FinishReason, Usage

type ScriptedReply = str | ModelResponse | Exception
"""One scripted model call: text, a full response, or an error to raise."""

_UNKNOWN_USAGE = Usage(
    input_tokens=None, output_tokens=None, total_tokens=None, estimated=False
)


class ScriptedModel:
    """A deterministic ``Model`` that replays scripted replies in order.

    Each ``generate`` call records its request, then uses the next reply:
    a ``str`` returns a completed response with unknown usage, a
    ``ModelResponse`` is returned as given, and an exception is raised.

    Args:
        replies: The replies for successive ``generate`` calls.
        capabilities: What ``capabilities()`` returns. Defaults to
            ``ModelCapabilities()``, which supports nothing optional.

    Attributes:
        requests: Every request received by ``generate``, in order.
        closed: Whether ``close()`` has been called.
    """

    def __init__(
        self,
        replies: Iterable[ScriptedReply] = (),
        *,
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        self._replies = deque(replies)
        self._capabilities = capabilities or ModelCapabilities()
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Record ``request`` and return or raise the next scripted reply.

        Raises:
            Exception: The next scripted reply, when it is an exception.
            AssertionError: All scripted replies have been used.
        """
        self.requests.append(request)
        if not self._replies:
            raise AssertionError(
                f"ScriptedModel was called {len(self.requests)} times "
                f"but only {len(self.requests) - 1} replies were scripted"
            )
        reply = self._replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            return ModelResponse(
                text=reply, usage=_UNKNOWN_USAGE, finish_reason=FinishReason.COMPLETED
            )
        return reply

    async def capabilities(self) -> ModelCapabilities:
        """Return the scripted capabilities."""
        return self._capabilities

    async def close(self) -> None:
        """Record that the model was closed."""
        self.closed = True

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
