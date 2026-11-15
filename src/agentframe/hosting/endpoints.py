"""The app's HTTP endpoints and the host's checks around the SDK's A2A endpoint."""

from collections.abc import Awaitable, Callable

from a2a.extensions.common import HTTP_EXTENSION_HEADER, get_requested_extensions
from a2a.types import AgentCard, Message, SendMessageRequest

# protobuf ships no type stubs; ParseDict is the parser the A2A SDK itself uses.
from google.protobuf.json_format import ParseDict  # type: ignore[import-untyped]
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agentframe.discovery.identity import UserToken, UserTokenExchange
from agentframe.errors import AuthenticationError
from agentframe.hosting.admission import RunAdmission
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.hosting.dedup import FindTask, MessageDeduplicator
from agentframe.hosting.host import HostConfig
from agentframe.hosting.lifecycle import Lifecycle
from agentframe.hosting.request_size import read_body_within, with_body
from agentframe.security.authentication import principal_for, www_authenticate
from agentframe.security.principal import Principal

type Endpoint = Callable[[Request], Awaitable[Response]]


async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def readiness_endpoint(lifecycle: Lifecycle) -> Endpoint:
    async def readiness(_request: Request) -> Response:
        if lifecycle.ready:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not ready"}, 503)

    return readiness


def guard_a2a_endpoint(
    sdk_endpoint: Endpoint,
    host: HostConfig,
    user_token_exchange: UserTokenExchange | None,
    card: AgentCard,
    deduplicator: MessageDeduplicator,
    task_finder: Callable[[Request], FindTask],
    admission: RunAdmission,
    lifecycle: Lifecycle,
) -> Endpoint:
    """Wrap the SDK's A2A endpoint with the host's checks, in a fixed order.

    1. Size: the body must fit ``host.max_request_bytes``. It is read here,
       before anything parses it, so an oversized body is never held in full.
    2. Authentication: every request needs a caller the host verifies. When
       ``user_token_exchange`` is given, a signed-in user's verified token
       is kept for the run's ``OnBehalfOf`` remotes, never anywhere else.
    3. Deduplication: a ``SendMessage`` that repeats one the caller already
       sent goes to the SDK for its original task, taking no run slot.
    4. Admission: any other ``SendMessage`` needs a free run slot and a host
       that is not shutting down.

    Every refusal happens before the SDK sees the request, so no task exists.
    """
    challenge = www_authenticate(card.security_schemes)
    challenge_headers = {"WWW-Authenticate": challenge} if challenge else None

    async def guarded_endpoint(request: Request) -> Response:
        body = await read_body_within(request, host.max_request_bytes)
        if body is None:
            # Nothing about the body is logged or echoed back.
            return JSONResponse({"error": "Request too large."}, 413)
        request = with_body(request, body)

        try:
            caller = await host.auth.authenticate(request.headers)
        except AuthenticationError:
            # The reply reveals nothing about the agent or its skills.
            return JSONResponse(
                {"error": "Authentication required."}, 401, headers=challenge_headers
            )
        request.state.principal = (
            principal_for(caller, host.role_permissions) if caller else None
        )
        request.state.user_token = None
        if user_token_exchange and caller and caller.user_assertion:
            request.state.user_token = UserToken(
                caller.user_assertion, user_token_exchange
            )

        if not await _is_send_message(request):
            return await sdk_endpoint(request)
        principal: Principal | None = request.state.principal
        message = await _read_message(request)
        # Anonymous callers share one namespace, so a guessable messageId
        # would hand one of them another's task.
        if principal is None or message is None or not message.message_id:
            return await admit_and_send(request)
        find_task = task_finder(request)
        async with deduplicator.check(principal.id, message, find_task) as outcome:
            request.state.dedup = outcome
            if outcome.is_repeat:
                return await sdk_endpoint(request)
            return await admit_and_send(request)

    async def admit_and_send(request: Request) -> Response:
        if lifecycle.shutting_down:
            return _unavailable("The agent is shutting down; retry later.")
        slot = admission.try_admit()
        if slot is None:
            # Overload fails fast: the caller retries rather than waiting here.
            return _unavailable("The agent is busy; retry later.")
        request.state.run_slot = slot
        try:
            with lifecycle.track_send():
                return await sdk_endpoint(request)
        finally:
            # A started run releases its own slot when it ends, which can be
            # after a non-blocking response. A message the SDK refused never
            # started a run.
            if not slot.started:
                slot.release()

    return guarded_endpoint


def _unavailable(error: str) -> Response:
    return JSONResponse({"error": error}, 503, headers={"Retry-After": "1"})


async def _is_send_message(request: Request) -> bool:
    """Return whether the JSON-RPC request is ``SendMessage``.

    It is the only method that runs a skill: task reads, lists and cancels
    take no run slot, and the SDK refuses streaming, which the card does not
    advertise. Malformed JSON is left for the SDK to report.
    """
    try:
        body = await request.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("method") == "SendMessage"


async def _read_message(request: Request) -> Message | None:
    """Return a ``SendMessage`` request's message, parsed as the SDK parses it.

    The JSON body was already read and parsed by ``_is_send_message``; this
    reads no more of the request. Malformed parameters give ``None`` and are
    left for the SDK to report.
    """
    params = (await request.json()).get("params", {})
    try:
        send = ParseDict(params, SendMessageRequest(), ignore_unknown_fields=True)
    except Exception:  # The SDK reports the same parse failure.
        return None
    message: Message = send.message
    return message


def acknowledge_skill_call_extension(endpoint: Endpoint) -> Endpoint:
    """Wrap ``endpoint`` to confirm activation of the skill-call extension.

    A2A clients activate an extension by listing its URI in the
    ``A2A-Extensions`` request header; the response names the extensions the
    agent activated in the same header. The skill-call extension is the only
    one this agent supports, so other requested URIs are ignored.
    """

    async def acknowledging_endpoint(request: Request) -> Response:
        response = await endpoint(request)
        requested = get_requested_extensions(
            request.headers.getlist(HTTP_EXTENSION_HEADER)
        )
        if SKILL_CALL_EXTENSION_URI in requested:
            response.headers[HTTP_EXTENSION_HEADER] = SKILL_CALL_EXTENSION_URI
        return response

    return acknowledging_endpoint
