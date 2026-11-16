"""Tests for hosted conversation history, keyed by verified caller and ``contextId``.

Requests travel over real localhost HTTP, through the official A2A client, to
an ``AgentApp`` served by Uvicorn in the test's event loop.
"""

import uuid

import pytest
from a2a.client import Client
from a2a.helpers import get_artifact_text, new_data_part
from a2a.types import Message, Role, Task, TaskState

from a2a_client import connect, send
from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    HostConfig,
    ModelSettings,
    RunContext,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

# Test-only values; real applications load keys from the environment.
ALICE_KEY = "alice-test-key"
BOB_KEY = "bob-test-key"
KEYS = APIKeyAuth(keys={ALICE_KEY: APIKey("alice"), BOB_KEY: APIKey("bob")})


@agent(version="1.0.0")
class Chat(Agent):
    """Chats, remembering earlier turns."""

    model_settings = ModelSettings(max_output_tokens=100)

    @skill("chat", tags=["test"])
    async def chat(self, text: str, ctx: RunContext) -> str:
        """Answer with the conversation so far, then remember the turn."""
        answer = await ctx.generate(text, history=await ctx.history())
        await ctx.save_turn(text, answer)
        return answer


async def chat(client: Client, text: str, context_id: str) -> Task:
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[new_data_part({"text": text})],
        metadata={"agentframe.skill": "chat"},
        context_id=context_id,
    )
    task = await send(client, message, skill_call=True)
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    return task


def user_texts(model: ScriptedModel, call: int) -> list[str]:
    return [m.content for m in model.requests[call].messages if m.role == "user"]


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel(["First.", "Second.", "Third."])


async def test_a_caller_continues_its_conversation_by_context_id(
    serve: ServeLocally, model: ScriptedModel
) -> None:
    def make_app(url: str) -> AgentApp:
        return AgentApp(
            agent=Chat(model=model), host=HostConfig(public_url=url, auth=KEYS)
        )

    async with serve(make_app) as url, await connect(url, ALICE_KEY) as alice:
        first = await chat(alice, "I'm Ann.", "shared")
        second = await chat(alice, "Who am I?", first.context_id)

    assert get_artifact_text(second.artifacts[0]) == "Second."
    assert user_texts(model, 1) == ["I'm Ann.", "Who am I?"]


async def test_callers_sharing_a_context_id_never_see_each_other(
    serve: ServeLocally, model: ScriptedModel
) -> None:
    def make_app(url: str) -> AgentApp:
        return AgentApp(
            agent=Chat(model=model), host=HostConfig(public_url=url, auth=KEYS)
        )

    async with (
        serve(make_app) as url,
        await connect(url, ALICE_KEY) as alice,
        await connect(url, BOB_KEY) as bob,
    ):
        await chat(alice, "My PIN is 1234.", "shared")
        await chat(bob, "What's my PIN?", "shared")

    assert user_texts(model, 1) == ["What's my PIN?"]


async def test_anonymous_callers_get_no_conversation(
    serve: ServeLocally, model: ScriptedModel
) -> None:
    def make_app(url: str) -> AgentApp:
        host = HostConfig(public_url=url, auth=AllowAnonymous())
        return AgentApp(agent=Chat(model=model), host=host)

    async with serve(make_app) as url, await connect(url) as anonymous:
        await chat(anonymous, "My PIN is 1234.", "shared")
        await chat(anonymous, "What's my PIN?", "shared")

    assert user_texts(model, 1) == ["What's my PIN?"]


async def test_an_over_long_context_id_gets_no_conversation(
    serve: ServeLocally, model: ScriptedModel
) -> None:
    def make_app(url: str) -> AgentApp:
        return AgentApp(
            agent=Chat(model=model), host=HostConfig(public_url=url, auth=KEYS)
        )

    long_id = "x" * 257
    async with serve(make_app) as url, await connect(url, ALICE_KEY) as alice:
        await chat(alice, "My PIN is 1234.", long_id)
        await chat(alice, "What's my PIN?", long_id)

    assert user_texts(model, 1) == ["What's my PIN?"]
