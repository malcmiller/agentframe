"""Return the original task when a caller sends the same A2A message again.

A caller that retries after a network failure resends its message with the
same ``messageId``. Within the deduplication window, the app answers such a
repeat with the task the first copy created instead of running the skill
again. The key is the verified caller plus the ``messageId``; anonymous
callers are not deduplicated, because they share one namespace.

A ``MessageDedupStore`` maps each key to the ID of the task its message runs
in. The app chooses that ID before the message runs, so the claim and the
task agree. A claimed key whose message never ran (for example because the
host was busy) does not count as a repeat: the retry runs, in the claimed
task. A message counts as having run once its task's history holds it.

Repeats from one caller are handled one at a time in this process, so two
concurrent copies run the skill once. This is not exactly-once execution:
hosts sharing a store but not a task store, process loss and expiry can all
still run a message twice.
"""

import asyncio
import hashlib
import json
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from a2a.types import Message, Task

DEDUP_STATE_KEY = "agentframe.dedup"
"""``ServerCallContext.state`` key holding the message's ``DedupOutcome``."""


class MessageDedupStore(Protocol):
    """Remembers which task each deduplication key belongs to.

    Implement it to share deduplication between host instances. ``put_if_absent``
    must be atomic: of two concurrent calls with the same key, exactly one may
    store its task ID.
    """

    async def put_if_absent(
        self, key: str, task_id: str, ttl_seconds: float
    ) -> str | None:
        """Store key -> task_id unless present; return the existing task_id if it was.

        Args:
            key: Identifies one caller's message.
            task_id: The task the message will run in.
            ttl_seconds: How long to keep the entry.

        Returns:
            ``None`` when ``task_id`` was stored, otherwise the task ID already
            stored under ``key``.
        """
        ...


class InMemoryMessageDedupStore:
    """The default ``MessageDedupStore``: a dict, lost when the process exits.

    Expired entries are pruned whenever the store is used.

    Args:
        clock: Returns the current time in seconds; tests pass a fake clock.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, tuple[str, float]] = {}

    async def put_if_absent(
        self, key: str, task_id: str, ttl_seconds: float
    ) -> str | None:
        """Store ``task_id`` unless ``key`` is present; return the stored ID if so."""
        now = self._clock()
        self._prune(now)
        existing = self._entries.get(key)
        if existing is not None:
            return existing[0]
        self._entries[key] = (task_id, now + ttl_seconds)
        return None

    def _prune(self, now: float) -> None:
        expired = [key for key, (_, expires) in self._entries.items() if expires <= now]
        for key in expired:
            del self._entries[key]


@dataclass(frozen=True)
class DedupOutcome:
    """What deduplication decided about one message.

    Attributes:
        task_id: The task the message runs in, or already ran in.
        original: The task to return instead of running the message, when the
            message already ran.
        conflict: The ``messageId`` was already used for a different message.
    """

    task_id: str
    original: Task | None = None
    conflict: bool = False

    @property
    def is_repeat(self) -> bool:
        """Whether the message must not run: it already did, or it conflicts."""
        return self.original is not None or self.conflict


type FindTask = Callable[[str], Awaitable[Task | None]]
"""Looks up one of the caller's tasks by ID; ``None`` when it does not exist."""


class MessageDeduplicator:
    """Decides whether a caller's message is a repeat of one already sent.

    Args:
        window_seconds: How long a ``messageId`` is remembered.
        store: Where the claims are kept.
    """

    def __init__(self, window_seconds: float, store: MessageDedupStore) -> None:
        self._window_seconds = window_seconds
        self._store = store
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiting: Counter[str] = Counter()

    @asynccontextmanager
    async def check(
        self, caller_id: str, message: Message, find_task: FindTask
    ) -> AsyncIterator[DedupOutcome]:
        """Decide about ``message`` and hold off its repeats until the block exits.

        Run the message inside the block, so a concurrent copy is decided
        only after this one has created its task.

        Args:
            caller_id: The verified caller's ID.
            message: The incoming message; it must have a ``messageId``.
            find_task: Looks up one of the caller's tasks.

        Yields:
            The decision about ``message``.
        """
        key = json.dumps([caller_id, message.message_id])
        async with self._one_at_a_time(key):
            yield await self._decide(key, message, find_task)

    async def _decide(
        self, key: str, message: Message, find_task: FindTask
    ) -> DedupOutcome:
        # An answer runs in the task it answers; a new message gets a new task.
        proposed = message.task_id or str(uuid.uuid4())
        claimed = await self._store.put_if_absent(key, proposed, self._window_seconds)
        if claimed is None:
            return DedupOutcome(proposed)
        if message.task_id and message.task_id != claimed:
            return DedupOutcome(claimed, conflict=True)
        task = await find_task(claimed)
        earlier = _find_message(task, message.message_id) if task else None
        if task is None or earlier is None:
            return DedupOutcome(claimed)  # Claimed, but the message never ran.
        if _digest(earlier) != _digest(message):
            return DedupOutcome(claimed, conflict=True)
        return DedupOutcome(claimed, original=task)

    @asynccontextmanager
    async def _one_at_a_time(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._waiting[key] += 1
        try:
            async with lock:
                yield
        finally:
            self._waiting[key] -= 1
            if not self._waiting[key]:
                del self._waiting[key]
                del self._locks[key]


def _find_message(task: Task, message_id: str) -> Message | None:
    return next((m for m in task.history if m.message_id == message_id), None)


def _digest(message: Message) -> str:
    """Hash what makes two messages with one ``messageId`` the same message."""
    content = Message(parts=message.parts, metadata=message.metadata)
    return hashlib.sha256(content.SerializeToString(deterministic=True)).hexdigest()
