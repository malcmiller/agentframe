"""The README examples and ``examples/echo.py``, kept working as written.

The quick start is read from README.md and run as written, and its agent and
the assistant must appear verbatim in ``examples/echo.py`` and
``examples/assistant.py``; the multi-agent example is read from README.md and
run against a plain A2A peer.
"""

import ast
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from a2a.client import ClientCallContext, ClientConfig, ClientFactory
from a2a.client.service_parameters import (
    ServiceParametersFactory,
    with_a2a_extensions,
)
from a2a.helpers import get_artifact_text, new_data_part
from a2a.types import Message, Role, SendMessageRequest

from a2a_peer import PeerExecutor, a2a_peer
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    FileAgentRegistry,
    Runtime,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from examples.echo import create_app
from local_server import ServeLocally


async def test_readme_quick_start_runs_as_written() -> None:
    block = readme_block("class Echo")
    code = compile(block, "README.md", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    namespace: dict[str, Any] = {"__name__": "readme_echo"}
    await eval(code, namespace)

    assert namespace["value"] == "hi"
    assert namespace["result"].value == "Hello, Ada!"


def test_readme_echo_agent_is_examples_echo() -> None:
    block = readme_block("class Echo")
    definitions = block[block.index("class Greeter") : block.index("runtime = ")]
    assert definitions.strip() in example_source("echo.py")


def test_readme_assistant_is_examples_assistant() -> None:
    assert readme_block("class Assistant").strip() in example_source("assistant.py")


async def test_readme_hosting_example(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECHO_API_KEY", "dev-only-key")

    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ECHO_PUBLIC_URL", public_url)
        return create_app()

    async with (
        serve(make_app) as base_url,
        httpx.AsyncClient(headers={"X-API-Key": "dev-only-key"}) as http,
    ):
        client = await ClientFactory(ClientConfig(httpx_client=http)).create_from_url(
            base_url
        )
        message = Message(
            message_id="m-1",
            role=Role.ROLE_USER,
            parts=[new_data_part({"name": "Ada"})],
            metadata={"agentframe.skill": "greet"},
        )
        activate = ClientCallContext(
            service_parameters=ServiceParametersFactory.create(
                [with_a2a_extensions([SKILL_CALL_EXTENSION_URI])]
            )
        )
        [response] = [
            r
            async for r in client.send_message(
                SendMessageRequest(message=message), context=activate
            )
        ]

    assert get_artifact_text(response.task.artifacts[0]) == "Hello, Ada!"


ROOT = Path(__file__).parents[2]


def example_source(name: str) -> str:
    return (ROOT / "examples" / name).read_text()


def readme_block(containing: str) -> str:
    """Return the README's Python code block containing the given text."""
    readme = (ROOT / "README.md").read_text()
    blocks: list[str] = re.findall(r"```python\n(.*?)```", readme, flags=re.DOTALL)
    [block] = [block for block in blocks if containing in block]
    return block


async def test_readme_multi_agent_example(serve: ServeLocally, tmp_path: Path) -> None:
    namespace: dict[str, object] = {"__name__": "readme_planner"}
    exec(readme_block("class Planner"), namespace)
    planner = namespace["Planner"]
    assert isinstance(planner, type) and issubclass(planner, Agent)

    async with serve(a2a_peer(PeerExecutor())) as peer_url:
        agents = tmp_path / "agents.yaml"
        agents.write_text(
            f"- name: research-agent\n  url: {peer_url}/.well-known/agent-card.json\n"
        )
        async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as directory:
            briefs = await Runtime(directory=directory).invoke(
                planner(),
                skill="plan",
                arguments={"goal": "a trip", "topics": ["tides", "weather"]},
            )

    assert briefs == [
        "Echo: Brief me on tides for a trip",
        "Echo: Brief me on weather for a trip",
    ]
