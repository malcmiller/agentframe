"""Conversation history: the ``StateStore`` protocol and its in-memory default.

A run's conversation is keyed by the agent, the verified caller and a
conversation ID (the A2A ``contextId`` for hosted runs). The runtime builds
the key, so a caller can only ever reach its own conversations: a context ID
alone, which any caller can send, never selects one.
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from agentframe.errors import DefinitionError, InputValidationError
from agentframe.models.base import Message

MAX_CONVERSATION_ID_LENGTH = 256
"""Longest conversation ID accepted, so a caller cannot make keys huge."""


@dataclass(frozen=True)
class ConversationKey:
    """Identifies one verified caller's conversation with one agent.

    Built only by the runtime, never from request arguments.

    Attributes:
        agent: The agent's name.
        caller: The verified caller's ``Principal.id``.
        conversation: The conversation ID: the A2A ``contextId`` for hosted
            runs, or ``conversation_id`` for local runs. Caller-chosen, so it
            is only meaningful together with ``caller``.
    """

    agent: str
    caller: str
    conversation: str


class StateStore(Protocol):
    """Where conversation history is kept between runs.

    Any class with these methods is a ``StateStore``; no base class is
    needed. Stores hold only ``user`` and ``assistant`` messages, and must
    keep each ``ConversationKey`` separate: treat all three fields together
    as the key. Exceptions they raise propagate unchanged and fail the run.
    """

    async def load(self, key: ConversationKey) -> Sequence[Message]:
        """Return the conversation's messages, oldest first; empty if unknown."""
        ...

    async def append(self, key: ConversationKey, messages: Sequence[Message]) -> None:
        """Add ``messages`` to the end of the conversation, creating it if needed.

        A store may drop the oldest messages to stay within its own bounds.
        """
        ...


class InMemoryStateStore:
    """The default ``StateStore``: bounded, in this process only, lost on restart.

    Each conversation keeps its newest messages up to
    ``max_chars_per_conversation`` characters in total (each message counts
    one more than its text), starting with a ``user`` message. At most
    ``max_conversations`` conversations are kept; the least recently used is
    dropped first. A conversation not loaded or
    appended to for ``ttl_seconds`` is dropped.

    Args:
        max_conversations: Most conversations kept at once.
        max_chars_per_conversation: Most characters of message content kept
            per conversation, counting one extra per message.
        ttl_seconds: Seconds an unused conversation is kept.
        clock: Returns the current time in seconds. Tests replace it.

    Raises:
        DefinitionError: A bound is not above 0.
    """

    def __init__(
        self,
        *,
        max_conversations: int = 1_000,
        max_chars_per_conversation: int = 100_000,
        ttl_seconds: float = 3_600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for name, value in (
            ("max_conversations", max_conversations),
            ("max_chars_per_conversation", max_chars_per_conversation),
            ("ttl_seconds", ttl_seconds),
        ):
            if not value > 0:
                raise DefinitionError(f"InMemoryStateStore {name} must be above 0")
        self._max_conversations = max_conversations
        self._max_chars = max_chars_per_conversation
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        # Least recently used first, with the time each was last used.
        self._conversations: OrderedDict[
            ConversationKey, tuple[float, list[Message]]
        ] = OrderedDict()

    async def load(self, key: ConversationKey) -> Sequence[Message]:
        """Return a copy of the conversation's messages, oldest first."""
        self._drop_expired()
        entry = self._conversations.get(key)
        if entry is None:
            return []
        messages = entry[1]
        self._touch(key, messages)
        return list(messages)

    async def append(self, key: ConversationKey, messages: Sequence[Message]) -> None:
        """Add copies of ``messages``, then drop the oldest beyond the bounds."""
        self._drop_expired()
        entry = self._conversations.get(key)
        stored = entry[1] if entry is not None else []
        # Copies without provider data, which belongs to one run only.
        stored += [Message(role=m.role, content=m.content) for m in messages]
        self._touch(key, newest_within(stored, self._max_chars, stored_length))
        while len(self._conversations) > self._max_conversations:
            self._conversations.popitem(last=False)

    def _touch(self, key: ConversationKey, messages: list[Message]) -> None:
        self._conversations[key] = (self._clock(), messages)
        self._conversations.move_to_end(key)

    def _drop_expired(self) -> None:
        cutoff = self._clock() - self._ttl_seconds
        while self._conversations:
            used_at, _ = next(iter(self._conversations.values()))
            if used_at > cutoff:
                return
            self._conversations.popitem(last=False)


def stored_length(message: Message) -> int:
    """Return the message's length for the store's cap: its text plus one.

    The extra character means empty messages still count, so a
    conversation cannot grow without bound.
    """
    return len(message.content) + 1


def newest_within(
    messages: Sequence[Message], limit: int, size: Callable[[Message], int]
) -> list[Message]:
    """Return the newest ``messages`` whose total ``size`` is at most ``limit``.

    The result starts with a ``user`` message, so a turn is never kept
    without the question that started it.
    """
    kept: list[Message] = []
    total = 0
    for message in reversed(messages):
        total += size(message)
        if total > limit:
            break
        kept.append(message)
    kept.reverse()
    while kept and kept[0].role != "user":
        kept.pop(0)
    return kept


@dataclass(frozen=True)
class RunConversation:
    """One run's conversation: its store and the key the runtime built."""

    store: StateStore
    key: ConversationKey

    async def load(self) -> list[Message]:
        """Return the conversation's earlier messages, oldest first."""
        return list(await self.store.load(self.key))

    async def save_turn(self, user: str, assistant: str) -> None:
        """Append one exchange: the caller's text and the agent's answer."""
        await self.store.append(
            self.key,
            [
                Message(role="user", content=user),
                Message(role="assistant", content=assistant),
            ],
        )


def run_conversation(
    store: StateStore, agent: str, caller: str | None, conversation_id: str | None
) -> RunConversation | None:
    """Return the run's conversation, or ``None`` when it has none.

    Args:
        store: The runtime's state store.
        agent: The agent's name.
        caller: The verified caller's ID, or ``None`` without a principal.
        conversation_id: The conversation ID, or ``None`` for none.

    Raises:
        DefinitionError: A conversation ID was given without a principal: a
            conversation must belong to a verified caller.
        InputValidationError: The conversation ID is empty or longer than
            ``MAX_CONVERSATION_ID_LENGTH`` characters.
    """
    if conversation_id is None:
        return None
    if caller is None:
        raise DefinitionError(
            "conversation_id needs a principal: a conversation belongs to a "
            "verified caller"
        )
    if not 0 < len(conversation_id) <= MAX_CONVERSATION_ID_LENGTH:
        raise InputValidationError(
            "The conversation ID must be 1 to "
            f"{MAX_CONVERSATION_ID_LENGTH} characters long"
        )
    return RunConversation(store, ConversationKey(agent, caller, conversation_id))
