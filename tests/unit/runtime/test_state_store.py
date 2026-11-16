"""Tests for ``InMemoryStateStore``, the default conversation history store."""

from typing import Any

import pytest

from agentframe import ConversationKey, DefinitionError, InMemoryStateStore, Message

KEY = ConversationKey(agent="chat", caller="alice", conversation="c1")


def user(text: str) -> Message:
    return Message(role="user", content=text)


def assistant(text: str) -> Message:
    return Message(role="assistant", content=text)


class FakeClock:
    """A clock the test moves forward by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_appended_messages_are_loaded_in_order() -> None:
    store = InMemoryStateStore()

    await store.append(KEY, [user("Hi"), assistant("Hello")])
    await store.append(KEY, [user("Bye"), assistant("Goodbye")])

    assert await store.load(KEY) == [
        user("Hi"),
        assistant("Hello"),
        user("Bye"),
        assistant("Goodbye"),
    ]


async def test_an_unknown_conversation_is_empty() -> None:
    assert await InMemoryStateStore().load(KEY) == []


async def test_each_key_field_separates_conversations() -> None:
    store = InMemoryStateStore()
    await store.append(KEY, [user("Alice's secret")])

    for other in (
        ConversationKey("other-agent", "alice", "c1"),
        ConversationKey("chat", "bob", "c1"),
        ConversationKey("chat", "alice", "c2"),
    ):
        assert await store.load(other) == []


async def test_stored_messages_drop_provider_data() -> None:
    store = InMemoryStateStore()
    message = Message(role="assistant", content="Hi", provider_items=({"id": "r"},))

    await store.append(KEY, [user("Hello"), message])

    assert (await store.load(KEY))[1].provider_items == ()


async def test_the_oldest_messages_go_beyond_the_character_cap() -> None:
    store = InMemoryStateStore(max_chars_per_conversation=10)

    await store.append(KEY, [user("aaaa"), assistant("bbbb")])
    await store.append(KEY, [user("cccc"), assistant("dddd")])

    assert await store.load(KEY) == [user("cccc"), assistant("dddd")]


async def test_a_kept_conversation_starts_with_a_user_message() -> None:
    store = InMemoryStateStore(max_chars_per_conversation=8)

    await store.append(KEY, [user("aaaa"), assistant("bbbb"), user("cc")])

    # "bbbb" + "cc" fit (each counts one extra), but a turn is never kept
    # without its question.
    assert await store.load(KEY) == [user("cc")]


async def test_empty_messages_still_count_towards_the_cap() -> None:
    store = InMemoryStateStore(max_chars_per_conversation=4)

    for _ in range(10):
        await store.append(KEY, [user(""), assistant("")])

    assert len(await store.load(KEY)) == 4


async def test_the_least_recently_used_conversation_is_dropped() -> None:
    store = InMemoryStateStore(max_conversations=2)
    first, second, third = (ConversationKey("chat", "alice", c) for c in "123")
    await store.append(first, [user("1")])
    await store.append(second, [user("2")])

    await store.load(first)  # Now the most recently used.
    await store.append(third, [user("3")])

    assert await store.load(first) == [user("1")]
    assert await store.load(second) == []
    assert await store.load(third) == [user("3")]


async def test_an_unused_conversation_expires() -> None:
    clock = FakeClock()
    store = InMemoryStateStore(ttl_seconds=60, clock=clock)
    await store.append(KEY, [user("Hi")])

    clock.now = 59
    assert await store.load(KEY) == [user("Hi")]  # Using it restarts its time.
    clock.now = 118
    assert await store.load(KEY) == [user("Hi")]
    clock.now = 178
    assert await store.load(KEY) == []


async def test_loaded_lists_are_copies() -> None:
    store = InMemoryStateStore()
    await store.append(KEY, [user("Hi")])

    loaded = await store.load(KEY)
    assert isinstance(loaded, list)
    loaded.append(user("Injected"))

    assert await store.load(KEY) == [user("Hi")]


@pytest.mark.parametrize(
    "bound", ["max_conversations", "max_chars_per_conversation", "ttl_seconds"]
)
async def test_bounds_must_be_above_zero(bound: str) -> None:
    bounds: dict[str, Any] = {bound: 0}
    with pytest.raises(DefinitionError, match=bound):
        InMemoryStateStore(**bounds)
