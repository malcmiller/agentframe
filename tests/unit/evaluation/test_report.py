"""Tests for ``EvalReport`` aggregates and its plain-text summary."""

from datetime import timedelta

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
