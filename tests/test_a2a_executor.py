"""Tests for mapping run outcomes to native A2A parts and task states.

Most rows of the mapping table are tested end to end in ``test_app.py``. The
rows whose triggers arrive in later stories (clarification from routing, #6;
cancellation from task operations, #8) are tested here, at the functions those
stories call.
"""

from datetime import date
from typing import Any

import pytest
from a2a.helpers import new_data_part, new_text_part
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState, TaskStatusUpdateEvent
from pydantic import BaseModel

from agentframe import (
    AgentFrameError,
    CancelledError,
    DefinitionError,
    IncompleteOutputError,
    InputValidationError,
    LimitExceededError,
    ModelError,
    ModelRefusalError,
    OutputValidationError,
    PermissionDeniedError,
    PolicyUnavailableError,
    ProviderError,
    UnsupportedCapabilityError,
)
from agentframe.a2a_executor import (
    failure_state,
    publish_failure,
    publish_input_required,
    result_part,
)
from agentframe.generation import RunAccounting


class RecordingQueue(EventQueue):
    """Collects the events a ``TaskUpdater`` publishes."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


def recording_task() -> tuple[TaskUpdater, RecordingQueue]:
    queue = RecordingQueue()
    return TaskUpdater(queue, "task-1", "context-1"), queue


def only_status(queue: RecordingQueue) -> TaskStatusUpdateEvent:
    [event] = queue.events
    assert isinstance(event, TaskStatusUpdateEvent)
    return event


class Point(BaseModel):
    x: int
    when: date


# Result serialization


def test_string_result_is_one_text_part() -> None:
    assert result_part("Hello", "run-1") == new_text_part("Hello")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(3, 3, id="int"),
        pytest.param(True, True, id="bool"),
        pytest.param(None, None, id="none"),
        pytest.param(["a", 1], ["a", 1], id="list"),
        pytest.param({"a": 1}, {"a": 1}, id="dict"),
        pytest.param(
            [Point(x=1, when=date(2026, 1, 2))],
            [{"x": 1, "when": "2026-01-02"}],
            id="nested-model-json-mode",
        ),
    ],
)
def test_other_results_are_one_data_part_holding_the_value(
    value: object, expected: object
) -> None:
    assert result_part(value, "run-1") == new_data_part({"value": expected})


def test_unserializable_result_is_an_output_validation_error() -> None:
    with pytest.raises(OutputValidationError, match="serialized") as raised:
        result_part(object(), "run-1")

    assert raised.value.invocation_id == "run-1"


# Failure states


@pytest.mark.parametrize(
    ("error_type", "state"),
    [
        (InputValidationError, TaskState.TASK_STATE_REJECTED),
        (PermissionDeniedError, TaskState.TASK_STATE_REJECTED),
        (LimitExceededError, TaskState.TASK_STATE_FAILED),
        (OutputValidationError, TaskState.TASK_STATE_FAILED),
        (PolicyUnavailableError, TaskState.TASK_STATE_FAILED),
        (AgentFrameError, TaskState.TASK_STATE_FAILED),
        (CancelledError, TaskState.TASK_STATE_CANCELED),
    ],
)
def test_failure_state_for_each_error(
    error_type: type[AgentFrameError], state: TaskState
) -> None:
    assert failure_state(error_type("safe")) == state


@pytest.mark.parametrize(
    "error",
    [
        UnsupportedCapabilityError("This agent has no model configured."),
        ModelRefusalError("The model refused the request."),
        IncompleteOutputError("The model's output was cut short."),
        ProviderError("The model provider is unavailable.", transient=True),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_model_errors_fail_the_task_with_their_safe_message(
    error: ModelError,
) -> None:
    task, queue = recording_task()

    await publish_failure(task, error, RunAccounting())

    status = only_status(queue).status
    assert status.state == TaskState.TASK_STATE_FAILED
    assert status.message.parts[0].text == error.message


async def test_cancelled_run_ends_the_task_canceled() -> None:
    task, queue = recording_task()

    await publish_failure(
        task, CancelledError("Cancelled.", invocation_id="run-1"), RunAccounting()
    )

    event = only_status(queue)
    assert event.status.state == TaskState.TASK_STATE_CANCELED
    assert event.metadata["invocation_id"] == "run-1"


async def test_failure_published_before_any_run_still_has_an_invocation_id() -> None:
    task, queue = recording_task()

    await publish_failure(task, AgentFrameError("The router failed."), RunAccounting())

    event = only_status(queue)
    assert event.status.state == TaskState.TASK_STATE_FAILED
    assert event.status.message.parts[0].text == "The router failed."
    assert event.metadata["invocation_id"]


async def test_definition_error_reaches_callers_only_as_a_generic_message() -> None:
    task, queue = recording_task()
    error = DefinitionError("no service registered for parameter 'db'")

    await publish_failure(task, error, RunAccounting())

    event = only_status(queue)
    assert event.status.state == TaskState.TASK_STATE_FAILED
    assert "db" not in event.status.message.parts[0].text


# Clarification


async def test_question_leaves_the_task_input_required() -> None:
    task, queue = recording_task()

    await publish_input_required(
        task, "Which city did you mean?", "run-1", RunAccounting()
    )

    event = only_status(queue)
    assert event.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert event.status.message.parts[0].text == "Which city did you mean?"
    assert event.metadata["invocation_id"] == "run-1"
