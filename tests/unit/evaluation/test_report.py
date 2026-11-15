"""Tests for ``EvalReport`` aggregates and its plain-text summary."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import agentframe
from agentframe import Agent, RunContext, Usage, agent, skill
from agentframe.evaluation import (
    Case,
    CaseResult,
    CaseStatus,
    EvalReport,
    Evaluation,
    Latency,
    LocalSkill,
    Score,
    ScoreResult,
    UsageTotals,
)

SECRET = "sk-secret-value"


def passed(
    case_id: str, *, usage: Usage | None = None, duration: timedelta | None = None
) -> CaseResult:
    return CaseResult(
        case_id=case_id, status=CaseStatus.PASSED, usage=usage, duration=duration
    )


def mixed_report() -> EvalReport:
    return EvalReport(
        results=(
            CaseResult(
                case_id="ok",
                status=CaseStatus.PASSED,
                scores=(ScoreResult(scorer="exact", passed=True),),
                usage=Usage(10, 5, 15, estimated=False),
                duration=timedelta(seconds=1),
                tags=("billing",),
            ),
            CaseResult(
                case_id="wrong",
                status=CaseStatus.FAILED,
                scores=(
                    ScoreResult(scorer="exact", passed=False),
                    ScoreResult(scorer="short", passed=True, value=0.5),
                ),
                usage=Usage(20, 10, 30, estimated=True),
                duration=timedelta(seconds=3),
                tags=("billing", "refunds"),
            ),
            CaseResult(
                case_id="broken",
                status=CaseStatus.ERRORED,
                error_type="TimeoutLimitExceededError",
                tags=("refunds",),
            ),
        )
    )


def test_counts_keep_errored_cases_apart_from_failed_ones() -> None:
    report = mixed_report()

    assert (report.passed_count, report.failed_count, report.errored_count) == (
        1,
        1,
        1,
    )
    assert report.pass_rate == 1 / 3
    assert report.errors_by_class == {"TimeoutLimitExceededError": 1}


def test_scorer_rates_count_only_cases_the_scorer_judged() -> None:
    report = mixed_report()

    assert report.scorer_pass_rates == {"exact": 0.5, "short": 1.0}
    assert report.tag_pass_rates == {"billing": 0.5, "refunds": 0.0}


def test_usage_is_unknown_when_any_case_has_no_usage() -> None:
    assert mixed_report().total_usage == UsageTotals(
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        known_total_tokens=45,
        unknown_cases=1,
        estimated=True,
    )


def test_usage_is_unknown_when_any_count_is_unknown() -> None:
    report = EvalReport(
        results=(
            passed("a", usage=Usage(10, 5, 15, estimated=False)),
            passed("b", usage=Usage(None, None, 7, estimated=False)),
        )
    )

    totals = report.total_usage

    assert (totals.input_tokens, totals.output_tokens) == (None, None)
    assert totals.total_tokens == 22
    assert totals.unknown_cases == 1


def test_usage_is_summed_when_every_case_reported_it() -> None:
    report = EvalReport(
        results=(
            passed("a", usage=Usage(10, 5, 15, estimated=False)),
            passed("b", usage=Usage(1, 2, 3, estimated=False)),
        )
    )

    assert report.total_usage == UsageTotals(
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        known_total_tokens=18,
        unknown_cases=0,
        estimated=False,
    )


def test_latency_uses_nearest_rank_percentiles_over_cases_with_a_duration() -> None:
    durations = [timedelta(seconds=second) for second in range(1, 21)]
    results = [passed(str(i), duration=d) for i, d in enumerate(durations)]
    results.append(CaseResult(case_id="raised", status=CaseStatus.ERRORED))

    latency = EvalReport(results=tuple(results)).latency

    assert latency == Latency(
        p50=timedelta(seconds=10),
        p95=timedelta(seconds=19),
        max=timedelta(seconds=20),
    )


def test_an_empty_report_has_no_rates_usage_or_latency() -> None:
    report = EvalReport(results=())

    assert report.pass_rate == 0.0
    assert report.latency is None
    assert report.total_usage.total_tokens == 0
    assert "0 cases" in str(report)


def test_the_summary_lists_totals_and_failing_cases_first() -> None:
    summary = str(mixed_report())
    lines = summary.splitlines()

    assert lines[0] == (
        "Evaluation: 3 cases, 1 passed, 1 failed, 1 errored (pass rate 33.3%)"
    )
    assert "Tokens: unknown; 45 reported, 1 cases with unknown usage" in lines
    assert "Latency: p50 1.00s, p95 3.00s, max 3.00s" in lines
    assert "Errors: TimeoutLimitExceededError 1" in lines
    assert "Scorers: exact 50.0%, short 100.0%" in lines
    assert "Tags: billing 50.0%, refunds 0.0%" in lines
    table = lines[lines.index("") + 1 :]
    assert table[0].split() == ["case", "status", "scores", "tokens", "duration"]
    assert [row.split()[0] for row in table[1:]] == ["broken", "wrong", "ok"]
    assert table[1].split() == [
        "broken",
        "errored",
        "error=TimeoutLimitExceededError",
        "unknown",
        "-",
    ]
    assert table[2].split() == [
        "wrong",
        "failed",
        "exact=fail",
        "short=pass(0.5)",
        "~30",
        "3.00s",
    ]


def repeated_report() -> EvalReport:
    tokens = Usage(10, 5, 15, estimated=False)
    return EvalReport(
        results=(
            CaseResult(case_id="a", attempt=1, status=CaseStatus.PASSED, usage=tokens),
            CaseResult(
                case_id="a",
                attempt=2,
                status=CaseStatus.ERRORED,
                error_type="ValueError",
                tags=("slow",),
            ),
            CaseResult(case_id="b", attempt=1, status=CaseStatus.PASSED, usage=tokens),
            CaseResult(
                case_id="b",
                attempt=2,
                status=CaseStatus.NOT_RUN,
                error_message="The evaluation's timeout expired before this attempt.",
                tags=("slow",),
            ),
        ),
        max_total_tokens=20,
    )


def test_not_run_attempts_are_counted_apart_from_every_other_aggregate() -> None:
    report = repeated_report()

    assert report.not_run_count == 1
    assert report.pass_rate == 2 / 3
    assert report.tag_pass_rates == {"slow": 0.0}
    assert report.total_usage.unknown_cases == 1
    assert report.flaky_cases == ["a"]


def test_reported_usage_over_the_budget_is_flagged() -> None:
    report = repeated_report()

    assert report.over_budget
    assert not EvalReport(results=report.results).over_budget


def judged_report(*, judge_known: bool = False) -> EvalReport:
    second_judge = (
        Usage(2, 1, 3, estimated=False)
        if judge_known
        else Usage(None, None, None, estimated=False)
    )
    results = (
        CaseResult(
            case_id="a",
            status=CaseStatus.PASSED,
            usage=Usage(5, 5, 10, estimated=False),
            judge_usage=Usage(5, 1, 6, estimated=False),
        ),
        CaseResult(
            case_id="b",
            status=CaseStatus.ERRORED,
            usage=Usage(5, 5, 10, estimated=False),
            judge_usage=second_judge,
        ),
        passed("c", usage=Usage(1, 1, 2, estimated=False)),
    )
    return EvalReport(results=results, max_total_tokens=25)


def test_judge_usage_is_totalled_apart_and_counts_against_the_budget() -> None:
    report = judged_report()

    assert report.total_usage.total_tokens == 22
    judge = report.total_judge_usage
    assert judge is not None
    assert (judge.total_tokens, judge.known_total_tokens) == (None, 6)
    assert judge.unknown_cases == 1
    assert report.over_budget
    assert "over budget (28 reported), not fully enforced (1 attempts" in str(report)
    assert EvalReport(results=report.results[2:]).total_judge_usage is None


def test_the_json_report_holds_judge_usage_with_unknown_as_null(
    tmp_path: Path,
) -> None:
    document = written(judged_report(), tmp_path)

    aggregates = document["aggregates"]
    assert aggregates["total_judge_usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "known_total_tokens": 6,
        "unknown_cases": 1,
        "estimated": False,
    }
    assert (aggregates["over_budget"], aggregates["budget_fully_enforced"]) == (
        True,
        False,
    )
    judged, unknown, unjudged = (r["judge_usage"] for r in document["results"])
    assert judged == {
        "input_tokens": 5,
        "output_tokens": 1,
        "total_tokens": 6,
        "estimated": False,
    }
    assert unknown == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "estimated": False,
    }
    assert unjudged is None
    assert written(mixed_report(), tmp_path)["aggregates"]["total_judge_usage"] is None


def test_require_counts_target_and_judge_tokens_together() -> None:
    report = judged_report(judge_known=True)  # 22 target + 9 judge tokens.

    report.require(max_errors=None, allow_over_budget=True, max_total_tokens=31)
    with pytest.raises(AssertionError) as raised:
        report.require(max_errors=None, max_total_tokens=30)

    assert str(raised.value).splitlines()[1:3] == [
        "- 31 reported tokens are over the budget of 25",
        "- 31 tokens used; at most 30 allowed",
    ]


def test_require_never_passes_a_token_threshold_on_unknown_judge_usage() -> None:
    with pytest.raises(AssertionError, match="unknown for 1 target or judge runs"):
        judged_report().require(
            max_errors=None, allow_over_budget=True, max_total_tokens=1_000_000
        )


def test_the_summary_shows_attempts_not_run_budget_and_flaky_cases() -> None:
    lines = str(repeated_report()).splitlines()

    assert lines[0] == (
        "Evaluation: 2 cases, 4 attempts, 2 passed, 0 failed, 1 errored, "
        "1 not run (pass rate 66.7%)"
    )
    assert lines[2] == (
        "Budget: 20 tokens, over budget (30 reported), "
        "not fully enforced (1 attempts with unknown usage)"
    )
    assert "Flaky: a" in lines
    table = lines[lines.index("") + 1 :]
    assert [row.split() for row in table[1:]] == [
        ["a", "#2", "errored", "error=ValueError", "unknown", "-"],
        ["b", "#2", "not_run", "-", "-", "-"],
        ["a", "#1", "passed", "-", "15", "-"],
        ["b", "#1", "passed", "-", "15", "-"],
    ]


@agent(version="1.0.0")
class Echo(Agent):
    """Echoes text."""

    @skill("echo", tags=["test"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Return the text."""
        return text


def never_matches(case: Case[str], output: str) -> Score:
    return Score(passed=False, reason=f"got {output}")


def secret_evaluation(*, capture_content: bool) -> Evaluation:
    return Evaluation(
        cases=[Case(id="leak", arguments={"text": SECRET}, expected=SECRET)],
        target=LocalSkill(Echo(), skill="echo"),
        scorers=[never_matches],
        capture_content=capture_content,
    )


async def test_the_summary_holds_no_case_content_by_default() -> None:
    report = await secret_evaluation(capture_content=False).run()

    summary = str(report)

    assert "leak" in summary
    assert SECRET not in summary


async def test_the_summary_shows_failure_reasons_when_content_is_captured() -> None:
    report = await secret_evaluation(capture_content=True).run()

    assert f"  leak never_matches: got {SECRET}" in str(report).splitlines()


def written(report: EvalReport, tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "eval-report.json"
    report.write_json(path)
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


async def test_the_json_report_is_versioned_and_holds_no_content_by_default(
    tmp_path: Path,
) -> None:
    report = await secret_evaluation(capture_content=False).run()

    document = written(report, tmp_path)

    assert SECRET not in (tmp_path / "eval-report.json").read_text(encoding="utf-8")
    assert document["schema"] == "agentframe.evaluation.report/1"
    assert document["agentframe_version"] == agentframe.__version__
    assert document["target"] == {
        "type": "local_skill",
        "agent": "Echo",
        "skill": "echo",
    }
    assert document["scorers"] == ["never_matches"]
    assert document["capture_content"] is False
    [result] = document["results"]
    assert result["status"] == "failed"
    assert result["scores"] == [
        {
            "scorer": "never_matches",
            "passed": False,
            "value": None,
            "votes_cast": None,
            "votes_passed": None,
        }
    ]
    assert result["finish_reason"] == "completed"
    assert not {"arguments", "text", "expected", "output"} & result.keys()


async def test_the_json_report_holds_content_when_captured(tmp_path: Path) -> None:
    report = await secret_evaluation(capture_content=True).run()

    [result] = written(report, tmp_path)["results"]

    assert result["arguments"] == {"text": SECRET}
    assert result["expected"] == SECRET
    assert result["output"] == SECRET
    assert result["scores"][0]["reason"] == f"got {SECRET}"


def test_the_json_report_keeps_only_not_run_reasons_by_default(
    tmp_path: Path,
) -> None:
    report = EvalReport(
        results=(
            CaseResult(
                case_id="a",
                status=CaseStatus.ERRORED,
                error_type="KeyError",
                error_message="Scorer 'exact' raised KeyError.",
            ),
            CaseResult(
                case_id="b",
                status=CaseStatus.NOT_RUN,
                error_message="The evaluation's timeout expired before this attempt.",
            ),
        )
    )

    errored, not_run = written(report, tmp_path)["results"]

    assert errored["error_type"] == "KeyError"
    assert errored["error_message"] is None
    assert not_run["error_message"] == (
        "The evaluation's timeout expired before this attempt."
    )


def test_the_json_report_holds_every_aggregate_with_unknown_usage_as_null(
    tmp_path: Path,
) -> None:
    document = written(repeated_report(), tmp_path)

    assert document["max_total_tokens"] == 20
    assert document["aggregates"] == {
        "attempt_count": 4,
        "passed_count": 2,
        "failed_count": 0,
        "errored_count": 1,
        "not_run_count": 1,
        "pass_rate": 2 / 3,
        "scorer_pass_rates": {},
        "tag_pass_rates": {"slow": 0.0},
        "flaky_cases": ["a"],
        "total_usage": {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "known_total_tokens": 30,
            "unknown_cases": 1,
            "estimated": False,
        },
        "total_judge_usage": None,
        "over_budget": True,
        "budget_fully_enforced": False,
        "latency": None,
        "errors_by_class": {"ValueError": 1},
    }
    first, second = document["results"][:2]
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert first["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "estimated": False,
    }
    assert second["usage"] is None


def test_the_json_report_writes_latency_and_durations_in_seconds(
    tmp_path: Path,
) -> None:
    document = written(mixed_report(), tmp_path)

    assert document["aggregates"]["latency"] == {
        "p50_seconds": 1.0,
        "p95_seconds": 3.0,
        "max_seconds": 3.0,
    }
    assert document["aggregates"]["budget_fully_enforced"] is None
    assert [result["duration_seconds"] for result in document["results"]] == [
        1.0,
        3.0,
        None,
    ]


def two_of_three_passed() -> EvalReport:
    tokens = Usage(10, 5, 15, estimated=False)
    return EvalReport(
        results=(
            CaseResult(case_id="a", status=CaseStatus.PASSED, usage=tokens),
            CaseResult(case_id="b", status=CaseStatus.PASSED, usage=tokens),
            CaseResult(case_id="c", status=CaseStatus.FAILED, usage=tokens),
        )
    )


def test_require_passes_at_the_thresholds() -> None:
    two_of_three_passed().require(min_pass_rate=2 / 3, max_total_tokens=45)


def test_require_lists_every_missed_threshold_then_the_summary() -> None:
    report = two_of_three_passed()

    with pytest.raises(AssertionError) as raised:
        report.require(min_pass_rate=0.9, max_total_tokens=44)

    assert str(raised.value) == (
        "Evaluation requirements not met:\n"
        "- pass rate 66.7% is below 90.0%\n"
        "- 45 tokens used; at most 44 allowed\n"
        f"\n{report}"
    )


def test_require_by_default_fails_errored_not_run_and_over_budget_attempts() -> None:
    report = repeated_report()

    with pytest.raises(AssertionError) as raised:
        report.require()

    assert str(raised.value).splitlines()[1:4] == [
        "- 1 attempts errored; at most 0 allowed",
        "- 1 attempts did not run",
        "- 30 reported tokens are over the budget of 20",
    ]
    report.require(max_errors=None, allow_not_run=True, allow_over_budget=True)
    report.require(max_errors=1, allow_not_run=True, allow_over_budget=True)


def test_require_never_passes_a_token_threshold_on_unknown_usage() -> None:
    with pytest.raises(AssertionError, match="token usage is unknown for 1 target"):
        mixed_report().require(max_errors=None, max_total_tokens=1_000_000)


def test_require_fails_a_pass_rate_threshold_when_nothing_ran() -> None:
    report = EvalReport(
        results=(CaseResult(case_id="a", status=CaseStatus.NOT_RUN),),
    )

    with pytest.raises(AssertionError, match="no attempt ran"):
        report.require(min_pass_rate=0.0, allow_not_run=True)


@pytest.mark.parametrize(
    "thresholds",
    [{"min_pass_rate": 1.5}, {"max_errors": -1}, {"max_total_tokens": 0}],
)
def test_require_refuses_thresholds_out_of_range(thresholds: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must be"):
        two_of_three_passed().require(**thresholds)
