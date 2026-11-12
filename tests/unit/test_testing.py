"""Tests for ``agentframe.testing.ScriptedModel``."""

import pytest

from agentframe import (
    FinishReason,
    Message,
    Model,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    ProviderError,
    ToolCall,
    Usage,
)
from agentframe.testing import SCRIPTED_CONTEXT_WINDOW, ScriptedModel


def make_request(text: str, max_output_tokens: int = 100) -> ModelRequest:
    return ModelRequest(
        messages=(
            Message(role="system", content="Be brief."),
            Message(role="user", content=text),
        ),
        settings=ModelSettings(max_output_tokens=max_output_tokens),
    )


async def test_text_reply_returns_completed_response_with_unknown_usage() -> None:
    model: Model = ScriptedModel(["Hello!"])

    response = await model.generate(make_request("Hi"))

    assert response.text == "Hello!"
    assert response.finish_reason is FinishReason.COMPLETED
    assert response.usage.total_tokens is None


async def test_full_response_is_returned_as_scripted() -> None:
    truncated = ModelResponse(
        text="A2A is",
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15, estimated=True),
        finish_reason=FinishReason.LENGTH,
    )
    model = ScriptedModel([truncated])

    assert await model.generate(make_request("Explain A2A")) == truncated


async def test_records_requests_in_order() -> None:
    model = ScriptedModel(["one", "two"])
    first, second = make_request("1"), make_request("2", max_output_tokens=50)

    await model.generate(first)
    await model.generate(second)

    assert model.requests == [first, second]
    assert model.requests[1].settings.max_output_tokens == 50


async def test_scripted_error_is_raised_and_the_next_reply_follows() -> None:
    rate_limited = ProviderError("Rate limited.", transient=True, retry_after=1.0)
    model = ScriptedModel([rate_limited, "Recovered"])

    with pytest.raises(ProviderError) as raised:
        await model.generate(make_request("Hi"))
    response = await model.generate(make_request("Hi"))

    assert raised.value is rate_limited
    assert raised.value.transient
    assert response.text == "Recovered"
    assert len(model.requests) == 2


async def test_calling_more_times_than_scripted_fails_clearly() -> None:
    model = ScriptedModel(["only one"])
    await model.generate(make_request("1"))

    with pytest.raises(AssertionError, match="called 2 times but only 1 replies"):
        await model.generate(make_request("2"))


async def test_capabilities_are_scripted_and_default_to_a_known_context() -> None:
    scripted = ScriptedModel(capabilities=ModelCapabilities(context_window=8192))

    assert (await scripted.capabilities()).context_window == 8192
    assert await ScriptedModel().capabilities() == ModelCapabilities(
        context_window=SCRIPTED_CONTEXT_WINDOW
    )


async def test_records_close_including_through_async_with() -> None:
    model = ScriptedModel()
    assert not model.closed

    async with model as entered:
        assert entered is model

    assert model.closed


async def test_tool_call_replies_ask_for_tools_then_text_answers() -> None:
    lookup = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')
    track = ToolCall(id="call_2", name="track_parcel", arguments='{"parcel": "P9"}')
    model = ScriptedModel([lookup, (lookup, track), "Order A1 has shipped."])

    one_call = await model.generate(make_request("Where is A1?"))
    two_calls = await model.generate(make_request("And the parcel?"))
    answer = await model.generate(make_request("Thanks"))

    assert one_call.tool_calls == (lookup,)
    assert one_call.finish_reason is FinishReason.TOOL_CALLS
    assert one_call.text == ""
    assert one_call.usage.total_tokens is None
    assert two_calls.tool_calls == (lookup, track)
    assert answer.text == "Order A1 has shipped."
    assert answer.tool_calls == ()
