"""Read a remote agent's reply: its result, text answer, question or usage."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from a2a.helpers import get_data_parts
from a2a.types import Part, Task, TaskState

# protobuf ships no type stubs.
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError

from agentframe.errors import (
    ClarificationNeededError,
    OutputValidationError,
    RemoteAgentError,
)
from agentframe.usage import Usage


@dataclass(frozen=True)
class RemoteReply:
    """One finished remote request, for callers that need more than its value.

    Attributes:
        value: What ``ask`` or ``call`` (without ``output``) returns.
        task_id: The remote task's ID, or ``None`` when the agent answered
            with a message and started no task.
        usage: The usage charged to the run for the request, or ``None``
            when it is unknown: the peer reports none, or reported it
            malformed.
    """

    value: Any
    task_id: str | None
    usage: Usage | None


class ReplyReader:
    """Reads one agent's replies for a ``Remote`` binding, never trusting them.

    Errors name the agent and carry the run's invocation ID; none repeats
    the remote's untrusted text.

    Args:
        agent: How errors name the agent, such as ``"Agent 'research'"``.
        invocation_id: The run that owns the binding.
    """

    def __init__(self, agent: str, invocation_id: str) -> None:
        self._agent = agent
        self._invocation_id = invocation_id

    def result_parts(self, task: Task) -> list[Part]:
        """Return a completed task's result parts, or raise for any other end.

        The parts are those of its artifacts, or of its final message when it
        has no artifacts.
        """
        state = task.status.state
        if state == TaskState.TASK_STATE_COMPLETED:
            parts = [part for artifact in task.artifacts for part in artifact.parts]
            return parts or list(task.status.message.parts)
        if state == TaskState.TASK_STATE_INPUT_REQUIRED:
            raise ClarificationNeededError(
                f"{self._agent} needs more input before it can answer",
                question="\n".join(_texts(task.status.message.parts)),
                task_id=task.id,
                context_id=task.context_id,
                invocation_id=self._invocation_id,
            )
        # The remote status message is not repeated: it is untrusted text.
        raise RemoteAgentError(
            f"{self._agent} did not complete its task",
            task_id=task.id,
            task_state=TaskState.Name(state),
            invocation_id=self._invocation_id,
        )

    def text(self, parts: Iterable[Part], task: Task | None = None) -> str:
        parts = list(parts)
        if not parts or any(part.WhichOneof("content") != "text" for part in parts):
            raise RemoteAgentError(
                f"{self._agent} did not answer with text",
                task_id=task.id if task else None,
                task_state=TaskState.Name(task.status.state) if task else None,
                invocation_id=self._invocation_id,
            )
        return "\n".join(_texts(parts))

    def result_value(self, parts: Iterable[Part], task: Task | None = None) -> Any:
        """Decode the one part an AgentFrame host sends as a skill's result.

        A ``str`` result is a text part; anything else a data part
        ``{"value": ...}``.
        """
        parts = list(parts)
        if len(parts) == 1:
            part = parts[0]
            if part.HasField("text"):
                return part.text
            if part.HasField("data"):
                [data] = get_data_parts([part])
                if isinstance(data, dict) and data.keys() == {"value"}:
                    return data["value"]
        raise RemoteAgentError(
            f"{self._agent} did not answer with a skill result",
            task_id=task.id if task else None,
            task_state=TaskState.Name(task.status.state) if task else None,
            invocation_id=self._invocation_id,
        )

    def validated(self, value: Any, output: type[Any]) -> Any:
        try:
            return TypeAdapter(output).validate_python(value)
        except ValidationError:
            # The validation details would repeat untrusted remote data.
            raise OutputValidationError(
                f"{self._agent} returned a result that does not match the "
                "requested output type",
                invocation_id=self._invocation_id,
            ) from None


def reported_usage(task: Task) -> Usage | None:
    """Read the usage snapshot an AgentFrame agent puts in its task's metadata.

    The snapshot is the peer's claim: it is charged to the run, never used
    for authorization.

    Returns:
        The usage, or ``None`` when the snapshot is missing, malformed or its
        total is unknown.
    """
    snapshot = MessageToDict(task.metadata).get("usage")
    if not isinstance(snapshot, dict):
        return None
    counts = [
        _token_count(snapshot.get(name))
        for name in ("input_tokens", "output_tokens", "total_tokens")
    ]
    input_tokens, output_tokens, total_tokens = counts
    estimated = snapshot.get("estimated")
    if total_tokens is None or not isinstance(estimated, bool):
        return None
    return Usage(input_tokens, output_tokens, total_tokens, estimated)


def _token_count(value: object) -> int | None:
    """Return a whole, non-negative token count, or ``None`` for anything else.

    JSON numbers arrive as floats; ``bool`` is an ``int`` but not a count.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not float(value).is_integer() or value < 0:
        return None
    return int(value)


def _texts(parts: Iterable[Part]) -> list[str]:
    return [part.text for part in parts if part.WhichOneof("content") == "text"]
