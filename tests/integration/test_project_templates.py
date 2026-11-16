"""Projects from ``agentframe new`` pass their own checks, and the fleet runs.

Each template is generated into a temporary folder, with short and with the
longest allowed names, then checked with the project's own ruff, mypy and
pytest settings. A fleet with a planner is also served in this process, as
``agentframe dev`` would serve it, and plans over its specialists by tag.
"""

import importlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from a2a.helpers import get_data_parts
from a2a.types import TaskState

from a2a_client import call_skill, connect
from agentframe import AgentApp, ModelCapabilities
from agentframe.new_project import (
    MAX_NAME_LENGTH,
    agent_project,
    fleet_project,
    write_project,
)
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

# One 30-letter word: the longest package, module and class names allowed.
LONGEST = "abcdefghij" * 3
assert len(LONGEST) == MAX_NAME_LENGTH

PROJECTS = {
    "agent": lambda: agent_project("support-bot"),
    "agent-longest-name": lambda: agent_project(LONGEST),
    "fleet": lambda: fleet_project("newsroom"),
    "fleet-with-planner": lambda: fleet_project(
        "newsroom", ["research", "writer"], planner=True
    ),
    "fleet-longest-names": lambda: fleet_project(LONGEST, [LONGEST, "b"], planner=True),
}
CHECKS = {
    "ruff format": ["ruff", "format", "--no-cache", "--check", "."],
    "ruff check": ["ruff", "check", "--no-cache", "."],
    "mypy": ["mypy", "--no-incremental"],
    "pytest": ["pytest", "-q", "-p", "no:cacheprovider"],
}


@pytest.mark.parametrize("make_files", PROJECTS.values(), ids=PROJECTS.keys())
def test_the_generated_project_passes_ruff_mypy_and_pytest(
    make_files: Callable[[], dict[str, str]], tmp_path: Path
) -> None:
    write_project(tmp_path, make_files())

    for check, arguments in CHECKS.items():
        completed = subprocess.run(
            [sys.executable, "-m", *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 0, (
            f"{check} failed:\n{completed.stdout}\n{completed.stderr}"
        )


CLIENT_KEY = "client-key"
PLANNER_KEY = "planner-key"
STRUCTURED = ModelCapabilities(context_window=8192, structured_output=True)


def step(step_id: str, use: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One step of a ``ctx.plan`` reply, as the planning model writes it."""
    return {"id": step_id, "use": use, "arguments": json.dumps(arguments), "after": []}


def import_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, ModuleType]:
    """Generate a fleet with a planner and import its agents' modules."""
    write_project(
        tmp_path, fleet_project("templated-news", ["research", "writer"], planner=True)
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in list(sys.modules):
        if name.startswith("templated_news"):
            monkeypatch.delitem(sys.modules, name)
    return {
        name: importlib.import_module(f"templated_news.{name}")
        for name in ("research", "writer", "planner")
    }


async def test_the_generated_planner_plans_over_its_specialists_by_tag(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    modules = import_fleet(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTFRAME_DEV", "1")
    monkeypatch.setenv("AGENTFRAME_DEV_API_KEYS", f"planner={PLANNER_KEY}")

    def dev_app(create_app: Callable[[], AgentApp]) -> Callable[[str], AgentApp]:
        def make_app(public_url: str) -> AgentApp:
            monkeypatch.setenv("AGENTFRAME_PUBLIC_URL", public_url)
            return create_app()

        return make_app

    steps = [
        step("notes", "research.research", {"text": "tides"}),
        step("post", "writer.writer", {"text": {"$ref": "notes"}}),
    ]
    reply = json.dumps({"steps": steps, "result": "post", "reason": None})
    model = ScriptedModel([reply], capabilities=STRUCTURED)

    async with (
        serve(dev_app(modules["research"].create_app)) as research_url,
        serve(dev_app(modules["writer"].create_app)) as writer_url,
    ):
        registry = tmp_path / "running-agents.yaml"
        registry.write_text(
            yaml.safe_dump(
                [
                    {"name": name, "url": f"{url}/.well-known/agent-card.json"}
                    for name, url in [
                        ("research", research_url),
                        ("writer", writer_url),
                    ]
                ]
            )
        )
        monkeypatch.setenv("AGENTFRAME_DEV_REGISTRY", str(registry))
        monkeypatch.setenv("AGENTFRAME_DEV_API_KEYS", f"developer={CLIENT_KEY}")
        monkeypatch.setenv("AGENTFRAME_DEV_REMOTE_KEYS", f"research={PLANNER_KEY}")
        planner = modules["planner"]
        async with serve(dev_app(lambda: planner.create_app(model))) as planner_url:
            client = await connect(planner_url, CLIENT_KEY)
            try:
                task = await call_skill(client, "run_goal", {"goal": "A tides post"})
            finally:
                await client.close()

    assert task.status.state == TaskState.TASK_STATE_COMPLETED, task.status
    [data] = get_data_parts(task.artifacts[0].parts)
    outcome = planner.PlanOutcome.model_validate(data["value"])
    assert outcome.skills == ["research.research", "writer.writer"]
    assert outcome.steps == {"notes": "completed", "post": "completed"}
    assert outcome.value == "[writer] [research] tides"
