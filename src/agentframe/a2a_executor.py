"""Run A2A requests through the AgentFrame ``Runtime``.

The official A2A SDK owns the protocol: JSON-RPC parsing, the task store and
task lifecycle. This module is the bridge it calls for each message: read which
skill to run, run it through the ``Runtime`` and publish the outcome on the task.

Outcomes map to native A2A results as follows:

- Callers name a skill through the AgentFrame skill-call extension: they
  activate it with the ``A2A-Extensions`` HTTP header, put the skill id in
  message metadata under ``agentframe.skill`` and the arguments in exactly one
  JSON data part. A malformed skill call is a JSON-RPC ``InvalidParams`` error
  and creates no task (the app's request handler checks it before the SDK
  creates one). A message without the extension is free text, refused until
  routing arrives.
- A result becomes the artifact of a ``COMPLETED`` task: a ``str`` is one text
  part, anything else one data part ``{"value": ...}`` in Pydantic JSON mode.
- Unknown skills, invalid arguments and denied permissions end the task in
  ``REJECTED``; every other failure ends it in ``FAILED``; ``CancelTask`` ends
  it in ``CANCELED`` through AgentFrame's ``CancelledError``.
- A question for the caller leaves the task in ``INPUT_REQUIRED``.

Every task status carries the run's ``invocation_id`` in its metadata.

Free-text routing (#6) replaces the refusal in ``read_skill_call`` and publishes
its questions with ``publish_input_required``.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from a2a.helpers import get_data_parts, new_data_part, new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, TaskState
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
from pydantic_core import to_jsonable_python

from agentframe.agent import Agent
from agentframe.card import SKILL_CALL_EXTENSION_URI
from agentframe.errors import (
    AgentFrameError,
    CancelledError,
    DefinitionError,
    InputValidationError,
    OutputValidationError,
    PermissionDeniedError,
)
from agentframe.principal import Principal
from agentframe.runtime import Runtime

SKILL_METADATA_NAMESPACE = "agentframe."
"""Prefix of the message metadata keys the skill-call extension owns."""

SKILL_METADATA_KEY = "agentframe.skill"
"""Message metadata key that names the skill to run."""

PRINCIPAL_STATE_KEY = "agentframe.principal"
"""``ServerCallContext.state`` key holding the authenticated caller."""

SKILL_FAILED_MESSAGE = "The skill failed."
"""What callers see when a skill raises an exception that is not AgentFrame's."""

CANCELLED_MESSAGE = "The task was cancelled."
"""What callers see on a task cancelled with ``CancelTask``."""

HOST_MISCONFIGURED_MESSAGE = "The agent is not configured correctly."
"""What callers see for a ``DefinitionError``, whose message names internals."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillCall:
    """The skill a request asks for and its unvalidated arguments."""

    skill: str
    arguments: dict[str, Any]


def read_skill_call(message: Message, requested_extensions: set[str]) -> SkillCall:
    """Read the skill id and JSON arguments of a skill-call extension message.

    Only a request that activates the skill-call extension is a skill call.
    The skill id comes from the ``agentframe.skill`` metadata key and the
    arguments from the message's only part, a data part holding a JSON object.
    Other metadata keys starting with ``agentframe.`` are unknown fields; keys
    outside that namespace belong to other extensions and are ignored.

    Args:
        message: The caller's message.
        requested_extensions: Extension URIs the caller activated with the
            ``A2A-Extensions`` header.

    Returns:
        The requested skill call; the runtime validates the skill id and
        arguments.

    Raises:
        UnsupportedOperationError: The caller did not activate the extension,
            so the message is free text. Free-text routing arrives with #6.
        InvalidParamsError: The message names no skill, carries unknown
            ``agentframe.`` metadata, or does not have exactly one part that is
            a JSON object data part. This is a protocol error, not a task
            failure, and the message is never reinterpreted as free text.
    """
    if SKILL_CALL_EXTENSION_URI not in requested_extensions:
        raise UnsupportedOperationError(
            "Free-text requests are not supported yet. Activate the "
            f"'{SKILL_CALL_EXTENSION_URI}' extension and name a skill."
        )
    metadata = dict(message.metadata.items())
    unknown = sorted(
        key
        for key in metadata
        if key.startswith(SKILL_METADATA_NAMESPACE) and key != SKILL_METADATA_KEY
    )
    if unknown:
        raise InvalidParamsError(
            f"Unknown skill-call metadata fields: {', '.join(unknown)}."
        )
    skill_id = metadata.get(SKILL_METADATA_KEY)
    if not isinstance(skill_id, str) or not skill_id:
        raise InvalidParamsError(
            f"A skill call must name a skill in metadata '{SKILL_METADATA_KEY}'."
        )
    data = get_data_parts(message.parts)
    if len(message.parts) != 1 or len(data) != 1 or not isinstance(data[0], dict):
        raise InvalidParamsError(
            "A skill call must have exactly one part: a data part holding a "
            "JSON object of arguments."
        )
    return SkillCall(skill_id, data[0])


class RuntimeExecutor(AgentExecutor):
    """Executes each A2A message as one ``Runtime.run`` of the agent's skill.

    Args:
        agent: The hosted agent instance.
        runtime: The runtime that validates, authorizes and limits every run.
    """

    def __init__(self, agent: Agent, runtime: Runtime) -> None:
        self._agent = agent
        self._runtime = runtime

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run the requested skill and publish the task's final state."""
        # The SDK always fills these in before calling the executor.
        assert context.task_id is not None and context.context_id is not None
        assert context.message is not None
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(
                    context.task_id,
                    context.context_id,
                    TaskState.TASK_STATE_SUBMITTED,
                    history=[context.message],
                )
            )
        task = TaskUpdater(event_queue, context.task_id, context.context_id)
        principal: Principal | None = context.call_context.state.get(
            PRINCIPAL_STATE_KEY
        )
        # The request handler has already refused malformed requests.
        call = read_skill_call(context.message, context.requested_extensions)
        invocation_id = _new_invocation_id()
        # Stored on the task now so a later cancel can report the same ID.
        await task.update_status(
            TaskState.TASK_STATE_WORKING, metadata=_invocation_metadata(invocation_id)
        )
        try:
            result = await self._runtime._run_invocation(
                invocation_id, self._agent, call.skill, call.arguments, principal
            )
            part = result_part(result.value, invocation_id)
        except AgentFrameError as error:
            await publish_failure(task, error)
            return
        except Exception:
            # Skill exceptions may carry submitted values or secrets, so the
            # caller gets a generic message and only the log keeps the details,
            # under the run's ID that the caller can quote.
            failure = AgentFrameError(SKILL_FAILED_MESSAGE, invocation_id=invocation_id)
            logger.exception("Skill failed (invocation_id=%s)", invocation_id)
            await publish_failure(task, failure)
            return
        await publish_result(task, part, invocation_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Mark the task ``CANCELED``; the SDK then stops the running skill.

        The SDK calls this only for the caller's own unfinished task, then
        cancels the ``execute`` call. That ``asyncio`` cancellation unwinds
        the run, so per-run services are still cleaned up. The task reports
        the ``invocation_id`` its run started with.
        """
        assert context.task_id is not None and context.context_id is not None
        task = TaskUpdater(event_queue, context.task_id, context.context_id)
        invocation_id = None
        if context.current_task is not None:
            metadata = dict(context.current_task.metadata.items())
            invocation_id = metadata.get("invocation_id")
        await publish_failure(
            task, CancelledError(CANCELLED_MESSAGE, invocation_id=invocation_id)
        )


def result_part(value: Any, invocation_id: str) -> Part:
    """Serialize a skill's validated result as one A2A part.

    Args:
        value: The skill's validated return value.
        invocation_id: The run that produced it, for the error.

    Returns:
        One text part for a ``str``; otherwise one data part ``{"value": ...}``
        holding the value in Pydantic JSON mode.

    Raises:
        OutputValidationError: The value cannot be represented as JSON.
    """
    if isinstance(value, str):
        return new_text_part(value)
    try:
        return new_data_part({"value": to_jsonable_python(value)})
    except (TypeError, ValueError):
        raise OutputValidationError(
            "The skill returned a value that cannot be serialized as JSON",
            invocation_id=invocation_id,
        ) from None


def failure_state(error: AgentFrameError) -> TaskState:
    """Return the A2A task state that reports ``error``.

    Args:
        error: Why the run did not complete.

    Returns:
        ``REJECTED`` when the request itself was refused (unknown skill,
        invalid arguments, permission or policy denied), ``CANCELED`` for a
        cancelled run, and ``FAILED`` for everything else.
    """
    if isinstance(error, CancelledError):
        return TaskState.TASK_STATE_CANCELED
    if isinstance(error, InputValidationError | PermissionDeniedError):
        return TaskState.TASK_STATE_REJECTED
    return TaskState.TASK_STATE_FAILED


async def publish_result(task: TaskUpdater, part: Part, invocation_id: str) -> None:
    """Publish a run's result as the task's artifact and complete the task."""
    await task.add_artifact([part], name="result")
    await task.update_status(
        TaskState.TASK_STATE_COMPLETED, metadata=_invocation_metadata(invocation_id)
    )


async def publish_failure(task: TaskUpdater, error: AgentFrameError) -> None:
    """End the task in the state for ``error`` with its safe message.

    A ``DefinitionError`` names agent internals, so it is logged and callers
    get a generic message instead. An error without an invocation ID (one
    raised before any run started) is given a new one, so every task reports
    an ID.
    """
    message = task.new_agent_message([new_text_part(_safe_message(error))])
    await task.update_status(
        failure_state(error),
        message=message,
        metadata=_invocation_metadata(error.invocation_id or _new_invocation_id()),
    )


async def publish_input_required(
    task: TaskUpdater, question: str, invocation_id: str
) -> None:
    """Ask the caller a question, leaving the task in ``INPUT_REQUIRED``.

    Args:
        task: The task to update.
        question: Safe text telling the caller what is missing or ambiguous.
        invocation_id: The run that needs the answer.
    """
    message = task.new_agent_message([new_text_part(question)])
    await task.update_status(
        TaskState.TASK_STATE_INPUT_REQUIRED,
        message=message,
        metadata=_invocation_metadata(invocation_id),
    )


def _safe_message(error: AgentFrameError) -> str:
    if isinstance(error, DefinitionError):
        logger.error(
            "Agent definition error (invocation_id=%s): %s",
            error.invocation_id,
            error.message,
        )
        return HOST_MISCONFIGURED_MESSAGE
    return error.message


def _new_invocation_id() -> str:
    return str(uuid.uuid4())


def _invocation_metadata(invocation_id: str) -> dict[str, Any]:
    return {"invocation_id": invocation_id}
