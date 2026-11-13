"""Tests for A2A message deduplication by caller and ``messageId``.

Requests travel over real localhost HTTP, through the official A2A client, to
an ``AgentApp`` served by Uvicorn in the test's event loop. A caller resends a
message with the same ``messageId``, as it would after a network failure.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence

import httpx
import pytest
from a2a.client import A2AClientError, Client
from a2a.helpers import get_artifact_text, new_data_part, new_text_part
from a2a.types import Message, Role, Task, TaskState
from a2a.utils.errors import InvalidParamsError

from a2a_client import connect, send
from agentframe import (
    A2AConfig,
    Agent,
    AgentApp,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    Authenticator,
    Clarification,
    DefinitionError,
    HostConfig,
    RoutingContext,
    RunContext,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    agent,
    skill,
)
from agentframe.hosting.dedup import InMemoryMessageDedupStore
from local_server import ServeLocally

# Test-only values; real applications load keys from the environment.
ALICE_KEY = "alice-test-key"
BOB_KEY = "bob-test-key"
KEYS = APIKeyAuth(keys={ALICE_KEY: APIKey("alice"), BOB_KEY: APIKey("bob")})

COMPLETED = TaskState.TASK_STATE_COMPLETED


@agent(version="1.0.0")
class Counter(Agent):
    """Counts how often each skill really runs."""

    def __init__(self) -> None:
        self.runs = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @skill("count", tags=["test"])
    async def count(self, ctx: RunContext) -> str:
        """Count one run."""
        self.runs += 1
        return f"run {self.runs}"

    @skill("wait", tags=["test"])
    async def wait(self, ctx: RunContext) -> str:
        """Count one run, then wait to be released."""
        self.runs += 1
        self.started.set()
        await self.release.wait()
        return f"run {self.runs}"

    @skill("fail", tags=["test"])
    async def fail(self, ctx: RunContext) -> str:
        """Count one run, then fail."""
        self.runs += 1
        raise RuntimeError("boom")


class AskFirstRouter(SkillRouter):
    """Asks a question about a new message and runs ``count`` on the answer."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        if len(conversation) == 1:
            return Clarification("Which one?")
        return SkillCall("count", {})


class RecordingStore(InMemoryMessageDedupStore):
    """A custom store that records every claim it is asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.claims: list[tuple[str, str, float]] = []

    async def put_if_absent(
        self, key: str, task_id: str, ttl_seconds: float
    ) -> str | None:
        self.claims.append((key, task_id, ttl_seconds))
        return await super().put_if_absent(key, task_id, ttl_seconds)


class FakeClock:
    """A clock the test moves forward by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def skill_message(skill_id: str, message_id: str, value: int = 0) -> Message:
    return Message(
        message_id=message_id,
        role=Role.ROLE_USER,
        parts=[new_data_part({"value": value} if value else {})],
        metadata={"agentframe.skill": skill_id},
    )


def text_message(text: str, message_id: str, task_id: str = "") -> Message:
    return Message(
        message_id=message_id,
        role=Role.ROLE_USER,
        parts=[new_text_part(text)],
        task_id=task_id,
    )


async def call(
    client: Client, message: Message, *, return_immediately: bool = False
) -> Task:
    """Send ``message`` as a skill call and return its task."""
    return await send(
        client,
        message,
        skill_call=True,
        return_immediately=return_immediately,
    )


def result_of(task: Task) -> str:
    return get_artifact_text(task.artifacts[0])


@pytest.fixture
def counter() -> Counter:
    return Counter()


def make_app(
    counter: Counter,
    public_url: str,
    *,
    auth: Authenticator = KEYS,
    a2a: A2AConfig | None = None,
    max_concurrent_runs: int = 32,
) -> AgentApp:
    host = HostConfig(
        public_url=public_url, auth=auth, max_concurrent_runs=max_concurrent_runs
    )
    return AgentApp(agent=counter, host=host, router=AskFirstRouter(), a2a=a2a)


@pytest.fixture
async def base_url(serve: ServeLocally, counter: Counter) -> AsyncIterator[str]:
    async with serve(lambda url: make_app(counter, url)) as url:
        yield url


@pytest.fixture
async def alice(base_url: str) -> AsyncIterator[Client]:
    async with await connect(base_url, ALICE_KEY) as client:
        yield client


# A repeated message returns the original task


async def test_a_repeated_message_returns_the_original_task(
    alice: Client, counter: Counter
) -> None:
    message = skill_message("count", "message-1")

    first = await call(alice, message)
    second = await call(alice, message)

    assert second.id == first.id
    assert second.status.state == COMPLETED
    assert result_of(second) == "run 1"
    assert counter.runs == 1


async def test_a_new_message_id_runs_again(alice: Client, counter: Counter) -> None:
    first = await call(alice, skill_message("count", "message-1"))
    second = await call(alice, skill_message("count", "message-2"))

    assert second.id != first.id
    assert counter.runs == 2


async def test_a_failed_task_is_returned_as_it_is_not_run_again(
    alice: Client, counter: Counter
) -> None:
    message = skill_message("fail", "message-1")

    first = await call(alice, message)
    second = await call(alice, message)

    assert first.status.state == TaskState.TASK_STATE_FAILED
    assert (second.id, second.status.state) == (first.id, first.status.state)
    assert counter.runs == 1


async def test_reusing_a_message_id_for_a_different_message_is_refused(
    alice: Client, counter: Counter
) -> None:
    await call(alice, skill_message("count", "message-1"))

    with pytest.raises(InvalidParamsError, match="used for a different message"):
        await call(alice, skill_message("count", "message-1", value=2))
    assert counter.runs == 1


async def test_two_concurrent_copies_run_the_skill_once(
    alice: Client, counter: Counter
) -> None:
    message = skill_message("wait", "message-1")

    first = asyncio.create_task(call(alice, message))
    second = asyncio.create_task(call(alice, message))
    await asyncio.wait_for(counter.started.wait(), timeout=5)
    await asyncio.sleep(0.05)  # Give the second copy time to arrive.
    counter.release.set()

    assert (await first).id == (await second).id
    assert counter.runs == 1


# Answers to input-required tasks are deduplicated too


async def test_a_repeated_answer_returns_the_task_without_routing_it_again(
    alice: Client, counter: Counter
) -> None:
    question = await send(alice, text_message("Do it", "ask-1"), skill_call=False)
    assert question.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    answer = text_message("The count one", "answer-1", task_id=question.id)

    first = await send(alice, answer, skill_call=False)
    second = await send(alice, answer, skill_call=False)

    assert first.id == second.id == question.id
    assert second.status.state == COMPLETED
    assert counter.runs == 1


# Callers are kept apart


async def test_another_caller_with_the_same_message_id_gets_its_own_task(
    base_url: str, alice: Client, counter: Counter
) -> None:
    message = skill_message("count", "message-1")
    alices = await call(alice, message)

    async with await connect(base_url, BOB_KEY) as bob:
        bobs = await call(bob, message)

    assert bobs.id != alices.id
    assert result_of(bobs) == "run 2"
    assert counter.runs == 2


async def test_anonymous_callers_are_not_deduplicated(
    serve: ServeLocally, counter: Counter
) -> None:
    def anonymous_app(url: str) -> AgentApp:
        return make_app(counter, url, auth=AllowAnonymous())

    message = skill_message("count", "message-1")
    async with serve(anonymous_app) as url, await connect(url) as client:
        first = await call(client, message)
        second = await call(client, message)

    assert second.id != first.id
    assert counter.runs == 2


# The window and the store


async def test_after_the_window_the_same_message_id_runs_again(
    serve: ServeLocally, counter: Counter
) -> None:
    clock = FakeClock()
    a2a = A2AConfig(
        dedup_window_seconds=60, dedup_store=InMemoryMessageDedupStore(clock)
    )
    message = skill_message("count", "message-1")
    async with (
        serve(lambda url: make_app(counter, url, a2a=a2a)) as url,
        await connect(url, ALICE_KEY) as alice,
    ):
        first = await call(alice, message)
        clock.now = 59
        within = await call(alice, message)
        clock.now = 60
        after = await call(alice, message)

    assert within.id == first.id
    assert after.id != first.id
    assert counter.runs == 2


async def test_a_custom_store_is_used(serve: ServeLocally, counter: Counter) -> None:
    store = RecordingStore()
    a2a = A2AConfig(dedup_window_seconds=30, dedup_store=store)
    message = skill_message("count", "message-1")
    async with (
        serve(lambda url: make_app(counter, url, a2a=a2a)) as url,
        await connect(url, ALICE_KEY) as alice,
    ):
        first = await call(alice, message)
        await call(alice, message)

    [(key, task_id, ttl), repeat] = store.claims
    assert key == '["alice", "message-1"]'
    assert (task_id, ttl) == (first.id, 30)
    assert repeat[0] == key
    assert counter.runs == 1


def test_the_window_must_be_positive() -> None:
    with pytest.raises(DefinitionError, match="dedup_window_seconds"):
        A2AConfig(dedup_window_seconds=0)


# Deduplication comes before admission


async def test_a_repeat_while_the_host_is_full_returns_the_original_task(
    serve: ServeLocally, counter: Counter
) -> None:
    message = skill_message("wait", "message-1")
    async with (
        serve(lambda url: make_app(counter, url, max_concurrent_runs=1)) as url,
        await connect(url, ALICE_KEY) as alice,
    ):
        running = await call(alice, message, return_immediately=True)
        await asyncio.wait_for(counter.started.wait(), timeout=5)

        repeat = await call(alice, message)

        assert repeat.id == running.id
        assert repeat.status.state == TaskState.TASK_STATE_WORKING
        counter.release.set()

    assert counter.runs == 1


async def test_a_message_refused_as_busy_runs_when_retried(
    serve: ServeLocally, counter: Counter
) -> None:
    message = skill_message("count", "message-1")
    async with (
        serve(lambda url: make_app(counter, url, max_concurrent_runs=1)) as url,
        await connect(url, ALICE_KEY) as alice,
    ):
        await call(
            alice, skill_message("wait", "holds-the-slot"), return_immediately=True
        )
        await asyncio.wait_for(counter.started.wait(), timeout=5)
        with pytest.raises(A2AClientError) as busy:
            await call(alice, message)
        counter.release.set()
        await asyncio.sleep(0.05)  # Let the waiting run free its slot.

        retried = await call(alice, message)

    http_error = busy.value.__cause__
    assert isinstance(http_error, httpx.HTTPStatusError)
    assert http_error.response.status_code == 503
    assert retried.status.state == COMPLETED
    assert counter.runs == 2
