"""Render an ``EvalReport`` as its plain-text summary or its versioned JSON."""

from dataclasses import asdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic_core import to_jsonable_python

import agentframe
from agentframe.evaluation.results import (
    CaseResult,
    CaseStatus,
    Latency,
    ScoreResult,
    UsageTotals,
)
from agentframe.usage import Usage

if TYPE_CHECKING:
    # Imported only for type checking: the report module imports this one.
    from agentframe.evaluation.report import EvalReport

# The JSON report's schema identifier; the number goes up on breaking changes.
SCHEMA = "agentframe.evaluation.report/1"


def known_tokens(report: "EvalReport") -> int:
    judge = report.total_judge_usage
    judged = judge.known_total_tokens if judge else 0
    return report.total_usage.known_total_tokens + judged


def total_tokens(report: "EvalReport") -> int | None:
    """Target plus judge tokens, or ``None`` when any count is unknown."""
    judge = report.total_judge_usage
    target_tokens = report.total_usage.total_tokens
    judge_tokens = judge.total_tokens if judge else 0
    if target_tokens is None or judge_tokens is None:
        return None
    return target_tokens + judge_tokens


def unknown_cases(report: "EvalReport") -> int:
    judge = report.total_judge_usage
    judged = judge.unknown_cases if judge else 0
    return report.total_usage.unknown_cases + judged


def report_json(report: "EvalReport") -> dict[str, Any]:
    judge_usage = report.total_judge_usage
    budget_fully_enforced = (
        None if report.max_total_tokens is None else unknown_cases(report) == 0
    )
    return {
        "schema": SCHEMA,
        "agentframe_version": agentframe.__version__,
        "target": dict(report.target),
        "scorers": list(report.scorers),
        "capture_content": report.capture_content,
        "max_total_tokens": report.max_total_tokens,
        "aggregates": {
            "attempt_count": len(report.results),
            "passed_count": report.passed_count,
            "failed_count": report.failed_count,
            "errored_count": report.errored_count,
            "not_run_count": report.not_run_count,
            "pass_rate": report.pass_rate,
            "scorer_pass_rates": report.scorer_pass_rates,
            "tag_pass_rates": report.tag_pass_rates,
            "flaky_cases": report.flaky_cases,
            "total_usage": asdict(report.total_usage),
            "total_judge_usage": asdict(judge_usage) if judge_usage else None,
            "over_budget": report.over_budget,
            "budget_fully_enforced": budget_fully_enforced,
            "latency": _latency_json(report.latency),
            "errors_by_class": report.errors_by_class,
        },
        "results": [
            _result_json(result, capture_content=report.capture_content)
            for result in report.results
        ],
    }


def _result_json(result: CaseResult, *, capture_content: bool) -> dict[str, Any]:
    # A not_run reason is one of the harness's fixed strings, never content.
    keep_message = capture_content or result.status is CaseStatus.NOT_RUN
    entry: dict[str, Any] = {
        "case_id": result.case_id,
        "attempt": result.attempt,
        "status": str(result.status),
        "tags": list(result.tags),
        "scores": [
            _score_json(score, capture_content=capture_content)
            for score in result.scores
        ],
        "error_type": result.error_type,
        "error_message": result.error_message if keep_message else None,
        "invocation_id": result.invocation_id,
        "usage": _usage_json(result.usage),
        "judge_usage": _usage_json(result.judge_usage),
        "duration_seconds": _seconds_json(result.duration),
        "model_calls": result.model_calls,
        "tool_calls": result.tool_calls,
        "finish_reason": result.finish_reason,
    }
    if capture_content:
        entry["arguments"] = _content_json(result.arguments)
        entry["text"] = result.text
        entry["expected"] = _content_json(result.expected)
        entry["output"] = _content_json(result.output)
    return entry


def _usage_json(usage: Usage | None) -> dict[str, Any] | None:
    return asdict(usage) if usage is not None else None


def _score_json(score: ScoreResult, *, capture_content: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "scorer": score.scorer,
        "passed": score.passed,
        "value": score.value,
        "votes_cast": score.votes_cast,
        "votes_passed": score.votes_passed,
    }
    if capture_content:
        entry["reason"] = score.reason
    return entry


def _latency_json(latency: Latency | None) -> dict[str, float] | None:
    if latency is None:
        return None
    return {
        "p50_seconds": latency.p50.total_seconds(),
        "p95_seconds": latency.p95.total_seconds(),
        "max_seconds": latency.max.total_seconds(),
    }


def _seconds_json(duration: timedelta | None) -> float | None:
    return duration.total_seconds() if duration is not None else None


def _content_json(value: Any) -> Any:
    # Outputs may be Pydantic models, dataclasses or other types json cannot
    # write; anything Pydantic cannot convert either is written as its repr.
    return to_jsonable_python(value, fallback=repr)


# Errored, failed and not-run attempts come before passed ones in the table.
_STATUS_ORDER = {
    CaseStatus.ERRORED: 0,
    CaseStatus.FAILED: 1,
    CaseStatus.NOT_RUN: 2,
    CaseStatus.PASSED: 3,
}


def summary(report: "EvalReport") -> str:
    lines = [
        f"Evaluation: {_size(report.results)}, {report.passed_count} passed, "
        f"{report.failed_count} failed, {report.errored_count} errored"
        + (f", {report.not_run_count} not run" if report.not_run_count else "")
        + f" (pass rate {percent(report.pass_rate)})",
        f"Tokens: {_usage_line(report.total_usage)}",
    ]
    if report.total_judge_usage is not None:
        lines.append(f"Judge tokens: {_usage_line(report.total_judge_usage)}")
    if report.max_total_tokens is not None:
        lines.append(f"Budget: {_budget_line(report)}")
    lines.append(f"Latency: {_latency_line(report.latency)}")
    if report.flaky_cases:
        lines.append(f"Flaky: {', '.join(report.flaky_cases)}")
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


def _size(results: tuple[CaseResult, ...]) -> str:
    cases = len({result.case_id for result in results})
    if len(results) == cases:
        return f"{cases} cases"
    return f"{cases} cases, {len(results)} attempts"


def _table(results: tuple[CaseResult, ...]) -> list[str]:
    ordered = sorted(results, key=lambda result: _STATUS_ORDER[result.status])
    repeated = any(result.attempt > 1 for result in results)
    rows = [("case", "status", "scores", "tokens", "duration")]
    rows.extend(
        (
            f"{result.case_id} #{result.attempt}" if repeated else result.case_id,
            str(result.status),
            _scores_cell(result),
            _tokens_cell(result),
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
        f"{score.scorer}={'pass' if score.passed else 'fail'}{_score_detail(score)}"
        for score in result.scores
    ]
    if result.error_type is not None:
        cells.append(f"error={result.error_type}")
    return " ".join(cells) or "-"


def _score_detail(score: ScoreResult) -> str:
    """The value and vote counts in brackets, such as ``(0.666667, 2/3 votes)``."""
    details = []
    if score.value is not None:
        details.append(f"{score.value:g}")
    if score.votes_cast is not None:
        details.append(f"{score.votes_passed}/{score.votes_cast} votes")
    return f"({', '.join(details)})" if details else ""


def _tokens_cell(result: CaseResult) -> str:
    usage = result.usage
    if result.status is CaseStatus.NOT_RUN:
        return "-"
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


def _budget_line(report: "EvalReport") -> str:
    line = f"{report.max_total_tokens} tokens"
    if report.over_budget:
        line += f", over budget ({known_tokens(report)} reported)"
    unknown = unknown_cases(report)
    if unknown:
        line += f", not fully enforced ({unknown} attempts with unknown usage)"
    return line


def _latency_line(latency: Latency | None) -> str:
    if latency is None:
        return "-"
    return (
        f"p50 {_seconds(latency.p50)}, p95 {_seconds(latency.p95)}, "
        f"max {_seconds(latency.max)}"
    )


def _rates_line(rates: dict[str, float]) -> str:
    return ", ".join(f"{name} {percent(rate)}" for name, rate in rates.items())


def _counts_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items())


def percent(rate: float) -> str:
    return f"{rate:.1%}"


def _seconds(duration: timedelta) -> str:
    return f"{duration.total_seconds():.2f}s"
