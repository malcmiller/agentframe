"""Tests for the provider-neutral model types, protocol and errors."""

import subprocess
import sys
from types import TracebackType
from typing import Self

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
    Usage,
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
