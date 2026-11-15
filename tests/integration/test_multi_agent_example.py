"""``examples/multi_agent`` starts without Ollama when no model tag is set (#218)."""

import pytest
from a2a.helpers import get_message_text
from a2a.types import TaskState

from a2a_client import call_skill, connect
from agentframe import AgentApp
from examples.multi_agent import planner, research, writer
from local_server import ServeLocally

CLIENT_KEY = "client-key"
PLANNER_KEY = "planner-key"


async def test_the_agents_start_without_a_model_and_planning_fails_clearly(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTFRAME_OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("AGENTFRAME_DEV", "1")
    monkeypatch.setenv("AGENTFRAME_DEV_API_KEYS", f"planner={PLANNER_KEY}")

    def research_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("AGENTFRAME_PUBLIC_URL", public_url)
        return research.create_app()

    def writer_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("AGENTFRAME_PUBLIC_URL", public_url)
        return writer.create_app()

    async with serve(research_app) as research_url, serve(writer_app) as writer_url:

        def planner_app(public_url: str) -> AgentApp:
            monkeypatch.setenv("AGENTFRAME_PUBLIC_URL", public_url)
            monkeypatch.setenv("AGENTFRAME_DEV_API_KEYS", f"developer={CLIENT_KEY}")
            monkeypatch.setenv(
                "AGENTFRAME_DEV_REMOTE_KEYS",
                f"research-agent={PLANNER_KEY},writer-agent={PLANNER_KEY}",
            )
            monkeypatch.setenv(
                "AGENTFRAME_AGENTS",
                f"research-agent={research_url}/.well-known/agent-card.json,"
                f"writer-agent={writer_url}/.well-known/agent-card.json",
            )
            return planner.create_app()

        async with serve(planner_app) as planner_url:
            client = await connect(planner_url, CLIENT_KEY)
            try:
                task = await call_skill(client, "propose", {"goal": "A beach day"})
            finally:
                await client.close()

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert get_message_text(task.status.message) == (
        "This agent has no model configured"
    )
