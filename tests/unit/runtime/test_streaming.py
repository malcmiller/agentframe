"""Tests for streaming model text to ``Runtime.run(on_text=...)``.

Models are scripted, and ``wait_before_retry`` is replaced by a recorder, so
no test calls a provider or actually waits.
"""

from typing import Any

import pytest
from pydantic import BaseModel

from agentframe import (
    Agent,
    DefinitionError,
    ExecutionPolicy,
    LimitExceededError,
    ModelRequest,
    ModelResponse,
    ProviderError,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    SkillDefinition,
    agent,
    skill,
)
from agentframe.models.base import TextSink
from agentframe.runtime import generation
from agentframe.security.principal import Principal
from agentframe.testing import ScriptedModel


class Title(BaseModel):
    text: str


@agent(version="1.0.0")
class Writer(Agent):
    """Writes text."""

    limits = RunLimits(timeout_seconds=5)

    @skill("write", tags=["text"])
    async def write(self, text: str, ctx: RunContext) -> str:
        """Write text, streaming it to the caller."""
        return await ctx.generate(text, stream=True)

    @skill("write_quietly", tags=["text"])
    async def write_quietly(self, text: str, ctx: RunContext) -> str:
        """Write text without streaming it."""
        return await ctx.generate(text)

    @skill("title", tags=["text"])
    async def title(self, text: str, ctx: RunContext) -> Title:
        """Ask for a streamed structured result, which is not allowed."""
        title: Title = await ctx.generate(text, output=Title, stream=True)  # type: ignore[call-overload]
        return title


class Collector:
    """A text sink that records every piece it receives."""

    def __init__(self) -> None:
        self.pieces: list[str] = []

    async def __call__(self, text: str) -> None:
        self.pieces.append(text)


class FailsAfterText:
    """A streaming model that sends some text, then fails transiently."""

    def __init__(self) -> None:
        self.calls = 0

    async def capabilities(self) -> Any:
        return await ScriptedModel([]).capabilities()

    async def close(self) -> None:
        return None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("streaming calls must use stream()")

    async def stream(self, request: ModelRequest, on_text: TextSink) -> ModelResponse:
        self.calls += 1
        await on_text("Half ")
        raise ProviderError("Connection lost", transient=True)


class TextOnly:
    """A model without ``stream``, wrapping a scripted model."""

    def __init__(self, text: str) -> None:
        self._model = ScriptedModel([text])

    async def capabilities(self) -> Any:
        return await self._model.capabilities()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return await self._model.generate(request)

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    async def skip(seconds: float) -> None:
        return None

    monkeypatch.setattr(generation, "wait_before_retry", skip)


async def run(
    model: Any,
    skill_id: str = "write",
    on_text: TextSink | None = None,
    runtime: Runtime | None = None,
    options: RunOptions | None = None,
) -> Any:
    return await (runtime or Runtime()).run(
        Writer(model=model),
        skill=skill_id,
        arguments={"text": "Go."},
        options=options,
        on_text=on_text,
    )


async def test_streamed_text_arrives_in_pieces_and_the_result_is_whole() -> None:
    sink = Collector()

    result = await run(ScriptedModel(["Hello there friend"]), on_text=sink)

    assert sink.pieces == ["Hello ", "there ", "friend"]
    assert result.value == "Hello there friend"


async def test_generate_without_stream_sends_nothing() -> None:
    sink = Collector()

    result = await run(ScriptedModel(["Hello"]), "write_quietly", on_text=sink)

    assert sink.pieces == []
    assert result.value == "Hello"


async def test_stream_without_a_sink_just_returns_the_answer() -> None:
    assert (await run(ScriptedModel(["Hello there"]))).value == "Hello there"


async def test_a_model_without_stream_answers_in_one_piece() -> None:
    sink = Collector()

    result = await run(TextOnly("Hello there"), on_text=sink)

    assert sink.pieces == []
    assert result.value == "Hello there"


async def test_stream_with_output_is_a_definition_error() -> None:
    with pytest.raises(DefinitionError, match="cannot stream structured output"):
        await run(ScriptedModel([]), "title", on_text=Collector())


async def test_output_checks_stop_streaming_and_still_see_the_answer() -> None:
    seen: list[str] = []

    async def check(
        skill: SkillDefinition | None, principal: Principal | None, content: str
    ) -> bool:
        seen.append(content)
        return True

    sink = Collector()
    runtime = Runtime(policy=ExecutionPolicy(output_checks=[check]))

    result = await run(ScriptedModel(["Hello there"]), on_text=sink, runtime=runtime)

    assert sink.pieces == []
    assert result.value == "Hello there"
    assert seen


async def test_a_failure_after_streamed_text_is_not_retried() -> None:
    model = FailsAfterText()
    sink = Collector()

    with pytest.raises(ProviderError, match="Connection lost"):
        await run(model, on_text=sink)

    assert model.calls == 1
    assert sink.pieces == ["Half "]


async def test_a_transient_failure_before_any_text_is_retried() -> None:
    model = ScriptedModel([ProviderError("Busy", transient=True), "Hello"])
    sink = Collector()

    result = await run(model, on_text=sink)

    assert result.value == "Hello"
    assert sink.pieces == ["Hello"]
    assert len(model.requests) == 2


async def test_an_error_from_the_sink_fails_the_run() -> None:
    async def refuse(text: str) -> None:
        raise LimitExceededError("Too much text.")

    with pytest.raises(LimitExceededError, match="Too much text"):
        await run(ScriptedModel(["Hello there"]), on_text=refuse)


async def test_streaming_still_counts_model_calls() -> None:
    options = RunOptions(limits=RunLimits(max_model_calls=1))

    result = await run(ScriptedModel(["Hello"]), on_text=Collector(), options=options)

    assert result.model_calls == 1
