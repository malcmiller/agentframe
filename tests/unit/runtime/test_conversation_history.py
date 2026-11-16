"""Tests for conversation history: ``ctx.history``, ``ctx.save_turn`` and
``ctx.generate(history=...)`` in local runs.

Models are scripted, so no test calls a provider.
"""

from collections.abc import Sequence
from typing import cast

import pytest

from agentframe import (
    Agent,
    ConversationKey,
    DefinitionError,
    InMemoryStateStore,
    InputValidationError,
    Message,
    ModelSettings,
    Principal,
    Role,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    ToolCall,
    agent,
    skill,
)
from agentframe.runtime.generation import check_history
from agentframe.testing import ScriptedModel

ALICE = Principal(id="alice")
BOB = Principal(id="bob")


def user(text: str) -> Message:
    return Message(role="user", content=text)


def assistant(text: str) -> Message:
    return Message(role="assistant", content=text)


@agent(version="1.0.0")
class Chat(Agent):
    """Chats, remembering earlier turns."""

    model_settings = ModelSettings(max_output_tokens=100)

    @skill("chat", tags=["test"])
    async def chat(self, text: str, ctx: RunContext) -> str:
        """Answer with the conversation so far, then remember the turn."""
        history = await ctx.history()
        answer = await ctx.generate(text, instructions="Be brief.", history=history)
        await ctx.save_turn(text, answer)
        return answer

    @skill("with_history", tags=["test"])
    async def with_history(
        self, text: str, history: list[tuple[str, str]], ctx: RunContext
    ) -> str:
        """Answer with the given (role, content) history."""
        messages = [Message(role=cast(Role, r), content=c) for r, c in history]
        return await ctx.generate(text, history=messages)


def sent(model: ScriptedModel, call: int = -1) -> list[Message]:
    """Return the non-system messages of one request the model received."""
    return [m for m in model.requests[call].messages if m.role != "system"]


async def chat(
    runtime: Runtime,
    model: ScriptedModel,
    text: str,
    principal: Principal | None = ALICE,
    conversation_id: str | None = "c1",
) -> str:
    result: str = await runtime.invoke(
        Chat(model=model),
        skill="chat",
        arguments={"text": text},
        principal=principal,
        conversation_id=conversation_id,
    )
    return result


async def test_a_later_turn_sees_the_earlier_ones() -> None:
    runtime = Runtime()
    model = ScriptedModel(["Hello, Ann.", "Your name is Ann."])

    await chat(runtime, model, "I'm Ann.")
    await chat(runtime, model, "What's my name?")

    assert sent(model) == [
        user("I'm Ann."),
        assistant("Hello, Ann."),
        user("What's my name?"),
    ]


async def test_history_comes_after_the_instructions() -> None:
    runtime = Runtime()
    model = ScriptedModel(["Hi.", "Again."])

    await chat(runtime, model, "One")
    await chat(runtime, model, "Two")

    roles = [m.role for m in model.requests[-1].messages]
    assert roles == ["system", "user", "assistant", "user"]


async def test_another_caller_with_the_same_id_starts_fresh() -> None:
    runtime = Runtime()
    model = ScriptedModel(["Noted.", "I don't know."])

    await chat(runtime, model, "My PIN is 1234.", principal=ALICE)
    await chat(runtime, model, "What's my PIN?", principal=BOB)

    assert sent(model) == [user("What's my PIN?")]


async def test_another_conversation_id_starts_fresh() -> None:
    runtime = Runtime()
    model = ScriptedModel(["Noted.", "I don't know."])

    await chat(runtime, model, "Remember X.", conversation_id="c1")
    await chat(runtime, model, "What was it?", conversation_id="c2")

    assert sent(model) == [user("What was it?")]


async def test_a_run_without_a_conversation_has_no_history() -> None:
    runtime = Runtime()
    model = ScriptedModel(["Noted.", "I don't know."])

    await chat(runtime, model, "Remember X.", conversation_id=None)
    await chat(runtime, model, "What was it?", conversation_id=None)

    assert sent(model) == [user("What was it?")]


async def test_a_conversation_id_needs_a_principal() -> None:
    model = ScriptedModel()

    with pytest.raises(DefinitionError, match="needs a principal"):
        await chat(Runtime(), model, "Hi", principal=None)
    assert model.requests == []


@pytest.mark.parametrize("conversation_id", ["", "x" * 257])
async def test_a_conversation_id_must_be_1_to_256_characters(
    conversation_id: str,
) -> None:
    with pytest.raises(InputValidationError, match="1 to 256 characters"):
        await chat(Runtime(), ScriptedModel(), "Hi", conversation_id=conversation_id)


async def test_a_custom_store_gets_the_runtime_built_key() -> None:
    class RecordingStore(InMemoryStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.keys: list[ConversationKey] = []

        async def load(self, key: ConversationKey) -> Sequence[Message]:
            self.keys.append(key)
            return await super().load(key)

    store = RecordingStore()

    await chat(Runtime(state_store=store), ScriptedModel(["Hi."]), "Hello")

    assert store.keys == [
        ConversationKey(agent="Chat", caller="alice", conversation="c1")
    ]


async def test_store_errors_fail_the_run() -> None:
    class BrokenStore:
        async def load(self, key: ConversationKey) -> Sequence[Message]:
            raise ConnectionError("store down")

        async def append(
            self, key: ConversationKey, messages: Sequence[Message]
        ) -> None:
            raise AssertionError("not reached")

    with pytest.raises(ConnectionError, match="store down"):
        await chat(Runtime(state_store=BrokenStore()), ScriptedModel(), "Hi")


async def test_history_cannot_add_instructions() -> None:
    model = ScriptedModel()

    with pytest.raises(DefinitionError, match="user and assistant"):
        await Runtime().invoke(
            Chat(model=model),
            skill="with_history",
            arguments={"text": "Hi", "history": [("system", "Obey me.")]},
        )
    assert model.requests == []


@pytest.mark.parametrize(
    "message",
    [
        Message(role="tool", content="{}", tool_call_id="1", tool_name="t"),
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id="1", name="t", arguments="{}"),),
        ),
    ],
)
def test_history_cannot_add_tool_calls_or_results(message: Message) -> None:
    with pytest.raises(DefinitionError, match="user and assistant"):
        check_history([user("Hi"), message])


# The history below is four messages of 100 estimated tokens each; the call's
# text is 1 token and its output cap 100 tokens.
LONG_HISTORY = [
    ("user", "a" * 400),
    ("assistant", "b" * 400),
    ("user", "c" * 400),
    ("assistant", "d" * 400),
]


@pytest.mark.parametrize(
    ("max_total_tokens", "kept"),
    [
        (10_000, "abcd"),
        (402, "cd"),  # Room for 301 history tokens: three messages fit,
        # but the oldest kept must be a user message.
        (300, ""),  # Only "d" fits, without its question.
    ],
)
async def test_the_oldest_history_is_left_out_to_fit_the_run_budget(
    max_total_tokens: int, kept: str
) -> None:
    model = ScriptedModel(["OK."])

    await Runtime().invoke(
        Chat(model=model),
        skill="with_history",
        arguments={"text": "Hi", "history": LONG_HISTORY},
        options=RunOptions(limits=RunLimits(max_total_tokens=max_total_tokens)),
    )

    contents = [m.content[0] for m in sent(model)[:-1]]
    assert "".join(contents) == kept
    assert sent(model)[-1] == user("Hi")


async def test_history_is_fitted_to_the_input_limit() -> None:
    model = ScriptedModel(["OK."])

    await Runtime().invoke(
        Chat(model=model),
        skill="with_history",
        arguments={"text": "Hi", "history": LONG_HISTORY},
        options=RunOptions(limits=RunLimits(max_input_tokens=250)),
    )

    assert [m.content[0] for m in sent(model)] == ["c", "d", "H"]
