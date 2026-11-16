"""Tests for ``RedisMessageDedupStore``: deduplication shared through Redis.

``fakeredis`` stands in for a Redis server, so the store's real redis-py
calls run without one. Two hosts sharing one Redis and one task store act as
two instances of the same agent behind a load balancer.
"""

import math
from collections.abc import AsyncIterator

import pytest
from a2a.client import Client
from a2a.helpers import new_data_part
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Message, Role, Task
from fakeredis import FakeAsyncRedis

from a2a_client import connect, send
from agentframe import (
    A2AConfig,
    Agent,
    AgentApp,
    APIKey,
    APIKeyAuth,
    HostConfig,
    RedisMessageDedupStore,
    RunContext,
    agent,
    skill,
)
from local_server import ServeLocally

# Test-only value; real applications load keys from the environment.
ALICE_KEY = "alice-test-key"


@agent(version="1.0.0")
class Counter(Agent):
    """Counts how often its skill really runs."""

    def __init__(self) -> None:
        self.runs = 0

    @skill("count", tags=["test"])
    async def count(self, ctx: RunContext) -> str:
        """Count one run."""
        self.runs += 1
        return f"run {self.runs}"


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


# The store


async def test_the_first_claim_is_stored_and_later_ones_get_it(
    redis: FakeAsyncRedis,
) -> None:
    store = RedisMessageDedupStore(redis)

    assert await store.put_if_absent("key", "task-1", 60) is None
    assert await store.put_if_absent("key", "task-2", 60) == "task-1"
    assert await store.put_if_absent("other", "task-3", 60) is None


async def test_a_claim_expires_after_its_ttl(redis: FakeAsyncRedis) -> None:
    store = RedisMessageDedupStore(redis, key_prefix="echo:")

    await store.put_if_absent("key", "task-1", 0.5)

    assert 0 < await redis.pttl("echo:key") <= 500


async def test_an_infinite_window_never_expires(redis: FakeAsyncRedis) -> None:
    store = RedisMessageDedupStore(redis, key_prefix="echo:")

    await store.put_if_absent("key", "task-1", math.inf)

    assert await redis.pttl("echo:key") == -1  # Present, with no expiry.
    assert await store.put_if_absent("key", "task-2", math.inf) == "task-1"


async def test_a_client_decoding_responses_works_too() -> None:
    client = FakeAsyncRedis(decode_responses=True)
    store = RedisMessageDedupStore(client)

    await store.put_if_absent("key", "task-1", 60)

    assert await store.put_if_absent("key", "task-2", 60) == "task-1"
    await client.aclose()


# Two host instances


def instance(
    counter: Counter, public_url: str, redis: FakeAsyncRedis, tasks: InMemoryTaskStore
) -> AgentApp:
    host = HostConfig(
        public_url=public_url, auth=APIKeyAuth(keys={ALICE_KEY: APIKey("alice")})
    )
    a2a = A2AConfig(dedup_store=RedisMessageDedupStore(redis), task_store=tasks)
    return AgentApp(agent=counter, host=host, a2a=a2a)


async def call(client: Client, message: Message) -> Task:
    return await send(client, message, skill_call=True)


async def test_a_retry_reaching_another_instance_returns_the_original_task(
    serve: ServeLocally, redis: FakeAsyncRedis
) -> None:
    first_counter, second_counter = Counter(), Counter()
    tasks = InMemoryTaskStore()
    message = Message(
        message_id="message-1",
        role=Role.ROLE_USER,
        parts=[new_data_part({})],
        metadata={"agentframe.skill": "count"},
    )
    async with (
        serve(lambda url: instance(first_counter, url, redis, tasks)) as first_url,
        serve(lambda url: instance(second_counter, url, redis, tasks)) as second_url,
        await connect(first_url, ALICE_KEY) as first,
        await connect(second_url, ALICE_KEY) as second,
    ):
        original = await call(first, message)
        retried = await call(second, message)

    assert retried.id == original.id
    assert (first_counter.runs, second_counter.runs) == (1, 0)
