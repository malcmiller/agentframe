"""Serve an agent over A2A 1.0 as an ASGI application."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from a2a.auth.user import UnauthenticatedUser, User
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    DefaultServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import ListTasksRequest, ListTasksResponse
from a2a.utils.errors import UnsupportedOperationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from agentframe.a2a_executor import PRINCIPAL_STATE_KEY, RuntimeExecutor
from agentframe.agent import Agent
from agentframe.auth import authenticate_request
from agentframe.authorization import AuthorizationPolicy
from agentframe.card import A2A_PATH, build_agent_card
from agentframe.errors import AuthenticationError, DefinitionError
from agentframe.host import HostConfig
from agentframe.principal import Principal
from agentframe.runtime import Runtime
from agentframe.services import Services

type Endpoint = Callable[[Request], Awaitable[Response]]


class AgentApp:
    """An ASGI application that hosts one agent over A2A 1.0.

    Serves the public Agent Card at ``/.well-known/agent-card.json`` and the
    JSON-RPC A2A endpoint at ``/a2a``. Every request to ``/a2a`` must pass the
    host's authenticator, then runs through the runtime. Tasks live in the
    SDK's in-memory store, scoped to the caller, and are lost on restart;
    anonymous callers share one namespace and cannot list tasks. Constructing
    the app validates everything and starts nothing; serve it with any ASGI
    server.

    Args:
        agent: Instance of an ``@agent``-decorated class.
        host: Where and how the agent is served, including authentication.
        runtime: The runtime that runs every request. Defaults to a new
            ``Runtime`` built from ``services`` and ``authorization``.
        services: Services for ``Inject[T]`` parameters, when ``runtime`` is
            not given.
        authorization: Resource authorization policy, when ``runtime`` is not
            given.

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
        handler = _TaskRequestHandler(
            agent_executor=RuntimeExecutor(agent, runtime),
            task_store=InMemoryTaskStore(),
            agent_card=card,
        )
        [a2a_route] = create_jsonrpc_routes(
            handler, rpc_url=A2A_PATH, context_builder=_CallerContextBuilder()
        )

        @asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            yield
            await handler.aclose()

        self._app = Starlette(
            routes=[
                *create_agent_card_routes(card),
                Route(
                    A2A_PATH,
                    _require_authentication(a2a_route.endpoint, host),
                    methods=["POST"],
                ),
            ],
            lifespan=lifespan,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection."""
        await self._app(scope, receive, send)


def _require_authentication(endpoint: Endpoint, host: HostConfig) -> Endpoint:
    """Wrap ``endpoint`` so it only runs for callers the host authenticates."""

    async def authenticated_endpoint(request: Request) -> Response:
        try:
            principal = await authenticate_request(
                host.auth, request.headers, host.role_permissions
            )
        except AuthenticationError:
            # Rejected before the A2A SDK sees the request: no task exists and
            # the reply reveals nothing about the agent or its skills.
            return JSONResponse({"error": "Authentication required."}, 401)
        request.state.principal = principal
        return await endpoint(request)

    return authenticated_endpoint


class _TaskRequestHandler(DefaultRequestHandler):
    """The SDK's request handler, with task listing refused to anonymous callers.

    All anonymous callers share one task namespace. Fetching or cancelling a
    task needs its unguessable ID, but listing would hand every anonymous
    caller the IDs, inputs and results of every other anonymous caller's tasks.
    """

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
