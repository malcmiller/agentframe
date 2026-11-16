"""Serve an agent over A2A 1.0 as an ASGI application."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskStore
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
from a2a.utils.errors import (
    InvalidParamsError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import BaseRoute, Route
from starlette.types import Receive, Scope, Send

from agentframe.authoring.agent import Agent
from agentframe.authoring.services import Services
from agentframe.discovery.directory import AgentDirectory
from agentframe.discovery.identity import (
    OnBehalfOf,
    Service,
    UserTokenExchange,
)
from agentframe.errors import DefinitionError
from agentframe.hosting.a2a_executor import (
    RuntimeExecutor,
    incoming_context,
    is_skill_call,
    read_skill_call,
)
from agentframe.hosting.admission import RunAdmission
from agentframe.hosting.card import A2A_PATH, SKILL_CALL_EXTENSION_URI, build_agent_card
from agentframe.hosting.context_builders import (
    CallerContextBuilder,
    ClaimedTaskIdBuilder,
)
from agentframe.hosting.dedup import (
    DEDUP_STATE_KEY,
    DedupOutcome,
    FindTask,
    InMemoryMessageDedupStore,
    MessageDeduplicator,
)
from agentframe.hosting.default_skill import DefaultSkill
from agentframe.hosting.endpoints import (
    acknowledge_skill_call_extension,
    guard_a2a_endpoint,
    health,
    readiness_endpoint,
)
from agentframe.hosting.host import A2AConfig, HostConfig, warn_if_insecure
from agentframe.hosting.lifecycle import Lifecycle
from agentframe.hosting.model_router import ModelRouter
from agentframe.hosting.routing import SkillRouter, routing_limits
from agentframe.hosting.task_store import BoundedTaskStore, FinalStateStore
from agentframe.runtime.limits import RunLimits, check_within_ceiling
from agentframe.runtime.options import merge_settings
from agentframe.runtime.runtime import Runtime
from agentframe.security.authentication import AnyOf
from agentframe.security.authorization import (
    AuthorizationPolicy,
    ToolAuthorizationPolicy,
)
from agentframe.security.execution_policy import ExecutionPolicy

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
    host's authenticator, then runs through the runtime. Tasks are scoped to
    the caller and kept in ``a2a.task_store``, by default a bounded in-memory
    store whose tasks are lost on restart; anonymous callers share one
    namespace and cannot list tasks. Messages that
    do not name a skill through the skill-call extension are free text, routed
    by ``router``, or else passed to ``default_skill``. A caller's deadline
    and call depth, sent in the extension,
    shorten hosted runs and reject requests deeper than
    ``RunLimits.max_remote_hops``. Constructing
    the app validates everything that needs no I/O and starts nothing; serve
    it with any ASGI server. Server startup then checks the model's input
    token limits with ``Runtime.validate_model`` and fails if one is unknown,
    checks a ``ModelRouter``'s model the same way, and checks every
    ``Remote`` parameter's agent with ``Runtime.validate_remotes``. The app
    never closes models or the directory; the application owns them.
    Construction logs a warning, without failing, when ``host`` is legal but
    insecure: a plain ``http://`` ``public_url`` on a non-loopback host with
    credential-bearing authentication, or ``AllowAnonymous`` on a
    non-loopback ``public_url``; not under ``AGENTFRAME_DEV=1``.
    Hosted runs and routing use limits capped by ``host.limits_ceiling``.
    At most ``host.max_concurrent_runs`` messages run at once; another
    ``SendMessage`` gets HTTP 503 with ``Retry-After: 1`` and creates no task.
    A task waiting for input holds no slot; its answer needs a free one.
    A request body over ``host.max_request_bytes`` gets HTTP 413 before
    authentication and creates no task; a result or question over
    ``host.max_response_bytes`` fails its task instead of being sent.
    A message that repeats one the same verified caller sent with the same
    ``messageId`` within ``a2a.dedup_window_seconds`` gets the original task
    back, whatever its state, and takes no run slot. Reusing a ``messageId``
    for a different message is refused with ``InvalidParams``. Anonymous
    callers are not deduplicated. This is not exactly-once execution.

    ``/health`` (liveness) and ``/ready`` (readiness) need no credentials and
    say nothing about the agent. ``/ready`` answers 200 only after startup
    succeeded and until shutdown starts. Shutdown then refuses new messages
    with HTTP 503 and ``Retry-After: 1``, while task reads and cancels keep
    working; waits up to ``host.shutdown_grace_seconds`` for running runs to
    finish and their final task states to be stored; and cancels the rest,
    so their tasks end ``CANCELED`` and their per-run services are cleaned
    up. Shutdown starts at ASGI lifespan shutdown, or on the shutdown signal
    under ``agentframe run``.

    Args:
        agent: Instance of an ``@agent``-decorated class.
        host: Where and how the agent is served, including authentication.
        runtime: The runtime that runs every request. Defaults to a new
            ``Runtime`` built from ``services``, ``authorization``,
            ``tool_authorization``, ``policy``, ``directory``, ``remotes``
            and ``default_remote``.
        services: Services for ``Inject[T]`` parameters, when ``runtime`` is
            not given.
        authorization: Resource authorization policy, when ``runtime`` is not
            given.
        tool_authorization: Policy that checks every tool call a model asks
            for, with its validated arguments, before it runs, when
            ``runtime`` is not given; see ``Runtime``. Strongly advised for
            tools with side effects.
        policy: Execution policy content checks, when ``runtime`` is not
            given. Its model-request checks also cover routing requests.
        router: Selects the skill for free-text messages or asks the caller a
            question. ``None`` refuses free text with a protocol error, even
            when the agent has a model, unless ``default_skill`` is set. A
            ``ModelRouter``'s schemas are built here and its model is
            checked at server startup.
        default_skill: ID of the skill that receives free-text messages when
            there is no ``router``, with no model call: the message's text
            parts, joined with newlines, become its only business argument,
            which must be a ``str`` (checked here). A message without text
            is rejected. A ``router``, when given, decides instead.
        a2a: A2A endpoint settings: message deduplication and the task store. Defaults
            to ``A2AConfig()``.
        directory: Where ``Remote[Literal["name"]]`` parameters find their
            agents, when ``runtime`` is not given. Server startup looks up
            every name and fails if one is unknown, its card is invalid, or
            it requires credentials that ``remotes`` cannot provide. The
            application opens and closes it.
        remotes: How to authenticate to each remote agent, by name, when
            ``runtime`` is not given; see ``Runtime``. ``OnBehalfOf`` remotes
            need ``host.auth`` to be ``EntraIDAuth`` with ``client_id=`` and
            ``client_credential=``, alone or as a member of ``AnyOf``; each
            request's verified user token is then exchanged for them, never
            forwarded.
        default_remote: How to authenticate to skill-catalog agents without
            a ``remotes=`` entry, when ``runtime`` is not given; see
            ``Runtime``. An ``OnBehalfOf`` default needs the same
            ``EntraIDAuth`` as ``OnBehalfOf`` remotes.

    Raises:
        DefinitionError: The agent is invalid, a skill's injected service is
            not registered, a skill takes a ``Remote`` parameter and there
            is no directory, an agent or skill limit is explicitly set above
            ``host.limits_ceiling``, ``runtime`` is combined with ``services``,
            ``authorization``, ``tool_authorization``, ``policy``,
            ``directory``, ``remotes`` or
            ``default_remote``, ``remotes`` is not a mapping of names to
            ``Service`` or ``OnBehalfOf`` (or ``default_remote`` is neither),
            an ``OnBehalfOf`` remote's or default's host does not
            authenticate with ``EntraIDAuth`` configured for on-behalf-of
            (API-key and anonymous callers support only ``Service``), the authenticator
            cannot describe its security on the Agent Card, a
            ``ModelRouter`` cannot describe a skill's arguments as a JSON
            schema, or ``default_skill`` is not one of the agent's skills or
            takes anything but exactly one ``str`` business argument.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        host: HostConfig,
        runtime: Runtime | None = None,
        services: Services | None = None,
        authorization: AuthorizationPolicy | None = None,
        tool_authorization: ToolAuthorizationPolicy | None = None,
        policy: ExecutionPolicy | None = None,
        router: SkillRouter | None = None,
        default_skill: str | None = None,
        a2a: A2AConfig | None = None,
        directory: AgentDirectory | None = None,
        remotes: Mapping[str, Service | OnBehalfOf] | None = None,
        default_remote: Service | OnBehalfOf | None = None,
    ) -> None:
        runtime = _runtime_for(
            runtime,
            services=services,
            authorization=authorization,
            tool_authorization=tool_authorization,
            policy=policy,
            directory=directory,
            remotes=remotes,
            default_remote=default_remote,
        )
        runtime.validate(agent)
        warn_if_insecure(host)
        user_token_exchange = _user_token_exchange(runtime, host)
        _check_limits_within_ceiling(agent, host)
        definition = type(agent).definition()
        if isinstance(router, ModelRouter):
            router.validate_skills(definition.skills.values())
        text_skill = (
            DefaultSkill.for_agent(definition, default_skill)
            if default_skill is not None
            else None
        )
        card = build_agent_card(definition, host)
        a2a = a2a or A2AConfig()
        task_store = FinalStateStore(_task_store(a2a))
        handler = _request_handler(
            agent, runtime, router, text_skill, host, card, task_store
        )
        self._host = host
        admission = RunAdmission(host.max_concurrent_runs)
        lifecycle = Lifecycle(host.shutdown_grace_seconds)
        self._lifecycle = lifecycle
        self._app = Starlette(
            routes=_routes(
                card, handler, host, user_token_exchange, a2a, admission, lifecycle
            ),
            lifespan=_lifespan(
                agent, runtime, router, host, admission, lifecycle, task_store, handler
            ),
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

    @property
    def shutdown_grace_seconds(self) -> float:
        """The host's ``shutdown_grace_seconds``.

        Package-internal: ``agentframe run`` limits Uvicorn's wait for open
        requests to it.
        """
        return self._host.shutdown_grace_seconds


def _runtime_for(
    runtime: Runtime | None,
    *,
    services: Services | None,
    authorization: AuthorizationPolicy | None,
    tool_authorization: ToolAuthorizationPolicy | None,
    policy: ExecutionPolicy | None,
    directory: AgentDirectory | None,
    remotes: Mapping[str, Service | OnBehalfOf] | None,
    default_remote: Service | OnBehalfOf | None,
) -> Runtime:
    """Return ``runtime``, or a new ``Runtime`` built from the other arguments.

    Raises:
        DefinitionError: ``runtime`` is combined with any of the others.
    """
    if runtime is not None and (
        services is not None
        or authorization is not None
        or tool_authorization is not None
        or policy is not None
        or directory is not None
        or remotes is not None
        or default_remote is not None
    ):
        raise DefinitionError(
            "Pass services, authorization, tool_authorization, policy, "
            "directory, remotes and default_remote either to AgentApp or to "
            "its Runtime, not both; "
            "the runtime is the single source of them."
        )
    if runtime is None:
        runtime = Runtime(
            services=services,
            authorization=authorization,
            tool_authorization=tool_authorization,
            policy=policy,
            directory=directory,
            remotes=remotes,
            default_remote=default_remote,
        )
    return runtime


def _request_handler(
    agent: Agent,
    runtime: Runtime,
    router: SkillRouter | None,
    default_skill: DefaultSkill | None,
    host: HostConfig,
    card: AgentCard,
    task_store: FinalStateStore,
) -> "_RequestHandler":
    """Return the SDK request handler that runs every message through ``runtime``."""
    return _RequestHandler(
        accepts_free_text=router is not None or default_skill is not None,
        agent_executor=RuntimeExecutor(
            agent,
            runtime,
            router,
            host.limits_ceiling,
            host.max_response_bytes,
            own_interface_url=card.supported_interfaces[0].url,
            final_states=task_store,
            default_skill=default_skill,
        ),
        task_store=task_store,
        agent_card=card,
        request_context_builder=ClaimedTaskIdBuilder(),
    )


def _routes(
    card: AgentCard,
    handler: "_RequestHandler",
    host: HostConfig,
    user_token_exchange: UserTokenExchange | None,
    a2a: A2AConfig,
    admission: RunAdmission,
    lifecycle: Lifecycle,
) -> list[BaseRoute]:
    """Return the Agent Card, health, readiness and guarded A2A routes."""
    deduplicator = MessageDeduplicator(
        a2a.dedup_window_seconds, a2a.dedup_store or InMemoryMessageDedupStore()
    )
    context_builder = CallerContextBuilder(card)
    [a2a_route] = create_jsonrpc_routes(
        handler, rpc_url=A2A_PATH, context_builder=context_builder
    )
    return [
        *create_agent_card_routes(card),
        Route(HEALTH_PATH, health, methods=["GET"]),
        Route(READY_PATH, readiness_endpoint(lifecycle), methods=["GET"]),
        Route(
            A2A_PATH,
            guard_a2a_endpoint(
                acknowledge_skill_call_extension(a2a_route.endpoint),
                host,
                user_token_exchange,
                card,
                deduplicator,
                _task_finder(handler, context_builder),
                admission,
                lifecycle,
            ),
            methods=["POST"],
        ),
    ]


def _lifespan(
    agent: Agent,
    runtime: Runtime,
    router: SkillRouter | None,
    host: HostConfig,
    admission: RunAdmission,
    lifecycle: Lifecycle,
    task_store: FinalStateStore,
    handler: "_RequestHandler",
) -> Callable[[Starlette], AbstractAsyncContextManager[None]]:
    """Return the app's lifespan: startup checks, then graceful shutdown."""

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Fails startup, before any request is served, when a skill or
        # the built-in router has no input token limit, or a remote agent
        # cannot be called. Custom routers check their own models.
        await runtime.validate_model(agent, limits_ceiling=host.limits_ceiling)
        await runtime.validate_remotes(agent)
        if isinstance(router, ModelRouter):
            await router.validate_model(routing_limits(agent, host.limits_ceiling))
        lifecycle.mark_started()
        yield
        await lifecycle.wait_for_runs(admission, task_store)
        await handler.cancel_running_tasks()
        # Models are application-owned, so they are never closed here.
        await handler.aclose()

    return lifespan


def _task_store(a2a: A2AConfig) -> TaskStore:
    """Return the configured task store, or a bounded in-memory one."""
    if a2a.task_store is not None:
        return a2a.task_store
    return BoundedTaskStore(
        InMemoryTaskStore(), a2a.task_retention_seconds, a2a.max_retained_tasks
    )


def _user_token_exchange(
    runtime: Runtime, host: HostConfig
) -> UserTokenExchange | None:
    """Return what exchanges user tokens, when a remote is called on behalf of one.

    Raises:
        DefinitionError: An ``OnBehalfOf`` remote's or default's host does
            not authenticate users with ``EntraIDAuth`` configured for it
            (alone or as the one such member of ``AnyOf``).
    """
    names, default_on_behalf_of = runtime.on_behalf_of_remotes()
    users = [f"Remote '{name}'" for name in names]
    if default_on_behalf_of:
        users.append("default_remote")
    if not users:
        return None
    exchange: object = host.auth
    if isinstance(host.auth, AnyOf):
        exchanges = [m for m in host.auth.members if isinstance(m, UserTokenExchange)]
        if len(exchanges) > 1:
            raise DefinitionError(
                "AnyOf can have only one member that exchanges user tokens "
                "for OnBehalfOf remotes."
            )
        exchange = exchanges[0] if exchanges else None
    if not isinstance(exchange, UserTokenExchange):
        raise DefinitionError(
            f"{users[0]} is called OnBehalfOf the signed-in user, which "
            "needs EntraIDAuth callers; with API-key or anonymous callers, use "
            "Service(credential=...)."
        )
    exchange.check_on_behalf_of()
    return exchange


def _check_limits_within_ceiling(agent: Agent, host: HostConfig) -> None:
    """Fail when an agent or skill explicitly sets a limit above the ceiling."""
    if host.limits_ceiling is None:
        return
    for skill in type(agent).definition().skills.values():
        explicit = merge_settings(RunLimits(), agent.limits, skill.limits)
        check_within_ceiling(explicit, host.limits_ceiling, f"Skill '{skill.id}'")


def _task_finder(
    handler: "_RequestHandler", context_builder: CallerContextBuilder
) -> Callable[[Request], FindTask]:
    """Return a function that looks up tasks as the request's caller."""

    def for_request(request: Request) -> FindTask:
        context = context_builder.build(request)
        return lambda task_id: handler.find_task(task_id, context)

    return for_request


class _RequestHandler(DefaultRequestHandler):
    """The SDK's request handler with AgentFrame's up-front refusals.

    The SDK creates or resumes a task before it calls the executor, so a
    message that must get a protocol error instead must be refused here:

    - a malformed skill call, or malformed caller context in the extension;
    - free text when there is neither a ``SkillRouter`` nor a default skill;
    - a message for one of the caller's tasks that is not waiting for input.
      The SDK already hides other callers' tasks behind ``TaskNotFoundError``,
      so only the task's owner can answer its question.

    Anonymous callers cannot list tasks. They share one task namespace:
    fetching or cancelling a task needs its unguessable ID, but listing would
    hand every anonymous caller the IDs, inputs and results of every other
    anonymous caller's tasks.

    Args:
        accepts_free_text: Whether the executor can run free-text messages
            (it has a router or a default skill); ``False`` refuses them.
        agent_executor: Runs each message.
        **sdk_options: Passed to the SDK's ``DefaultRequestHandler``.
    """

    def __init__(
        self,
        *,
        accepts_free_text: bool,
        agent_executor: RuntimeExecutor,
        **sdk_options: Any,
    ) -> None:
        super().__init__(agent_executor=agent_executor, **sdk_options)
        self._accepts_free_text = accepts_free_text
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
        dedup: DedupOutcome | None = context.state.get(DEDUP_STATE_KEY)
        if dedup is not None and dedup.conflict:
            raise InvalidParamsError(
                "messageId was already used for a different message"
            )
        if dedup is not None and dedup.original is not None:
            return dedup.original
        incoming_context(context.requested_extensions, params.message)
        if is_skill_call(context.requested_extensions, params.message):
            read_skill_call(params.message)
        elif not self._accepts_free_text:
            raise UnsupportedOperationError(
                "This agent has no skill router or default skill for free-text "
                f"requests. Activate the '{SKILL_CALL_EXTENSION_URI}' extension "
                "and name a skill."
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

    async def find_task(self, task_id: str, context: ServerCallContext) -> Task | None:
        """Return the caller's task ``task_id``, or ``None`` if they have none."""
        try:
            task: Task = await self.on_get_task(GetTaskRequest(id=task_id), context)
        except TaskNotFoundError:
            return None
        return task

    async def on_list_tasks(
        self, params: ListTasksRequest, context: ServerCallContext
    ) -> ListTasksResponse:
        if not context.user.is_authenticated:
            raise UnsupportedOperationError(
                "Listing tasks requires an authenticated caller."
            )
        page: ListTasksResponse = await super().on_list_tasks(params, context)
        return page
