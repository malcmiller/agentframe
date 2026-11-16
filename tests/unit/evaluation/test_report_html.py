"""Tests for ``EvalReport.write_html``, the self-contained HTML report."""

import base64
import hashlib
import re
from datetime import timedelta
from pathlib import Path

import pytest

from agentframe import Usage
from agentframe.evaluation import CaseResult, CaseStatus, EvalReport, ScoreResult

SCRIPT = "<script>alert('pwned')</script>"
SECRET = "sk-secret-value"


def html(report: EvalReport, tmp_path: Path, baseline: Path | None = None) -> str:
    path = tmp_path / "eval-report.html"
    report.write_html(path, baseline=baseline)
    return path.read_text(encoding="utf-8")


def untrusted_report(*, capture_content: bool) -> EvalReport:
    return EvalReport(
        results=(
            CaseResult(
                case_id=f"case {SCRIPT}",
                status=CaseStatus.FAILED,
                scores=(
                    ScoreResult(
                        scorer="judge",
                        passed=False,
                        value=1 / 3,
                        reason=f"{SECRET} {SCRIPT}",
                        votes_cast=3,
                        votes_passed=1,
                    ),
                ),
                error_message=f"{SECRET} message",
                invocation_id="inv-1",
                usage=Usage(10, 5, 15, estimated=False),
                duration=timedelta(seconds=1.5),
                tags=('" onmouseover="alert(1)',),
                arguments={"text": f"{SECRET} {SCRIPT}"},
                expected=f"{SECRET} expected",
                output=f"{SECRET} {SCRIPT}",
            ),
        ),
        target={"type": "local_skill", "agent": SCRIPT, "skill": "answer"},
        scorers=("judge",),
        capture_content=capture_content,
    )


def test_untrusted_text_is_escaped_so_a_script_like_output_is_inert(
    tmp_path: Path,
) -> None:
    page = html(untrusted_report(capture_content=True), tmp_path)

    # The page's own script is the only one; every injected one is text.
    assert page.count("<script") == 1
    assert "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt;" in page
    assert 'onmouseover="alert' not in page
    assert "&quot; onmouseover=&quot;alert(1)" in page


def test_the_policy_allows_only_the_page_s_own_style_and_script(
    tmp_path: Path,
) -> None:
    page = html(untrusted_report(capture_content=True), tmp_path)

    style = page.split("<style>")[1].split("</style>")[0]
    script = page.split("<script>")[1].split("</script>")[0]
    assert "default-src 'none'" in page
    assert f"style-src '{sha256(style)}'" in page
    assert f"script-src '{sha256(script)}'" in page


def test_content_appears_only_when_it_was_captured(tmp_path: Path) -> None:
    captured = html(untrusted_report(capture_content=True), tmp_path)
    hidden = html(untrusted_report(capture_content=False), tmp_path)

    assert f"{SECRET} expected" in captured
    assert f"{SECRET} message" in captured
    assert SECRET not in hidden
    assert "Content was not captured" in hidden
    # Metadata is not content, so it is always shown.
    assert "inv-1" in hidden
    assert "1/3 passed" in hidden


def test_the_file_has_no_external_urls(tmp_path: Path) -> None:
    page = html(untrusted_report(capture_content=False), tmp_path)

    assert "://" not in page
    assert "url(" not in page
    assert "@import" not in page
    links = re.findall(r'(?:src|href)="([^"]*)"', page)
    assert links
    assert all(link.startswith("#attempt-") for link in links)


def test_a_baseline_shows_newly_failing_and_passing_cases_and_deltas(
    tmp_path: Path,
) -> None:
    baseline_path = tmp_path / "baseline.json"
    EvalReport(
        results=(
            result("regressed", CaseStatus.PASSED, seconds=1),
            result("fixed", CaseStatus.FAILED, seconds=1),
            result("steady", CaseStatus.PASSED, seconds=1),
        )
    ).write_json(baseline_path)
    report = EvalReport(
        results=(
            result("regressed", CaseStatus.ERRORED, seconds=None),
            result("fixed", CaseStatus.PASSED, seconds=2),
            result("steady", CaseStatus.PASSED, seconds=3),
        )
    )

    page = html(report, tmp_path, baseline=baseline_path)

    comparison = page.split("Compared with the baseline")[1].split("<h2>")[0]
    newly_failing = comparison.split("Newly failing")[1].split("Newly passing")[0]
    newly_passing = comparison.split("Newly passing")[1]
    assert "regressed" in newly_failing
    assert "fixed" not in newly_failing
    assert "fixed" in newly_passing
    assert "steady" not in comparison
    assert "+0.0 points" in comparison  # 66.7% before and after
    assert "<td>1.00s</td><td>3.00s</td><td>+2.00s</td>" in comparison


def test_a_baseline_that_is_not_a_report_is_refused(tmp_path: Path) -> None:
    not_json = tmp_path / "not.json"
    not_json.write_text("<html>", encoding="utf-8")
    too_deep = tmp_path / "deep.json"
    too_deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    other = tmp_path / "other.json"
    other.write_text('{"schema": "something/2"}', encoding="utf-8")
    report = EvalReport(results=(result("a", CaseStatus.PASSED, seconds=1),))

    with pytest.raises(ValueError, match="is not a JSON file"):
        report.write_html(tmp_path / "out.html", baseline=not_json)
    # Older Pythons fail to parse this; newer ones parse it but it is no report.
    with pytest.raises(ValueError, match=r"is not (a JSON file|an AgentFrame)"):
        report.write_html(tmp_path / "out.html", baseline=too_deep)
    with pytest.raises(ValueError, match="is not an AgentFrame evaluation report"):
        report.write_html(tmp_path / "out.html", baseline=other)


def result(case_id: str, status: CaseStatus, *, seconds: float | None) -> CaseResult:
    duration = timedelta(seconds=seconds) if seconds is not None else None
    return CaseResult(case_id=case_id, status=status, duration=duration)


def sha256(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")
