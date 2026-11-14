"""``A2ATarget`` evaluates an A2A agent that is not built with AgentFrame (#159).

The peer is a bare official-SDK server (``a2a_peer``) with none of AgentFrame's
extensions, standing in for another framework's agent. It can only be reached
with free text, it reports no usage, and it may ask a question back.
``test_foundry_agents.py`` evaluates a real Foundry agent the same way, opt-in.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from a2a_peer import PEER_QUESTION, PEER_SECRET, PeerExecutor, a2a_peer
from agentframe import AgentDirectory, FileAgentRegistry, RunLimits, RunOptions
from agentframe.evaluation import A2ATarget, Case, CaseStatus, Evaluation, contains
from local_server import ServeLocally


@pytest.fixture
def peer() -> PeerExecutor:
    return PeerExecutor()


@pytest.fixture
async def directory(
    serve: ServeLocally, peer: PeerExecutor, tmp_path: Path
) -> AsyncIterator[AgentDirectory]:
    async with serve(a2a_peer(peer)) as url:
        agents = tmp_path / "agents.yaml"
        agents.write_text(
            f"- name: plain-peer\n  url: {url}/.well-known/agent-card.json\n"
        )
        async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as found:
            yield found


async def test_free_text_cases_pass_and_report_unknown_usage_not_zero(
    directory: AgentDirectory, tmp_path: Path
) -> None:
    evaluation = Evaluation(
        cases=[Case(id="tides", text="tides", expected="Echo: tides")],
        target=A2ATarget(directory, "plain-peer"),
        scorers=[contains("Echo")],
    )

    report = await evaluation.run()
    report.write_json(tmp_path / "report.json")

    [result] = report.results
    assert result.status is CaseStatus.PASSED
    assert result.usage is not None and result.usage.total_tokens is None
    assert report.total_usage.total_tokens is None
    assert report.total_usage.unknown_cases == 1
    document = json.loads((tmp_path / "report.json").read_text())
    assert document["aggregates"]["total_usage"]["total_tokens"] is None
    assert document["results"][0]["usage"]["total_tokens"] is None
    assert "unknown" in str(report)


async def test_a_skill_target_is_refused_before_anything_is_sent(
    directory: AgentDirectory, peer: PeerExecutor
) -> None:
    # The card lists ``echo`` but not AgentFrame's skill-call extension.
    evaluation = Evaluation(
        cases=[Case(id="echo", arguments={"text": "tides"})],
        target=A2ATarget(directory, "plain-peer", skill="echo"),
        scorers=[contains("Echo")],
    )

    [result] = (await evaluation.run()).results

    assert result.status is CaseStatus.ERRORED
    assert result.error_type == "RemoteAgentError"
    assert peer.requests == []


async def test_a_question_back_errors_the_case_without_an_answer(
    directory: AgentDirectory, peer: PeerExecutor, tmp_path: Path
) -> None:
    evaluation = Evaluation(
        cases=[Case(id="ocean", text="question"), Case(id="fail", text="fail")],
        target=A2ATarget(directory, "plain-peer"),
        scorers=[contains("Echo")],
    )

    report = await evaluation.run()
    report.write_json(tmp_path / "report.json")

    asked, failed = report.results
    assert asked.status is CaseStatus.ERRORED
    assert asked.error_type == "ClarificationNeededError"
    assert failed.status is CaseStatus.ERRORED
    assert failed.error_type == "RemoteAgentError"
    # The evaluation never answers back: each case sent exactly one message.
    assert len(peer.requests) == 2
    written = (tmp_path / "report.json").read_text()
    for text in (written, str(report)):
        assert PEER_QUESTION not in text
        assert PEER_SECRET not in text


async def test_unknown_usage_keeps_the_whole_hold_so_later_cases_do_not_run(
    directory: AgentDirectory,
) -> None:
    options = RunOptions(limits=RunLimits(max_total_tokens=1000))
    evaluation = Evaluation(
        cases=[Case(id=name, text=name) for name in ("a", "b", "c")],
        target=A2ATarget(directory, "plain-peer", options=options),
        scorers=[contains("Echo")],
        concurrency=1,
        max_total_tokens=2000,
    )

    report = await evaluation.run()

    statuses = [result.status for result in report.results]
    assert statuses == [CaseStatus.PASSED, CaseStatus.PASSED, CaseStatus.NOT_RUN]
    assert "not fully enforced (2 attempts with unknown usage)" in str(report)
