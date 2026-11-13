"""``agentframe compose`` output runs: two containers, the planner calls the researcher.

Opt-in and local only: it builds images with a real Docker daemon. Enable it
with ``AGENTFRAME_TEST_DOCKER=1``; it is skipped without that or without a
running Docker with Compose v2. It builds this repository's wheel and a tiny
project wheel around ``tests/support/dev_targets.py``, generates the files with
``agentframe compose``, starts them with ``docker compose up --wait`` and
removes the containers and images afterwards.
"""

import os
import secrets
import shutil
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from a2a.helpers import get_artifact_text
from a2a.types import TaskState

from a2a_client import call_skill, connect

pytestmark = pytest.mark.docker

REPOSITORY = Path(__file__).parent.parent.parent
SUPPORT_DIRECTORY = REPOSITORY / "tests" / "support"
CLIENT_KEY = secrets.token_urlsafe(32)
PROJECT_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "compose-targets"
version = "0.1.0"
dependencies = ["agentframe-py[server]"]

[tool.hatch.build.targets.wheel]
only-include = ["dev_targets.py"]
"""


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    check = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, check=False
    )
    info = subprocess.run(["docker", "info"], capture_output=True, check=False)
    return check.returncode == 0 and info.returncode == 0


if os.environ.get("AGENTFRAME_TEST_DOCKER") != "1":
    pytest.skip("set AGENTFRAME_TEST_DOCKER=1 to run", allow_module_level=True)
if not docker_available():
    pytest.skip("needs a running Docker with Compose v2", allow_module_level=True)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def run(command: list[str], directory: Path) -> None:
    subprocess.run(command, cwd=directory, check=True)


@pytest.fixture
def compose_project(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    """Generate, build and start the two-agent project; yield the planner port."""
    dist = tmp_path / "dist"
    shutil.copy(SUPPORT_DIRECTORY / "dev_targets.py", tmp_path)
    (tmp_path / "pyproject.toml").write_text(PROJECT_PYPROJECT)
    # This repository's wheel, so the containers run the code under test.
    run(["uv", "build", "--wheel", "--out-dir", str(dist)], REPOSITORY)
    run(["uv", "build", "--wheel", "--out-dir", str(dist)], tmp_path)
    research_port, planner_port = free_port(), free_port()
    project = {
        "agents": {
            "research-agent": {
                "app": "dev_targets:create_research_app",
                "port": research_port,
            },
            "planner": {"app": "dev_targets:create_planner_app", "port": planner_port},
        }
    }
    (tmp_path / "agentframe.yaml").write_text(yaml.safe_dump(project, sort_keys=False))
    run([sys.executable, "-m", "agentframe", "compose"], tmp_path)
    (tmp_path / ".env").write_text(
        f"RESEARCH_AGENT_API_KEY={secrets.token_urlsafe(32)}\n"
        f"PLANNER_API_KEY={secrets.token_urlsafe(32)}\n"
        f"AGENTFRAME_DEV_CLIENT_KEY={CLIENT_KEY}\n"
    )
    compose = ["docker", "compose", "--project-name", f"af-{uuid.uuid4().hex[:8]}"]
    try:
        run([*compose, "up", "--detach", "--build", "--wait"], tmp_path)
        yield tmp_path, planner_port
    finally:
        subprocess.run([*compose, "logs"], cwd=tmp_path, check=False)
        run([*compose, "down", "--volumes", "--rmi", "local"], tmp_path)


async def test_the_planner_container_calls_the_researcher_container(
    compose_project: tuple[Path, int],
) -> None:
    _, planner_port = compose_project

    # The cards advertise service names, which only resolve inside the
    # Compose network, so this host-side client sends them to the published
    # ports on 127.0.0.1 instead.
    async def to_published_port(request: httpx.Request) -> None:
        request.url = request.url.copy_with(host="127.0.0.1")

    async with httpx.AsyncClient(
        event_hooks={"request": [to_published_port]}, timeout=30
    ) as http:
        client = await connect(
            f"http://127.0.0.1:{planner_port}", CLIENT_KEY, http=http
        )
        task = await call_skill(client, "plan")

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Called by planner"
