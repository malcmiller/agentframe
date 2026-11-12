"""Tests for token and model-call limits and the run's token accounting.

Each model call reserves its estimated input plus its ``max_output_tokens``
before it is sent, then settles to its actual usage. Models are scripted, and
estimates use four characters per token, so the numbers below are exact.
"""

import asyncio
import contextlib
from typing import Any

import pytest

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    DefinitionError,
    FinishReason,
    HostConfig,
    LimitExceededError,
    ModelCallLimitExceededError,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    ProviderError,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    TimeoutLimitExceededError,
    TokenLimitExceededError,
    Usage,
    agent,
    skill,
)
from agentframe.limits import DEFAULT_LIMITS
from agentframe.testing import ScriptedModel

UNKNOWN_WINDOW = ModelCapabilities(context_window=None)


def usage(input_tokens: int, output_tokens: int) -> Usage:
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        estimated=False,
    )


def reply(tokens: Usage) -> ModelResponse:
    return ModelResponse(
        text="Done.", usage=tokens, finish_reason=FinishReason.COMPLETED
    )


@agent(version="1.0.0")
class Echo(Agent):
    """Sends text to its model. Each call reserves 1 input + 100 output tokens."""

    model_settings = ModelSettings(max_output_tokens=100)

    @skill("echo", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Send ``text`` once."""
        return await ctx.generate(text)

    @skill("echo_each", tags=["text"])
    async def echo_each(self, texts: list[str], ctx: RunContext) -> list[str]:
        """Send each text in turn; a failed call is skipped."""
        answers = []
        for text in texts:
            try:
                answers.append(await ctx.generate(text))
            except ProviderError:
                answers.append("failed")
        return answers

    @skill("echo_together", tags=["text"])
    async def echo_together(self, texts: list[str], ctx: RunContext) -> list[str]:
        """Send all texts at once."""
        async with asyncio.TaskGroup() as group:
            calls = [group.create_task(ctx.generate(text)) for text in texts]
        return [call.result() for call in calls]

    @skill("count", tags=["text"])
    async def count(self, text: str, ctx: RunContext) -> int:
        """Count words without a model."""
        return len(text.split())


async def run_echo(
    model: ScriptedModel,
    skill_id: str,
    arguments: dict[str, Any],
    limits: RunLimits | None = None,
) -> Any:
    return await Runtime().run(
        Echo(model=model),
        skill=skill_id,
        arguments=arguments,
        options=RunOptions(limits=limits),
    )


# RunLimits


def test_the_library_defaults_bound_tokens_and_model_calls() -> None:
    assert DEFAULT_LIMITS.max_total_tokens == 100_000
    assert DEFAULT_LIMITS.max_model_calls == 10
    # Unset: the model's context window is the default input limit.
    assert DEFAULT_LIMITS.max_input_tokens is None


@pytest.mark.parametrize(
    "field", ["max_total_tokens", "max_model_calls", "max_input_tokens"]
)
def test_token_and_call_limits_must_be_positive(field: str) -> None:
    with pytest.raises(DefinitionError, match=f"RunLimits.{field}"):
        RunLimits(**{field: 0})


# Model calls


async def test_a_call_over_max_model_calls_is_not_sent() -> None:
    model = ScriptedModel(["One.", "Two.", "Three."])

    with pytest.raises(ModelCallLimitExceededError, match="2 model calls") as raised:
        await run_echo(
            model, "echo_each", {"texts": ["a", "b", "c"]}, RunLimits(max_model_calls=2)
        )

    assert len(model.requests) == 2
    assert isinstance(raised.value, LimitExceededError)
    assert raised.value.invocation_id


async def test_failed_calls_count_toward_max_model_calls() -> None:
    model = ScriptedModel([ProviderError("Down", transient=True), "Two."])

    with pytest.raises(ModelCallLimitExceededError):
        await run_echo(
            model, "echo_each", {"texts": ["a", "b", "c"]}, RunLimits(max_model_calls=2)
        )


# Total tokens: reserve, then settle


async def test_a_call_whose_reservation_does_not_fit_is_not_sent() -> None:
    model = ScriptedModel(["Done."])

    with pytest.raises(TokenLimitExceededError, match="up to 101 tokens") as raised:
        await run_echo(model, "echo", {"text": "a"}, RunLimits(max_total_tokens=100))

    assert model.requests == []
    assert isinstance(raised.value, LimitExceededError)


async def test_the_output_cap_is_never_lowered_to_fit_the_budget() -> None:
    model = ScriptedModel([reply(usage(1, 5)), "Two."])

    # 6 of 150 tokens are used after the first call, so 144 remain: enough
    # for 45 input tokens and a 99-token answer, but the call reserves its
    # full 100-token output cap.
    with pytest.raises(TokenLimitExceededError):
        await run_echo(
            model,
            "echo_each",
            {"texts": ["a", "b" * 180]},
            RunLimits(max_total_tokens=150),
        )

    assert [request.settings.max_output_tokens for request in model.requests] == [100]


async def test_settled_calls_count_their_actual_usage_not_their_reservation() -> None:
    model = ScriptedModel([reply(usage(1, 5)), reply(usage(1, 5))])

    # Two reservations of 101 would not fit in 150, but the first call
    # settles at 6 tokens before the second reserves.
    result = await run_echo(
        model, "echo_each", {"texts": ["a", "b"]}, RunLimits(max_total_tokens=150)
    )

    assert result.model_calls == 2
    assert result.usage == usage(2, 10)


async def test_a_failed_call_with_unknown_usage_keeps_its_reservation() -> None:
    model = ScriptedModel([ProviderError("Down", transient=True), "Two."])

    # The failed call may have used up to its 101 reserved tokens, so they
    # stay counted and the second call no longer fits in 150.
    with pytest.raises(TokenLimitExceededError):
        await run_echo(
            model, "echo_each", {"texts": ["a", "b"]}, RunLimits(max_total_tokens=150)
        )

    assert len(model.requests) == 1


async def test_a_failed_call_with_known_usage_settles_to_it() -> None:
    model = ScriptedModel(
        [ProviderError("Down", transient=True, usage=usage(1, 0)), "Two."]
    )

    result = await run_echo(
        model, "echo_each", {"texts": ["a", "b"]}, RunLimits(max_total_tokens=150)
    )

    assert result.value == ["failed", "Two."]


class GatedModel(ScriptedModel):
    """Holds every call open until the test releases it."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(replies)
        self.release = asyncio.Event()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        await self.release.wait()
        return await super().generate(request)


async def test_concurrent_calls_cannot_reserve_the_same_tokens() -> None:
    model = GatedModel(["One.", "Two."])
    run = asyncio.create_task(
        run_echo(
            model,
            "echo_together",
            {"texts": ["a", "b"]},
            RunLimits(max_total_tokens=150),
        )
    )

    # The first call holds 101 tokens while it runs, so the second call's
    # reservation fails without being sent, and the task group cancels the first.
    with pytest.raises(ExceptionGroup) as raised:
        await run
    assert raised.group_contains(TokenLimitExceededError)
    assert model.requests == []


# Input size and the context window


async def test_input_over_max_input_tokens_fails_and_is_not_truncated() -> None:
    model = ScriptedModel(["Done."])

    with pytest.raises(TokenLimitExceededError, match="limit of 10 input tokens"):
        await run_echo(
            model, "echo", {"text": "x" * 44}, RunLimits(max_input_tokens=10)
        )

    assert model.requests == []


async def test_input_plus_output_must_fit_the_context_window() -> None:
    model = ScriptedModel(["Done."], capabilities=ModelCapabilities(context_window=150))

    # 60 input tokens fit the window, but not with 100 reserved for output.
    with pytest.raises(TokenLimitExceededError, match="context window of 150"):
        await run_echo(model, "echo", {"text": "x" * 240})

    assert model.requests == []


async def test_unknown_context_window_without_max_input_tokens_fails_locally() -> None:
    model = ScriptedModel(["Done."], capabilities=UNKNOWN_WINDOW)

    with pytest.raises(DefinitionError, match="max_input_tokens") as raised:
        await run_echo(model, "echo", {"text": "a"})

    assert model.requests == []
    assert raised.value.invocation_id


async def test_unknown_context_window_with_max_input_tokens_runs() -> None:
    model = ScriptedModel(["Done."], capabilities=UNKNOWN_WINDOW)

    result = await run_echo(
        model, "echo", {"text": "a"}, RunLimits(max_input_tokens=1_000)
    )

    assert result.value == "Done."


# Startup checks


async def test_validate_model_names_the_skill_with_an_unknown_input_limit() -> None:
    echo = Echo(model=ScriptedModel(capabilities=UNKNOWN_WINDOW))

    with pytest.raises(DefinitionError, match="Skill 'echo'"):
        await Runtime().validate_model(echo)


@agent(version="1.0.0")
class BoundedEcho(Agent):
    """Sets its own input limit, so it needs no context window."""

    limits = RunLimits(max_input_tokens=1_000)

    @skill("echo", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Send ``text`` once."""
        return await ctx.generate(text)


async def test_validate_model_passes_with_an_agent_input_limit_or_no_model() -> None:
    bounded = BoundedEcho(model=ScriptedModel(capabilities=UNKNOWN_WINDOW))

    await Runtime().validate_model(bounded)
    await Runtime().validate_model(Echo())


async def lifespan_startup(app: AgentApp) -> dict[str, Any]:
    """Run the ASGI lifespan startup and return the server's reply."""
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await events.put({"type": "lifespan.startup"})
    replies: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return await events.get()

    async def send(message: Any) -> None:
        replies.append(message)
        await events.put({"type": "lifespan.shutdown"})

    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    # Starlette re-raises a startup error after reporting it.
    with contextlib.suppress(DefinitionError):
        await app(scope, receive, send)
    return replies[0]


@pytest.fixture
def anonymous_host() -> HostConfig:
    return HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous())


async def test_agent_app_fails_startup_when_an_input_limit_is_unknown(
    anonymous_host: HostConfig,
) -> None:
    echo = Echo(model=ScriptedModel(capabilities=UNKNOWN_WINDOW))
    app = AgentApp(agent=echo, host=anonymous_host)

    startup = await lifespan_startup(app)

    assert startup["type"] == "lifespan.startup.failed"
    assert "max_input_tokens" in startup["message"]


async def test_agent_app_starts_when_every_input_limit_is_known(
    anonymous_host: HostConfig,
) -> None:
    app = AgentApp(agent=Echo(model=ScriptedModel()), host=anonymous_host)

    startup = await lifespan_startup(app)

    assert startup["type"] == "lifespan.startup.complete"


# Timeouts


async def test_timeouts_raise_the_timeout_subtype_of_limit_exceeded() -> None:
    @agent(version="1.0.0")
    class Sleeper(Agent):
        """Sleeps past its timeout."""

        @skill("sleep", limits=RunLimits(timeout_seconds=0.01), tags=["test"])
        async def sleep(self, ctx: RunContext) -> None:
            """Sleep for a second."""
            await asyncio.sleep(1)

    with pytest.raises(TimeoutLimitExceededError) as raised:
        await Runtime().invoke(Sleeper(), skill="sleep")

    assert isinstance(raised.value, LimitExceededError)


# Model-free runs


async def test_model_free_runs_need_no_context_window_and_use_no_tokens() -> None:
    model = ScriptedModel(capabilities=UNKNOWN_WINDOW)

    result = await run_echo(model, "count", {"text": "a b"})

    assert result.model_calls == 0
    assert result.usage == usage(0, 0)
