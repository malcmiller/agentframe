"""Task store wrappers: final-state tracking for shutdown, and bounded retention.

``FinalStateStore`` knows which runs' final states are not yet saved.

A run publishes its final state and releases its run slot at once, but the
SDK saves that state a moment later, from its own queue. Shutdown closes
those queues, so it must also wait for the saves, or a persistent store
would keep a finished task as ``WORKING``. The SDK has no public signal for
"stored", so the app wraps its store, a public interface, and watches saves.

``BoundedTaskStore`` deletes finished tasks after a while, so the default
in-memory store does not grow until the process exits.

Everything runs on one event loop, so plain sets and dicts need no lock.
"""

import time
from collections import OrderedDict
from collections.abc import Callable

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, ListTasksResponse, Task, TaskState

_RUNNING_STATES = frozenset(
    {TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING}
)
"""States a run passes through; any other state it saves is its last."""


class FinalStateStore(TaskStore):
    """Saves to ``store`` and counts runs whose final state is not saved yet.

    Args:
        store: The store that actually keeps the tasks.
    """

    def __init__(self, store: TaskStore) -> None:
        self._store = store
        self._unsaved: set[str] = set()

    def expect_final_state(self, task_id: str) -> None:
        """Note that a run of ``task_id`` started and will save a final state."""
        self._unsaved.add(task_id)

    @property
    def unsaved_final_states(self) -> int:
        """How many started runs have not yet had their final state saved."""
        return len(self._unsaved)

    async def save(self, task: Task, context: ServerCallContext) -> None:
        """Save ``task``; once saved, a final state is no longer awaited."""
        await self._store.save(task, context)
        if task.status.state not in _RUNNING_STATES:
            self._unsaved.discard(task.id)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        """Return the stored task, or ``None``."""
        return await self._store.get(task_id, context)

    async def list(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        """Return the stored tasks ``params`` selects."""
        return await self._store.list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        """Delete the stored task; no final state is awaited for it any more."""
        await self._store.delete(task_id, context)
        self._unsaved.discard(task_id)


_FINISHED_STATES = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
)
"""Terminal states: a task in one of them never changes again."""


class BoundedTaskStore(TaskStore):
    """Keeps finished tasks in ``store`` for a while, then deletes them.

    A task is finished once it is saved in a terminal state. It is deleted
    ``retention_seconds`` after that save, or earlier when more than
    ``max_tasks`` finished tasks are kept, oldest first. Tasks in any other
    state, such as working or waiting for input, are never deleted. Expired
    tasks are deleted whenever the store is used, so a caller never sees one.

    Args:
        store: The store that actually keeps the tasks.
        retention_seconds: How long a finished task is kept.
        max_tasks: How many finished tasks are kept at most.
        clock: Returns the current time in seconds; tests pass a fake one.
    """

    def __init__(
        self,
        store: TaskStore,
        retention_seconds: float,
        max_tasks: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._retention_seconds = retention_seconds
        self._max_tasks = max_tasks
        self._clock = clock
        # Task ID -> when it finished and who owns it, oldest first. Deleting
        # needs the owner, because the SDK store keeps each caller's tasks apart.
        self._finished: OrderedDict[str, tuple[float, ServerCallContext]] = (
            OrderedDict()
        )

    async def save(self, task: Task, context: ServerCallContext) -> None:
        """Save ``task``; a finished one is deleted once its time is up."""
        await self._store.save(task, context)
        self._finished.pop(task.id, None)
        if task.status.state in _FINISHED_STATES:
            owner = ServerCallContext(user=context.user, tenant=context.tenant)
            self._finished[task.id] = (self._clock(), owner)
        await self._delete_expired()

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        """Return the stored task, or ``None`` when it never existed or expired."""
        await self._delete_expired()
        return await self._store.get(task_id, context)

    async def list(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        """Return the stored tasks ``params`` selects."""
        await self._delete_expired()
        return await self._store.list(params, context)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        """Delete the stored task."""
        self._finished.pop(task_id, None)
        await self._store.delete(task_id, context)

    async def _delete_expired(self) -> None:
        oldest_kept = self._clock() - self._retention_seconds
        while self._finished:
            task_id, (finished_at, owner) = next(iter(self._finished.items()))
            if finished_at > oldest_kept and len(self._finished) <= self._max_tasks:
                return
            # Forget it before awaiting, so a concurrent call skips it.
            del self._finished[task_id]
            await self._store.delete(task_id, owner)
