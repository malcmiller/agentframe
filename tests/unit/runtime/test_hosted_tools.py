"""Hosted tools in ``ctx.generate``: opt-in, provider-checked and counted.

Models are scripted, so no test calls a provider.
"""

from collections.abc import Callable

import pytest

from agentframe import (
    Agent,
    DefinitionError,
    FinishReason,
    FoundryCodeInterpreter,
    FoundryFileSearch,
    FoundryWebSearch,
    ModelCapabilities,
    ModelResponse,
    ProviderError,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    ToolCall,
    ToolCallLimitExceededError,
    UnsupportedCapabilityError,
    Usage,
    agent,
    skill,
    tool,
)
from agentframe.runtime import generation
from agentframe.testing import ScriptedModel

WEB_SEARCH_MODEL = ModelCapabilities(
    context_window=128_000, tools=True, hosted_tools=frozenset({FoundryWebSearch})
)


def answered(text: str, hosted_tool_calls: int) -> ModelResponse:
    return ModelResponse(
        text=text,
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15, estimated=False),
        finish_reason=FinishReason.COMPLETED,
        hosted_tool_calls=hosted_tool_calls,
    )


@tool(description="Read today's date.")
def today() -> str:
    return "2026-11-12"


@agent(version="1.0.0")
class Researcher(Agent):
    """Answers questions, searching the web when it needs to."""

    @skill("search", tags=["research"])
    async def search(self, question: str, ctx: RunContext) -> str:
        """Answer with web search and the date tool."""
        return await ctx.generate(question, tools=[today, FoundryWebSearch()])

    @skill("search_twice", tags=["research"])
    async def search_twice(self, question: str, ctx: RunContext) -> str:
        """Select web search twice, which is a mistake."""
        return await ctx.generate(
            question, tools=[FoundryWebSearch(), FoundryWebSearch()]
        )

    @skill("plain", tags=["research"])
    async def plain(self, question: str, ctx: RunContext) -> str:
        """Answer without hosted tools."""
        return await ctx.generate(question)


async def run_search(
    model: ScriptedModel, skill_id: str = "search", max_tool_calls: int = 20
) -> str:
    researcher = Researcher(model=model, tools=[today])
    result = await Runtime().run(
        researcher,
        skill=skill_id,
        arguments={"question": "What's new in A2A?"},
        options=RunOptions(limits=RunLimits(max_tool_calls=max_tool_calls)),
    )
    assert isinstance(result.value, str)
    return f"{result.value} ({result.tool_calls} tool calls)"


async def test_hosted_tools_are_offered_capped_and_counted() -> None:
    model = ScriptedModel(
        [ToolCall(id="c1", name="today", arguments="{}"), answered("Found it.", 2)],
        capabilities=WEB_SEARCH_MODEL,
    )

    assert await run_search(model, max_tool_calls=5) == "Found it. (3 tool calls)"

    first, second = model.requests
    assert first.hosted_tools == (FoundryWebSearch(),)
    assert [spec.name for spec in first.tools] == ["today"]
    assert first.max_hosted_tool_calls == 5
    # The function call used one of the run's tool calls.
    assert second.max_hosted_tool_calls == 4


async def no_wait(seconds: float) -> None:
    pass


def failed_after_searching(hosted_tool_calls: int) -> ProviderError:
    return ProviderError(
        "Foundry reported a failed response (code server_error)",
        transient=True,
        hosted_tool_calls=hosted_tool_calls,
    )


async def test_hosted_calls_of_a_failed_attempt_count_and_shrink_the_retry_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation, "wait_before_retry", no_wait)
    model = ScriptedModel(
        [failed_after_searching(2), answered("Found it.", 1)],
        capabilities=WEB_SEARCH_MODEL,
    )

    assert await run_search(model, max_tool_calls=5) == "Found it. (3 tool calls)"

    first, retry = model.requests
    assert first.max_hosted_tool_calls == 5
    assert retry.max_hosted_tool_calls == 3


async def test_a_failed_attempt_that_uses_every_tool_call_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation, "wait_before_retry", no_wait)
    model = ScriptedModel(
        [failed_after_searching(2), "unused"], capabilities=WEB_SEARCH_MODEL
    )

    with pytest.raises(ToolCallLimitExceededError, match="cannot offer hosted tools"):
        await run_search(model, max_tool_calls=2)

    assert len(model.requests) == 1


async def test_requests_without_hosted_tools_set_no_cap() -> None:
    model = ScriptedModel(["Hi."], capabilities=WEB_SEARCH_MODEL)

    await run_search(model, "plain")

    [request] = model.requests
    assert request.hosted_tools == ()
    assert request.max_hosted_tool_calls is None


async def test_a_model_without_the_hosted_tool_fails_before_sending() -> None:
    model = ScriptedModel(
        ["unused"], capabilities=ModelCapabilities(context_window=128_000, tools=True)
    )

    with pytest.raises(UnsupportedCapabilityError, match="FoundryWebSearch"):
        await run_search(model)

    assert model.requests == []


async def test_no_tool_calls_left_fails_before_offering_hosted_tools() -> None:
    model = ScriptedModel(
        [ToolCall(id="c1", name="today", arguments="{}"), "unused"],
        capabilities=WEB_SEARCH_MODEL,
    )

    with pytest.raises(ToolCallLimitExceededError, match="hosted tools"):
        await run_search(model, max_tool_calls=1)

    assert len(model.requests) == 1


async def test_hosted_calls_past_the_limit_fail_the_run() -> None:
    model = ScriptedModel([answered("Found it.", 3)], capabilities=WEB_SEARCH_MODEL)

    with pytest.raises(ToolCallLimitExceededError, match="past its limit of 2"):
        await run_search(model, max_tool_calls=2)


async def test_each_hosted_tool_type_can_be_selected_once() -> None:
    model = ScriptedModel(["unused"], capabilities=WEB_SEARCH_MODEL)

    with pytest.raises(DefinitionError, match="only once"):
        await run_search(model, "search_twice")

    assert model.requests == []


def test_a_single_string_is_not_read_as_one_id_per_character() -> None:
    with pytest.raises(DefinitionError, match="not a string"):
        FoundryWebSearch(allowed_domains="learn.microsoft.com")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: FoundryCodeInterpreter(file_ids=("f",) * 51), "at most 50"),
        (lambda: FoundryWebSearch(allowed_domains=("a.com",) * 101), "at most 100"),
        (lambda: FoundryFileSearch(vector_store_ids=()), "at least one"),
        (lambda: FoundryFileSearch(("vs_1",), max_num_results=0), "1 to 50"),
        (lambda: FoundryFileSearch(("vs_1",), max_num_results=51), "1 to 50"),
    ],
)
def test_invalid_hosted_tool_settings_fail_when_created(
    make: Callable[[], object], message: str
) -> None:
    with pytest.raises(DefinitionError, match=message):
        make()
