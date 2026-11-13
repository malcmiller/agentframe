"""``Remote[Literal["name"]]``: a skill's binding to another A2A agent."""

import asyncio
import logging
import uuid
from collections.abc import Coroutine, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Literal, TypeAliasType, get_args, get_origin, overload

from a2a.client import ClientCallContext
from a2a.client.errors import A2AClientTimeoutError
from a2a.client.transports import JsonRpcTransport
from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.helpers import get_data_parts, new_data_part, new_text_part
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, VERSION_HEADER
from a2a.utils.errors import A2AError

# protobuf ships no type stubs; ParseError is what the SDK raises for bad replies.
from google.protobuf.json_format import ParseError  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from agentframe.discovery.directory import AgentDirectory, DiscoveredAgent
from agentframe.errors import (
    ClarificationNeededError,
    DefinitionError,
    OutputValidationError,
    RemoteAgentError,
    TimeoutLimitExceededError,
)
from agentframe.hosting.skill_call import SKILL_CALL_EXTENSION_URI, SKILL_METADATA_KEY
from agentframe.security.principal import Principal

logger = logging.getLogger(__name__)

_FIRST_POLL_SECONDS = 0.25
_MAX_POLL_SECONDS = 1.0
_CANCEL_SECONDS = 2.0
"""How long a best-effort ``CancelTask`` may take; shutdown waits 5 seconds."""

_cancel_requests: set[asyncio.Task[None]] = set()
"""Cancellations still being sent; see ``Remote._in_background``."""

_RESERVED_ARGUMENT = "max_total_tokens"
"""A ``call`` keyword reserved for the per-call token ceiling (#90)."""

_UNFINISHED_STATES = (TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING)


class Remote[N: str]:
    """Another A2A agent that a skill can send work to.

    Declare it as a skill parameter, naming the agent in a ``Literal``;
    a ``type`` alias per remote keeps signatures short::

        type Research = Remote[Literal["research-agent"]]


        @skill(id="plan", name="Plan", description="Research a goal.", tags=["plan"])
        async def plan(self, goal: str, research: Research, ctx: RunContext) -> str:
            return await research.ask("Research " + goal)

    Like ``Inject[T]``, the parameter is never a caller argument and never
    appears in the Agent Card or any schema. The runtime looks the name up in
    its ``AgentDirectory`` and passes a new binding to every run; it is not
    meant to be constructed by application code.

    Args:
        found: The agent and its validated card, from the directory.
        transport: The SDK JSON-RPC client for ``found.interface_url``.
        deadline: Event-loop time when the run that owns the binding times out.
        invocation_id: The run that owns the binding.
        principal: The run's verified caller, kept for outbound identity.
    """

    def __init__(
        self,
        found: DiscoveredAgent,
        transport: JsonRpcTransport,
        *,
        deadline: float,
        invocation_id: str,
        principal: Principal | None,
    ) -> None:
        self._found = found
        self._transport = transport
        self._deadline = deadline
        self._invocation_id = invocation_id
        self._principal = principal
        self._agent = f"Agent '{found.reference.name}'"

    @property
    def name(self) -> str:
        """The agent's name in the directory."""
        return self._found.reference.name

    async def ask(self, text: str) -> str:
        """Send plain text to the agent and return its text answer.

        The text goes as one native A2A text part, so any A2A 1.0 agent can
        answer it. The answer is untrusted content, never instructions or
        identity. Waits no longer than the run's deadline; if the run times
        out or is cancelled while waiting, the remote task is cancelled
        (best effort) and the timeout or cancellation goes on.

        Args:
            text: The request. Untrusted as far as the remote agent knows.

        Returns:
            The text parts of the completed task's artifacts (or of its final
            message, when it has no artifacts), joined by newlines.

        Raises:
            ClarificationNeededError: The remote agent asked a question.
                Nothing answers it automatically.
            RemoteAgentError: The agent could not be reached, refused the
                request, ended its task in any state but completed, or
                answered with something other than text.
            TimeoutLimitExceededError: The run's deadline passed first.
        """
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_text_part(text)],
        )
        reply = await self._start(message)
        if isinstance(reply, Message):
            return self._text(reply.parts)
        task = await self._wait(reply)
        return self._text(self._result_parts(task), task)

    @overload
    async def call[T](
        self, skill: str, /, *, output: type[T], **arguments: Any
    ) -> T: ...

    @overload
    async def call(self, skill: str, /, **arguments: Any) -> Any: ...

    async def call(
        self, skill: str, /, *, output: type[Any] | None = None, **arguments: Any
    ) -> Any:
        """Run one of the agent's skills by ID and return its result.

        The call uses AgentFrame's skill-call extension, so the peer runs
        exactly ``skill`` with ``arguments``; it never falls back to free text.
        Waiting, deadline, cancellation and question handling match ``ask``.

        Args:
            skill: The skill's ID on the agent's card.
            output: The type to validate the result into, such as
                ``list[Source]``. Without it the result is returned as decoded
                JSON (numbers come back as ``float``) or as ``str`` for a text
                result.
            **arguments: The skill's arguments, sent as one JSON object in
                Pydantic JSON mode, so Pydantic models and dates work.
                ``output`` and ``max_total_tokens`` are reserved and cannot be
                remote argument names.

        Returns:
            The skill's result, validated as ``output`` when given.

        Raises:
            RemoteAgentError: The card does not declare the skill-call
                extension or the skill (nothing is sent), or the agent could
                not be reached, refused the request (for example invalid
                arguments), did not complete its task or sent no result.
            OutputValidationError: The result does not match ``output``.
            DefinitionError: ``max_total_tokens`` was passed; it is reserved
                for a per-call token ceiling that is not implemented yet.
            ClarificationNeededError: The remote agent asked a question.
            TimeoutLimitExceededError: The run's deadline passed first.
            pydantic_core.PydanticSerializationError: An argument cannot be
                represented as JSON. Nothing is sent.
        """
        if _RESERVED_ARGUMENT in arguments:
            raise DefinitionError(
                f"'{_RESERVED_ARGUMENT}' is reserved for Remote.call's per-call "
                "token ceiling, which is not implemented yet",
                invocation_id=self._invocation_id,
            )
        self._check_card_offers(skill)
        message = self._skill_call_message(skill, arguments)
        reply = await self._start(message, extensions=[SKILL_CALL_EXTENSION_URI])
        if isinstance(reply, Message):
            value = self._result_value(reply.parts)
        else:
            task = await self._wait(reply)
            value = self._result_value(self._result_parts(task), task)
        return value if output is None else self._validated(value, output)

    def _check_card_offers(self, skill: str) -> None:
        card = self._found.card
        uris = {extension.uri for extension in card.capabilities.extensions}
        if SKILL_CALL_EXTENSION_URI not in uris:
            raise RemoteAgentError(
                f"{self._agent} does not declare the skill-call extension "
                f"'{SKILL_CALL_EXTENSION_URI}'",
                invocation_id=self._invocation_id,
            )
        if skill not in {card_skill.id for card_skill in card.skills}:
            raise RemoteAgentError(
                f"{self._agent} has no skill '{skill}' on its card",
                invocation_id=self._invocation_id,
            )

    def _skill_call_message(self, skill: str, arguments: dict[str, Any]) -> Message:
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_data_part(to_jsonable_python(arguments))],
        )
        message.metadata.update({SKILL_METADATA_KEY: skill})
        return message

    def _result_value(self, parts: Iterable[Part], task: Task | None = None) -> Any:
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

    def _validated(self, value: Any, output: type[Any]) -> Any:
        try:
            return TypeAdapter(output).validate_python(value)
        except ValidationError:
            # The validation details would repeat untrusted remote data.
            raise OutputValidationError(
                f"{self._agent} returned a result that does not match the "
                "requested output type",
                invocation_id=self._invocation_id,
            ) from None

    async def _start(
        self, message: Message, *, extensions: Sequence[str] = ()
    ) -> Task | Message:
        """Send the message; if the run stops meanwhile, cancel what it started.

        The peer may start the task before its reply arrives, so a cancelled
        send still finishes in the background to learn the task ID.

        Args:
            message: The message to send.
            extensions: Extension URIs to activate for this request only.
        """
        sending = asyncio.create_task(self._send(message, extensions))
        try:
            return await asyncio.shield(sending)
        except asyncio.CancelledError:
            await self._in_background(self._cancel_when_started(sending))
            raise

    async def _send(
        self, message: Message, extensions: Sequence[str]
    ) -> Task | Message:
        # Returning at once gives the task ID needed to cancel it later.
        request = SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(return_immediately=True),
        )
        with self._translated_errors():
            response = await self._transport.send_message(
                request, context=self._call_context(extensions)
            )
        if response.HasField("task"):
            return response.task
        if response.HasField("message"):
            return response.message
        raise RemoteAgentError(
            f"{self._agent} sent an empty reply", invocation_id=self._invocation_id
        )

    async def _wait(self, task: Task) -> Task:
        """Poll the task until it stops working; cancel it if the run stops first.

        The run's own deadline cancels the wait; every poll is also bounded
        by the time that remains.
        """
        delays = poll_delays()
        try:
            while task.status.state in _UNFINISHED_STATES:
                await asyncio.sleep(next(delays))
                with self._translated_errors(task.id):
                    task = await self._transport.get_task(
                        GetTaskRequest(id=task.id), context=self._call_context()
                    )
        except (asyncio.CancelledError, TimeoutLimitExceededError):
            await self._in_background(self._cancel(task.id))
            raise
        return task

    async def _in_background(self, cancelling: Coroutine[Any, Any, None]) -> None:
        """Run a cancellation to the end, even if the run is cancelled again.

        It runs as its own task, bounded by ``_CANCEL_SECONDS``; a second
        cancellation stops only the waiting for it.
        """
        task = asyncio.create_task(cancelling)
        # The event loop keeps only weak references to tasks; this one must
        # survive the run that started it.
        _cancel_requests.add(task)
        task.add_done_callback(_cancel_requests.discard)
        await asyncio.shield(task)

    async def _cancel_when_started(self, sending: asyncio.Task[Task | Message]) -> None:
        try:
            async with asyncio.timeout(_CANCEL_SECONDS):
                reply = await sending
        except (RemoteAgentError, TimeoutLimitExceededError, TimeoutError):
            return
        if isinstance(reply, Task) and reply.status.state in _UNFINISHED_STATES:
            await self._cancel(reply.id)

    async def _cancel(self, task_id: str) -> None:
        """Ask the agent to cancel ``task_id``; a failure is only logged."""
        context = ClientCallContext(
            timeout=_CANCEL_SECONDS,
            service_parameters={VERSION_HEADER: PROTOCOL_VERSION_1_0},
        )
        try:
            async with asyncio.timeout(_CANCEL_SECONDS):
                await self._transport.cancel_task(
                    CancelTaskRequest(id=task_id), context=context
                )
        except (A2AError, ParseError, TimeoutError):
            logger.warning(
                "%s did not accept the cancellation of its task (invocation_id=%s)",
                self._agent,
                self._invocation_id,
            )

    def _result_parts(self, task: Task) -> list[Part]:
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

    def _text(self, parts: Iterable[Part], task: Task | None = None) -> str:
        parts = list(parts)
        if not parts or any(part.WhichOneof("content") != "text" for part in parts):
            raise RemoteAgentError(
                f"{self._agent} did not answer with text",
                task_id=task.id if task else None,
                task_state=TaskState.Name(task.status.state) if task else None,
                invocation_id=self._invocation_id,
            )
        return "\n".join(_texts(parts))

    def _call_context(self, extensions: Sequence[str] = ()) -> ClientCallContext:
        """Bound one request by the run's remaining time.

        Headers go per request, never into the shared client's defaults.

        Args:
            extensions: Extension URIs the request activates.
        """
        remaining = self._deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise self._timeout()
        headers = {VERSION_HEADER: PROTOCOL_VERSION_1_0}
        if extensions:
            headers[HTTP_EXTENSION_HEADER] = ",".join(extensions)
        return ClientCallContext(timeout=remaining, service_parameters=headers)

    @contextmanager
    def _translated_errors(self, task_id: str | None = None) -> Iterator[None]:
        """Turn SDK errors into AgentFrame's, without the remote's text.

        Args:
            task_id: The remote task the request is about, if any; a poll
                that finds it unknown reports it.
        """
        try:
            yield
        except A2AClientTimeoutError:
            raise self._timeout() from None
        except (A2AError, ParseError) as error:
            raise RemoteAgentError(
                f"{self._agent} could not be reached or refused the request",
                task_id=task_id,
                invocation_id=self._invocation_id,
            ) from error

    def _timeout(self) -> TimeoutLimitExceededError:
        return TimeoutLimitExceededError(
            f"{self._agent} did not answer before the run's deadline",
            invocation_id=self._invocation_id,
        )


def poll_delays() -> Iterator[float]:
    """Yield the seconds to wait before each poll: 0.25, 0.5, then 1 each time."""
    delay = _FIRST_POLL_SECONDS
    while True:
        yield delay
        delay = min(delay * 2, _MAX_POLL_SECONDS)


def _texts(parts: Iterable[Part]) -> list[str]:
    return [part.text for part in parts if part.WhichOneof("content") == "text"]


def remote_name(annotation: object) -> str | None:
    """Return the agent name of a ``Remote[Literal["name"]]`` annotation.

    A ``type`` alias of one is read through.

    Returns:
        The name, or ``None`` when ``annotation`` is not a ``Remote``.

    Raises:
        DefinitionError: ``Remote`` is used without exactly one string name.
    """
    if isinstance(annotation, TypeAliasType):
        annotation = annotation.__value__
    if annotation is not Remote and get_origin(annotation) is not Remote:
        return None
    [name_type] = get_args(annotation) or [None]
    names = get_args(name_type) if get_origin(name_type) is Literal else ()
    if len(names) != 1 or not isinstance(names[0], str):
        raise DefinitionError("Name the remote agent as Remote[Literal['agent-name']]")
    return names[0]


async def find_remote(directory: AgentDirectory, name: str) -> DiscoveredAgent:
    """Look an agent up and check that a binding can call it.

    Raises:
        DefinitionError: No source lists the agent, or its card requires
            credentials, which remote calls cannot send yet.
        RemoteAgentError: Its card cannot be fetched or is invalid.
    """
    found = await directory.get(name)
    card = found.card
    if card.security_schemes or card.security_requirements:
        raise DefinitionError(
            f"Agent '{name}' requires credentials; remote calls cannot "
            "authenticate yet, so only agents without card security can be called"
        )
    return found


async def bind_remote(
    directory: AgentDirectory,
    name: str,
    *,
    deadline: float,
    invocation_id: str,
    principal: Principal | None,
) -> Remote[Any]:
    """Create the binding one run passes to a ``Remote`` skill parameter.

    Raises:
        DefinitionError: As for ``find_remote``, or the directory is not open.
        RemoteAgentError: As for ``find_remote``.
    """
    found = await find_remote(directory, name)
    # noinspection PyProtectedMember
    client = directory._open_client()
    transport = JsonRpcTransport(
        client, found.card, found.interface_url, is_shared_httpx=True
    )
    return Remote(
        found,
        transport,
        deadline=deadline,
        invocation_id=invocation_id,
        principal=principal,
    )
