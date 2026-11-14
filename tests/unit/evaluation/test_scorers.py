"""Tests for the built-in scorers ``equals``, ``contains`` and ``valid``."""

from typing import Any

import pytest
from pydantic import BaseModel

from agentframe import Agent, DefinitionError, RunContext, agent, skill
from agentframe.evaluation import (
    Case,
    CaseStatus,
    Evaluation,
    LocalSkill,
    Score,
    contains,
    equals,
    valid,
)
from agentframe.evaluation.scoring import run_scorer

SECRET = "sk-secret-value"


class Summary(BaseModel):
    title: str
    words: int


@agent(version="1.0.0")
class Writer(Agent):
    """Writes and summarizes text."""

    @skill("echo", tags=["test"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Return the text unchanged."""
        return text

    @skill("summarize", tags=["test"])
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        """Summarize text without a model."""
        return Summary(title=text.split()[0], words=len(text.split()))


def case(expected: Any = None) -> Case[Any]:
    return Case(id="one", arguments={"text": "unused"}, expected=expected)


async def score(scorer: Any, output: Any, expected: Any = None) -> Score:
    return await run_scorer(scorer, case(expected), output)


async def test_equals_compares_output_with_expected() -> None:
    assert (await score(equals, 3, expected=3)).passed
    assert not (await score(equals, 4, expected=3)).passed


async def test_equals_compares_pydantic_models_field_by_field() -> None:
    expected = Summary(title="Hello", words=2)

    assert (await score(equals, Summary(title="Hello", words=2), expected)).passed
    assert not (await score(equals, Summary(title="Hello", words=3), expected)).passed


async def test_contains_ignores_case_by_default() -> None:
    assert (await score(contains("refund"), "Your REFUND is on its way")).passed
    assert not (await score(contains("refund"), "No money back")).passed


async def test_contains_can_be_case_sensitive() -> None:
    scorer = contains("Refund", case_sensitive=True)

    assert (await score(scorer, "Refund sent")).passed
    assert not (await score(scorer, "refund sent")).passed


async def test_contains_without_text_uses_the_expected_value() -> None:
    assert (await score(contains(), "Order 42 shipped", expected="order 42")).passed
    assert not (await score(contains(), "Order 7 shipped", expected="order 42")).passed


def test_contains_refuses_text_that_is_not_a_string() -> None:
    with pytest.raises(DefinitionError, match="contains\\(\\) text must be a string"):
        contains(42)  # type: ignore[arg-type]


async def test_contains_refuses_output_that_is_not_text() -> None:
    with pytest.raises(DefinitionError, match="needs text output, not int"):
        await score(contains("4"), 42)


async def test_contains_refuses_an_expected_value_that_is_not_text() -> None:
    with pytest.raises(
        DefinitionError, match="string expected value in case 'one', not int"
    ):
        await score(contains(), "4", expected=4)


@pytest.mark.parametrize("scorer", [equals, contains()])
async def test_scorers_needing_expected_refuse_a_case_without_one(
    scorer: Any,
) -> None:
    with pytest.raises(DefinitionError, match="needs case 'one' to have an expected"):
        await score(scorer, "anything")


async def test_valid_passes_data_that_validates_as_the_type() -> None:
    scorer = valid(Summary)

    assert (await score(scorer, {"title": "Hello", "words": 2})).passed
    assert (await score(scorer, Summary(title="Hello", words=2))).passed
    assert (await score(valid(list[int]), [1, 2])).passed


async def test_valid_parses_json_text() -> None:
    assert (await score(valid(Summary), '{"title": "Hello", "words": 2}')).passed
    assert (await score(valid(str), "plain text")).passed


async def test_valid_fails_with_a_reason_that_holds_no_output_values() -> None:
    result = await score(valid(Summary), {"title": SECRET, "words": SECRET})

    assert not result.passed
    assert result.reason is not None
    assert result.reason.startswith("words: Input should be a valid integer")
    assert SECRET not in result.reason
    assert not (await score(valid(Summary), f"not json {SECRET}")).passed


def test_valid_refuses_a_type_pydantic_cannot_validate() -> None:
    class Opaque:
        pass

    with pytest.raises(DefinitionError, match="valid\\(\\) cannot validate"):
        valid(Opaque)


def test_built_in_scorers_are_named_after_themselves() -> None:
    evaluation_names = [
        getattr(scorer, "__name__", None)
        for scorer in (equals, contains("x"), valid(int))
    ]

    assert evaluation_names == ["equals", "contains", "valid"]


async def test_built_ins_score_an_evaluation() -> None:
    cases = [
        Case(
            id="match",
            arguments={"text": "Hello world"},
            expected={"title": "Hello", "words": 2},
        ),
        Case(
            id="differs",
            arguments={"text": "Hello there world"},
            expected={"title": "Hello", "words": 2},
        ),
    ]
    evaluation = Evaluation(
        cases=cases,
        target=LocalSkill(Writer(), skill="summarize"),
        scorers=[equals, valid(Summary)],
    )

    report = await evaluation.run()

    assert [result.status for result in report.results] == [
        CaseStatus.PASSED,
        CaseStatus.FAILED,
    ]
    assert [score.scorer for score in report.results[0].scores] == ["equals", "valid"]


async def test_a_case_without_expected_is_errored_with_a_clear_message() -> None:
    evaluation = Evaluation(
        cases=[Case(id="bare", arguments={"text": SECRET})],
        target=LocalSkill(Writer(), skill="echo"),
        scorers=[contains()],
    )

    [result] = (await evaluation.run()).results

    assert result.status is CaseStatus.ERRORED
    assert result.error_type == "DefinitionError"
    assert result.error_message == (
        "Scorer 'contains' needs case 'bare' to have an expected value."
    )


@pytest.mark.parametrize("capture_content", [False, True])
async def test_valid_reasons_are_reported_only_when_capturing_content(
    capture_content: bool,
) -> None:
    evaluation = Evaluation(
        cases=[Case(id="text", arguments={"text": SECRET})],
        target=LocalSkill(Writer(), skill="echo"),
        scorers=[valid(int)],
        capture_content=capture_content,
    )

    [result] = (await evaluation.run()).results

    assert result.status is CaseStatus.FAILED
    assert (result.scores[0].reason is not None) is capture_content
    assert SECRET not in (result.scores[0].reason or "")
