"""Tests for ``ctx.generate(text, output=Model)``: typed, validated model output.

Models are scripted, so no test calls a provider. A scripted string is the
JSON text a real adapter would return.
"""

from typing import Any, assert_type

import httpx
import pytest
from pydantic import BaseModel, Field, RootModel

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    DefinitionError,
    FinishReason,
    HostConfig,
    IncompleteOutputError,
    Message,
    ModelCapabilities,
    ModelError,
    ModelRefusalError,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    OutputSchema,
    OutputValidationError,
    RunContext,
    RunLimits,
    Runtime,
    TokenLimitExceededError,
    UnsupportedCapabilityError,
    Usage,
    agent,
    skill,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

STRUCTURED = ModelCapabilities(context_window=128_000, structured_output=True)
VALID_JSON = '{"text": "A summary.", "key_points": ["one", "two"]}'


class Summary(BaseModel):
    text: str = Field(min_length=1)
    key_points: list[str]


@agent(version="1.0.0")
class Writer(Agent):
    """Summarizes text."""

    instructions = "Preserve the source meaning."
    model_settings = ModelSettings(max_output_tokens=256)

    @skill("summarize", tags=["text"])
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        """Summarize text as a typed summary."""
        summary = await ctx.generate(text, instructions="Be brief.", output=Summary)
        assert_type(summary, Summary)
        return summary

    @skill("summarize_or_none", tags=["text"])
    async def summarize_or_none(self, text: str, ctx: RunContext) -> Summary | None:
        """Summarize text, returning ``None`` when the model fails."""
        try:
            return await ctx.generate(text, output=Summary)
        except (ModelError, OutputValidationError):
            return None

    @skill("rewrite", tags=["text"])
    async def rewrite(self, text: str, ctx: RunContext) -> str:
        """Rewrite text as plain text."""
        plain = await ctx.generate(text)
        assert_type(plain, str)
        return plain


def usage(input_tokens: int, output_tokens: int) -> Usage:
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        estimated=False,
    )


def response(text: str, finish_reason: FinishReason) -> ModelResponse:
    return ModelResponse(text=text, usage=usage(10, 5), finish_reason=finish_reason)


async def summarize(model: ScriptedModel, skill_id: str = "summarize") -> Any:
    return await Runtime().invoke(
        Writer(model=model), skill=skill_id, arguments={"text": "Long text."}
    )


# Successful calls


async def test_generate_with_output_returns_a_validated_instance() -> None:
    summary = await summarize(ScriptedModel([VALID_JSON], capabilities=STRUCTURED))

    assert summary == Summary(text="A summary.", key_points=["one", "two"])


async def test_request_is_the_text_request_plus_the_output_schema() -> None:
    model = ScriptedModel([VALID_JSON], capabilities=STRUCTURED)

    await summarize(model)

    assert model.requests == [
        ModelRequest(
            messages=(
                Message("system", "Preserve the source meaning."),
                Message("system", "Be brief."),
                Message("user", "Long text."),
            ),
            settings=ModelSettings(max_output_tokens=256),
            output=OutputSchema(
                name="Summary", json_schema=Summary.model_json_schema()
            ),
        )
    ]


async def test_text_requests_have_no_output_schema() -> None:
    model = ScriptedModel(["Plain."])

    await Runtime().invoke(
        Writer(model=model), skill="rewrite", arguments={"text": "a"}
    )

    assert model.requests[0].output is None


async def test_a_model_configured_strict_is_validated_strictly() -> None:
    class Count(BaseModel, strict=True):
        n: int

    @agent(version="1.0.0")
    class Counter(Agent):
        """Counts."""

        @skill("count", tags=["text"])
        async def count(self, text: str, ctx: RunContext) -> int:
            """Count with the model."""
            return (await ctx.generate(text, output=Count)).n

    model = ScriptedModel(['{"n": "3"}'], capabilities=STRUCTURED)

    with pytest.raises(OutputValidationError, match="n: Input should be"):
        await Runtime().invoke(
            Counter(model=model), skill="count", arguments={"text": "a"}
        )


# Failures before anything is sent


async def test_a_model_without_structured_output_is_never_called() -> None:
    model = ScriptedModel([VALID_JSON])  # Default capabilities: no structured output.

    result = await Runtime().run(
        Writer(model=model), skill="summarize_or_none", arguments={"text": "a"}
    )

    assert result.value is None
    assert model.requests == []
    assert result.model_calls == 0
    assert result.usage == usage(0, 0)


async def test_unsupported_structured_output_names_the_capability() -> None:
    with pytest.raises(UnsupportedCapabilityError, match="structured output"):
        await summarize(ScriptedModel([VALID_JSON]))


@pytest.mark.parametrize(
    "output",
    [
        pytest.param(list[str], id="list"),
        pytest.param(int, id="int"),
        pytest.param(RootModel[list[str]], id="root-model"),
        pytest.param(Summary(text="a", key_points=[]), id="instance"),
    ],
)
async def test_unsupported_output_types_fail_before_sending(output: Any) -> None:
    @agent(version="1.0.0")
    class Loose(Agent):
        """Asks for an unsupported output type."""

        @skill("ask", tags=["text"])
        async def ask(self, text: str, ctx: RunContext) -> Any:
            """Ask with an unsupported output type."""
            return await ctx.generate(text, output=output)

    model = ScriptedModel([VALID_JSON], capabilities=STRUCTURED)

    with pytest.raises(DefinitionError, match="BaseModel subclass"):
        await Runtime().invoke(Loose(model=model), skill="ask", arguments={"text": "a"})

    assert model.requests == []


async def test_the_schema_counts_toward_the_token_reservation() -> None:
    class Wide(BaseModel):
        counts: dict[str, int] = Field(description="x" * 4000)

    @agent(version="1.0.0")
    class Tight(Agent):
        """Has a small token budget."""

        model_settings = ModelSettings(max_output_tokens=100)
        limits = RunLimits(max_total_tokens=500)

        @skill("ask", tags=["text"])
        async def ask(self, text: str, ctx: RunContext) -> int:
            """Ask for a wide object."""
            return len((await ctx.generate(text, output=Wide)).counts)

    model = ScriptedModel(['{"counts": {}}'], capabilities=STRUCTURED)

    # The short text alone fits the budget; the 4000-character schema does not.
    with pytest.raises(TokenLimitExceededError, match="needs up to"):
        await Runtime().invoke(Tight(model=model), skill="ask", arguments={"text": "a"})

    assert model.requests == []


# Invalid or incomplete output


@pytest.mark.parametrize(
    ("reply", "locations"),
    [
        pytest.param(
            "Sure! Here is the secret summary", "value: Invalid JSON", id="prose"
        ),
        pytest.param(
            '{"text": "", "key_points": "secret"}',
            "text: String should have at least 1 character; key_points: Input "
            "should be a valid array",
            id="schema-invalid",
        ),
    ],
)
async def test_invalid_output_lists_field_locations_but_not_model_text(
    reply: str, locations: str
) -> None:
    model = ScriptedModel([reply], capabilities=STRUCTURED)

    with pytest.raises(OutputValidationError) as raised:
        await summarize(model)

    assert raised.value.message.startswith(
        f"The model's output is not a valid 'Summary': {locations}"
    )
    assert "secret" not in raised.value.message
    assert raised.value.invocation_id
    assert len(model.requests) == 1  # Never repaired or re-requested.


async def test_dictionary_keys_chosen_by_the_model_are_masked() -> None:
    class Inventory(BaseModel):
        counts: dict[str, int]
        shelves: list[dict[str, int]]

    @agent(version="1.0.0")
    class Stocktaker(Agent):
        """Counts stock."""

        @skill("count", tags=["text"])
        async def count(self, text: str, ctx: RunContext) -> int:
            """Count stock with the model."""
            return len((await ctx.generate(text, output=Inventory)).counts)

    reply = '{"counts": {"SECRET-KEY": "x"}, "shelves": [{"OTHER-SECRET": "y"}]}'
    model = ScriptedModel([reply], capabilities=STRUCTURED)

    with pytest.raises(OutputValidationError) as raised:
        await Runtime().invoke(
            Stocktaker(model=model), skill="count", arguments={"text": "a"}
        )

    assert raised.value.message == (
        "The model's output is not a valid 'Inventory': "
        "counts.<key>: Input should be a valid integer, unable to parse string "
        "as an integer; shelves.0.<key>: Input should be a valid integer, "
        "unable to parse string as an integer"
    )


async def test_invalid_output_still_records_the_call_usage() -> None:
    model = ScriptedModel(
        [response("not json", FinishReason.COMPLETED)], capabilities=STRUCTURED
    )

    result = await Runtime().run(
        Writer(model=model), skill="summarize_or_none", arguments={"text": "a"}
    )

    assert result.value is None
    assert result.model_calls == 1
    assert result.usage == usage(10, 5)


@pytest.mark.parametrize(
    ("reply", "error_type"),
    [
        pytest.param(
            response(VALID_JSON, FinishReason.LENGTH),
            IncompleteOutputError,
            id="length-with-parseable-json",
        ),
        pytest.param(
            response('{"text": "A sum', FinishReason.LENGTH),
            IncompleteOutputError,
            id="length",
        ),
        pytest.param(
            response("", FinishReason.COMPLETED), IncompleteOutputError, id="empty"
        ),
        pytest.param(
            response("", FinishReason.REFUSED), ModelRefusalError, id="refused"
        ),
        pytest.param(
            response("", FinishReason.CONTENT_FILTER),
            ModelRefusalError,
            id="content-filter",
        ),
    ],
)
async def test_incomplete_or_refused_output_is_never_parsed(
    reply: ModelResponse, error_type: type[ModelError]
) -> None:
    with pytest.raises(error_type) as raised:
        await summarize(ScriptedModel([reply], capabilities=STRUCTURED))

    if isinstance(raised.value, IncompleteOutputError):
        assert raised.value.partial_text is None


# Hosted over A2A

HEADERS = {"A2A-Version": "1.0", "A2A-Extensions": SKILL_CALL_EXTENSION_URI}


async def run_hosted(serve: ServeLocally, model: ScriptedModel) -> dict[str, Any]:
    def make_app(public_url: str) -> AgentApp:
        host = HostConfig(public_url=public_url, auth=AllowAnonymous())
        return AgentApp(agent=Writer(model=model), host=host)

    message = {
        "messageId": "m-1",
        "role": "ROLE_USER",
        "parts": [{"data": {"text": "Long text."}}],
        "metadata": {"agentframe.skill": "summarize"},
    }
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    async with (
        serve(make_app) as base_url,
        httpx.AsyncClient(base_url=base_url) as http,
    ):
        reply = await http.post("/a2a", json=body, headers=HEADERS)
    task: dict[str, Any] = reply.json()["result"]["task"]
    return task


async def test_hosted_generated_output_is_one_data_part(serve: ServeLocally) -> None:
    task = await run_hosted(serve, ScriptedModel([VALID_JSON], capabilities=STRUCTURED))

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"] == [
        {"data": {"value": {"text": "A summary.", "key_points": ["one", "two"]}}}
    ]


async def test_hosted_invalid_output_fails_with_the_safe_message(
    serve: ServeLocally,
) -> None:
    model = ScriptedModel(['{"text": "secret"}'], capabilities=STRUCTURED)

    task = await run_hosted(serve, model)

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert task["status"]["message"]["parts"] == [
        {
            "text": "The model's output is not a valid 'Summary': "
            "key_points: Field required"
        }
    ]
    assert "artifacts" not in task
