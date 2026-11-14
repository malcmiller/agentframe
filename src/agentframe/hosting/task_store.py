"""A task store wrapper that knows which runs' final states are not yet saved.

A run publishes its final state and releases its run slot at once, but the
SDK saves that state a moment later, from its own queue. Shutdown closes
those queues, so it must also wait for the saves, or a persistent store
would keep a finished task as ``WORKING``. The SDK has no public signal for
"stored", so the app wraps its store, a public interface, and watches saves.

Everything runs on one event loop, so a plain set needs no lock.
"""

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
