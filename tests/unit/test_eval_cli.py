"""``agentframe eval`` run as a real command, as CI would run it."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SUPPORT_DIRECTORY = Path(__file__).parent.parent / "support"
COMMAND = Path(sys.executable).parent / "agentframe"


def agentframe_eval(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [COMMAND, "eval", *arguments],
        cwd=SUPPORT_DIRECTORY,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_passing_evaluation_prints_its_summary_and_exits_with_0(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "eval-report.json"

    result = agentframe_eval(
        "eval_targets:create_passing_evaluation",
        "--json",
        str(report_path),
        "--min-pass-rate",
        "1.0",
    )

    assert result.returncode == 0, result.stderr
    assert "3 passed" in result.stdout
    assert result.stderr == ""
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["schema"] == "agentframe.evaluation.report/1"
    assert document["capture_content"] is False


def test_a_missed_threshold_lists_the_miss_writes_the_report_and_exits_with_1(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "eval-report.json"

    result = agentframe_eval(
        "eval_targets:create_failing_evaluation",
        "--json",
        str(report_path),
        "--min-pass-rate",
        "0.9",
    )

    assert result.returncode == 1
    assert "pass rate 66.7% is below 90.0%" in result.stdout
    assert "2 passed, 1 failed" in result.stdout
    assert result.stderr == "agentframe: error: The evaluation missed a threshold.\n"
    assert report_path.exists()
    assert "classified" not in result.stdout + report_path.read_text()


def test_thresholds_are_passed_straight_to_require() -> None:
    failing = "eval_targets:create_failing_evaluation"
    passing = "eval_targets:create_passing_evaluation"

    # Failed cases are not errors, so only a pass-rate threshold catches them.
    assert agentframe_eval(failing).returncode == 0
    assert agentframe_eval(failing, "--min-pass-rate", "0.6").returncode == 0
    assert agentframe_eval(passing, "--max-total-tokens", "100").returncode == 0


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("eval_targets", "not an import path"),
        ("no_such_module:create_evaluation", "Cannot import module 'no_such_module'"),
        ("eval_targets:missing", "has no attribute 'missing'"),
        ("eval_targets:WordCounter", "returned WordCounter, not an Evaluation"),
        ("eval_targets:create_nothing", "returned NoneType, not an Evaluation"),
        ("eval_targets:create_invalid_evaluation", "needs at least one case"),
        ("eval_targets:create_capturing_evaluation", "pass --capture-content"),
    ],
)
def test_a_bad_target_prints_one_line_and_exits_with_2(
    target: str, message: str
) -> None:
    result = agentframe_eval(target)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("agentframe: error: ")
    assert result.stderr.count("\n") == 1
    assert message in result.stderr


def test_an_unwritable_report_path_exits_with_2(tmp_path: Path) -> None:
    missing_directory = tmp_path / "missing" / "eval-report.json"

    result = agentframe_eval(
        "eval_targets:create_passing_evaluation", "--json", str(missing_directory)
    )

    assert result.returncode == 2
    assert result.stderr.startswith("agentframe: error: Cannot write the JSON report")


@pytest.mark.parametrize(
    "arguments",
    [
        ["--min-pass-rate", "1.5"],
        ["--min-pass-rate", "high"],
        ["--max-errors", "-1"],
        ["--max-total-tokens", "0"],
    ],
)
def test_invalid_thresholds_are_usage_errors(arguments: list[str]) -> None:
    result = agentframe_eval("eval_targets:create_passing_evaluation", *arguments)

    assert result.returncode == 2
    assert "agentframe eval: error: argument" in result.stderr


def test_content_is_printed_only_with_capture_content(tmp_path: Path) -> None:
    report_path = tmp_path / "eval-report.json"

    result = agentframe_eval(
        "eval_targets:create_capturing_evaluation",
        "--capture-content",
        "--json",
        str(report_path),
    )

    assert result.returncode == 0, result.stderr
    assert "classified" in report_path.read_text(encoding="utf-8")
