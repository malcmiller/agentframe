"""M10 release acceptance: model-planned orchestration across separate processes.

Every agent runs as its own process on localhost, as in the M8+ release test
(``test_multi_agent_release.py``):

- the research agent of ``examples/multi_agent``, whose briefs report 300
  tokens (``tests/support/release_targets.py``), and the example's writer;
- the plain A2A peer (``tests/support/a2a_peer.py``), built on the official
  SDK only, with no AgentFrame code or extensions;
- the example's planner, whose model-made plans run over the research and
  writer agents only, with its plan check, tool check and permissions;
- a coordinator whose catalog is every registered agent, including the peer,
  one agent that is down and one it holds no key for.

Both orchestrators plan with ``GoalModel``, a stand-in model with a fixed plan
per goal, so CI needs no provider. Real planners are opt-in tests in
``test_providers.py``. The official A2A client calls the orchestrators, as any
A2A caller would. Identity across agents is service mode: each orchestrator
calls with its own development key.
"""

import json
import logging
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from a2a.helpers import get_artifact_text, get_data_parts
from a2a.types import Task, TaskState

from a2a_client import call_skill, connect
from agent_processes import (
    CARD_PATH,
    AgentProcess,
    agent_process,
    free_port,
    peer_process,
    wait_until_ready,
)
from agentframe import (
    AgentDirectory,
    APIKeyCredential,
    FileAgentRegistry,
    FinishReason,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Plan,
    Runtime,
    Service,
    Usage,
)
from agentframe.evaluation import CaseStatus
from agentframe.testing import ScriptedModel
from examples.multi_agent.planner import PlanOutcome
from examples.multi_agent.planner_evaluation import GOALS, create_planner_evaluation
from release_targets import (
    BRIEF_TOKENS,
    PEER_GOAL,
    PLANNING_TOKENS,
    POST_GOAL,
    PRIVATE_GOAL,
    PRIVATE_URL,
    TOOLS_GOAL,
    TWO_BRIEFS_GOAL,
)

CLIENT_KEY = "client-key"
PLANNER_KEY = "planner-key"
COORDINATOR_KEY = "coordinator-key"
DEADLINE_MS = 30_000
MAX_TOTAL_TOKENS = 5_000
STRUCTURED = ModelCapabilities(context_window=8192, structured_output=True)


@dataclass
class Agents:
    research: AgentProcess
    writer: AgentProcess
    peer: AgentProcess
    planner: AgentProcess
    coordinator: AgentProcess
    planner_records: Path
    coordinator_records: Path


@pytest.fixture(scope="module")
def agents(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Agents]:
    """Start the specialists, the peer and both orchestrators, each in a process."""
    logs = tmp_path_factory.mktemp("logs")
    specialist_keys = f"planner={PLANNER_KEY},coordinator={COORDINATOR_KEY}"
    with (
        agent_process(
            "release_targets:create_research_app", logs, DEV_API_KEYS=specialist_keys
        ) as research,
        agent_process(
            "examples.multi_agent.writer:create_app",
            logs,
            DEV_API_KEYS=specialist_keys,
        ) as writer,
        peer_process(logs) as peer,
    ):
        wait_until_ready(research)
        wait_until_ready(writer)
        wait_until_ready(peer, CARD_PATH)
        specialists = (
            f"research-agent={research.card_url},writer-agent={writer.card_url}"
        )
        planner_records = logs / "planner-records.jsonl"
        coordinator_records = logs / "coordinator-records.jsonl"
        stopped_card = f"http://127.0.0.1:{free_port()}{CARD_PATH}"
        with (
            agent_process(
                "release_targets:create_planner_app",
                logs,
                AGENTS=specialists,
                DEV_API_KEYS=f"developer={CLIENT_KEY}",
                DEV_REMOTE_KEYS=(
                    f"research-agent={PLANNER_KEY},writer-agent={PLANNER_KEY}"
                ),
                TEST_PLAN_LOG=str(planner_records),
            ) as planner,
            agent_process(
                "release_targets:create_coordinator_app",
                logs,
                # The registry is the allowlist: every agent here is considered.
                AGENTS=(
                    f"{specialists},peer={peer.card_url},"
                    f"stopped-agent={stopped_card},locked-agent={research.card_url}"
                ),
                DEV_API_KEYS=f"developer={CLIENT_KEY}",
                DEV_REMOTE_KEYS=(
                    f"research-agent={COORDINATOR_KEY},writer-agent={COORDINATOR_KEY}"
                ),
                TEST_PLAN_LOG=str(coordinator_records),
            ) as coordinator,
        ):
            wait_until_ready(planner)
            wait_until_ready(coordinator)
            yield Agents(
                research,
                writer,
                peer,
                planner,
                coordinator,
                planner_records,
                coordinator_records,
            )


async def call(
    orchestrator: AgentProcess, skill: str, goal: str, **metadata: object
) -> Task:
    """Call an orchestrator's skill with a goal, as the ``developer`` caller."""
    client = await connect(orchestrator.url, CLIENT_KEY)
    try:
        return await call_skill(client, skill, {"goal": goal}, metadata=metadata)
    finally:
        await client.close()


def value(task: Task) -> Any:
    """Return a completed task's result: its text, or its data part's value."""
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, task.status
    data = get_data_parts(task.artifacts[0].parts)
    return data[0]["value"] if data else get_artifact_text(task.artifacts[0])


def records(path: Path, root: str) -> list[dict[str, Any]]:
    """Return the plan log records of one request, by its root ID.

    Each record gets an ``event``: ``"Plan created"``, ``"Plan step started"``
    or ``"Plan step completed"``, the start of its message.
    """
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    events = ("Plan created", "Plan step started", "Plan step completed")
    return [
        line | {"event": event}
        for line in lines
        for event in events
        if line["message"].startswith(event) and line["root_invocation_id"] == root
    ]


def step_records(path: Path, root: str, event: str) -> dict[str, dict[str, Any]]:
    """Return one request's records of ``event``, by step ID."""
    return {
        record["plan_step"]: record
        for record in records(path, root)
        if record["event"] == event
    }


# A model-made plan: parallel steps, data references, usage and correlation


async def test_a_planned_post_researches_in_parallel_then_writes_from_both(
    agents: Agents,
) -> None:
    task = await call(
        agents.planner,
        "planned",
        POST_GOAL,
        **{
            "agentframe.deadline_ms": DEADLINE_MS,
            "agentframe.max_total_tokens": MAX_TOTAL_TOKENS,
        },
    )

    outcome = PlanOutcome.model_validate(value(task))
    assert outcome.complete
    assert outcome.steps == dict.fromkeys(["tides", "brief", "post"], "completed")
    # The writer got the sources and the brief through {"$ref": ...}; the
    # brief says who asked and how long the caller's deadline had left.
    assert isinstance(outcome.value, str)
    post = outcome.value
    assert post.startswith("# Tides\n\nBrief: ")
    assert "- [Tide tables explained](https://example.org/tides)" in post
    match = re.search(r"\(for (\S+), (\d+) s left\)", post)
    assert match is not None, post
    assert match.group(1) == "planner"
    assert 0 < int(match.group(2)) <= DEADLINE_MS // 1000


async def test_independent_steps_start_together_and_the_dependent_one_after(
    agents: Agents,
) -> None:
    task = await call(agents.planner, "planned", POST_GOAL)

    value(task)
    lines = records(agents.planner_records, task.metadata["invocation_id"])
    events = [(line["event"], line["plan_step"]) for line in lines]
    assert events[0] == ("Plan created", None)
    # Both research steps start before either completes; the post waits.
    assert set(events[1:3]) == {
        ("Plan step started", "tides"),
        ("Plan step started", "brief"),
    }
    assert events.index(("Plan step started", "post")) > max(
        events.index(("Plan step completed", "tides")),
        events.index(("Plan step completed", "brief")),
    )


async def test_usage_is_charged_once_and_reported_per_step_within_limits(
    agents: Agents,
) -> None:
    task = await call(
        agents.planner,
        "planned",
        POST_GOAL,
        **{"agentframe.max_total_tokens": MAX_TOTAL_TOKENS},
    )

    value(task)
    usage = task.metadata["usage"]
    # One planning call plus one brief; sources and the post use no model.
    assert usage["total_tokens"] == PLANNING_TOKENS + BRIEF_TOKENS
    assert usage["total_tokens"] <= MAX_TOTAL_TOKENS
    assert usage["estimated"] is False
    assert (usage["model_calls"], usage["tool_calls"]) == (1, 3)
    root = task.metadata["invocation_id"]
    [created] = [
        r for r in records(agents.planner_records, root) if r["event"] == "Plan created"
    ]
    assert created["uses"] == [
        "research-agent.find_sources",
        "research-agent.brief",
        "writer-agent.write_post",
    ]
    completed = step_records(agents.planner_records, root, "Plan step completed")
    tokens = {step: record["total_tokens"] for step, record in completed.items()}
    assert tokens == {"tides": 0, "brief": BRIEF_TOKENS, "post": 0}
    for record in completed.values():
        assert record["status"] == "completed"
        assert record["remote_task_id"]
        assert record["duration_ms"] >= 0


async def test_every_agent_logs_the_same_root_invocation_id(agents: Agents) -> None:
    task = await call(agents.planner, "planned", POST_GOAL)

    value(task)
    root = task.metadata["invocation_id"]
    assert (root, root) in agents.planner.started_runs()
    # Two research steps and one writer step, each its own run.
    assert len(agents.research.runs_in(root)) == 2
    assert len(agents.writer.runs_in(root)) == 1
    assert {r["invocation_id"] for r in records(agents.planner_records, root)} == {root}


# Safety: plan checks, tool checks, permissions and depth


async def test_a_plan_check_refuses_a_plan_before_anything_is_sent(
    agents: Agents,
) -> None:
    task = await call(agents.planner, "planned", TWO_BRIEFS_GOAL)

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    root = task.metadata["invocation_id"]
    assert agents.research.runs_in(root) == []
    assert step_records(agents.planner_records, root, "Plan step started") == {}


async def test_a_tool_check_stops_a_private_source_reaching_the_writer(
    agents: Agents,
) -> None:
    task = await call(agents.planner, "planned", PRIVATE_GOAL)

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    root = task.metadata["invocation_id"]
    # The step before it ran; the writer never received anything.
    assert len(agents.research.runs_in(root)) == 1
    assert agents.writer.runs_in(root) == []
    completed = step_records(agents.planner_records, root, "Plan step completed")
    assert completed["post"]["error_type"] == "PermissionDeniedError"
    assert PRIVATE_URL not in agents.planner.log.read_text()
    assert PRIVATE_URL not in agents.planner_records.read_text()


async def test_a_caller_without_plans_run_cannot_run_a_plan(
    agents: Agents, tmp_path: Path
) -> None:
    # The same key, verified as a caller other than ``developer``, holds no
    # role, so no plans:run.
    with agent_process(
        "release_targets:create_planner_app",
        tmp_path,
        AGENTS=f"research-agent={agents.research.card_url}",
        DEV_API_KEYS=f"someone={CLIENT_KEY}",
        DEV_REMOTE_KEYS=f"research-agent={PLANNER_KEY}",
    ) as planner:
        wait_until_ready(planner)
        task = await call(planner, "planned", POST_GOAL)

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert agents.research.runs_in(task.metadata["invocation_id"]) == []


async def test_a_plan_at_the_hop_limit_sends_nothing(agents: Agents) -> None:
    task = await call(agents.planner, "planned", POST_GOAL, **{"agentframe.hops": 5})

    assert task.status.state == TaskState.TASK_STATE_FAILED
    root = task.metadata["invocation_id"]
    assert agents.research.runs_in(root) == []
    assert agents.writer.runs_in(root) == []
    assert "Maximum remote call depth of 5 exceeded" in agents.planner.log.read_text()


# The whole registry: skipped agents, a non-AgentFrame peer, tools and partial plans


async def test_remote_skills_are_model_tools_including_a_plain_peer(
    agents: Agents,
) -> None:
    task = await call(agents.coordinator, "answer", TOOLS_GOAL)

    answer = value(task)
    assert "Tide tables explained" in answer
    assert answer.endswith("| Echo: hello")
    usage = task.metadata["usage"]
    assert (usage["model_calls"], usage["tool_calls"]) == (2, 2)
    # The peer reports no usage, so the run's usage is unknown, never zero.
    assert usage["total_tokens"] is None
    root = task.metadata["invocation_id"]
    assert len(agents.research.runs_in(root)) == 1


async def test_unreachable_and_unsatisfiable_agents_are_skipped_with_a_warning(
    agents: Agents,
) -> None:
    task = await call(agents.coordinator, "answer", TOOLS_GOAL)

    value(task)
    log = agents.coordinator.log.read_text()
    root = task.metadata["invocation_id"]
    assert (
        f"Skill catalog skipped agent 'stopped-agent': card unavailable "
        f"(invocation_id={root})"
    ) in log
    assert (
        f"Skill catalog skipped agent 'locked-agent': security not satisfied "
        f"(invocation_id={root})"
    ) in log


async def test_a_failed_peer_step_leaves_a_partial_result(agents: Agents) -> None:
    task = await call(agents.coordinator, "run_goal", PEER_GOAL)

    result = value(task)
    assert result["steps"] == {
        "tides": "completed",
        "tell": "completed",
        "fail": "failed",
        "post": "skipped",
    }
    assert result["complete"] is False
    # The result step completed: the peer echoed the research step's value.
    assert result["value"].startswith('Echo: Sources: [{"title":"Tide tables')
    # The peer reports no usage, so the plan's usage is unknown.
    assert result["usage"] is None
    root = task.metadata["invocation_id"]
    assert agents.writer.runs_in(root) == []
    completed = step_records(agents.coordinator_records, root, "Plan step completed")
    assert completed["fail"]["error_type"] == "RemoteAgentError"
    assert completed["post"]["status"] == "skipped"
    assert "secret peer detail" not in agents.coordinator.log.read_text()


# Measuring the planner with an M9 evaluation


def as_reply(plan: Plan) -> ModelResponse:
    """A planning model's reply holding ``plan``, reporting 100 tokens used."""
    steps = [
        {
            "id": step.id,
            "use": step.use,
            "arguments": json.dumps(step.arguments),
            "after": step.after,
        }
        for step in plan.steps
    ]
    text = json.dumps({"steps": steps, "result": plan.result, "reason": None})
    usage = Usage(input_tokens=90, output_tokens=10, total_tokens=100, estimated=False)
    return ModelResponse(text=text, usage=usage, finish_reason=FinishReason.COMPLETED)


class GoalPlanner(ScriptedModel):
    """Answers each goal with the plan ``plans`` holds for it."""

    def __init__(self, plans: dict[str, Plan]) -> None:
        super().__init__(capabilities=STRUCTURED)
        self.plans = plans

    async def generate(self, request: ModelRequest) -> ModelResponse:
        goal = json.loads(request.messages[-1].content)["goal"]
        return as_reply(self.plans[goal])


@pytest.fixture
async def runtime(agents: Agents, tmp_path: Path) -> AsyncIterator[Runtime]:
    """A runtime that reaches the research and writer agents with the planner's key."""
    registry = tmp_path / "agents.yaml"
    registry.write_text(
        yaml.safe_dump(
            [
                {"name": "research-agent", "url": agents.research.card_url},
                {"name": "writer-agent", "url": agents.writer.card_url},
            ]
        )
    )
    key = Service(credential=APIKeyCredential(PLANNER_KEY))
    async with AgentDirectory(sources=[FileAgentRegistry(registry)]) as directory:
        yield Runtime(
            directory=directory, remotes={"research-agent": key, "writer-agent": key}
        )


async def test_a_planner_evaluation_scores_plan_structure_without_content(
    runtime: Runtime, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    plans: dict[str, Plan] = {}
    goals: dict[str, str] = {}
    for case in GOALS:
        assert case.arguments is not None and case.expected is not None
        goals[case.id] = case.arguments["goal"]
        plans[goals[case.id]] = case.expected
    # One wrong plan: a post from sources only, without the brief it asked for.
    plans[goals["post_with_brief"]] = plans[goals["post_from_sources"]]
    caplog.set_level(logging.INFO, logger="agentframe")

    report = await create_planner_evaluation(GoalPlanner(plans), runtime).run()
    report.write_json(tmp_path / "planner-report.json")

    report.require(min_pass_rate=0.8)
    with pytest.raises(AssertionError, match=r"pass rate 83\.3% is below 100\.0%"):
        report.require(min_pass_rate=1.0)
    [failed] = [r for r in report.results if r.status != CaseStatus.PASSED]
    assert (failed.case_id, failed.status) == ("post_with_brief", CaseStatus.FAILED)
    assert [score.passed for score in failed.scores] == [False, False]
    document = json.loads((tmp_path / "planner-report.json").read_text())
    assert document["scorers"] == ["same_entries", "same_dependencies"]
    assert document["aggregates"]["total_usage"]["total_tokens"] == 100 * len(GOALS)
    # Neither the report nor the logs hold a goal, a plan or an argument.
    for content in ["tides", "Weather", "Tide tables"]:
        assert content not in json.dumps(document)
        assert content not in caplog.text
