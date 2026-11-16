"""Tests for A2A ``SendStreamingMessage``: model text streamed as it arrives.

Models are scripted, and every request travels over real localhost HTTP
through the official SDK client with streaming on.
"""

import uuid
from typing import Any

import pytest
from a2a.client import Client, ClientCallContext
from a2a.client.service_parameters import (
    ServiceParametersFactory,
    with_a2a_extensions,
)
from a2a.helpers import new_data_part, new_text_part
from a2a.types import (
    GetTaskRequest,
    Message,
    Role,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskState,
)
from a2a.utils.errors import UnsupportedOperationError

from a2a_client import connect
from agentframe import (
    Agent,
    AgentApp,
    APIKey,
    APIKeyAuth,
    ExecutionPolicy,
    HostConfig,
    Principal,
    RunContext,
    SkillDefinition,
    agent,
    skill,
)
from agentframe.hosting.a2a_executor import (
    MAX_STREAMED_UPDATES,
    MIN_STREAMED_UPDATE_BYTES,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.testing import ScriptedModel
from local_server import AppFactory, ServeLocally

# Test-only value; real applications load keys from the environment.
API_KEY = "test-key"


@agent(version="1.0.0")
class Writer(Agent):
    """Writes text."""

    @skill("write", tags=["text"])
    async def write(self, text: str, ctx: RunContext) -> str:
        """Write text, streaming it to the caller."""
        return await ctx.generate(text, stream=True)


def writer_app(
    model: ScriptedModel,
    public_url: str,
    *,
    max_response_bytes: int = 4_194_304,
    policy: ExecutionPolicy | None = None,
) -> AgentApp:
    host = HostConfig(
        public_url=public_url,
        auth=APIKeyAuth(keys={API_KEY: APIKey("alice")}),
        max_response_bytes=max_response_bytes,
    )
    return AgentApp(agent=Writer(model=model), host=host, policy=policy)


async def connect_streaming(base_url: str) -> Client:
    return await connect(base_url, API_KEY, streaming=True)


def write_message(text: str, message_id: str | None = None) -> Message:
    return Message(
        message_id=message_id or str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[new_data_part({"text": text})],
        metadata={"agentframe.skill": "write"},
    )


async def stream(client: Client, message: Message) -> list[StreamResponse]:
    extensions = with_a2a_extensions([SKILL_CALL_EXTENSION_URI])
    context = ClientCallContext(
        service_parameters=ServiceParametersFactory.create_from({}, [extensions])
    )
    request = SendMessageRequest(message=message)
    return [event async for event in client.send_message(request, context=context)]


def deltas(events: list[StreamResponse]) -> list[str]:
    return [
        part.text
        for event in events
        if event.HasField("artifact_update") and not event.artifact_update.last_chunk
        for part in event.artifact_update.artifact.parts
    ]


async def stored_task(base_url: str, task_id: str) -> Task:
    async with await connect(base_url, API_KEY) as client:
        return await client.get_task(GetTaskRequest(id=task_id))


def task_id_of(events: list[StreamResponse]) -> str:
    first = events[0]
    return first.task.id if first.HasField("task") else first.status_update.task_id


def final_state(events: list[StreamResponse]) -> TaskState:
    updates = [e.status_update for e in events if e.HasField("status_update")]
    return updates[-1].status.state


def make(model: ScriptedModel, **options: Any) -> AppFactory:
    return lambda public_url: writer_app(model, public_url, **options)


async def test_text_streams_as_deltas_and_the_task_keeps_the_whole_result(
    serve: ServeLocally,
) -> None:
    async with serve(make(ScriptedModel(["Hello there friend"]))) as base_url:
        async with await connect_streaming(base_url) as client:
            events = await stream(client, write_message("Go."))
        task = await stored_task(base_url, task_id_of(events))

    # The first piece is sent at once; the short rest arrives with the result.
    assert deltas(events) == ["Hello "]
    assert final_state(events) == TaskState.TASK_STATE_COMPLETED
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [artifact] = task.artifacts
    assert [part.text for part in artifact.parts] == ["Hello there friend"]


async def test_many_pieces_are_combined_and_capped_and_the_result_still_arrives(
    serve: ServeLocally,
) -> None:
    small = "w " * 2000
    large = ("x" * 63 + " ") * (MAX_STREAMED_UPDATES + 100)
    async with (
        serve(make(ScriptedModel([small, large]))) as base_url,
        await connect_streaming(base_url) as client,
    ):
        few = await stream(client, write_message("Go."))
        capped = await stream(client, write_message("Go."))

    # 2-byte pieces are sent in updates of at least 64 bytes.
    assert 1 < len(deltas(few)) <= 1 + len(small) // MIN_STREAMED_UPDATE_BYTES
    assert "".join(deltas(few)) in small
    # Pieces that each fill an update stop streaming at the cap.
    assert len(deltas(capped)) == MAX_STREAMED_UPDATES
    for events, text in [(few, small), (capped, large)]:
        [last] = [
            e.artifact_update.artifact
            for e in events
            if e.HasField("artifact_update") and e.artifact_update.last_chunk
        ]
        assert last.parts[0].text == text
        assert final_state(events) == TaskState.TASK_STATE_COMPLETED


async def test_streamed_text_over_the_response_limit_fails_the_task(
    serve: ServeLocally,
) -> None:
    app = make(ScriptedModel(["Hello there friend"]), max_response_bytes=10)
    async with serve(app) as base_url, await connect_streaming(base_url) as client:
        events = await stream(client, write_message("Go."))

    # "Hello " (6 bytes) fits; "there " would make 12.
    assert deltas(events) == ["Hello "]
    assert final_state(events) == TaskState.TASK_STATE_FAILED


async def test_output_checks_release_only_the_checked_result(
    serve: ServeLocally,
) -> None:
    async def allow(
        skill: SkillDefinition | None, principal: Principal | None, content: str
    ) -> bool:
        return True

    policy = ExecutionPolicy(output_checks=[allow])
    async with serve(make(ScriptedModel(["Hello there"]), policy=policy)) as base_url:
        async with await connect_streaming(base_url) as client:
            events = await stream(client, write_message("Go."))
        task = await stored_task(base_url, task_id_of(events))

    assert deltas(events) == []
    assert task.artifacts[0].parts[0].text == "Hello there"


async def test_a_denied_output_check_releases_no_text(serve: ServeLocally) -> None:
    async def deny(
        skill: SkillDefinition | None, principal: Principal | None, content: str
    ) -> bool:
        return False

    policy = ExecutionPolicy(output_checks=[deny])
    app = make(ScriptedModel(["Secret"]), policy=policy)
    async with serve(app) as base_url, await connect_streaming(base_url) as client:
        events = await stream(client, write_message("Go."))

    assert deltas(events) == []
    assert not any(e.HasField("artifact_update") for e in events)
    assert final_state(events) != TaskState.TASK_STATE_COMPLETED


async def test_a_repeated_streaming_message_gets_the_original_task(
    serve: ServeLocally,
) -> None:
    model = ScriptedModel(["Hello there"])
    message_id = str(uuid.uuid4())
    async with (
        serve(make(model)) as base_url,
        await connect_streaming(base_url) as client,
    ):
        first = await stream(client, write_message("Go.", message_id))
        again = await stream(client, write_message("Go.", message_id))

    assert len(model.requests) == 1
    [repeat] = again
    assert repeat.task.id == task_id_of(first)
    assert repeat.task.status.state == TaskState.TASK_STATE_COMPLETED


async def test_free_text_without_a_router_is_refused_on_a_stream(
    serve: ServeLocally,
) -> None:
    model = ScriptedModel([])
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[new_text_part("Write something.")],
    )
    async with (
        serve(make(model)) as base_url,
        await connect_streaming(base_url) as client,
    ):
        with pytest.raises(UnsupportedOperationError):
            await stream(client, message)

    assert model.requests == []
