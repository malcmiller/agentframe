"""Serve an agent over A2A 1.0 as an ASGI application."""

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
from agentframe.hosting.card import A2A_PATH, SKILL_CALL_EXTENSION_URI, build_agent_card
from agentframe.hosting.host import HostConfig
from agentframe.hosting.routing import SkillRouter
from agentframe.runtime.runtime import Runtime
from agentframe.security.authentication import authenticate_request, www_authenticate
from agentframe.security.authorization import AuthorizationPolicy
from agentframe.security.principal import Principal

type Endpoint = Callable[[Request], Awaitable[Response]]


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
    token limits with ``Runtime.validate_model`` and fails if one is unknown.

    Args:
        agent: Instance of an ``@agent``-decorated class.
        host: Where and how the agent is served, including authentication.
        runtime: The runtime that runs every request. Defaults to a new
            ``Runtime`` built from ``services`` and ``authorization``. Pass
            one to authorize tool calls with ``Runtime(tool_authorization=...)``.
        services: Services for ``Inject[T]`` parameters, when ``runtime`` is
            not given.
        authorization: Resource authorization policy, when ``runtime`` is not
            given.
        router: Selects the skill for free-text messages or asks the caller a
            question. ``None`` refuses free text with a protocol error.

    Raises:
        DefinitionError: The agent is invalid, a skill's injected service is
            not registered, ``runtime`` is combined with ``services`` or
            ``authorization``, or the authenticator cannot describe its
            security on the Agent Card.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        host: HostConfig,
        runtime: Runtime | None = None,
        services: Services | None = None,
        authorization: AuthorizationPolicy | None = None,
        router: SkillRouter | None = None,
    ) -> None:
        if runtime is not None and (services is not None or authorization is not None):
            raise DefinitionError(
                "Pass services and authorization either to AgentApp or to its "
                "Runtime, not both; the runtime is the single source of them."
            )
        if runtime is None:
            runtime = Runtime(services=services, authorization=authorization)
        runtime.validate(agent)
        card = build_agent_card(type(agent).definition(), host)
        handler = _RequestHandler(
            router=router,
            agent_executor=RuntimeExecutor(agent, runtime, router),
            task_store=InMemoryTaskStore(),
            agent_card=card,
        )
        [a2a_route] = create_jsonrpc_routes(
            handler, rpc_url=A2A_PATH, context_builder=_CallerContextBuilder()
        )

        @asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            # Fails startup, before any request is served, when a skill has
            # no input token limit.
            await runtime.validate_model(agent)
            yield
            await handler.aclose()

        self._app = Starlette(
            routes=[
                *create_agent_card_routes(card),
                Route(
                    A2A_PATH,
                    _require_authentication(
                        _acknowledge_skill_call_extension(a2a_route.endpoint),
                        host,
                        card,
                    ),
                    methods=["POST"],
                ),
            ],
            lifespan=lifespan,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection."""
        await self._app(scope, receive, send)


def _require_authentication(
    endpoint: Endpoint, host: HostConfig, card: AgentCard
) -> Endpoint:
    """Wrap ``endpoint`` so it only runs for callers the host authenticates.

    Rejected callers get a 401 whose ``WWW-Authenticate`` header names the
    security schemes the Agent Card declares.
    """
    challenge = www_authenticate(card.security_schemes)
    challenge_headers = {"WWW-Authenticate": challenge} if challenge else None

    async def authenticated_endpoint(request: Request) -> Response:
        try:
            principal = await authenticate_request(
                host.auth, request.headers, host.role_permissions
            )
        except AuthenticationError:
            # Rejected before the A2A SDK sees the request: no task exists and
            # the reply reveals nothing about the agent or its skills.
            return JSONResponse(
                {"error": "Authentication required."}, 401, headers=challenge_headers
            )
        request.state.principal = principal
        return await endpoint(request)

    return authenticated_endpoint


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
        **sdk_options: Passed to the SDK's ``DefaultRequestHandler``.
    """

    def __init__(self, *, router: SkillRouter | None, **sdk_options: Any) -> None:
        super().__init__(**sdk_options)
        self._router = router

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
    """Add the authenticated caller to the SDK's per-request call context."""

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.state[PRINCIPAL_STATE_KEY] = request.state.principal
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
