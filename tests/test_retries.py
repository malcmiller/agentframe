"""Tests for retrying transient model-provider failures.

Models are scripted, and ``wait_before_retry`` is replaced by a recorder, so
no test calls a provider or actually waits.
"""

import random
from typing import Any

import pytest

from agentframe import (
    Agent,
    DefinitionError,
    FinishReason,
    IncompleteOutputError,
    ModelCallLimitExceededError,
    ModelError,
    ModelRefusalError,
    ModelResponse,
    ModelSettings,
    ProviderError,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    Usage,
    agent,
    skill,
)
from agentframe.runtime import generation
from agentframe.runtime.generation import backoff_seconds
from agentframe.runtime.limits import DEFAULT_LIMITS
from agentframe.testing import ScriptedModel


def usage(input_tokens: int, output_tokens: int) -> Usage:
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        estimated=False,
    )


def transient(retry_after: float | None = None) -> ProviderError:
    return ProviderError("Rate limited", transient=True, retry_after=retry_after)


DONE = ModelResponse(
    text="Done.", usage=usage(10, 5), finish_reason=FinishReason.COMPLETED
)


@agent(version="1.0.0")
class Writer(Agent):
    """Rewrites text."""

    model_settings = ModelSettings(max_output_tokens=100)
    limits = RunLimits(timeout_seconds=5)

    @skill("rewrite", tags=["text"])
    async def rewrite(self, text: str, ctx: RunContext) -> str:
        """Rewrite text."""
        return await ctx.generate(text)


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record each wait before a retry instead of sleeping."""
    recorded: list[float] = []

    async def record(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(generation, "wait_before_retry", record)
    return recorded


async def run_writer(model: ScriptedModel, limits: RunLimits | None = None) -> Any:
    return await Runtime().run(
        Writer(model=model),
        skill="rewrite",
        arguments={"text": "Text."},
        options=RunOptions(limits=limits),
    )


# Which failures are retried


async def test_a_transient_failure_is_retried_and_the_run_succeeds(
    waits: list[float],
) -> None:
    model = ScriptedModel([transient(), DONE])

    result = await run_writer(model)

    assert result.value == "Done."
    assert len(model.requests) == 2
    assert model.requests[0] == model.requests[1]
    assert len(waits) == 1


def test_the_library_default_is_two_retries() -> None:
    assert DEFAULT_LIMITS.max_model_retries == 2


async def test_by_default_a_call_is_retried_twice_then_the_error_is_raised(
    waits: list[float],
) -> None:
    error = transient()
    model = ScriptedModel([transient(), transient(), error, "Unused."])

    with pytest.raises(ProviderError) as raised:
        await run_writer(model)

    assert raised.value is error
    assert raised.value.invocation_id
    assert len(model.requests) == 3
    assert len(waits) == 2


async def test_max_model_retries_sets_how_many_retries_a_run_makes(
    waits: list[float],
) -> None:
    model = ScriptedModel([transient(), transient(), "Unused."])

    with pytest.raises(ProviderError):
        await run_writer(model, RunLimits(max_model_retries=1))

    assert len(model.requests) == 2


async def test_zero_max_model_retries_disables_retries(waits: list[float]) -> None:
    model = ScriptedModel([transient(), "Unused."])

    with pytest.raises(ProviderError):
        await run_writer(model, RunLimits(max_model_retries=0))

    assert len(model.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    ("reply", "error_type"),
    [
        pytest.param(
            ProviderError("Bad request", transient=False),
            ProviderError,
            id="non-transient-provider-error",
        ),
        pytest.param(
            ModelResponse(
                text="", usage=usage(4, 1), finish_reason=FinishReason.REFUSED
            ),
            ModelRefusalError,
            id="refusal",
        ),
        pytest.param(
            ModelRefusalError("Blocked by a content filter"),
            ModelRefusalError,
            id="refusal-error",
        ),
        pytest.param(
            ModelResponse(
                text="Part", usage=usage(4, 100), finish_reason=FinishReason.LENGTH
            ),
            IncompleteOutputError,
            id="incomplete-output",
        ),
        pytest.param(
            IncompleteOutputError("Stopped early"),
            IncompleteOutputError,
            id="incomplete-output-error",
        ),
    ],
)
async def test_other_failures_are_never_retried(
    waits: list[float],
    reply: ModelResponse | ModelError,
    error_type: type[ModelError],
) -> None:
    model = ScriptedModel([reply, "Unused."])

    with pytest.raises(error_type):
        await run_writer(model)

    assert len(model.requests) == 1
    assert waits == []


def test_max_model_retries_must_not_be_negative() -> None:
    with pytest.raises(DefinitionError, match="max_model_retries"):
        RunLimits(max_model_retries=-1)


# Waiting between attempts


async def test_the_provider_retry_after_hint_is_honored(waits: list[float]) -> None:
    model = ScriptedModel([transient(retry_after=1.5), transient(retry_after=3), DONE])

    await run_writer(model)

    assert waits == [1.5, 3]


def test_without_a_hint_the_wait_is_random_up_to_an_exponential_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ranges: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        ranges.append((low, high))
        return high

    monkeypatch.setattr(random, "uniform", uniform)

    waits = [backoff_seconds(transient(), retry) for retry in range(6)]

    assert waits == [0.5, 1, 2, 4, 8, 8]
    assert {low for low, _ in ranges} == {0}


async def test_a_retry_that_would_wait_past_the_deadline_is_not_made(
    waits: list[float],
) -> None:
    error = transient(retry_after=60)
    model = ScriptedModel([error, "Unused."])

    with pytest.raises(ProviderError) as raised:
        await run_writer(model)

    assert raised.value is error
    assert len(model.requests) == 1
    assert waits == []


# Every attempt is a counted model call


async def test_every_attempt_counts_toward_max_model_calls(
    waits: list[float],
) -> None:
    model = ScriptedModel([transient(), transient(), "Unused."])

    with pytest.raises(ModelCallLimitExceededError):
        await run_writer(model, RunLimits(max_model_calls=2))

    assert len(model.requests) == 2


async def test_every_attempt_counts_toward_usage(waits: list[float]) -> None:
    failed = ProviderError("Server error", transient=True, usage=usage(4, 0))
    model = ScriptedModel([failed, DONE])

    result = await run_writer(model)

    assert result.model_calls == 2
    assert result.usage == usage(10 + 4, 5)
