"""``Remote[Literal["name"]]``: a skill's binding to another A2A agent."""

import asyncio
import logging
import uuid
from collections.abc import Coroutine, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
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
from google.protobuf.json_format import (  # type: ignore[import-untyped]
    MessageToDict,
    ParseError,
)
from pydantic import TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from agentframe.discovery.directory import AgentDirectory, DiscoveredAgent
from agentframe.discovery.identity import (
    OutboundCredential,
    RemoteIdentity,
    check_identity,
)
from agentframe.errors import (
    AuthenticationError,
    ClarificationNeededError,
    DefinitionError,
    OutputValidationError,
    RemoteAgentError,
    TimeoutLimitExceededError,
    TokenLimitExceededError,
)
from agentframe.hosting.skill_call import (
    MAX_TOTAL_TOKENS_FIELD,
    SKILL_CALL_EXTENSION_URI,
    SKILL_METADATA_KEY,
    advertised_context,
    context_metadata,
    depth_exceeded,
)
from agentframe.runtime.generation import UNKNOWN_USAGE, RunAccounting
from agentframe.runtime.usage import Usage
from agentframe.security.principal import Principal

logger = logging.getLogger(__name__)

_FIRST_POLL_SECONDS = 0.25
_MAX_POLL_SECONDS = 1.0
_CANCEL_SECONDS = 2.0
"""How long a best-effort ``CancelTask`` may take; shutdown waits 5 seconds."""

_cancel_requests: set[asyncio.Task[None]] = set()
"""Cancellations still being sent; see ``Remote._in_background``."""

_UNFINISHED_STATES = (TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING)


@dataclass
class _Charge:
    """One remote request's hold on its run's token budget.

    Attributes:
        tokens: The tokens held, also sent as the request's ceiling. 0 for a
            peer that takes no ceiling.
        reports_usage: Whether the peer advertises ``max_total_tokens``, so
            the usage its task reports is charged to the run.
        sent: Whether the request may have reached the peer.
    """

    tokens: int
    reports_usage: bool
    sent: bool = False


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
        credential: What to authenticate every request with, or ``None`` to
            send none. Its headers go only to ``found.interface_url``.
        hops: The run's depth in a chain of calls between agents.
        max_hops: The run's ``RunLimits.max_remote_hops``.
        accounting: The run's accounting, which every call's tokens are
            held against and charged to.
        max_total_tokens: The run's ``RunLimits.max_total_tokens``.
    """

    def __init__(
        self,
        found: DiscoveredAgent,
        transport: JsonRpcTransport,
        *,
        deadline: float,
        invocation_id: str,
        principal: Principal | None,
        credential: OutboundCredential | None = None,
        hops: int,
        max_hops: int,
        accounting: RunAccounting,
        max_total_tokens: int,
    ) -> None:
        self._found = found
        self._transport = transport
        self._deadline = deadline
        self._invocation_id = invocation_id
        self._principal = principal
        self._credential = credential
        self._hops = hops
        self._max_hops = max_hops
        self._accounting = accounting
        self._max_total_tokens = max_total_tokens
        # What the peer's card says it reads; a peer that lists nothing,
        # like any non-AgentFrame agent, gets plain A2A.
        self._context_fields = advertised_context(found.card)
        self._agent = f"Agent '{found.reference.name}'"

    @property
    def name(self) -> str:
        """The agent's name in the directory."""
        return self._found.reference.name

    async def ask(self, text: str, *, max_total_tokens: int | None = None) -> str:
        """Send plain text to the agent and return its text answer.

        The text goes as one native A2A text part, so any A2A 1.0 agent can
        answer it. When the agent's card lists AgentFrame context fields, the
        run's remaining time, the request's depth and its token ceiling go
        along with it in the AgentFrame extension; otherwise the message is
        plain A2A. The answer is untrusted content, never instructions or
        identity. Waits no longer than the run's deadline; if the run times
        out or is cancelled while waiting, the remote task is cancelled (best
        effort) and the timeout or cancellation goes on.

        Token budget: when the card lists ``max_total_tokens``, the ceiling is
        held against the run's ``max_total_tokens`` before sending, the agent
        runs within it, and the usage its task reports replaces the hold in
        the run's usage, even when it is higher. When the card does not list
        it (non-AgentFrame agents and older AgentFrame versions), nothing is held
        and the run's usage becomes unknown, never zero.

        Args:
            text: The request. Untrusted as far as the remote agent knows.
            max_total_tokens: The most tokens the request may use. ``None``
                means everything the run has left, so a second concurrent
                call fails; pass a ceiling to each call to fan out.

        Returns:
            The text parts of the completed task's artifacts (or of its final
            message, when it has no artifacts), joined by newlines.

        Raises:
            AuthenticationError: The configured credential could not provide
                its headers (for example, no Entra token was issued).
            ClarificationNeededError: The remote agent asked a question.
                Nothing answers it automatically.
            RemoteAgentError: The agent could not be reached, refused the
                request, ended its task in any state but completed, or
                answered with something other than text.
            TimeoutLimitExceededError: The run's deadline passed first.
            TokenLimitExceededError: ``max_total_tokens``, or with ``None``
                any token at all, does not fit what the run has left; nothing
                is sent.
            LimitExceededError: The run is already at its
                ``max_remote_hops`` depth; nothing is sent.
            DefinitionError: ``max_total_tokens`` is not greater than 0.
        """
        self._check_depth()
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_text_part(text)],
        )
        reply = await self._exchange(message, max_total_tokens)
        if isinstance(reply, Message):
            return self._text(reply.parts)
        return self._text(self._result_parts(reply), reply)

    @overload
    async def call[T](
        self,
        skill: str,
        /,
        *,
        output: type[T],
        max_total_tokens: int | None = None,
        **arguments: Any,
    ) -> T: ...

    @overload
    async def call(
        self, skill: str, /, *, max_total_tokens: int | None = None, **arguments: Any
    ) -> Any: ...

    async def call(
        self,
        skill: str,
        /,
        *,
        output: type[Any] | None = None,
        max_total_tokens: int | None = None,
        **arguments: Any,
    ) -> Any:
        """Run one of the agent's skills by ID and return its result.

        The call uses AgentFrame's skill-call extension, so the peer runs
        exactly ``skill`` with ``arguments``; it never falls back to free text.
        Waiting, deadline, cancellation, question handling, the token budget
        and the caller context sent to peers that advertise it match ``ask``.

        Args:
            skill: The skill's ID on the agent's card.
            output: The type to validate the result into, such as
                ``list[Source]``. Without it the result is returned as decoded
                JSON (numbers come back as ``float``) or as ``str`` for a text
                result.
            max_total_tokens: The most tokens the call may use; as for ``ask``.
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
            ClarificationNeededError: The remote agent asked a question.
            TimeoutLimitExceededError: The run's deadline passed first.
            TokenLimitExceededError: As for ``ask``; nothing is sent.
            LimitExceededError: The run is already at its
                ``max_remote_hops`` depth; nothing is sent.
            DefinitionError: ``max_total_tokens`` is not greater than 0.
            pydantic_core.PydanticSerializationError: An argument cannot be
                represented as JSON. Nothing is sent.
        """
        self._check_depth()
        self._check_card_offers(skill)
        message = self._skill_call_message(skill, arguments)
        reply = await self._exchange(
            message, max_total_tokens, extensions=[SKILL_CALL_EXTENSION_URI]
        )
        if isinstance(reply, Message):
            value = self._result_value(reply.parts)
        else:
            value = self._result_value(self._result_parts(reply), reply)
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

    async def _exchange(
        self,
        message: Message,
        max_total_tokens: int | None,
        *,
        extensions: Sequence[str] = (),
    ) -> Task | Message:
        """Send the message, wait for its task to finish and charge its usage.

        Args:
            message: The message to send.
            max_total_tokens: The request's token ceiling; ``None`` means all
                the run has left.
            extensions: Extension URIs to activate for this request only.

        Returns:
            The agent's message reply, or its task in a state it stopped in.
        """
        if max_total_tokens is not None and max_total_tokens < 1:
            raise DefinitionError(
                "max_total_tokens must be greater than 0",
                invocation_id=self._invocation_id,
            )
        charge = self._hold(max_total_tokens)
        try:
            reply = await self._start(message, charge, extensions)
            if isinstance(reply, Task):
                reply = await self._wait(reply)
        except BaseException:
            self._settle(charge, None)
            raise
        self._settle(charge, reply)
        return reply

    def _hold(self, max_total_tokens: int | None) -> _Charge:
        """Hold the request's ceiling against the run's tokens, without awaiting.

        Not awaiting means concurrent calls in one run cannot both take the
        same tokens. A peer that does not advertise ``max_total_tokens``
        takes no ceiling, so nothing is held.

        Raises:
            TokenLimitExceededError: The ceiling does not fit what is left.
        """
        if MAX_TOTAL_TOKENS_FIELD not in self._context_fields:
            return _Charge(tokens=0, reports_usage=False)
        run_total = self._max_total_tokens
        remaining = self._accounting.remaining_tokens(run_total)
        if max_total_tokens is None and remaining == 0:
            raise TokenLimitExceededError(
                f"{self._agent}: none of the run's {run_total} tokens are left "
                "for the call. Without max_total_tokens a call holds all the "
                "run has left, so when calling agents concurrently, pass "
                "max_total_tokens to each call to share the budget",
                invocation_id=self._invocation_id,
            )
        if max_total_tokens is not None and max_total_tokens > remaining:
            raise TokenLimitExceededError(
                f"{self._agent}: the call needs up to {max_total_tokens} "
                f"tokens, but only {remaining} of the run's {run_total} remain",
                invocation_id=self._invocation_id,
            )
        tokens = remaining if max_total_tokens is None else max_total_tokens
        self._accounting.hold_tokens(tokens)
        return _Charge(tokens=tokens, reports_usage=True)

    def _settle(self, charge: _Charge, reply: Task | Message | None) -> None:
        """Replace the request's hold with the usage its peer reported.

        A request that was never sent used nothing. Otherwise usage the peer
        does not report, or reports in a malformed way, is unknown: the hold
        stays counted and the run's usage becomes unknown, never zero.
        Reported usage above the hold is charged as reported.

        Args:
            charge: The request's hold.
            reply: The finished reply, or ``None`` when the request failed.
        """
        if not charge.sent:
            self._accounting.release_tokens(charge.tokens)
            return
        usage = None
        if charge.reports_usage and isinstance(reply, Task):
            usage = reported_usage(reply)
        self._accounting.settle(charge.tokens, usage or UNKNOWN_USAGE)

    async def _start(
        self, message: Message, charge: _Charge, extensions: Sequence[str] = ()
    ) -> Task | Message:
        """Send the message; if the run stops meanwhile, cancel what it started.

        The peer may start the task before its reply arrives, so a cancelled
        send still finishes in the background to learn the task ID.

        Args:
            message: The message to send.
            charge: The request's token hold, marked sent once it may be.
            extensions: Extension URIs to activate for this request only.
        """
        sending = asyncio.create_task(self._send(message, charge, extensions))
        try:
            return await asyncio.shield(sending)
        except asyncio.CancelledError:
            await self._in_background(self._cancel_when_started(sending))
            raise

    async def _send(
        self, message: Message, charge: _Charge, extensions: Sequence[str]
    ) -> Task | Message:
        # A peer that lists no context fields, like any non-AgentFrame
        # agent, gets plain A2A.
        if self._context_fields and SKILL_CALL_EXTENSION_URI not in extensions:
            extensions = [*extensions, SKILL_CALL_EXTENSION_URI]
        context = await self._call_context(extensions)
        # After the credential's headers, so the time they took is not sent.
        self._add_caller_context(message, context, charge)
        # Returning at once gives the task ID needed to cancel it later.
        request = SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(return_immediately=True),
        )
        charge.sent = True
        with self._translated_errors():
            response = await self._transport.send_message(request, context=context)
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
                context = await self._call_context()
                with self._translated_errors(task.id):
                    task = await self._transport.get_task(
                        GetTaskRequest(id=task.id), context=context
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
        except (
            AuthenticationError,
            RemoteAgentError,
            TimeoutLimitExceededError,
            TimeoutError,
        ):
            return
        if isinstance(reply, Task) and reply.status.state in _UNFINISHED_STATES:
            await self._cancel(reply.id)

    async def _cancel(self, task_id: str) -> None:
        """Ask the agent to cancel ``task_id``; a failure is only logged."""
        try:
            async with asyncio.timeout(_CANCEL_SECONDS):
                context = ClientCallContext(
                    timeout=_CANCEL_SECONDS,
                    service_parameters=await self._headers(),
                )
                await self._transport.cancel_task(
                    CancelTaskRequest(id=task_id), context=context
                )
        except (AuthenticationError, A2AError, ParseError, TimeoutError):
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

    def _check_depth(self) -> None:
        if self._hops + 1 > self._max_hops:
            raise depth_exceeded(self._max_hops, self._invocation_id)

    def _add_caller_context(
        self, message: Message, context: ClientCallContext, charge: _Charge
    ) -> None:
        """Add the context fields the peer's card lists to ``message``.

        Args:
            message: The message to send.
            context: The request's call context; its timeout is the run's
                remaining time.
            charge: The request's token hold, sent as its ceiling.
        """
        assert context.timeout is not None  # _call_context always sets one.
        message.metadata.update(
            context_metadata(
                self._context_fields,
                remaining_seconds=context.timeout,
                hops=self._hops + 1,
                max_total_tokens=charge.tokens if charge.reports_usage else None,
            )
        )

    def _remaining(self) -> float:
        """Return the run's remaining seconds, or raise once there are none."""
        remaining = self._deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise self._timeout()
        return remaining

    async def _call_context(self, extensions: Sequence[str] = ()) -> ClientCallContext:
        """Give one request its headers and bound it by the run's remaining time.

        Headers go per request, never into the shared client's defaults, so
        credentials reach only this agent's interface URL.

        Args:
            extensions: Extension URIs the request activates.
        """
        headers = await self._headers()
        remaining = self._remaining()
        if extensions:
            headers[HTTP_EXTENSION_HEADER] = ",".join(extensions)
        return ClientCallContext(timeout=remaining, service_parameters=headers)

    async def _headers(self) -> dict[str, str]:
        """Return the credential's headers, if any, and the A2A version.

        Raises:
            AuthenticationError: The credential failed. Its error is logged
                by type only, since it may describe the secret.
        """
        headers: dict[str, str] = {}
        if self._credential is not None:
            try:
                headers.update(await self._credential.headers())
            except Exception as error:
                logger.warning(
                    "The credential for %s failed with %s (invocation_id=%s)",
                    self._agent,
                    type(error).__name__,
                    self._invocation_id,
                )
                raise AuthenticationError(
                    f"{self._agent}: the configured credential could not "
                    "provide its headers",
                    invocation_id=self._invocation_id,
                ) from None
        headers[VERSION_HEADER] = PROTOCOL_VERSION_1_0
        return headers

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


async def find_remote(
    directory: AgentDirectory, name: str, identity: RemoteIdentity | None
) -> DiscoveredAgent:
    """Look an agent up and check that a binding can call it.

    Args:
        directory: Where to look the agent up.
        name: The agent's name.
        identity: The agent's entry in ``remotes=``, if any.

    Raises:
        DefinitionError: No source lists the agent, its card requires
            credentials that ``identity`` cannot provide, or credentials
            would be sent with an HTTP client that follows redirects.
        RemoteAgentError: Its card cannot be fetched or is invalid.
    """
    found = await directory.get(name)
    check_identity(name, found.card, identity)
    # noinspection PyProtectedMember
    if identity is not None and directory._open_client().follow_redirects:
        raise DefinitionError(
            f"Remote '{name}' has credentials, but the AgentDirectory's "
            "http_client follows redirects; credentials are only sent to the "
            "card's own origin, so pass a client with follow_redirects=False."
        )
    return found


async def bind_remote(
    directory: AgentDirectory,
    name: str,
    *,
    deadline: float,
    invocation_id: str,
    principal: Principal | None,
    identity: RemoteIdentity | None,
    credential: OutboundCredential | None,
    hops: int,
    max_hops: int,
    accounting: RunAccounting,
    max_total_tokens: int,
) -> Remote[Any]:
    """Create the binding one run passes to a ``Remote`` skill parameter.

    Args:
        identity: The agent's entry in ``remotes=``, if any, checked against
            its card.
        credential: What this run sends for ``identity``: a ``Service``'s
            credential, or the run's on-behalf-of token for ``OnBehalfOf``.

    Raises:
        DefinitionError: As for ``find_remote``, or the directory is not open.
        RemoteAgentError: As for ``find_remote``.
    """
    found = await find_remote(directory, name, identity)
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
        credential=credential,
        hops=hops,
        max_hops=max_hops,
        accounting=accounting,
        max_total_tokens=max_total_tokens,
    )
