"""Serve AG-UI 1.0 runs to frontends at ``POST /ag-ui`` (the ``ag-ui`` extra).

The official ``ag-ui-protocol`` SDK owns the protocol types and their JSON;
this module checks each request like ``/a2a`` does, runs it through the same
``Runtime`` and writes the run's events as server-sent events.

A request is checked in a fixed order, and every refusal is an HTTP error
sent before any event: body size (413), authentication (401), the caller's
rate limit (429), a valid ``RunAgentInput`` ending with a user message
(400), then shutdown and a free run slot (503). Then one run streams:

- ``RUN_STARTED``, echoing the frontend's ``threadId`` and ``runId`` and
  carrying the server's own ``invocation_id`` in its metadata. The frontend's
  IDs are never trusted as identity or correlation.
- Model text the skill generates with ``stream=True``, as
  ``TEXT_MESSAGE_START`` / ``TEXT_MESSAGE_CONTENT`` / ``TEXT_MESSAGE_END``.
  With execution policy output checks, nothing streams; the whole answer
  is sent at the end, after they pass.
- Each tool call the model makes, by name only, as ``TOOL_CALL_START`` and
  ``TOOL_CALL_END``; never its arguments or result.
- On success, a ``str`` result the latest streamed message does not end with
  becomes one more assistant message, a router's question likewise; any other
  result is ``RUN_FINISHED``'s ``result`` in Pydantic JSON mode. Then
  ``RUN_FINISHED``.
- On failure, ``RUN_ERROR`` with the error's safe message and its class name
  as ``code``, as ``/a2a`` reports it; a skill's own exception gets a
  generic message and stays in the operator log.

The frontend's ``user`` and ``assistant`` text messages are the conversation
the ``SkillRouter`` (or ``default_skill``) sees, as untrusted text. Its
``system``, ``developer``, ``tool``, reasoning and activity messages, and its
``tools``, ``state``, ``context``, ``forwardedProps`` and ``resume``, are not
used: they are never trusted instructions, and frontend tools, shared state
and interrupts are not supported.

A client disconnect cancels the run; there is no reconnect or resume.
Shutdown waits for runs like ``/a2a`` does, then cancels the rest, closing
them with a cancelled ``RUN_FINISHED``; any cancellation once shutdown has
started counts as shutdown's, since servers such as Uvicorn cancel open
requests themselves at the end of their grace.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from a2a.helpers import new_text_message
from a2a.types import AgentCard, Message, Role
from ag_ui.core import (
    PROTOCOL_VERSION,
    AssistantMessage,
    BaseEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedCancelledOutcome,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    TextPart,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UserMessage,
)
from ag_ui.encoder import EventEncoder
from pydantic import ValidationError
from pydantic_core import to_json, to_jsonable_python
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from agentframe.authoring.agent import Agent
from agentframe.discovery.identity import UserToken, UserTokenExchange
from agentframe.errors import (
    AgentFrameError,
    CancelledError,
    LimitExceededError,
    OutputValidationError,
)
from agentframe.hosting.a2a_executor import (
    RESPONSE_TOO_LARGE_MESSAGE,
    SKILL_FAILED_MESSAGE,
    check_response_size,
    hosted_conversation_id,
    log_failed_run,
    safe_message,
)
from agentframe.hosting.admission import RunAdmission, RunSlot
from agentframe.hosting.default_skill import DefaultSkill
from agentframe.hosting.endpoints import admit, caller_check, rate_limited
from agentframe.hosting.host import HostConfig
from agentframe.hosting.lifecycle import Lifecycle
from agentframe.hosting.rate_limit import CallerRateLimiter
from agentframe.runtime.accounting import RunAccounting
from agentframe.runtime.correlation import (
    TRACEPARENT_HEADER,
    TRACESTATE_HEADER,
    RunCorrelation,
    TraceContext,
    read_trace_context,
)
from agentframe.runtime.generation import ToolStatus
from agentframe.runtime.options import RunOptions
from agentframe.runtime.routing import (
    Clarification,
    SkillCall,
    SkillRouter,
    eligible_skills,
    new_routing_context,
    route,
)
from agentframe.runtime.runtime import Runtime
from agentframe.runtime.state import MAX_CONVERSATION_ID_LENGTH
from agentframe.runtime.telemetry import Span
from agentframe.security.principal import Principal

_CANCEL_SECONDS = 5
"""How long shutdown waits for cancelled runs to close their streams."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RunRequest:
    """A checked AG-UI request: who sent it, and what to run."""

    thread_id: str
    run_id: str
    conversation: list[Message]
    principal: Principal | None
    user_token: UserToken | None
    trace: TraceContext | None


class AGUIEndpoint:
    """The ``POST /ag-ui`` endpoint: checks each request, then streams its run.

    Args:
        agent: The hosted agent instance.
        runtime: The runtime that validates, authorizes and limits every run.
        router: Selects the skill for each run, or asks a question.
        default_skill: Receives the last user message's text when there is
            no router.
        host: The host's authentication, limits and sizes.
        card: The agent's card, for the 401 challenge and its own URL.
        user_token_exchange: Exchanges signed-in users' tokens for
            ``OnBehalfOf`` remotes, or ``None``.
        admission: The app's run slots, shared with ``/a2a``.
        lifecycle: The app's readiness and shutdown.
        rate_limiter: The callers' allowances, shared with ``/a2a``, or
            ``None`` for no rate limit.
    """

    def __init__(
        self,
        agent: Agent,
        runtime: Runtime,
        router: SkillRouter | None,
        default_skill: DefaultSkill | None,
        host: HostConfig,
        card: AgentCard,
        user_token_exchange: UserTokenExchange | None,
        admission: RunAdmission,
        lifecycle: Lifecycle,
        rate_limiter: CallerRateLimiter | None,
    ) -> None:
        self._agent = agent
        self._runtime = runtime
        self._router = router
        self._default_skill = default_skill
        self._host = host
        self._agent_name = card.name
        self._own_interface_url = card.supported_interfaces[0].url
        self._check_caller = caller_check(host, user_token_exchange, card)
        self._admission = admission
        self._lifecycle = lifecycle
        self._rate_limiter = rate_limiter
        self._running: set[asyncio.Task[None]] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ``POST /ag-ui`` as an ASGI app.

        It is an ASGI app rather than a Starlette endpoint, so the run's
        stream can watch the connection for a disconnect.
        """
        response = await self._respond(Request(scope, receive))
        await response(scope, receive, send)

    async def _respond(self, request: Request) -> "Response | _EventStream":
        """Check one request; return its refusal or the stream of its run.

        The checks are the host's shared ones (``endpoints``), in the same
        order as for ``/a2a``, with the input's validation before admission.
        """
        checked = await self._check_caller(request)
        if isinstance(checked, Response):
            return checked
        principal: Principal | None = checked.state.principal
        refusal = rate_limited(self._rate_limiter, principal)
        if refusal is not None:
            return refusal
        run_request = _read_run_request(
            await checked.body(),
            principal,
            checked.state.user_token,
            read_trace_context(
                checked.headers.getlist(TRACEPARENT_HEADER),
                checked.headers.getlist(TRACESTATE_HEADER),
            ),
        )
        if run_request is None:
            # Nothing about the input is echoed back.
            return JSONResponse(
                {"error": "Invalid AG-UI run input, or no final user message."}, 400
            )
        slot = admit(
            self._admission, self._lifecycle, self._runtime.telemetry, self._agent_name
        )
        if isinstance(slot, Response):
            return slot
        return _EventStream(
            lambda events: self._run(events, run_request),
            slot,
            self._running,
            self._host.max_response_bytes,
        )

    async def cancel_running(self) -> None:
        """Cancel every run still streaming, for shutdown, and wait for them.

        Each run closes its stream with a cancelled ``RUN_FINISHED`` when it
        can. A server that cancels the requests itself at the end of its
        grace (as Uvicorn does under ``agentframe run``) gets the same
        closing events, since any cancellation during shutdown is treated as
        a shutdown. Waits at most a few seconds.
        """
        running = list(self._running)
        for task in running:
            task.cancel()
        if running:
            await asyncio.wait(running, timeout=_CANCEL_SECONDS)

    async def _run(self, events: "_RunEvents", request: _RunRequest) -> None:
        """Run the request and write its events, from ``RUN_STARTED`` to the end.

        The run is recorded as a server ``invoke_agent`` span continuing the
        request's ``traceparent``, as for an A2A message or MCP tool call.
        """
        invocation_id = str(uuid.uuid4())
        correlation = RunCorrelation(
            self._agent_name,
            invocation_id,
            root_invocation_id=invocation_id,
            trace=request.trace,
        )
        with self._runtime.telemetry.run(
            agent=self._agent_name,
            invocation_id=invocation_id,
            kind="server",
            parent=correlation.trace,
        ) as span:
            await self._run_traced(events, request, correlation, span)

    async def _run_traced(
        self,
        events: "_RunEvents",
        request: _RunRequest,
        correlation: RunCorrelation,
        span: Span,
    ) -> None:
        """Run the request inside the run's span, which records any failure."""
        invocation_id = correlation.invocation_id
        accounting = RunAccounting()
        started_at = asyncio.get_running_loop().time()
        await events.emit(
            RunStartedEvent(
                thread_id=request.thread_id,
                run_id=request.run_id,
                protocol_version=PROTOCOL_VERSION,
                metadata={"invocation_id": invocation_id},
            )
        )
        logger.info(
            "Run started (invocation_id=%s, root_invocation_id=%s)",
            invocation_id,
            invocation_id,
            extra=correlation.log_extra(),
        )
        events.invocation_id = invocation_id
        # noinspection PyBroadException
        try:
            result = await self._answer(
                events, request, accounting, started_at, correlation, span
            )
        except asyncio.CancelledError:
            if not self._lifecycle.shutting_down:
                raise  # The client disconnected: nothing more can be sent.
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            span.fail(CancelledError("The run was cancelled at shutdown."))
            await events.close_open()
            await events.emit(
                RunFinishedEvent(
                    thread_id=request.thread_id,
                    run_id=request.run_id,
                    outcome=RunFinishedCancelledOutcome(),
                )
            )
            return
        except AgentFrameError as error:
            error.invocation_id = invocation_id
            span.fail(error)
            log_failed_run(error, correlation)
            await events.fail(safe_message(error), type(error).__name__)
            return
        except Exception as error:
            # Skill exceptions may carry submitted values or secrets, so the
            # frontend gets a generic message and only the log keeps the details.
            span.fail(error)
            logger.exception(
                "Skill failed (invocation_id=%s)",
                invocation_id,
                extra=correlation.log_extra(),
            )
            await events.fail(SKILL_FAILED_MESSAGE, AgentFrameError.__name__)
            return
        await events.emit(
            RunFinishedEvent(
                thread_id=request.thread_id, run_id=request.run_id, result=result
            )
        )
        logger.info(
            "Run completed (invocation_id=%s)",
            invocation_id,
            extra=correlation.log_extra(),
        )

    async def _answer(
        self,
        events: "_RunEvents",
        request: _RunRequest,
        accounting: RunAccounting,
        started_at: float,
        correlation: RunCorrelation,
        span: Span,
    ) -> Any:
        """Run the chosen skill, or send the router's question.

        Returns:
            What ``RUN_FINISHED`` carries as ``result``: ``None`` for a
            text answer or question, which is sent as a message instead.
        """
        invocation_id = correlation.invocation_id
        call = await self._choose_skill(request, invocation_id, accounting, started_at)
        if isinstance(call, Clarification):
            await events.finish_text(call.question)
            return None
        span.set_skill(call.skill)
        result = await self._runtime.run_invocation(
            invocation_id,
            self._agent,
            call.skill,
            call.arguments,
            request.principal,
            RunOptions(),
            started_at,
            accounting,
            self._host.limits_ceiling,
            0,
            request.user_token,
            correlation,
            own_interface_url=self._own_interface_url,
            conversation_id=hosted_conversation_id(
                request.thread_id, request.principal
            ),
            on_text=events.text,
            on_tool=events.tool,
        )
        if isinstance(result.value, str):
            await events.finish_text(result.value)
            return None
        await events.end_message()
        return _json_result(result.value, invocation_id, self._host.max_response_bytes)

    async def _choose_skill(
        self,
        request: _RunRequest,
        invocation_id: str,
        accounting: RunAccounting,
        started_at: float,
    ) -> SkillCall | Clarification:
        """Route the conversation, or give its last message to the default skill.

        Raises:
            RuntimeError: There is neither a router nor a default skill,
                which ``AgentApp`` refuses at construction.
        """
        if self._router is None:
            if self._default_skill is None:
                raise RuntimeError("An AG-UI run reached an app with no router")
            return self._default_skill.call(request.conversation[-1])
        ctx = new_routing_context(
            self._agent,
            invocation_id,
            request.principal,
            started_at,
            accounting,
            self._host.limits_ceiling,
            self._runtime.policy,
        )
        definition = type(self._agent).definition()
        return await route(
            self._router,
            request.conversation,
            eligible_skills(definition, request.principal),
            ctx,
        )


def _read_run_request(
    body: bytes,
    principal: Principal | None,
    user_token: UserToken | None,
    trace: TraceContext | None,
) -> _RunRequest | None:
    """Parse and check a ``RunAgentInput``; ``None`` when it cannot run.

    It cannot run when it is not a valid ``RunAgentInput``, its ``threadId``
    or ``runId`` is empty or longer than 256 characters, or its last
    ``user`` or ``assistant`` text message is not a ``user`` message.
    """
    try:
        run_input = RunAgentInput.model_validate_json(body)
    except ValidationError:
        return None
    for native_id in (run_input.thread_id, run_input.run_id):
        if not native_id or len(native_id) > MAX_CONVERSATION_ID_LENGTH:
            return None
    conversation = _conversation(run_input)
    if not conversation or conversation[-1].role != Role.ROLE_USER:
        return None
    return _RunRequest(
        run_input.thread_id,
        run_input.run_id,
        conversation,
        principal,
        user_token,
        trace,
    )


def _conversation(run_input: RunAgentInput) -> list[Message]:
    """Return the input's ``user`` and ``assistant`` text as A2A messages.

    Only these two roles become the conversation, as untrusted text, so a
    frontend can never pass trusted instructions. Messages without text are
    left out.
    """
    conversation: list[Message] = []
    for message in run_input.messages:
        if isinstance(message, UserMessage):
            content = message.content
            text = (
                content
                if isinstance(content, str)
                else "\n".join(
                    part.text for part in content if isinstance(part, TextPart)
                )
            )
            role = Role.ROLE_USER
        elif isinstance(message, AssistantMessage):
            text = message.content or ""
            role = Role.ROLE_AGENT
        else:
            continue
        if text.strip():
            conversation.append(new_text_message(text, role=role))
    return conversation


def _json_result(value: Any, invocation_id: str, max_bytes: int) -> Any:
    """Return a non-text result in Pydantic JSON mode, within ``max_bytes``.

    Raises:
        OutputValidationError: The value cannot be represented as JSON.
        LimitExceededError: Its compact JSON is larger than ``max_bytes``.
    """
    try:
        data = to_jsonable_python(value)
    except (TypeError, ValueError):
        raise OutputValidationError(
            "The skill returned a value that cannot be serialized as JSON",
            invocation_id=invocation_id,
        ) from None
    check_response_size(to_json(data), max_bytes, invocation_id)
    return data


class _RunEvents:
    """Writes one run's AG-UI events to the response, in protocol order.

    Text pieces open an assistant message on first use and close it before a
    tool call starts or the run ends. Streamed text counts toward
    ``max_response_bytes`` as it arrives, like over ``/a2a``.

    Args:
        send: The ASGI ``send`` of the response.
        max_bytes: The host's ``max_response_bytes``.
    """

    def __init__(self, send: Send, max_bytes: int) -> None:
        self._send = send
        self._encoder = EventEncoder()
        self._max_bytes = max_bytes
        self.invocation_id: str | None = None
        self._message_id: str | None = None
        self._open_tool_calls: set[str] = set()
        # The text of the latest assistant message, open or closed.
        self._message_text: list[str] = []
        self._streamed_bytes = 0

    async def emit(self, event: BaseEvent) -> None:
        """Send one event as one server-sent event."""
        data = self._encoder.encode(event).encode()
        await self._send(
            {"type": "http.response.body", "body": data, "more_body": True}
        )

    async def text(self, piece: str) -> None:
        """Send one piece of streamed model text (the run's ``TextSink``).

        Raises:
            LimitExceededError: The text streamed so far would exceed
                ``max_response_bytes``; the piece is not sent.
        """
        if not piece:
            return
        self._streamed_bytes += len(piece.encode())
        if self._streamed_bytes > self._max_bytes:
            raise LimitExceededError(
                RESPONSE_TOO_LARGE_MESSAGE, invocation_id=self.invocation_id
            )
        if self._message_id is None:
            self._message_id = str(uuid.uuid4())
            await self.emit(
                TextMessageStartEvent(message_id=self._message_id, role="assistant")
            )
            self._message_text = []
        self._message_text.append(piece)
        await self.emit(
            TextMessageContentEvent(message_id=self._message_id, delta=piece)
        )

    async def tool(self, status: ToolStatus) -> None:
        """Send a tool call's start or end, by name only."""
        if not status.finished:
            await self.end_message()
            self._open_tool_calls.add(status.call_id)
            await self.emit(
                ToolCallStartEvent(
                    tool_call_id=status.call_id, tool_call_name=status.name
                )
            )
            return
        self._open_tool_calls.discard(status.call_id)
        await self.emit(ToolCallEndEvent(tool_call_id=status.call_id))

    async def end_message(self) -> None:
        """Close the open assistant message, if there is one."""
        if self._message_id is not None:
            await self.emit(TextMessageEndEvent(message_id=self._message_id))
            self._message_id = None

    async def finish_text(self, text: str) -> None:
        """Close streamed text, then send ``text`` as a message of its own.

        Nothing more is sent when the latest streamed message already ends
        with ``text``: the result is what the model streamed last, and text
        streamed before a tool call or an earlier model call stays released.

        Raises:
            LimitExceededError: ``text`` is larger than ``max_response_bytes``.
        """
        await self.end_message()
        if not text or "".join(self._message_text).endswith(text):
            return
        assert self.invocation_id is not None  # Set when the run started.
        check_response_size(text.encode(), self._max_bytes, self.invocation_id)
        message_id = str(uuid.uuid4())
        await self.emit(TextMessageStartEvent(message_id=message_id, role="assistant"))
        await self.emit(TextMessageContentEvent(message_id=message_id, delta=text))
        await self.emit(TextMessageEndEvent(message_id=message_id))

    async def close_open(self) -> None:
        """Close the open message and tool calls, before a cancelled end."""
        await self.end_message()
        for call_id in sorted(self._open_tool_calls):
            await self.emit(ToolCallEndEvent(tool_call_id=call_id))
        self._open_tool_calls.clear()

    async def fail(self, message: str, code: str) -> None:
        """End the run with ``RUN_ERROR``, which also ends whatever is open."""
        await self.emit(
            RunErrorEvent(
                message=message,
                code=code,
                metadata={"invocation_id": self.invocation_id},
            )
        )


class _EventStream:
    """The response that streams one run; a client disconnect cancels the run.

    The run is a task of its own, so that a disconnect noticed while it is
    quiet cancels it at once, whatever ASGI version the server speaks.
    The run's slot is released when the response ends, however it ends.
    """

    def __init__(
        self,
        run: Callable[[_RunEvents], Coroutine[Any, Any, None]],
        slot: RunSlot,
        running: set[asyncio.Task[None]],
        max_bytes: int,
    ) -> None:
        self._run = run
        self._slot = slot
        self._running = running
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self._stream(receive, send)
        finally:
            self._slot.release()

    async def _stream(self, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", EventEncoder().get_content_type().encode()),
                    (b"cache-control", b"no-cache"),
                    (b"x-accel-buffering", b"no"),
                ],
            }
        )
        run = asyncio.create_task(self._run(_RunEvents(send, self._max_bytes)))
        self._running.add(run)
        disconnect = asyncio.create_task(_disconnected(receive))
        try:
            await asyncio.wait({run, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            run.cancel()
            disconnect.cancel()
            await asyncio.gather(run, disconnect, return_exceptions=True)
            self._running.discard(run)
        if disconnect.done() and not disconnect.cancelled():
            return  # The client has gone: there is no one to finish for.
        error = None if run.cancelled() else run.exception()
        if error is not None:
            raise error  # Sending failed: the server logs it and drops the stream.
        await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _disconnected(receive: Receive) -> None:
    """Return once the client disconnects; the request body was already read."""
    while (await receive())["type"] != "http.disconnect":
        pass
