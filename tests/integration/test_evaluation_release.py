"""M9 release acceptance: evaluate ``examples/assistant.py`` and gate a build.

The rest of the acceptance (#164) is proven elsewhere:

- a non-AgentFrame A2A agent is evaluated over A2A in
  ``test_evaluation_interop.py`` (CI) and a real Foundry agent in
  ``test_foundry_agents.py`` (opt-in);
- the example's answers are graded by a rubric judge on real Ollama and
  Foundry models in ``test_providers.py`` (opt-in);
- ``examples/smoke.py`` runs the example against the installed wheel.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentframe import FinishReason, ModelCapabilities, ModelResponse, Usage
from agentframe.testing import ScriptedModel
from examples.evaluation import create_answer_evaluation, create_evaluation

REPOSITORY = Path(__file__).parent.parent.parent
COMMAND = Path(sys.executable).parent / "agentframe"
STRUCTURED = ModelCapabilities(context_window=8192, structured_output=True)
CONTENT_KEYS = {"arguments", "text", "expected", "output"}


def reply(text: str, total_tokens: int) -> ModelResponse:
    usage = Usage(total_tokens - 5, 5, total_tokens, estimated=False)
    return ModelResponse(text=text, usage=usage, finish_reason=FinishReason.COMPLETED)


def verdict(passed: bool, total_tokens: int = 120) -> ModelResponse:
    return reply(json.dumps({"passed": passed, "reason": "Checked."}), total_tokens)


async def test_a_model_free_evaluation_writes_a_report_without_content(
    tmp_path: Path,
) -> None:
    report = await create_evaluation().run()
    report.write_json(tmp_path / "eval-report.json")
    report.require(min_pass_rate=1.0)

    document = json.loads((tmp_path / "eval-report.json").read_text())
    assert document["schema"] == "agentframe.evaluation.report/1"
    assert document["target"] == {
        "type": "local_skill",
        "agent": "Assistant",
        "skill": "count_words",
    }
    assert document["scorers"] == ["equals"]
    assert document["capture_content"] is False
    assert document["max_total_tokens"] is None
    aggregates = document["aggregates"]
    assert (aggregates["attempt_count"], aggregates["passed_count"]) == (3, 3)
    assert aggregates["pass_rate"] == 1.0
    for result in document["results"]:
        assert CONTENT_KEYS.isdisjoint(result)
        assert all("reason" not in score for score in result["scores"])


async def test_scripted_answers_report_judge_tokens_apart_from_the_target(
    tmp_path: Path,
) -> None:
    assistant = ScriptedModel([reply("Paris.", 40), reply("Tokyo.", 42)])
    # Two agreeing votes settle each case, so votes=3 makes two judge runs.
    judge = ScriptedModel([verdict(True)] * 4, capabilities=STRUCTURED)

    report = await create_answer_evaluation(assistant, judge, votes=3).run()
    report.write_json(tmp_path / "eval-report.json")
    report.require(min_pass_rate=1.0)

    assert len(judge.requests) == 4
    assert str(report).startswith(
        "Evaluation: 2 cases, 2 passed, 0 failed, 0 errored (pass rate 100.0%)\n"
        "Tokens: 82 total\n"
        "Judge tokens: 480 total\n"
        "Budget: 20000 tokens\n"
    )
    document = json.loads((tmp_path / "eval-report.json").read_text())
    assert document["aggregates"]["total_usage"]["total_tokens"] == 82
    assert document["aggregates"]["total_judge_usage"]["total_tokens"] == 480
    judge_tokens = [
        result["judge_usage"]["total_tokens"] for result in document["results"]
    ]
    assert judge_tokens == [240, 240]
    assert document["scorers"] == ["contains", "judge"]


async def test_a_split_vote_shows_as_the_judge_score_value() -> None:
    assistant = ScriptedModel(["Paris.", "Tokyo."])
    # Calls arrive in case order: france gets pass, fail, pass; japan two passes.
    judge = ScriptedModel(
        [verdict(True), verdict(False), verdict(True), verdict(True), verdict(True)],
        capabilities=STRUCTURED,
    )

    report = await create_answer_evaluation(assistant, judge, votes=3).run()

    france, japan = report.results
    assert france.scores[1].value == pytest.approx(2 / 3)
    assert japan.scores[1].value == 1.0
    assert "contains=pass judge=pass(0.666667)" in str(report)


async def test_a_missed_threshold_fails_the_build_after_the_report_is_kept(
    tmp_path: Path,
) -> None:
    assistant = ScriptedModel(["Lyon.", "Tokyo."])
    judge = ScriptedModel([verdict(False), verdict(True)], capabilities=STRUCTURED)

    report = await create_answer_evaluation(assistant, judge).run()
    report.write_json(tmp_path / "eval-report.json")

    with pytest.raises(AssertionError, match=r"pass rate 50\.0% is below 100\.0%"):
        report.require(min_pass_rate=1.0)
    assert (tmp_path / "eval-report.json").exists()


def test_agentframe_eval_gates_ci_on_the_example_without_pytest(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "eval-report.json"

    result = subprocess.run(
        [
            COMMAND,
            "eval",
            "examples.evaluation:create_evaluation",
            "--json",
            str(report_path),
            "--min-pass-rate",
            "1.0",
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "3 passed" in result.stdout
    document = json.loads(report_path.read_text())
    assert document["schema"] == "agentframe.evaluation.report/1"
