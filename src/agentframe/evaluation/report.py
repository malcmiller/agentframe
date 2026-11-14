"""Evaluation results: one ``CaseResult`` per case, collected in an ``EvalReport``."""

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from agentframe.runtime.usage import FinishReason, Usage


class CaseStatus(StrEnum):
    """How one case ended.

    Attributes:
        PASSED: The target ran and every scorer passed.
        FAILED: The target ran and at least one scorer failed.
        ERRORED: The target or a scorer raised, or a scorer returned
            something other than a ``bool`` or ``Score``.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"


@dataclass(frozen=True, kw_only=True)
class ScoreResult:
    """One scorer's verdict on one case.

    Attributes:
        scorer: The scorer's name.
        passed: Whether the output passed.
        value: The scorer's optional number.
        reason: The scorer's explanation; ``None`` unless the evaluation
            captures content, since it may quote the output.
    """

    scorer: str
    passed: bool
    value: float | None = None
    reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class CaseResult:
    """The outcome of one case: its status, scores and run metadata.

    Run metadata comes from the target's ``RunResult`` and is ``None`` when
    the target raised. Content fields (``arguments``, ``text``, ``expected``,
    ``output`` and score reasons) are ``None`` unless the evaluation was
    created with ``capture_content=True``.

    Attributes:
        case_id: The case's ID.
        status: Whether the case passed, failed or errored.
        scores: Each scorer's verdict, in scorer order. Scorers after one
            that raised do not run.
        error_type: The class name of the error that made the case errored.
        error_message: A safe description of that error: AgentFrame's own
            message for AgentFrame errors, otherwise which part raised. Any
            other exception's message is never kept, since it may hold
            content; its traceback is logged instead.
        invocation_id: The run's invocation ID, also kept when the run failed
            with an AgentFrame error.
        usage: Token usage; fields may be ``None`` (unknown, not zero).
        duration: How long the run took.
        model_calls: Model calls the run made.
        tool_calls: Tool calls the model asked for.
        finish_reason: Why the run finished.
        tags: The case's tags.
        arguments: The case's arguments, when captured.
        text: The case's text, when captured.
        expected: The case's expected value, when captured.
        output: The target's output, when captured.
    """

    case_id: str
    status: CaseStatus
    scores: tuple[ScoreResult, ...] = ()
    error_type: str | None = None
    error_message: str | None = None
    invocation_id: str | None = None
    usage: Usage | None = None
    duration: timedelta | None = None
    model_calls: int | None = None
    tool_calls: int | None = None
    finish_reason: FinishReason | None = None
    tags: tuple[str, ...] = ()
    arguments: Mapping[str, Any] | None = None
    text: str | None = None
    expected: Any = None
    output: Any = None


@dataclass(frozen=True, kw_only=True)
class UsageTotals:
    """Token usage summed over an evaluation's cases.

    A count is only summed when every case reported it, so unknown usage is
    never counted as zero.

    Attributes:
        input_tokens: Total input tokens, or ``None`` when any case's count is
            unknown.
        output_tokens: Total output tokens, or ``None`` when any case's count
            is unknown.
        total_tokens: Total tokens, or ``None`` when any case's count is
            unknown.
        known_total_tokens: The sum of the total token counts cases did
            report; a lower bound when ``unknown_cases`` is not zero.
        unknown_cases: Cases with no usage (the target raised) or with any
            unknown count.
        estimated: Whether any reported usage is an estimate.
    """

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    known_total_tokens: int
    unknown_cases: int
    estimated: bool


@dataclass(frozen=True, kw_only=True)
class Latency:
    """Case durations, by the nearest-rank percentile.

    Attributes:
        p50: The median duration.
        p95: The 95th percentile duration.
        max: The longest duration.
    """

    p50: timedelta
    p95: timedelta
    max: timedelta


@dataclass(frozen=True, kw_only=True)
class EvalReport:
    """The results of one evaluation run, with aggregates over them.

    ``str(report)`` is a plain-text summary: totals, then a table with failing
    cases first. It holds IDs, statuses, scorer names, numbers and error
    classes only; score reasons appear only when the evaluation captured
    content.

    Attributes:
        results: One result per case, in case order.
    """

    results: tuple[CaseResult, ...]

    @property
    def passed_count(self) -> int:
        """Cases that passed."""
        return self._count(CaseStatus.PASSED)

    @property
    def failed_count(self) -> int:
        """Cases that ran but a scorer failed."""
        return self._count(CaseStatus.FAILED)

    @property
    def errored_count(self) -> int:
        """Cases where the target or a scorer raised."""
        return self._count(CaseStatus.ERRORED)

    @property
    def pass_rate(self) -> float:
        """Passed cases divided by cases that ran; ``0.0`` with no cases.

        Errored cases ran, so they count against the rate.
        """
        return _rate(self.passed_count, len(self.results))

    @property
    def scorer_pass_rates(self) -> dict[str, float]:
        """Each scorer's pass rate over the cases it scored, in scorer order.

        A scorer that did not run for a case (the target or an earlier scorer
        raised) does not count for that case.
        """
        passed: Counter[str] = Counter()
        scored: Counter[str] = Counter()
        for result in self.results:
            for score in result.scores:
                scored[score.scorer] += 1
                if score.passed:
                    passed[score.scorer] += 1
        return {name: _rate(passed[name], scored[name]) for name in scored}

    @property
    def tag_pass_rates(self) -> dict[str, float]:
        """The pass rate of the cases with each tag, in first-seen tag order."""
        passed: Counter[str] = Counter()
        tagged: Counter[str] = Counter()
        for result in self.results:
            for tag in result.tags:
                tagged[tag] += 1
                if result.status is CaseStatus.PASSED:
                    passed[tag] += 1
        return {tag: _rate(passed[tag], tagged[tag]) for tag in tagged}

    @property
    def total_usage(self) -> UsageTotals:
        """Token usage summed over every case; unknown is never zero."""
        usages = [result.usage for result in self.results]
        unknown_cases = sum(not _is_fully_known(usage) for usage in usages)
        reported = [usage for usage in usages if usage is not None]

        def total(counts: list[int | None]) -> int | None:
            known = [count for count in counts if count is not None]
            return sum(known) if len(known) == len(usages) else None

        return UsageTotals(
            input_tokens=total([usage.input_tokens for usage in reported]),
            output_tokens=total([usage.output_tokens for usage in reported]),
            total_tokens=total([usage.total_tokens for usage in reported]),
            known_total_tokens=sum(usage.total_tokens or 0 for usage in reported),
            unknown_cases=unknown_cases,
            estimated=any(usage.estimated for usage in reported),
        )

    @property
    def latency(self) -> Latency | None:
        """p50, p95 and max of case durations; ``None`` when no case has one.

        Cases whose target raised have no duration and are left out.
        """
        durations = sorted(
            result.duration for result in self.results if result.duration is not None
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
        """How many cases errored with each error class.

        For example ``{"TimeoutLimitExceededError": 2}``, in first-seen order.
        """
        error_types = [result.error_type for result in self.results]
        return dict(Counter(name for name in error_types if name is not None))

    def _count(self, status: CaseStatus) -> int:
        return sum(result.status is status for result in self.results)

    def __str__(self) -> str:
        return _summary(self)


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def _is_fully_known(usage: Usage | None) -> bool:
    if usage is None:
        return False
    counts = (usage.input_tokens, usage.output_tokens, usage.total_tokens)
    return None not in counts


def _percentile(sorted_durations: list[timedelta], percent: int) -> timedelta:
    # Nearest rank: the smallest duration with at least `percent`% at or below it.
    rank = math.ceil(percent / 100 * len(sorted_durations))
    return sorted_durations[max(rank, 1) - 1]


# Errored and failed cases come before passed ones in the summary table.
_STATUS_ORDER = {CaseStatus.ERRORED: 0, CaseStatus.FAILED: 1, CaseStatus.PASSED: 2}


def _summary(report: EvalReport) -> str:
    lines = [
        f"Evaluation: {len(report.results)} cases, {report.passed_count} passed, "
        f"{report.failed_count} failed, {report.errored_count} errored "
        f"(pass rate {_percent(report.pass_rate)})",
        f"Tokens: {_usage_line(report.total_usage)}",
        f"Latency: {_latency_line(report.latency)}",
    ]
    if report.errors_by_class:
        lines.append(f"Errors: {_counts_line(report.errors_by_class)}")
    if report.scorer_pass_rates:
        lines.append(f"Scorers: {_rates_line(report.scorer_pass_rates)}")
    if report.tag_pass_rates:
        lines.append(f"Tags: {_rates_line(report.tag_pass_rates)}")
    lines.append("")
    lines.extend(_table(report.results))
    lines.extend(_reasons(report.results))
    return "\n".join(lines)


def _table(results: tuple[CaseResult, ...]) -> list[str]:
    ordered = sorted(results, key=lambda result: _STATUS_ORDER[result.status])
    rows = [("case", "status", "scores", "tokens", "duration")]
    rows.extend(
        (
            result.case_id,
            str(result.status),
            _scores_cell(result),
            _tokens_cell(result.usage),
            _seconds(result.duration) if result.duration is not None else "-",
        )
        for result in ordered
    )
    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    return [
        "  ".join(
            cell.ljust(width) for cell, width in zip(row, widths, strict=True)
        ).rstrip()
        for row in rows
    ]


def _reasons(results: tuple[CaseResult, ...]) -> list[str]:
    # Reasons are content, so they exist only when the evaluation captured it.
    lines = [
        f"  {result.case_id} {score.scorer}: {score.reason}"
        for result in results
        for score in result.scores
        if not score.passed and score.reason is not None
    ]
    return ["", "Failure reasons:", *lines] if lines else []


def _scores_cell(result: CaseResult) -> str:
    cells = [
        f"{score.scorer}={'pass' if score.passed else 'fail'}"
        + (f"({score.value:g})" if score.value is not None else "")
        for score in result.scores
    ]
    if result.error_type is not None:
        cells.append(f"error={result.error_type}")
    return " ".join(cells) or "-"


def _tokens_cell(usage: Usage | None) -> str:
    if usage is None or usage.total_tokens is None:
        return "unknown"
    return f"~{usage.total_tokens}" if usage.estimated else str(usage.total_tokens)


def _usage_line(usage: UsageTotals) -> str:
    if usage.total_tokens is not None:
        estimated = " (estimated)" if usage.estimated else ""
        return f"{usage.total_tokens} total{estimated}"
    return (
        f"unknown; {usage.known_total_tokens} reported, "
        f"{usage.unknown_cases} cases with unknown usage"
    )


def _latency_line(latency: Latency | None) -> str:
    if latency is None:
        return "-"
    return (
        f"p50 {_seconds(latency.p50)}, p95 {_seconds(latency.p95)}, "
        f"max {_seconds(latency.max)}"
    )


def _rates_line(rates: dict[str, float]) -> str:
    return ", ".join(f"{name} {_percent(rate)}" for name, rate in rates.items())


def _counts_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items())


def _percent(rate: float) -> str:
    return f"{rate:.1%}"


def _seconds(duration: timedelta) -> str:
    return f"{duration.total_seconds():.2f}s"
