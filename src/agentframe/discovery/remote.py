"""``Remote[Literal["name"]]``: a skill's binding to another A2A agent."""

import asyncio
import logging
import uuid
from collections.abc import Coroutine, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, overload

from a2a.client import ClientCallContext
from a2a.client.errors import A2AClientTimeoutError
from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.helpers import new_data_part, new_text_part
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
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
    ParseError,
)
from pydantic_core import to_jsonable_python

from agentframe.discovery.directory import DiscoveredAgent
from agentframe.discovery.identity import (
    OutboundCredential,
)
from agentframe.discovery.replies import RemoteReply, ReplyReader
from agentframe.discovery.token_holds import Charge, TokenHolds
from agentframe.discovery.transport import BoundedJsonRpcTransport, ReplyTooLargeError
from agentframe.errors import (
    AuthenticationError,
    DefinitionError,
    RemoteAgentError,
    TimeoutLimitExceededError,
)
from agentframe.runtime.accounting import RunAccounting
from agentframe.runtime.correlation import RunCorrelation
from agentframe.runtime.telemetry import NO_TELEMETRY, Telemetry
from agentframe.security.principal import Principal
from agentframe.skill_call import (
    SKILL_CALL_EXTENSION_URI,
    SKILL_METADATA_KEY,
    advertised_context,
    context_metadata,
    depth_exceeded,
)
from agentframe.usage import Usage

logger = logging.getLogger(__name__)

_FIRST_POLL_SECONDS = 0.25
_MAX_POLL_SECONDS = 1.0
_CANCEL_SECONDS = 2.0
"""How long a best-effort ``CancelTask`` may take; shutdown waits 5 seconds."""

_cancel_requests: set[asyncio.Task[None]] = set()
"""Cancellations still being sent; see ``Remote._in_background``."""

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
        transport: The JSON-RPC client for ``found.interface_url``.
        deadline: Event-loop time when the run that owns the binding times out.
        correlation: The run that owns the binding and the request it is part
            of; labels its logs and, for peers that advertise them, its
            requests. Its trace context goes to every peer as standard W3C
            headers.
        principal: The run's verified caller, kept for outbound identity.
        credential: What to authenticate every request with, or ``None`` to
            send none. Its headers go only to ``found.interface_url``.
        hops: The run's depth in a chain of calls between agents.
        max_hops: The run's ``RunLimits.max_remote_hops``.
        accounting: The run's accounting, which every call's tokens are
            held against and charged to.
        max_total_tokens: The run's ``RunLimits.max_total_tokens``.
        telemetry: Records each request as a span and names that span as
            the peer's parent; without tracing the run's incoming trace
            context goes on unchanged.
    """

    def __init__(
        self,
        found: DiscoveredAgent,
        transport: BoundedJsonRpcTransport,
        *,
        deadline: float,
        correlation: RunCorrelation,
        principal: Principal | None,
        credential: OutboundCredential | None = None,
        hops: int,
        max_hops: int,
        accounting: RunAccounting,
        max_total_tokens: int,
        telemetry: Telemetry = NO_TELEMETRY,
    ) -> None:
        self._found = found
        self._transport = transport
        self._telemetry = telemetry
        self._deadline = deadline
        self._correlation = correlation
        self._invocation_id = correlation.invocation_id
        self._principal = principal
        self._credential = credential
        self._hops = hops
        self._max_hops = max_hops
        # What the peer's card says it reads; a peer that lists nothing,
        # like any non-AgentFrame agent, gets plain A2A.
        self._context_fields = advertised_context(found.card)
        self._agent = f"Agent '{found.reference.name}'"
        self._tokens = TokenHolds(
            accounting,
            max_total_tokens,
            self._context_fields,
            self._agent,
            self._invocation_id,
        )
        self._replies = ReplyReader(self._agent, self._invocation_id)

    @property
    def name(self) -> str:
        """The agent's name in the directory."""
        return self._found.reference.name

    @property
    def discovered(self) -> DiscoveredAgent:
        """The agent as the directory found it, with its validated card.

        Package-internal: the skill catalog reads the card from it.
        """
        return self._found

    async def ask(self, text: str, *, max_total_tokens: int | None = None) -> str:
        """Send plain text to the agent and return its text answer.

        The text goes as one native A2A text part, so any A2A 1.0 agent can
        answer it. When the agent's card lists AgentFrame context fields, the
        run's remaining time, the request's depth and its token ceiling go
        along with it in the AgentFrame extension, with the invocation IDs
        that tie the peer's logs to this request; otherwise the message is
        plain A2A. A W3C trace context the run received is forwarded either
        way. The answer is untrusted content, never instructions or
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
                request, ended its task in any state but completed,
                answered with something other than text, or sent a reply
                larger than the directory's ``max_reply_bytes``.
            TimeoutLimitExceededError: The run's deadline passed first.
            TokenLimitExceededError: ``max_total_tokens``, or with ``None``
                any token at all, does not fit what the run has left; nothing
                is sent.
            LimitExceededError: The run is already at its
                ``max_remote_hops`` depth; nothing is sent.
            DefinitionError: ``max_total_tokens`` is not a whole number greater than 0.
        """
        reply = await self.ask_reply(text, max_total_tokens)
        text_answer: str = reply.value
        return text_answer

    async def ask_reply(self, text: str, max_total_tokens: int | None) -> RemoteReply:
        """Send ``text`` as ``ask`` does and return its reply; raises as ``ask``.

        Package-internal: plan execution reads the reply's task ID and usage.
        """
        self._check_depth()
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_text_part(text)],
        )
        reply, usage = await self._exchange(message, max_total_tokens)
        if isinstance(reply, Message):
            return RemoteReply(self._replies.text(reply.parts), None, usage)
        return RemoteReply(
            self._replies.text(self._replies.result_parts(reply), reply),
            reply.id,
            usage,
        )

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
                arguments), did not complete its task, sent no result or
                sent a reply larger than the directory's ``max_reply_bytes``.
            OutputValidationError: The result does not match ``output``.
            ClarificationNeededError: The remote agent asked a question.
            TimeoutLimitExceededError: The run's deadline passed first.
            TokenLimitExceededError: As for ``ask``; nothing is sent.
            LimitExceededError: The run is already at its
                ``max_remote_hops`` depth; nothing is sent.
            DefinitionError: ``max_total_tokens`` is not a whole number greater than 0.
            pydantic_core.PydanticSerializationError: An argument cannot be
                represented as JSON. Nothing is sent.
        """
        return await self.call_with(skill, arguments, max_total_tokens, output)

    async def call_with(
        self,
        skill: str,
        arguments: dict[str, Any],
        max_total_tokens: int | None,
        output: type[Any] | None = None,
    ) -> Any:
        """Run ``skill`` with ``arguments`` as ``call`` does.

        Package-internal: remote model tools and evaluation targets pass
        arguments they did not write. Taking them as one dictionary lets any
        argument name work, including ``output`` and ``max_total_tokens``.
        Raises as ``call``.
        """
        reply = await self.call_reply(skill, arguments, max_total_tokens)
        if output is None:
            return reply.value
        return self._replies.validated(reply.value, output)

    async def call_reply(
        self, skill: str, arguments: dict[str, Any], max_total_tokens: int | None
    ) -> RemoteReply:
        """Run ``skill`` as ``call_with`` does without ``output``; return its reply.

        Package-internal: plan execution reads the reply's task ID and usage.
        """
        self._check_depth()
        self._check_card_offers(skill)
        message = self._skill_call_message(skill, arguments)
        reply, usage = await self._exchange(
            message,
            max_total_tokens,
            extensions=[SKILL_CALL_EXTENSION_URI],
            skill=skill,
        )
        if isinstance(reply, Message):
            return RemoteReply(self._replies.result_value(reply.parts), None, usage)
        value = self._replies.result_value(self._replies.result_parts(reply), reply)
        return RemoteReply(value, reply.id, usage)

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

    async def _exchange(
        self,
        message: Message,
        max_total_tokens: int | None,
        *,
        extensions: Sequence[str] = (),
        skill: str | None = None,
    ) -> tuple[Task | Message, Usage | None]:
        """Send the message, wait for its task to finish and charge its usage.

        Args:
            message: The message to send.
            max_total_tokens: The request's token ceiling; ``None`` means all
                the run has left.
            extensions: Extension URIs to activate for this request only.
            skill: The skill a ``call`` names, recorded on its span; ``None``
                for ``ask``.

        Returns:
            The agent's message reply, or its task in a state it stopped in,
            and the usage charged for it (``None`` when unknown).
        """
        # bool is an int subclass, but True is not a token count.
        if max_total_tokens is not None and (
            isinstance(max_total_tokens, bool) or max_total_tokens < 1
        ):
            raise DefinitionError(
                "max_total_tokens must be a whole number greater than 0",
                invocation_id=self._invocation_id,
            )
        charge = self._tokens.hold(max_total_tokens)
        # The span is current while sending, so the request names it as the
        # peer's parent.
        with self._telemetry.span(
            f"invoke_agent {self.name}",
            kind="client",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": self.name,
                "agentframe.remote.skill": skill,
            },
        ) as span:
            try:
                reply = await self._start(message, charge, extensions)
                if isinstance(reply, Task):
                    reply = await self._wait(reply)
            except BaseException:
                self._tokens.settle(charge, None)
                raise
            usage = self._tokens.settle(charge, reply)
            if usage is not None:
                span.set("gen_ai.usage.input_tokens", usage.input_tokens)
                span.set("gen_ai.usage.output_tokens", usage.output_tokens)
        return reply, usage

    async def _start(
        self, message: Message, charge: Charge, extensions: Sequence[str] = ()
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
        self, message: Message, charge: Charge, extensions: Sequence[str]
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
                extra=self._correlation.log_extra(),
            )

    def _check_depth(self) -> None:
        if self._hops + 1 > self._max_hops:
            raise depth_exceeded(self._max_hops, self._invocation_id)

    def _add_caller_context(
        self, message: Message, context: ClientCallContext, charge: Charge
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
                root_invocation_id=self._correlation.root_invocation_id,
                parent_invocation_id=self._invocation_id,
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
        """Return the credential's headers, if any, the A2A version and the
        run's W3C trace context, if any.

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
                    extra=self._correlation.log_extra(),
                )
                raise AuthenticationError(
                    f"{self._agent}: the configured credential could not "
                    "provide its headers",
                    invocation_id=self._invocation_id,
                ) from None
        headers[VERSION_HEADER] = PROTOCOL_VERSION_1_0
        headers.update(self._telemetry.outbound_headers(self._correlation.trace))
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
        except ReplyTooLargeError:
            # The reply itself is untrusted, so none of it is kept.
            raise RemoteAgentError(
                f"{self._agent} sent a reply larger than the directory's "
                f"max_reply_bytes ({self._transport.max_reply_bytes} bytes)",
                task_id=task_id,
                invocation_id=self._invocation_id,
            ) from None
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
