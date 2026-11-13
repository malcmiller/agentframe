"""Serve an agent over A2A 1.0 as an ASGI application."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from a2a.auth.user import UnauthenticatedUser, User
from a2a.extensions.common import HTTP_EXTENSION_HEADER, get_requested_extensions
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    DefaultServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    SendMessageRequest,
    Task,
    TaskState,
)
from a2a.utils.errors import TaskNotFoundError, UnsupportedOperationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from agentframe.authoring.agent import Agent
from agentframe.authoring.services import Services
from agentframe.errors import AuthenticationError, DefinitionError
from agentframe.hosting.a2a_executor import (
    PRINCIPAL_STATE_KEY,
    RuntimeExecutor,
    is_skill_call,
    read_skill_call,
)
from agentframe.hosting.admission import RUN_SLOT_STATE_KEY, RunAdmission, RunSlot
from agentframe.hosting.card import A2A_PATH, SKILL_CALL_EXTENSION_URI, build_agent_card
from agentframe.hosting.host import HostConfig
from agentframe.hosting.lifecycle import Lifecycle
from agentframe.hosting.model_router import ModelRouter
from agentframe.hosting.request_size import read_body_within, with_body
from agentframe.hosting.routing import SkillRouter, routing_limits
from agentframe.runtime.limits import RunLimits, check_within_ceiling
from agentframe.runtime.options import merge_settings
from agentframe.runtime.runtime import Runtime
from agentframe.security.authentication import authenticate_request, www_authenticate
from agentframe.security.authorization import AuthorizationPolicy
from agentframe.security.execution_policy import ExecutionPolicy
from agentframe.security.principal import Principal

type Endpoint = Callable[[Request], Awaitable[Response]]

HEALTH_PATH = "/health"
"""Liveness probe: ``200 {"status": "ok"}`` whenever the app is serving."""

READY_PATH = "/ready"
"""Readiness probe: ``200`` after startup succeeded until shutdown starts."""

_CANCEL_SECONDS = 5
"""How long shutdown waits for runs to unwind once it has cancelled them."""

logger = logging.getLogger(__name__)


class AgentApp:
    """An ASGI application that hosts one agent over A2A 1.0.

    Serves the public Agent Card at ``/.well-known/agent-card.json`` and the
    JSON-RPC A2A endpoint at ``/a2a``. Every request to ``/a2a`` must pass the
    host's authenticator, then runs through the runtime. Tasks live in the
    SDK's in-memory store, scoped to the caller, and are lost on restart;
    anonymous callers share one namespace and cannot list tasks. Messages that
    do not activate the skill-call extension are free text, routed by
    ``router``. Constructing
    the app validates everything that needs no I/O and starts nothing; serve
    it with any ASGI server. Server startup then checks the model's input
    token limits with ``Runtime.validate_model`` and fails if one is unknown,
    and checks a ``ModelRouter``'s model the same way. The app never closes
    models; the application owns them.
    Hosted runs and routing use limits capped by ``host.limits_ceiling``.
    At most ``host.max_concurrent_runs`` messages run at once; another
    ``SendMessage`` gets HTTP 503 with ``Retry-After: 1`` and creates no task.
    A task waiting for input holds no slot; its answer needs a free one.
    A request body over ``host.max_request_bytes`` gets HTTP 413 before
    authentication and creates no task; a result or question over
    ``host.max_response_bytes`` fails its task instead of being sent.

    ``/health`` (liveness) and ``/ready`` (readiness) need no credentials and
    say nothing about the agent. ``/ready`` answers 200 only after startup
    succeeded and until shutdown starts. Shutdown then refuses new messages
    with HTTP 503 and ``Retry-After: 1``, while task reads and cancels keep
    working; waits up to ``host.shutdown_grace_seconds`` for running runs to
    finish; and cancels the rest, so their tasks end ``CANCELED`` and their
    per-run services are cleaned up. Shutdown starts at ASGI lifespan
    shutdown, or on the shutdown signal under ``agentframe run``.

    Args:
        agent: Instance of an ``@agent``-decorated class.
        host: Where and how the agent is served, including authentication.
        runtime: The runtime that runs every request. Defaults to a new
            ``Runtime`` built from ``services``, ``authorization`` and
            ``policy``. Pass
            one to authorize tool calls with ``Runtime(tool_authorization=...)``.
        services: Services for ``Inject[T]`` parameters, when ``runtime`` is
            not given.
        authorization: Resource authorization policy, when ``runtime`` is not
            given.
        policy: Execution policy content checks, when ``runtime`` is not
            given. Its model-request checks also cover routing requests.
        router: Selects the skill for free-text messages or asks the caller a
            question. ``None`` refuses free text with a protocol error, even
            when the agent has a model. A ``ModelRouter``'s schemas are built
            here and its model is checked at server startup.

    Raises:
        DefinitionError: The agent is invalid, a skill's injected service is
            not registered, an agent or skill limit is explicitly set above
            ``host.limits_ceiling``, ``runtime`` is combined with ``services``,
            ``authorization`` or ``policy``, the authenticator cannot describe its
            security on the Agent Card, or a ``ModelRouter`` cannot describe a
            skill's arguments as a JSON schema.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        host: HostConfig,
        runtime: Runtime | None = None,
        services: Services | None = None,
        authorization: AuthorizationPolicy | None = None,
        policy: ExecutionPolicy | None = None,
        router: SkillRouter | None = None,
    ) -> None:
        if runtime is not None and (
            services is not None or authorization is not None or policy is not None
        ):
            raise DefinitionError(
                "Pass services, authorization and policy either to AgentApp or "
                "to its Runtime, not both; the runtime is the single source of them."
            )
        if runtime is None:
            runtime = Runtime(
                services=services, authorization=authorization, policy=policy
            )
        runtime.validate(agent)
        _check_limits_within_ceiling(agent, host)
        definition = type(agent).definition()
        if isinstance(router, ModelRouter):
            router.validate_skills(definition.skills.values())
        card = build_agent_card(definition, host)
        handler = _RequestHandler(
            router=router,
            agent_executor=RuntimeExecutor(
                agent,
                runtime,
                router,
                host.limits_ceiling,
                host.max_response_bytes,
            ),
            task_store=InMemoryTaskStore(),
            agent_card=card,
        )
        self._host = host
        admission = RunAdmission(host.max_concurrent_runs)
        lifecycle = Lifecycle(host.shutdown_grace_seconds)
        self._lifecycle = lifecycle
        [a2a_route] = create_jsonrpc_routes(
            handler, rpc_url=A2A_PATH, context_builder=_CallerContextBuilder()
        )

        @asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            # Fails startup, before any request is served, when a skill or
            # the built-in router has no input token limit. Custom routers
            # check their own models.
            await runtime.validate_model(agent, limits_ceiling=host.limits_ceiling)
            if isinstance(router, ModelRouter):
                await router.validate_model(routing_limits(agent, host.limits_ceiling))
            lifecycle.mark_started()
            yield
            await lifecycle.wait_for_runs(admission)
            await handler.cancel_running_tasks()
            # Models are application-owned, so they are never closed here.
            await handler.aclose()

        self._app = Starlette(
            routes=[
                *create_agent_card_routes(card),
                Route(HEALTH_PATH, _health, methods=["GET"]),
                Route(READY_PATH, _readiness_endpoint(lifecycle), methods=["GET"]),
                Route(
                    A2A_PATH,
                    _guard_a2a_endpoint(
                        _acknowledge_skill_call_extension(a2a_route.endpoint),
                        host,
                        card,
                        admission,
                        lifecycle,
                    ),
                    methods=["POST"],
                ),
            ],
            lifespan=lifespan,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection."""
        await self._app(scope, receive, send)

    def begin_shutdown(self) -> None:
        """Start shutdown now: stop being ready and refuse new messages.

        Package-internal: ``agentframe run`` calls it from Uvicorn's signal
        handler, because Uvicorn waits for open requests before ASGI lifespan
        shutdown. The grace period is measured from the first call, and lifespan
        shutdown then only waits for what remains of it. Safe to call more
        than once and from a signal handler.
        """
        self._lifecycle.begin_shutdown()


def _check_limits_within_ceiling(agent: Agent, host: HostConfig) -> None:
    """Fail when an agent or skill explicitly sets a limit above the ceiling."""
    if host.limits_ceiling is None:
        return
    for skill in type(agent).definition().skills.values():
        explicit = merge_settings(RunLimits(), agent.limits, skill.limits)
        check_within_ceiling(explicit, host.limits_ceiling, f"Skill '{skill.id}'")


async def _health(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def _readiness_endpoint(lifecycle: Lifecycle) -> Endpoint:
    async def readiness(_request: Request) -> Response:
        if lifecycle.ready:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not ready"}, 503)

    return readiness


def _guard_a2a_endpoint(
    sdk_endpoint: Endpoint,
    host: HostConfig,
    card: AgentCard,
    admission: RunAdmission,
    lifecycle: Lifecycle,
) -> Endpoint:
    """Wrap the SDK's A2A endpoint with the host's checks, in a fixed order.

    1. Size: the body must fit ``host.max_request_bytes``. It is read here,
       before anything parses it, so an oversized body is never held in full.
    2. Authentication: every request needs a caller the host verifies.
    3. Admission: a ``SendMessage`` needs a free run slot and a host that
       is not shutting down.

    Every refusal happens before the SDK sees the request, so no task exists.
    Deduplication belongs between authentication and admission, so a
    repeated message takes no slot.
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
            request.state.principal = await authenticate_request(
                host.auth, request.headers, host.role_permissions
            )
        except AuthenticationError:
            # The reply reveals nothing about the agent or its skills.
            return JSONResponse(
                {"error": "Authentication required."}, 401, headers=challenge_headers
            )

        if not await _is_send_message(request):
            return await sdk_endpoint(request)
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


def _acknowledge_skill_call_extension(endpoint: Endpoint) -> Endpoint:
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


class _RequestHandler(DefaultRequestHandler):
    """The SDK's request handler with AgentFrame's up-front refusals.

    The SDK creates or resumes a task before it calls the executor, so a
    message that must get a protocol error instead must be refused here:

    - a malformed skill call;
    - free text when no ``SkillRouter`` is configured;
    - a message for one of the caller's tasks that is not waiting for input.
      The SDK already hides other callers' tasks behind ``TaskNotFoundError``,
      so only the task's owner can answer its question.

    Anonymous callers cannot list tasks. They share one task namespace:
    fetching or cancelling a task needs its unguessable ID, but listing would
    hand every anonymous caller the IDs, inputs and results of every other
    anonymous caller's tasks.

    Args:
        router: The executor's router; ``None`` refuses free text.
        agent_executor: Runs each message.
        **sdk_options: Passed to the SDK's ``DefaultRequestHandler``.
    """

    def __init__(
        self,
        *,
        router: SkillRouter | None,
        agent_executor: RuntimeExecutor,
        **sdk_options: Any,
    ) -> None:
        super().__init__(agent_executor=agent_executor, **sdk_options)
        self._router = router
        self._executor = agent_executor

    async def cancel_running_tasks(self) -> None:
        """Cancel every running task as its caller's ``CancelTask`` would.

        Each task ends ``CANCELED`` and its run unwinds, cleaning up its
        per-run services. Waiting for that is bounded, because the handler
        is force-closed next anyway.
        """
        running = self._executor.running_tasks()
        if not running:
            return
        logger.warning("Cancelling %d run(s) still going at shutdown", len(running))
        try:
            async with asyncio.timeout(_CANCEL_SECONDS):
                await asyncio.gather(
                    *(
                        self._cancel_quietly(task_id, context)
                        for task_id, context in running.items()
                    )
                )
        except TimeoutError:
            logger.error("Runs did not stop within %d seconds", _CANCEL_SECONDS)

    async def _cancel_quietly(self, task_id: str, context: ServerCallContext) -> None:
        # noinspection PyBroadException
        try:
            await self.on_cancel_task(CancelTaskRequest(id=task_id), context)
        except Exception:
            # The run may have just finished; shutdown goes on either way.
            logger.debug("Could not cancel task %s at shutdown", task_id, exc_info=True)

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Message | Task:
        if is_skill_call(context.requested_extensions):
            read_skill_call(params.message)
        elif self._router is None:
            raise UnsupportedOperationError(
                "This agent has no skill router for free-text requests. Activate "
                f"the '{SKILL_CALL_EXTENSION_URI}' extension and name a skill."
            )
        if params.message.task_id:
            await self._prepare_answer(params.message, context)
        response: Message | Task = await super().on_message_send(params, context)
        return response

    async def _prepare_answer(
        self, message: Message, context: ServerCallContext
    ) -> None:
        """Check that ``message`` answers a task waiting for the caller's input.

        An answer that omits its ``contextId`` joins the task's context; the SDK
        would otherwise give it a new one.

        Two answers sent at once can both pass this check; the SDK runs one
        task's messages one at a time, so the later answer is routed after the
        earlier one finishes or fails on the finished task.

        Raises:
            UnsupportedOperationError: The task is working or finished.
        """
        try:
            task: Task = await self.on_get_task(
                GetTaskRequest(id=message.task_id), context
            )
        except TaskNotFoundError:
            return  # The SDK reports it the same way for the message.
        if task.status.state != TaskState.TASK_STATE_INPUT_REQUIRED:
            raise UnsupportedOperationError(
                f"Task {task.id} is not waiting for input; start a new task."
            )
        if not message.context_id:
            message.context_id = task.context_id

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        if not context.user.is_authenticated:
            raise UnsupportedOperationError(
                "Listing tasks requires an authenticated caller."
            )
        page: ListTasksResponse = await super().on_list_tasks(params, context)
        return page


class _CallerContextBuilder(DefaultServerCallContextBuilder):
    """Add the caller and any run slot to the SDK's per-request call context."""

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.state[PRINCIPAL_STATE_KEY] = request.state.principal
        slot: RunSlot | None = getattr(request.state, "run_slot", None)
        if slot is not None:
            context.state[RUN_SLOT_STATE_KEY] = slot
        return context

    def build_user(self, request: Request) -> User:
        principal: Principal | None = request.state.principal
        if principal is None:
            return UnauthenticatedUser()
        return _Caller(principal)


class _Caller(User):
    """An authenticated caller; the SDK scopes tasks by its ``user_name``."""

    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._principal.id
