"""Run A2A requests through the AgentFrame ``Runtime``.

The official A2A SDK owns the protocol: JSON-RPC parsing, the task store and
task lifecycle. This module is the bridge it calls for each message: read which
skill to run, run it through ``Runtime.run`` and publish the outcome on the task.

Only the minimal path exists so far. Later M2 stories replace the marked seams:
``read_skill_call`` gains extension activation and protocol errors (#5) plus
free-text routing (#6); ``_publish_result`` and ``_publish_failure`` gain the
full result and error mapping (#7); cancellation arrives with task operations
(#8).
"""

import logging
from dataclasses import dataclass
from typing import Any

from a2a.helpers import get_data_parts, new_data_part, new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, TaskState
from pydantic_core import to_jsonable_python

from agentframe.agent import Agent
from agentframe.errors import AgentFrameError, InputValidationError
from agentframe.principal import Principal
from agentframe.runtime import RunResult, Runtime

SKILL_METADATA_KEY = "agentframe.skill"
"""Message metadata key that names the skill to run."""

PRINCIPAL_STATE_KEY = "agentframe.principal"
"""``ServerCallContext.state`` key holding the authenticated caller."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillCall:
    """The skill a request asks for and its unvalidated arguments."""

    skill: str
    arguments: dict[str, Any]


def read_skill_call(message: Message) -> SkillCall:
    """Read the skill id and JSON arguments from an A2A message.

    The skill id comes from the ``agentframe.skill`` metadata key and the
    arguments from the message's single data part (none means no arguments).

    Args:
        message: The caller's message.

    Returns:
        The requested skill call; the runtime validates it.

    Raises:
        InputValidationError: The message names no skill, or its arguments are
            not one JSON object.
    """
    skill_id = dict(message.metadata.items()).get(SKILL_METADATA_KEY)
    if not isinstance(skill_id, str) or not skill_id:
        raise InputValidationError(
            f"The message must name a skill in metadata '{SKILL_METADATA_KEY}'."
        )
    data = get_data_parts(message.parts)
    if not data:
        return SkillCall(skill_id, {})
    if len(data) > 1 or not isinstance(data[0], dict):
        raise InputValidationError(
            "Skill arguments must be a single JSON object data part."
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
        try:
            call = read_skill_call(context.message)
            await task.start_work()
            result = await self._runtime.run(
                self._agent,
                skill=call.skill,
                arguments=call.arguments,
                principal=principal,
            )
        except AgentFrameError as error:
            await _publish_failure(task, str(error), error.invocation_id)
            return
        except Exception:
            # Skill exceptions may carry submitted values or secrets, so the
            # caller gets a generic message and only the log keeps the details.
            logger.exception("Skill execution failed")
            await _publish_failure(task, "The skill failed.", None)
            return
        await _publish_result(task, result)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancellation is not supported yet (#8)."""
        raise NotImplementedError("Task cancellation is not supported yet.")


async def _publish_result(task: TaskUpdater, result: RunResult[Any]) -> None:
    await task.add_artifact([_result_part(result.value)], name="result")
    await task.update_status(
        TaskState.TASK_STATE_COMPLETED,
        metadata=_invocation_metadata(result.invocation_id),
    )


async def _publish_failure(
    task: TaskUpdater, safe_message: str, invocation_id: str | None
) -> None:
    message = task.new_agent_message([new_text_part(safe_message)])
    await task.update_status(
        TaskState.TASK_STATE_FAILED,
        message=message,
        metadata=_invocation_metadata(invocation_id),
    )


def _result_part(value: Any) -> Part:
    if isinstance(value, str):
        return new_text_part(value)
    return new_data_part({"value": to_jsonable_python(value)})


def _invocation_metadata(invocation_id: str | None) -> dict[str, Any] | None:
    if invocation_id is None:
        return None
    return {"invocation_id": invocation_id}
