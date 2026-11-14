"""Tests for concurrency, repeats, the token budget and the timeout.

A gated ``ScriptedModel`` holds model calls open until the test releases
them, so the tests see exactly how many cases run at once without sleeping.
"""

import asyncio
from typing import Any

import pytest

from agentframe import (
    Agent,
    DefinitionError,
    FinishReason,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    RunContext,
    RunLimits,
    RunOptions,
    Usage,
    agent,
    skill,
)
from agentframe.evaluation import Case, CaseStatus, Evaluation, LocalSkill
from agentframe.evaluation.budget import TokenBudget
from agentframe.testing import ScriptedModel


class GatedModel(ScriptedModel):
    """Answers every call once ``gate`` is set, counting calls in flight.

    Questions starting with ``"slow"`` also wait for ``slow_gate``.
    """

    def __init__(self, total_tokens: int = 10) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.slow_gate = asyncio.Event()
        self.running = 0
        self.most_running = 0
        self._usage = Usage(total_tokens - 1, 1, total_tokens, estimated=False)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            await self.gate.wait()
            if _question(request).startswith("slow"):
                await self.slow_gate.wait()
        finally:
            self.running -= 1
        return ModelResponse(
            text=_question(request),
            usage=self._usage,
            finish_reason=FinishReason.COMPLETED,
        )


def _question(request: ModelRequest) -> str:
    return str(request.messages[-1].content)


@agent(version="1.0.0")
class Echo(Agent):
    """Echoes questions through a model, or counts its own runs."""

    def __init__(self, model: ScriptedModel | None = None) -> None:
        super().__init__(model=model)
        self.runs = 0

    @skill("echo", tags=["test"])
    async def echo(self, question: str, ctx: RunContext) -> str:
        """Return the model's answer to the question."""
        return await ctx.generate(question)

    @skill("count_runs", tags=["test"])
    async def count_runs(self, ctx: RunContext) -> int:
        """Return how many times this skill has run."""
        self.runs += 1
        return self.runs

    @skill("broken", tags=["test"])
    async def broken(self, ctx: RunContext) -> int:
        """Always fail."""
        raise ValueError("broken")

    @skill("small", limits=RunLimits(max_total_tokens=50), tags=["test"])
    async def small(self, ctx: RunContext) -> int:
        """A skill with its own token limit."""
        return 0


def anything(case: Case[Any], output: Any) -> bool:
    return True


def is_odd(case: Case[Any], output: int) -> bool:
    return output % 2 == 1


def questions(*ids: str) -> list[Case[Any]]:
    return [Case(id=case_id, arguments={"question": case_id}) for case_id in ids]


def echo_target(model: GatedModel, max_total_tokens: int = 100) -> LocalSkill:
    options = RunOptions(
        limits=RunLimits(max_total_tokens=max_total_tokens),
        model_settings=ModelSettings(max_output_tokens=20),
    )
    return LocalSkill(Echo(model=model), skill="echo", options=options)


async def until(condition: Any) -> None:
    """Yield to the event loop until ``condition()`` holds."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


async def settle_loop() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# Concurrency


@pytest.mark.parametrize("concurrency", [1, 3])
async def test_concurrency_never_exceeds_the_setting(concurrency: int) -> None:
    model = GatedModel()
    evaluation = Evaluation(
        cases=questions(*"abcdefg"),
        target=echo_target(model),
        scorers=[anything],
        concurrency=concurrency,
    )
    running = asyncio.create_task(evaluation.run())

    await until(lambda: model.running == concurrency)
    await settle_loop()
    assert model.running == concurrency
    model.gate.set()
    report = await running

    assert model.most_running == concurrency
    assert report.passed_count == 7


async def test_the_default_concurrency_is_four() -> None:
    model = GatedModel()
    evaluation = Evaluation(
        cases=questions(*"abcdef"), target=echo_target(model), scorers=[anything]
    )
    running = asyncio.create_task(evaluation.run())

    await until(lambda: model.running == 4)
    await settle_loop()
    model.gate.set()
    await running

    assert model.most_running == 4


async def test_results_stay_in_case_order_whatever_order_cases_finish_in() -> None:
    model = GatedModel()
    model.gate.set()
    evaluation = Evaluation(
        cases=questions("slow", "b", "c"), target=echo_target(model), scorers=[anything]
    )
    running = asyncio.create_task(evaluation.run())

    await until(lambda: len(model.requests) == 3 and model.running == 1)
    model.slow_gate.set()
    report = await running

    assert [_question(request) for request in model.requests] == ["slow", "b", "c"]
    assert [result.case_id for result in report.results] == ["slow", "b", "c"]


# Repeats


async def test_repeats_count_every_attempt_and_flag_mixed_outcomes() -> None:
    evaluation = Evaluation(
        cases=[Case(id="first", arguments={}), Case(id="second", arguments={})],
        target=LocalSkill(Echo(), skill="count_runs"),
        scorers=[is_odd],
        concurrency=1,
        repeats=3,
    )
    report = await evaluation.run()

    # Runs 1-3 are "first" (odd, even, odd); runs 4-6 are "second".
    assert [(result.case_id, result.attempt) for result in report.results] == [
        ("first", 1),
        ("first", 2),
        ("first", 3),
        ("second", 1),
        ("second", 2),
        ("second", 3),
    ]
    assert report.passed_count == 3
    assert report.pass_rate == 0.5
    assert report.flaky_cases == ["first", "second"]


async def test_cases_with_one_outcome_are_not_flaky() -> None:
    evaluation = Evaluation(
        cases=[Case(id="steady", arguments={})],
        target=LocalSkill(Echo(), skill="count_runs"),
        scorers=[anything],
        repeats=3,
    )
    report = await evaluation.run()

    assert report.pass_rate == 1.0
    assert report.flaky_cases == []
    assert "steady #3" in str(report)


# Evaluation-wide token budget


async def test_a_small_budget_admits_only_what_fits_and_reports_the_rest() -> None:
    model = GatedModel(total_tokens=100)
    evaluation = Evaluation(
        cases=questions(*"abcde"),
        target=echo_target(model, max_total_tokens=100),
        scorers=[anything],
        max_total_tokens=250,
    )
    running = asyncio.create_task(evaluation.run())

    # Two 100-token holds fit in 250; a third would over-commit the budget.
    await until(lambda: model.running == 2)
    await settle_loop()
    assert model.running == 2
    model.gate.set()
    report = await running

    assert model.most_running == 2
    statuses = [result.status for result in report.results]
    assert statuses == [CaseStatus.PASSED] * 2 + [CaseStatus.NOT_RUN] * 3
    assert report.not_run_count == 3
    assert report.pass_rate == 1.0
    assert report.total_usage.total_tokens == 200
    assert not report.over_budget
    not_run = report.results[-1]
    assert not_run.error_message == (
        "The evaluation's token budget could not fit this attempt."
    )
    assert not_run.error_type is None
    assert not_run.invocation_id is None
    assert "2 passed, 0 failed, 0 errored, 3 not run" in str(report)


async def test_unused_tokens_return_to_the_budget_for_later_cases() -> None:
    model = GatedModel(total_tokens=10)
    model.gate.set()
    evaluation = Evaluation(
        cases=questions(*"abcde"),
        target=echo_target(model, max_total_tokens=100),
        scorers=[anything],
        max_total_tokens=250,
    )
    report = await evaluation.run()

    assert report.passed_count == 5
    assert report.total_usage.total_tokens == 50


async def test_unknown_usage_keeps_the_whole_hold_and_is_reported() -> None:
    # A failed run reports no usage, so it may have used its whole ceiling.
    limits = RunLimits(max_total_tokens=100)
    evaluation = Evaluation(
        cases=[Case(id=case_id, arguments={}) for case_id in "abc"],
        target=LocalSkill(Echo(), skill="broken", options=RunOptions(limits=limits)),
        scorers=[anything],
        concurrency=1,
        max_total_tokens=250,
    )
    report = await evaluation.run()

    statuses = [result.status for result in report.results]
    assert statuses == [CaseStatus.ERRORED, CaseStatus.ERRORED, CaseStatus.NOT_RUN]
    assert report.total_usage.unknown_cases == 2
    assert (
        "Budget: 250 tokens, not fully enforced (2 attempts with unknown usage)"
        in str(report).splitlines()
    )


async def test_without_a_budget_holds_never_wait() -> None:
    model = GatedModel(total_tokens=100)
    model.gate.set()
    evaluation = Evaluation(
        cases=questions(*"abcdef"),
        target=echo_target(model, max_total_tokens=100),
        scorers=[anything],
    )
    report = await evaluation.run()

    assert report.passed_count == 6
    assert report.max_total_tokens is None
    assert "Budget" not in str(report)


async def test_holds_never_exceed_the_budget_and_wait_for_running_runs() -> None:
    budget = TokenBudget(250)
    assert await budget.hold(100)
    assert await budget.hold(100)

    third = asyncio.create_task(budget.hold(100))
    await settle_loop()
    assert not third.done()
    assert budget.charged == 200

    await budget.settle(100, 30)  # 30 used + 100 still held
    assert await third
    assert budget.charged == 230


async def test_a_hold_is_refused_once_nothing_runs_and_it_still_does_not_fit() -> None:
    budget = TokenBudget(150)
    assert await budget.hold(100)
    second = asyncio.create_task(budget.hold(100))
    await settle_loop()

    await budget.settle(100, None)  # unknown usage keeps the whole hold

    assert await second is False
    assert budget.charged == 100


async def test_reported_usage_replaces_a_hold_even_when_higher() -> None:
    budget = TokenBudget(100)
    assert await budget.hold(100)
    await budget.settle(100, 140)

    assert budget.charged == 140
    assert await budget.hold(1) is False


def test_the_token_ceiling_resolves_like_the_run_limits() -> None:
    assert LocalSkill(Echo(), skill="echo").token_ceiling == 100_000
    assert LocalSkill(Echo(), skill="small").token_ceiling == 50
    limits = RunLimits(max_total_tokens=20)
    target = LocalSkill(Echo(), skill="small", options=RunOptions(limits=limits))
    assert target.token_ceiling == 20


# Evaluation-wide timeout


async def test_the_timeout_cancels_running_cases_and_skips_the_rest() -> None:
    model = GatedModel()  # the gate never opens
    evaluation = Evaluation(
        cases=questions(*"abc"),
        target=echo_target(model),
        scorers=[anything],
        concurrency=1,
        timeout_seconds=0.01,
    )
    report = await evaluation.run()

    running, *rest = report.results
    assert running.status is CaseStatus.ERRORED
    assert running.error_type == "TimeoutLimitExceededError"
    assert running.error_message == (
        "The evaluation's timeout expired while this attempt ran."
    )
    assert [result.status for result in rest] == [CaseStatus.NOT_RUN] * 2
    assert rest[0].error_message == (
        "The evaluation's timeout expired before this attempt."
    )
    assert model.running == 0
    assert report.errors_by_class == {"TimeoutLimitExceededError": 1}


async def test_outside_cancellation_still_propagates() -> None:
    model = GatedModel()
    evaluation = Evaluation(
        cases=questions(*"abc"),
        target=echo_target(model),
        scorers=[anything],
        timeout_seconds=60,
    )
    running = asyncio.create_task(evaluation.run())
    await until(lambda: model.running == 3)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert model.running == 0


# Settings


@pytest.mark.parametrize(
    "settings",
    [
        {"concurrency": 0},
        {"concurrency": True},
        {"repeats": 0},
        {"repeats": 1.5},
        {"max_total_tokens": 0},
        {"timeout_seconds": 0},
    ],
)
def test_invalid_settings_are_refused(settings: dict[str, Any]) -> None:
    with pytest.raises(DefinitionError):
        Evaluation(
            cases=[Case(id="a", arguments={})],
            target=LocalSkill(Echo(), skill="count_runs"),
            scorers=[anything],
            **settings,
        )
