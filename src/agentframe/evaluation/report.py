"""Evaluation results: a ``CaseResult`` per attempt, in an ``EvalReport``.

The result types are defined in ``evaluation.results`` and the summary and
JSON rendering in ``evaluation.report_format``; the result types are also
importable from here.
"""

import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from agentframe.evaluation.report_format import (
    known_tokens,
    percent,
    report_json,
    summary,
    total_tokens,
    unknown_cases,
)
from agentframe.evaluation.results import (
    CaseResult,
    CaseStatus,
    Latency,
    ScoreResult,
    UsageTotals,
)
from agentframe.usage import Usage

__all__ = [
    "CaseResult",
    "CaseStatus",
    "EvalReport",
    "Latency",
    "ScoreResult",
    "UsageTotals",
]


@dataclass(frozen=True, kw_only=True)
class EvalReport:
    """The results of one evaluation run, with aggregates over them.

    Every aggregate counts attempts: with ``repeats=3`` each case adds three
    results. Attempts that did not run (``not_run``) are counted by
    ``not_run_count`` and left out of every other aggregate, so they never
    count as passes or failures.

    ``str(report)`` is a plain-text summary: totals, then a table with failing
    cases first. It holds IDs, statuses, scorer names, numbers and error
    classes only; score reasons appear only when the evaluation captured
    content.

    Attributes:
        results: One result per attempt, in case order, then attempt order.
        max_total_tokens: The evaluation-wide token budget, or ``None``.
        target: What the cases ran against, for example
            ``{"type": "local_skill", "agent": "assistant", "skill": "count_words"}``.
        scorers: The scorers' names, in scorer order.
        capture_content: Whether the results keep case content.
    """

    results: tuple[CaseResult, ...]
    max_total_tokens: int | None = None
    target: Mapping[str, str] = field(default_factory=dict)
    scorers: tuple[str, ...] = ()
    capture_content: bool = False

    @property
    def passed_count(self) -> int:
        """Attempts that passed."""
        return self._count(CaseStatus.PASSED)

    @property
    def failed_count(self) -> int:
        """Attempts that ran but a scorer failed."""
        return self._count(CaseStatus.FAILED)

    @property
    def errored_count(self) -> int:
        """Attempts where the target or a scorer raised."""
        return self._count(CaseStatus.ERRORED)

    @property
    def not_run_count(self) -> int:
        """Attempts that never started, because of the budget or timeout."""
        return self._count(CaseStatus.NOT_RUN)

    @property
    def pass_rate(self) -> float:
        """Passed attempts divided by attempts that ran; ``0.0`` if none ran.

        Errored attempts ran, so they count against the rate. ``not_run``
        attempts are reported by ``not_run_count`` instead.
        """
        return _rate(self.passed_count, len(self._ran))

    @property
    def scorer_pass_rates(self) -> dict[str, float]:
        """Each scorer's pass rate over the attempts it scored, in scorer order.

        A scorer that did not run for an attempt (the target or an earlier
        scorer raised) does not count for that attempt.
        """
        passed: Counter[str] = Counter()
        scored: Counter[str] = Counter()
        for result in self._ran:
            for score in result.scores:
                scored[score.scorer] += 1
                if score.passed:
                    passed[score.scorer] += 1
        return {name: _rate(passed[name], scored[name]) for name in scored}

    @property
    def tag_pass_rates(self) -> dict[str, float]:
        """The pass rate of the attempts with each tag, in first-seen tag order."""
        passed: Counter[str] = Counter()
        tagged: Counter[str] = Counter()
        for result in self._ran:
            for tag in result.tags:
                tagged[tag] += 1
                if result.status is CaseStatus.PASSED:
                    passed[tag] += 1
        return {tag: _rate(passed[tag], tagged[tag]) for tag in tagged}

    @property
    def flaky_cases(self) -> list[str]:
        """IDs of cases whose attempts that ran ended differently, in case order.

        For example, one attempt passed and another failed or errored. Only
        possible with ``repeats`` above 1.
        """
        statuses: dict[str, set[CaseStatus]] = {}
        for result in self._ran:
            statuses.setdefault(result.case_id, set()).add(result.status)
        return [case_id for case_id, seen in statuses.items() if len(seen) > 1]

    @property
    def total_usage(self) -> UsageTotals:
        """The targets' token usage summed over the attempts that ran.

        Unknown is never zero. ``Judge`` usage is in ``total_judge_usage``.
        """
        return _usage_totals([result.usage for result in self._ran])

    @property
    def total_judge_usage(self) -> UsageTotals | None:
        """``Judge`` token usage summed over the attempts a judge ran for.

        ``None`` when no judge ran. Unknown is never zero.
        """
        judged = [result for result in self._ran if result.judge_usage is not None]
        if not judged:
            return None
        return _usage_totals([result.judge_usage for result in judged])

    @property
    def over_budget(self) -> bool:
        """Whether reported target and judge usage went over ``max_total_tokens``.

        Admission never over-commits the budget, but a run's reported usage
        replaces its hold even when higher, so an overrun is reported here
        rather than prevented. Attempts with unknown usage
        (``unknown_cases`` of ``total_usage`` or ``total_judge_usage``) mean
        the budget could not be fully enforced.
        """
        if self.max_total_tokens is None:
            return False
        return known_tokens(self) > self.max_total_tokens

    @property
    def latency(self) -> Latency | None:
        """p50, p95 and max of attempt durations; ``None`` when none has one.

        Attempts whose target raised, or that did not run, have no duration
        and are left out.
        """
        durations = sorted(
            result.duration for result in self._ran if result.duration is not None
        )
        if not durations:
            return None
        return Latency(
            p50=_percentile(durations, 50),
            p95=_percentile(durations, 95),
            max=durations[-1],
        )

    @property
    def errors_by_class(self) -> dict[str, int]:
        """How many attempts errored with each error class.

        For example ``{"TimeoutLimitExceededError": 2}``, in first-seen order.
        """
        error_types = [result.error_type for result in self._ran]
        return dict(Counter(name for name in error_types if name is not None))

    def write_json(self, path: str | Path) -> None:
        """Write the report as a versioned JSON document, for CI artifacts.

        The document's ``"schema"`` is ``"agentframe.evaluation.report/1"``.
        It holds the AgentFrame version, the target, scorer names, every
        aggregate and one entry per attempt. Unknown token counts are
        ``null``, never 0. Like the summary, it holds no case content (case
        inputs, expected values, outputs, score reasons) and no error
        messages unless the evaluation captured content; a ``not_run``
        attempt's fixed reason is always kept. Treat a file with content as
        sensitive.

        Example::

            report.write_json("eval-report.json")

        Args:
            path: Where to write the file; an existing file is replaced.
        """
        document = json.dumps(report_json(self), indent=2, ensure_ascii=False)
        Path(path).write_text(document + "\n", encoding="utf-8")

    def require(
        self,
        *,
        min_pass_rate: float | None = None,
        max_errors: int | None = 0,
        max_total_tokens: int | None = None,
        allow_not_run: bool = False,
        allow_over_budget: bool = False,
    ) -> None:
        """Fail, like a test assertion, when the report misses a threshold.

        By default an errored or not-run attempt, or reported usage over the
        evaluation's budget, fails; ``max_errors=None``, ``allow_not_run=True``
        and ``allow_over_budget=True`` accept them.

        Example::

            async def test_count_words() -> None:
                report = await evaluation.run()
                report.require(min_pass_rate=0.9)

        Args:
            min_pass_rate: The lowest acceptable ``pass_rate``, from 0.0 to
                1.0. Missed when no attempt ran, whatever the value.
            max_errors: The most errored attempts accepted; ``None`` accepts
                any number.
            max_total_tokens: The most tokens the evaluation may have used,
                target and ``Judge`` runs together. Missed when any target or
                judge usage is unknown, since the total cannot be shown to be
                within it.
            allow_not_run: Whether attempts that never started are accepted.
            allow_over_budget: Whether reported usage over
                ``EvalReport.max_total_tokens`` is accepted.

        Raises:
            AssertionError: A threshold was missed. The message lists each
                miss, then the summary (``str(report)``), so it holds no case
                content unless the evaluation captured it.
            ValueError: ``min_pass_rate`` is outside 0.0 to 1.0,
                ``max_errors`` is negative or ``max_total_tokens`` is not
                greater than 0.
        """
        _check_thresholds(min_pass_rate, max_errors, max_total_tokens)
        misses = _misses(
            self,
            min_pass_rate=min_pass_rate,
            max_errors=max_errors,
            max_total_tokens=max_total_tokens,
            allow_not_run=allow_not_run,
            allow_over_budget=allow_over_budget,
        )
        if misses:
            listed = "\n".join(f"- {miss}" for miss in misses)
            raise AssertionError(
                f"Evaluation requirements not met:\n{listed}\n\n{self}"
            )

    @property
    def _ran(self) -> list[CaseResult]:
        # The one place that leaves not_run attempts out of the aggregates.
        return [
            result for result in self.results if result.status is not CaseStatus.NOT_RUN
        ]

    def _count(self, status: CaseStatus) -> int:
        return sum(result.status is status for result in self.results)

    def __str__(self) -> str:
        return summary(self)


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def _usage_totals(usages: list[Usage | None]) -> UsageTotals:
    reported = [usage for usage in usages if usage is not None]

    def total(counts: list[int | None]) -> int | None:
        known = [count for count in counts if count is not None]
        return sum(known) if len(known) == len(usages) else None

    return UsageTotals(
        input_tokens=total([usage.input_tokens for usage in reported]),
        output_tokens=total([usage.output_tokens for usage in reported]),
        total_tokens=total([usage.total_tokens for usage in reported]),
        known_total_tokens=sum(usage.total_tokens or 0 for usage in reported),
        unknown_cases=sum(not _is_fully_known(usage) for usage in usages),
        estimated=any(usage.estimated for usage in reported),
    )


def _is_fully_known(usage: Usage | None) -> bool:
    if usage is None:
        return False
    counts = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
    return None not in counts


def _percentile(sorted_durations: list[timedelta], percent: int) -> timedelta:
    # Nearest rank: the smallest duration with at least `percent`% at or below it.
    rank = math.ceil(percent / 100 * len(sorted_durations))
    return sorted_durations[max(rank, 1) - 1]


def _check_thresholds(
    min_pass_rate: float | None, max_errors: int | None, max_total_tokens: int | None
) -> None:
    if min_pass_rate is not None and not 0.0 <= min_pass_rate <= 1.0:
        raise ValueError("min_pass_rate must be from 0.0 to 1.0.")
    if max_errors is not None and max_errors < 0:
        raise ValueError("max_errors must be 0 or more.")
    if max_total_tokens is not None and not max_total_tokens > 0:
        raise ValueError("max_total_tokens must be greater than 0.")


def _misses(
    report: EvalReport,
    *,
    min_pass_rate: float | None,
    max_errors: int | None,
    max_total_tokens: int | None,
    allow_not_run: bool,
    allow_over_budget: bool,
) -> list[str]:
    # Each miss names only counts and thresholds, never case content.
    misses: list[str] = []
    if min_pass_rate is not None:
        if not report._ran:
            misses.append("no attempt ran, so the pass rate cannot be checked")
        elif report.pass_rate < min_pass_rate:
            misses.append(
                f"pass rate {percent(report.pass_rate)} is below "
                f"{percent(min_pass_rate)}"
            )
    if max_errors is not None and report.errored_count > max_errors:
        misses.append(
            f"{report.errored_count} attempts errored; at most {max_errors} allowed"
        )
    if not allow_not_run and report.not_run_count:
        misses.append(f"{report.not_run_count} attempts did not run")
    if not allow_over_budget and report.over_budget:
        misses.append(
            f"{known_tokens(report)} reported tokens are over the budget of "
            f"{report.max_total_tokens}"
        )
    if max_total_tokens is not None:
        # Target and judge tokens count together, as for the budget.
        used = total_tokens(report)
        if used is None:
            misses.append(
                f"token usage is unknown for {unknown_cases(report)} target or "
                f"judge runs, so it cannot be shown to be at most {max_total_tokens}"
            )
        elif used > max_total_tokens:
            misses.append(f"{used} tokens used; at most {max_total_tokens} allowed")
    return misses
