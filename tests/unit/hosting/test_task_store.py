"""Tests for how long the default in-memory task store keeps A2A tasks.

The store runs directly, with a clock the test moves by hand, so nothing
sleeps. End-to-end eviction over HTTP is in ``test_task_operations.py``.
"""

import pytest
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Task, TaskState, TaskStatus

from agentframe import A2AConfig, DefinitionError
from agentframe.hosting.task_store import BoundedTaskStore

COMPLETED = TaskState.TASK_STATE_COMPLETED
FAILED = TaskState.TASK_STATE_FAILED
WORKING = TaskState.TASK_STATE_WORKING
INPUT_REQUIRED = TaskState.TASK_STATE_INPUT_REQUIRED


class FakeClock:
    """A clock the test moves forward by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Caller(User):
    """A signed-in caller; the SDK store keeps each caller's tasks apart."""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._name


ALICE = ServerCallContext(user=Caller("alice"))
BOB = ServerCallContext(user=Caller("bob"))


def task(task_id: str, state: TaskState) -> Task:
    return Task(id=task_id, context_id="context", status=TaskStatus(state=state))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def inner() -> InMemoryTaskStore:
    return InMemoryTaskStore()


def bounded(
    inner: InMemoryTaskStore, clock: FakeClock, max_tasks: int = 100
) -> BoundedTaskStore:
    return BoundedTaskStore(
        inner, retention_seconds=60, max_tasks=max_tasks, clock=clock
    )


async def test_a_finished_task_is_kept_until_its_retention_ends(
    inner: InMemoryTaskStore, clock: FakeClock
) -> None:
    store = bounded(inner, clock)
    await store.save(task("t1", COMPLETED), ALICE)

    clock.now = 59
    assert await store.get("t1", ALICE) is not None
    clock.now = 60
    assert await store.get("t1", ALICE) is None
    assert await inner.get("t1", ALICE) is None


async def test_retention_counts_from_when_the_task_finished(
    inner: InMemoryTaskStore, clock: FakeClock
) -> None:
    store = bounded(inner, clock)
    await store.save(task("t1", WORKING), ALICE)
    clock.now = 100
    await store.save(task("t1", COMPLETED), ALICE)

    clock.now = 159
    assert await store.get("t1", ALICE) is not None
    clock.now = 160
    assert await store.get("t1", ALICE) is None


async def test_unfinished_tasks_are_never_deleted(
    inner: InMemoryTaskStore, clock: FakeClock
) -> None:
    store = bounded(inner, clock, max_tasks=1)
    await store.save(task("working", WORKING), ALICE)
    await store.save(task("waiting", INPUT_REQUIRED), ALICE)
    await store.save(task("done-1", COMPLETED), ALICE)
    await store.save(task("done-2", COMPLETED), ALICE)

    clock.now = 1000

    assert await store.get("working", ALICE) is not None
    assert await store.get("waiting", ALICE) is not None
    assert await store.get("done-1", ALICE) is None
    assert await store.get("done-2", ALICE) is None


async def test_past_the_maximum_the_oldest_finished_task_goes_first(
    inner: InMemoryTaskStore, clock: FakeClock
) -> None:
    store = bounded(inner, clock, max_tasks=2)
    await store.save(task("t1", COMPLETED), ALICE)
    await store.save(task("t2", FAILED), BOB)
    await store.save(task("t3", COMPLETED), ALICE)

    assert await store.get("t1", ALICE) is None
    assert await store.get("t2", BOB) is not None
    assert await store.get("t3", ALICE) is not None


async def test_each_task_is_deleted_from_its_own_callers_tasks(
    inner: InMemoryTaskStore, clock: FakeClock
) -> None:
    store = bounded(inner, clock)
    await store.save(task("t1", COMPLETED), BOB)

    clock.now = 60
    # Alice's request triggers the cleanup of Bob's expired task.
    await store.get("other", ALICE)

    assert await inner.get("t1", BOB) is None


# Configuration


def test_retention_settings_must_be_positive() -> None:
    with pytest.raises(DefinitionError, match="task_retention_seconds"):
        A2AConfig(task_retention_seconds=0)
    with pytest.raises(DefinitionError, match="max_retained_tasks"):
        A2AConfig(max_retained_tasks=0)


def test_retention_must_cover_the_dedup_window() -> None:
    with pytest.raises(DefinitionError, match="dedup_window_seconds"):
        A2AConfig(dedup_window_seconds=600, task_retention_seconds=599)


def test_a_custom_task_store_skips_the_retention_check() -> None:
    config = A2AConfig(
        task_store=InMemoryTaskStore(),
        dedup_window_seconds=600,
        task_retention_seconds=60,
    )

    assert config.task_retention_seconds == 60
