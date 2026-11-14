"""Tests for ``Evaluation.run`` against a ``LocalSkill`` with function scorers.

Skills are model-free or use ``ScriptedModel``, so no test calls a provider.
"""

import asyncio
import logging
from typing import Any

import pytest
from pydantic import BaseModel

from agentframe import (
    Agent,
    DefinitionError,
    ExecutionPolicy,
    FinishReason,
    ModelResponse,
    Principal,
    RunContext,
    RunLimits,
    Runtime,
    Usage,
    agent,
    skill,
)
from agentframe.evaluation import (
    Case,
    CaseStatus,
    Evaluation,
    LocalSkill,
    Score,
    ScoreResult,
)
from agentframe.testing import ScriptedModel

SECRET = "sk-secret-value"


class Summary(BaseModel):
    title: str
    words: int


@agent(version="1.0.0")
class Assistant(Agent):
    """Counts and summarizes text."""

    def __init__(self, model: ScriptedModel | None = None) -> None:
        super().__init__(model=model)
        self.runs = 0

    @skill("count_words", tags=["test"])
    async def count_words(self, text: str, ctx: RunContext) -> int:
        """Count the words in text."""
        self.runs += 1
        return len(text.split())

    @skill("answer", tags=["test"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer a question with the model."""
        return await ctx.generate(question)

    @skill("summarize", tags=["test"])
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        """Summarize text without a model."""
        return Summary(title=text.split()[0], words=len(text.split()))

    @skill("check", limits=RunLimits(timeout_seconds=0.05), tags=["test"])
    async def check(self, text: str, ctx: RunContext) -> int:
        """Count words, but wait past the timeout or fail when asked to."""
        if text == "wait":
            await asyncio.sleep(10)
        if text.startswith("fail"):
            raise ValueError(f"cannot handle {text}")
        return len(text.split())

    @skill("reveal", requires_permissions=["vault.read"], tags=["test"])
    async def reveal(self, text: str, ctx: RunContext) -> str:
        """Return text to callers allowed to read it."""
        return text

    @skill("append", tags=["test"])
    async def append(self, items: Any, ctx: RunContext) -> int:
        """Append to the given list and return its new length."""
        items.append("extra")
        return len(items)


def is_three(case: Case[int], output: int) -> bool:
    return output == 3


async def is_positive(case: Case[int], output: int) -> Score:
    return Score(passed=output > 0, value=float(output), reason=f"got {output}")


def word_cases() -> list[Case[int]]:
    return [
        Case(id="three", arguments={"text": "one two three"}),
        Case(id="two", arguments={"text": "one two"}),
    ]


async def evaluate(
    skill_id: str,
    cases: list[Case[Any]],
    scorers: list[Any],
    **target_options: Any,
) -> list[Any]:
    target = LocalSkill(Assistant(), skill=skill_id, **target_options)
    report = await Evaluation(cases=cases, target=target, scorers=scorers).run()
    return list(report.results)


# Scoring


async def test_model_free_skill_is_scored_by_sync_and_async_scorers() -> None:
    three, two = await evaluate("count_words", word_cases(), [is_three, is_positive])

    assert three.case_id == "three"
    assert three.status is CaseStatus.PASSED
    assert three.scores == (
        ScoreResult(scorer="is_three", passed=True),
        ScoreResult(scorer="is_positive", passed=True, value=3.0),
    )
    assert two.status is CaseStatus.FAILED
    assert [score.passed for score in two.scores] == [False, True]
    assert three.invocation_id is not None
    assert three.model_calls == 0
    assert three.tool_calls == 0
    assert three.finish_reason is FinishReason.COMPLETED
    assert three.duration is not None
    assert three.error_type is None


async def test_scripted_model_skill_records_run_metadata() -> None:
    usage = Usage(input_tokens=12, output_tokens=4, total_tokens=16, estimated=False)
    reply = ModelResponse(
        text="Order A1 has shipped.", usage=usage, finish_reason=FinishReason.COMPLETED
    )
    model = ScriptedModel([reply])

    def mentions_a1(case: Case[str], output: str) -> bool:
        return "A1" in output

    evaluation = Evaluation(
        cases=[Case(id="a1", arguments={"question": "Where is order A1?"})],
        target=LocalSkill(Assistant(model=model), skill="answer"),
        scorers=[mentions_a1],
    )
    [result] = (await evaluation.run()).results

    assert result.status is CaseStatus.PASSED
    assert result.usage == usage
    assert result.model_calls == 1
    assert result.finish_reason is FinishReason.COMPLETED


async def test_a_case_passes_only_when_every_scorer_passes() -> None:
    def always(case: Case[int], output: int) -> bool:
        return True

    def never(case: Case[int], output: int) -> Score:
        return Score(passed=False)

    [result] = await evaluate("count_words", word_cases()[:1], [always, never])

    assert result.status is CaseStatus.FAILED


async def test_callable_objects_are_named_after_their_class() -> None:
    class AtLeast:
        def __init__(self, minimum: int) -> None:
            self.minimum = minimum

        def __call__(self, case: Case[int], output: int) -> bool:
            return output >= self.minimum

    [result] = await evaluate("count_words", word_cases()[:1], [AtLeast(2)])

    assert result.scores == (ScoreResult(scorer="AtLeast", passed=True),)


# Errors mark one case errored and the run continues


async def test_a_raising_scorer_errors_the_case_without_its_message() -> None:
    def explodes(case: Case[int], output: int) -> bool:
        raise RuntimeError(SECRET)

    first, second = await evaluate("count_words", word_cases(), [is_three, explodes])

    assert first.status is CaseStatus.ERRORED
    assert first.error_type == "RuntimeError"
    assert first.error_message == "Scorer 'explodes' raised RuntimeError."
    assert first.scores == (ScoreResult(scorer="is_three", passed=True),)
    assert first.invocation_id is not None
    assert second.status is CaseStatus.ERRORED


@pytest.mark.parametrize("verdict", [0.9, 1, None, "yes"])
async def test_a_scorer_returning_a_number_or_other_value_errors_the_case(
    verdict: object,
) -> None:
    def numeric(case: Case[int], output: int) -> Any:
        return verdict

    [result] = await evaluate("count_words", word_cases()[:1], [numeric])

    assert result.status is CaseStatus.ERRORED
    assert result.error_type == "DefinitionError"
    assert result.error_message is not None
    assert "Score(passed=" in result.error_message


async def test_target_errors_mark_cases_errored_and_the_run_continues() -> None:
    cases: list[Case[Any]] = [
        Case(id="limit", arguments={"text": "wait"}),
        Case(id="invalid", arguments={"words": 3}),
        Case(id="raises", arguments={"text": f"fail {SECRET}"}),
        Case(id="ok", arguments={"text": "one two three"}),
    ]

    limit, invalid, raises, ok = await evaluate("check", cases, [is_three])

    assert limit.status is CaseStatus.ERRORED
    assert limit.error_type == "TimeoutLimitExceededError"
    assert limit.invocation_id is not None
    assert limit.usage is None
    assert invalid.status is CaseStatus.ERRORED
    assert invalid.error_type == "InputValidationError"
    assert invalid.invocation_id is not None
    assert raises.status is CaseStatus.ERRORED
    assert raises.error_type == "ValueError"
    assert raises.error_message == "The target raised ValueError."
    assert ok.status is CaseStatus.PASSED


async def test_cancellation_propagates() -> None:
    def cancelled(case: Case[int], output: int) -> bool:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await evaluate("count_words", word_cases(), [cancelled])


# Permissions


async def test_a_protected_skill_needs_a_trusted_principal() -> None:
    cases: list[Case[Any]] = [Case(id="read", arguments={"text": "hello"})]

    def says_hello(case: Case[str], output: str) -> bool:
        return output == "hello"

    [denied] = await evaluate("reveal", cases, [says_hello])
    [allowed] = await evaluate(
        "reveal",
        cases,
        [says_hello],
        principal=Principal(id="tester", permissions=frozenset({"vault.read"})),
    )

    assert denied.status is CaseStatus.ERRORED
    assert denied.error_type == "PermissionDeniedError"
    assert allowed.status is CaseStatus.PASSED


async def test_the_target_uses_the_given_runtime() -> None:
    async def deny_all(*args: object) -> bool:
        return False

    runtime = Runtime(policy=ExecutionPolicy(input_checks=[deny_all]))
    [result] = await evaluate(
        "count_words", word_cases()[:1], [is_three], runtime=runtime
    )

    assert result.error_type == "PermissionDeniedError"


# Content capture


async def test_results_hold_no_content_by_default() -> None:
    cases = [Case(id="secret", arguments={"text": SECRET}, expected=1)]

    [result] = await evaluate("count_words", cases, [is_positive])

    assert result.status is CaseStatus.PASSED
    assert result.arguments is None
    assert result.text is None
    assert result.expected is None
    assert result.output is None
    assert result.scores[0].reason is None
    assert SECRET not in repr(result)


async def test_capture_content_keeps_inputs_outputs_and_reasons() -> None:
    cases = [Case(id="secret", arguments={"text": SECRET}, expected=1)]
    target = LocalSkill(Assistant(), skill="count_words")
    evaluation = Evaluation(
        cases=cases, target=target, scorers=[is_positive], capture_content=True
    )

    [result] = (await evaluation.run()).results

    assert result.arguments == {"text": SECRET}
    assert result.expected == 1
    assert result.output == 1
    assert result.scores[0].reason == "got 1"


async def test_the_harness_logs_case_ids_but_no_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cases: list[Case[Any]] = [Case(id="secret", arguments={"text": SECRET})]
    target = LocalSkill(Assistant(), skill="count_words")
    evaluation = Evaluation(
        cases=cases, target=target, scorers=[is_positive], capture_content=True
    )

    with caplog.at_level(logging.DEBUG, logger="agentframe.evaluation"):
        await evaluation.run()

    assert "secret" in caplog.text
    assert SECRET not in caplog.text


# Each run gets its own copy of the case's arguments


async def test_a_skill_cannot_change_the_case_arguments() -> None:
    items = ["a"]
    case: Case[int] = Case(id="append", arguments={"items": items})

    def any_length(case: Case[int], output: int) -> bool:
        return output == 2

    [result] = await evaluate("append", [case], [any_length])

    assert result.status is CaseStatus.PASSED
    assert items == ["a"]


# Checks before any case runs


async def test_text_cases_are_refused_before_any_case_runs() -> None:
    assistant = Assistant()
    cases: list[Case[Any]] = [
        Case(id="ok", arguments={"text": "one"}),
        Case(id="free-text", text="Count these words"),
    ]

    with pytest.raises(DefinitionError, match="'free-text' has text"):
        Evaluation(
            cases=cases,
            target=LocalSkill(assistant, skill="count_words"),
            scorers=[is_three],
        )
    assert assistant.runs == 0


def test_duplicate_case_ids_are_refused_without_echoing_them() -> None:
    cases: list[Case[Any]] = [
        Case(id=SECRET, arguments={"text": "one"}),
        Case(id="other", arguments={"text": "two"}),
        Case(id=SECRET, arguments={"text": "three"}),
    ]

    with pytest.raises(
        DefinitionError, match="Case 3 repeats the ID of case 1"
    ) as caught:
        Evaluation(
            cases=cases,
            target=LocalSkill(Assistant(), skill="count_words"),
            scorers=[is_three],
        )
    assert SECRET not in str(caught.value)


def test_expected_values_that_do_not_fit_the_skill_are_refused() -> None:
    cases = [Case(id="typo", arguments={"text": SECRET}, expected={"title": SECRET})]

    with pytest.raises(DefinitionError, match="'typo' expected value") as caught:
        Evaluation(
            cases=cases,
            target=LocalSkill(Assistant(), skill="summarize"),
            scorers=[is_three],
        )
    assert SECRET not in str(caught.value)


async def test_expected_values_are_converted_to_the_skill_return_type() -> None:
    cases = [
        Case(
            id="summary",
            arguments={"text": "Hello brave world"},
            expected={"title": "Hello", "words": 3},
        )
    ]

    def equals(case: Case[Summary], output: Summary) -> bool:
        return output == case.expected

    [result] = await evaluate("summarize", cases, [equals])

    assert result.status is CaseStatus.PASSED


@pytest.mark.parametrize(
    ("cases", "scorers", "message"),
    [
        ([], [is_three], "at least one case"),
        (word_cases(), [], "at least one scorer"),
        (word_cases(), ["is_three"], "must be callable"),
    ],
)
def test_an_evaluation_needs_cases_and_callable_scorers(
    cases: list[Case[Any]], scorers: list[Any], message: str
) -> None:
    target = LocalSkill(Assistant(), skill="count_words")

    with pytest.raises(DefinitionError, match=message):
        Evaluation(cases=cases, target=target, scorers=scorers)


def test_an_unknown_skill_is_refused() -> None:
    with pytest.raises(DefinitionError, match="no skill 'missing'"):
        LocalSkill(Assistant(), skill="missing")


@pytest.mark.parametrize(
    "fields",
    [{"passed": 1}, {"passed": True, "value": "high"}, {"passed": True, "value": True}],
)
def test_score_refuses_non_boolean_verdicts_and_non_numeric_values(
    fields: dict[str, Any],
) -> None:
    with pytest.raises(DefinitionError):
        Score(**fields)
