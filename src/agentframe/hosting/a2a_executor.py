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
  creates one).
- A message without the extension is free text. The application's
  ``SkillRouter`` selects the skill and its arguments from the task's
  conversation, or asks the caller a question; without a router free text is
  refused before a task exists. Routing time counts against the run's timeout,
  and the router's model calls (through ``RoutingContext.generate``) are
  charged to the same run as the selected skill.
- A result becomes the artifact of a ``COMPLETED`` task: a ``str`` is one text
  part, anything else one data part ``{"value": ...}`` in Pydantic JSON mode.
- Unknown skills, invalid arguments and denied permissions end the task in
  ``REJECTED``; every other failure ends it in ``FAILED``; ``CancelTask`` ends
  it in ``CANCELED`` through AgentFrame's ``CancelledError``.
- A result or question larger than the host's ``max_response_bytes`` ends the
  task in ``FAILED`` with ``LimitExceededError``; none of it is sent.
- A router's question leaves the task in ``INPUT_REQUIRED``; the caller answers
  with a new message in the same task, which is routed again with the earlier
  messages.
- Router exceptions and timeouts end the task in ``FAILED``; invalid router
  decisions in ``REJECTED``. ``AgentFrameError``s raised while routing, such
  as limit, refusal and provider errors, keep their safe messages; any other
  router exception gets a generic one.

Every task status carries the run's ``invocation_id`` and a ``usage``
snapshot in its metadata: ``input_tokens``, ``output_tokens``,
``total_tokens`` (``null`` when unknown), ``estimated``, ``model_calls``
and ``tool_calls``.
A cancelled task reports the usage its run had reached when it was cancelled.

Every run that ends ``FAILED`` is logged as a warning with its invocation ID
and safe message; skill and router exceptions are logged with their traceback.
Arguments, prompts and results are never logged.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from a2a.helpers import get_data_parts, new_data_part, new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, Task, TaskState
from a2a.utils.errors import InvalidParamsError
from pydantic_core import to_json, to_jsonable_python

from agentframe.authoring.agent import Agent
from agentframe.errors import (
    AgentFrameError,
    CancelledError,
    DefinitionError,
    InputValidationError,
    LimitExceededError,
    OutputValidationError,
    PermissionDeniedError,
)
from agentframe.hosting.admission import RUN_SLOT_STATE_KEY, RunSlot
from agentframe.hosting.routing import (
    Clarification,
    SkillCall,
    SkillRouter,
    eligible_skills,
    new_routing_context,
    route,
)
from agentframe.hosting.skill_call import SKILL_CALL_EXTENSION_URI, SKILL_METADATA_KEY
from agentframe.runtime.generation import RunAccounting
from agentframe.runtime.limits import RunLimits
from agentframe.runtime.options import RunOptions
from agentframe.runtime.runtime import Runtime
from agentframe.security.principal import Principal

SKILL_METADATA_NAMESPACE = "agentframe."
"""Prefix of the message metadata keys the skill-call extension owns."""

PRINCIPAL_STATE_KEY = "agentframe.principal"
"""``ServerCallContext.state`` key holding the authenticated caller."""

SKILL_FAILED_MESSAGE = "The skill failed."
"""What callers see when a skill raises an exception that is not AgentFrame's."""

CANCELLED_MESSAGE = "The task was cancelled."
"""What callers see on a task cancelled with ``CancelTask``."""

HOST_MISCONFIGURED_MESSAGE = "The agent is not configured correctly."
"""What callers see for a ``DefinitionError``, whose message names internals."""

RESPONSE_TOO_LARGE_MESSAGE = "The result exceeds the host's response size limit"
"""What callers see when a result or question is over ``max_response_bytes``."""

logger = logging.getLogger(__name__)


def is_skill_call(requested_extensions: set[str]) -> bool:
    """Return whether a request activated the skill-call extension.

    Only such a request is a skill call; any other message is free text.

    Args:
        requested_extensions: Extension URIs the caller activated with the
            ``A2A-Extensions`` header.
    """
    return SKILL_CALL_EXTENSION_URI in requested_extensions


def read_skill_call(message: Message) -> SkillCall:
    """Read the skill id and JSON arguments of a skill-call extension message.

    Call it only for requests that activated the extension. The skill id comes
    from the ``agentframe.skill`` metadata key and the arguments from the
    message's only part, a data part holding a JSON object. Other metadata
    keys starting with ``agentframe.`` are unknown fields; keys outside that
    namespace belong to other extensions and are ignored.

    Args:
        message: The caller's message.

    Returns:
        The requested skill call; the runtime validates the skill id and
        arguments.

    Raises:
        InvalidParamsError: The message names no skill, carries unknown
            ``agentframe.`` metadata, or does not have exactly one part that is
            a JSON object data part. This is a protocol error, not a task
            failure, and the message is never reinterpreted as free text.
    """
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


@dataclass(frozen=True)
class _RunningTask:
    """What a cancel needs to know about a task whose run is going."""

    accounting: RunAccounting
    call_context: ServerCallContext


class RuntimeExecutor(AgentExecutor):
    """Executes each A2A message as one ``Runtime.run`` of the agent's skill.

    Args:
        agent: The hosted agent instance.
        runtime: The runtime that validates, authorizes and limits every run.
        router: Selects the skill for free-text messages. ``None`` means the
            request handler refuses free text before it reaches the executor.
        limits_ceiling: The host's ceiling, which caps the limits of every
            run and of routing. ``None`` caps nothing.
        max_response_bytes: The largest result or question a task may
            return, measured as by ``check_response_size``. ``None`` bounds
            nothing.
    """

    def __init__(
        self,
        agent: Agent,
        runtime: Runtime,
        router: SkillRouter | None = None,
        limits_ceiling: RunLimits | None = None,
        max_response_bytes: int | None = None,
    ) -> None:
        self._agent = agent
        self._runtime = runtime
        self._router = router
        self._limits_ceiling = limits_ceiling
        self._max_response_bytes = max_response_bytes
        # Each running task, so a cancel can report its usage and a shutdown
        # can cancel it on behalf of the caller who started it.
        self._running: dict[str, _RunningTask] = {}

    def running_tasks(self) -> dict[str, ServerCallContext]:
        """Return the ID and starting call context of every task now running.

        Tasks waiting for input have no run, so they are not included.
        """
        return {
            task_id: running.call_context for task_id, running in self._running.items()
        }

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run the requested or routed skill and publish the task's new state.

        A run the app admitted holds its ``RunSlot`` until it ends: completed,
        failed, cancelled, timed out or asking the caller a question.
        """
        slot: RunSlot | None = context.call_context.state.get(RUN_SLOT_STATE_KEY)
        if slot is not None:
            slot.start()
        try:
            await self._run_task(context, event_queue)
        finally:
            if slot is not None:
                slot.release()

    async def _run_task(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Start or resume the task, run it and record its accounting."""
        # The SDK always fills these in before calling the executor.
        task_id, context_id, message = (
            context.task_id,
            context.context_id,
            context.message,
        )
        assert task_id is not None and context_id is not None
        assert message is not None
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(
                    task_id,
                    context_id,
                    TaskState.TASK_STATE_SUBMITTED,
                    history=[message],
                )
            )
        task = TaskUpdater(event_queue, task_id, context_id)
        invocation_id = _new_invocation_id()
        accounting = RunAccounting()
        # Stored on the task now so a later cancel can report the same ID.
        await task.update_status(
            TaskState.TASK_STATE_WORKING,
            metadata=run_metadata(invocation_id, accounting),
        )
        self._running[task_id] = _RunningTask(accounting, context.call_context)
        try:
            await self._execute(context, task, invocation_id, accounting)
        finally:
            self._running.pop(task_id, None)

    async def _execute(
        self,
        context: RequestContext,
        task: TaskUpdater,
        invocation_id: str,
        accounting: RunAccounting,
    ) -> None:
        """Run the skill and publish its result, question or failure."""
        principal: Principal | None = context.call_context.state.get(
            PRINCIPAL_STATE_KEY
        )
        started_at = asyncio.get_running_loop().time()
        # noinspection PyBroadException
        try:
            call = await self._choose_skill(
                context, principal, started_at, invocation_id, accounting
            )
            if isinstance(call, Clarification):
                check_response_size(
                    call.question.encode(), self._max_response_bytes, invocation_id
                )
                await publish_input_required(
                    task, call.question, invocation_id, accounting
                )
                return
            # The executor shares the runtime's pipeline under its own invocation ID.
            # noinspection PyProtectedMember
            result = await self._runtime._run_invocation(
                invocation_id,
                self._agent,
                call.skill,
                call.arguments,
                principal,
                # Hosted callers cannot send per-run options in M3.
                RunOptions(),
                started_at,
                accounting,
                self._limits_ceiling,
            )
            part = result_part(result.value, invocation_id, self._max_response_bytes)
        except AgentFrameError as error:
            error.invocation_id = invocation_id
            _log_failed_run(error)
            await publish_failure(task, error, accounting)
            return
        except Exception:
            # Skill exceptions may carry submitted values or secrets, so the
            # caller gets a generic message and only the log keeps the details,
            # under the run's ID that the caller can quote.
            failure = AgentFrameError(SKILL_FAILED_MESSAGE, invocation_id=invocation_id)
            logger.exception("Skill failed (invocation_id=%s)", invocation_id)
            await publish_failure(task, failure, accounting)
            return
        await publish_result(task, part, invocation_id, accounting)

    async def _choose_skill(
        self,
        context: RequestContext,
        principal: Principal | None,
        started_at: float,
        invocation_id: str,
        accounting: RunAccounting,
    ) -> SkillCall | Clarification:
        """Read the skill call, or ask the router about the free-text message."""
        message = context.message
        assert message is not None
        if is_skill_call(context.requested_extensions):
            # The request handler has already refused malformed skill calls.
            return read_skill_call(message)
        # The request handler refuses free text when there is no router.
        assert self._router is not None
        conversation = [*_earlier_messages(context.current_task), message]
        definition = type(self._agent).definition()
        ctx = new_routing_context(
            self._agent,
            invocation_id,
            principal,
            started_at,
            accounting,
            self._limits_ceiling,
            # Routing requests pass the same model-request checks as the skill's.
            # noinspection PyProtectedMember
            self._runtime._policy,
        )
        return await route(
            self._router, conversation, eligible_skills(definition, principal), ctx
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Mark the task ``CANCELED``; the SDK then stops the running skill.

        The SDK calls this only for the caller's own unfinished task, then
        cancels the ``execute`` call. That ``asyncio`` cancellation unwinds
        the run, so per-run services are still cleaned up. The task reports
        the ``invocation_id`` its run started with and the usage it reached.
        """
        task_id, context_id = context.task_id, context.context_id
        assert task_id is not None and context_id is not None
        task = TaskUpdater(event_queue, task_id, context_id)
        invocation_id: str | None = None
        current_task = context.current_task
        if current_task is not None:
            stored = dict(current_task.metadata.items()).get("invocation_id")
            invocation_id = stored if isinstance(stored, str) else None
        # A task waiting for input has no running run, so it used nothing more.
        running = self._running.get(task_id)
        accounting = running.accounting if running else RunAccounting()
        await publish_failure(
            task,
            CancelledError(CANCELLED_MESSAGE, invocation_id=invocation_id),
            accounting,
        )


def result_part(value: Any, invocation_id: str, max_bytes: int | None = None) -> Part:
    """Serialize a skill's validated result as one A2A part.

    Args:
        value: The skill's validated return value.
        invocation_id: The run that produced it, for the error.
        max_bytes: The host's ``max_response_bytes``, checked against the
            UTF-8 text or the compact JSON of ``{"value": ...}``. ``None``
            bounds nothing.

    Returns:
        One text part for a ``str``; otherwise one data part ``{"value": ...}``
        holding the value in Pydantic JSON mode.

    Raises:
        OutputValidationError: The value cannot be represented as JSON.
        LimitExceededError: The serialized value is larger than ``max_bytes``.
    """
    if isinstance(value, str):
        check_response_size(value.encode(), max_bytes, invocation_id)
        return new_text_part(value)
    try:
        data = {"value": to_jsonable_python(value)}
    except (TypeError, ValueError):
        raise OutputValidationError(
            "The skill returned a value that cannot be serialized as JSON",
            invocation_id=invocation_id,
        ) from None
    check_response_size(to_json(data), max_bytes, invocation_id)
    return new_data_part(data)


def check_response_size(
    content: bytes, max_bytes: int | None, invocation_id: str
) -> None:
    """Fail when content to send the caller is larger than ``max_bytes``.

    Only the content is measured, not the JSON-RPC envelope around it.

    Args:
        content: A result's UTF-8 text or compact JSON, or a question's text.
        max_bytes: The host's ``max_response_bytes``; ``None`` bounds nothing.
        invocation_id: The run that produced the content, for the error.

    Raises:
        LimitExceededError: The content is larger than ``max_bytes``. The
            task fails instead; none of the content is sent or logged.
    """
    if max_bytes is not None and len(content) > max_bytes:
        raise LimitExceededError(
            RESPONSE_TOO_LARGE_MESSAGE, invocation_id=invocation_id
        )


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


async def publish_result(
    task: TaskUpdater, part: Part, invocation_id: str, accounting: RunAccounting
) -> None:
    """Publish a run's result as the task's artifact and complete the task."""
    await task.add_artifact([part], name="result")
    await task.update_status(
        TaskState.TASK_STATE_COMPLETED,
        metadata=run_metadata(invocation_id, accounting),
    )


async def publish_failure(
    task: TaskUpdater, error: AgentFrameError, accounting: RunAccounting
) -> None:
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
        metadata=run_metadata(error.invocation_id or _new_invocation_id(), accounting),
    )


async def publish_input_required(
    task: TaskUpdater, question: str, invocation_id: str, accounting: RunAccounting
) -> None:
    """Ask the caller a question, leaving the task in ``INPUT_REQUIRED``.

    Args:
        task: The task to update.
        question: Safe text telling the caller what is missing or ambiguous.
        invocation_id: The run that needs the answer.
        accounting: The run's usage so far.
    """
    message = task.new_agent_message([new_text_part(question)])
    await task.update_status(
        TaskState.TASK_STATE_INPUT_REQUIRED,
        message=message,
        metadata=run_metadata(invocation_id, accounting),
    )


def run_metadata(invocation_id: str, accounting: RunAccounting) -> dict[str, Any]:
    """Return the task metadata that identifies a run and reports its usage.

    Only safe values are included: the run's ID, token counts (``None`` when
    unknown, including while a model call is still running, as when a task is
    cancelled), whether any count is estimated, and the numbers of model
    and tool calls.
    """
    usage = accounting.current_usage()
    return {
        "invocation_id": invocation_id,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "estimated": usage.estimated,
            "model_calls": accounting.model_calls,
            "tool_calls": accounting.tool_calls,
        },
    }


def _earlier_messages(task: Task | None) -> list[Message]:
    """Return a task's messages before the current one, oldest first.

    The SDK keeps the agent's latest question in the task status, not yet in
    the history, and appends the caller's new message only after the run.
    """
    if task is None:
        return []
    earlier = list(task.history)
    if task.status.HasField("message"):
        earlier.append(task.status.message)
    return earlier


def _log_failed_run(error: AgentFrameError) -> None:
    """Log a run that ends ``FAILED``, so operators can find it by its ID.

    Only the error's class and safe message are logged: never its cause,
    which may hold arguments, prompts or provider payloads. Rejections are
    the caller's doing and are not logged; definition errors are logged by
    ``_safe_message``.
    """
    if isinstance(error, DefinitionError) or failure_state(error) != (
        TaskState.TASK_STATE_FAILED
    ):
        return
    logger.warning(
        "Run failed (invocation_id=%s): %s: %s",
        error.invocation_id,
        type(error).__name__,
        error.message,
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
