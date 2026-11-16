"""Serve an agent's allowlisted skills as MCP tools at ``/mcp``.

The official MCP SDK owns the protocol: Streamable HTTP, JSON-RPC and
version negotiation. This module puts the host's checks in front of it and
runs each ``tools/call`` through the ``Runtime``, like an A2A skill call:

- Every request passes the same size limit and authentication as ``/a2a``
  (``endpoints.caller_check``). The SDK runs statelessly and answers each
  POST with JSON, so there are no MCP sessions one caller could reuse for
  another, and no server-to-client requests.
- ``tools/list`` lists the ``MCPConfig.exposed_skills`` whose
  ``requires_permissions`` the caller holds, each as the tool named by its
  skill ID with the skill's argument schema. ``ctx``, ``Inject[T]`` and
  ``Remote`` parameters never appear in it.
- A ``tools/call`` beyond the caller's ``HostConfig.rate_limit`` allowance,
  shared with A2A messages, gets HTTP 429. It needs a free run slot and a
  host that is not shutting down, or gets HTTP 503 with ``Retry-After: 1``.
  A tool that is not published is an ``Invalid params`` protocol error. A
  published one runs through the runtime with the caller's principal and
  the host's limits ceiling, which check its permissions, authorization
  policy, execution policy and arguments. Routing is never involved.
- A skill returning ``str`` gives one text block. Any other skill gives
  ``structuredContent`` ``{"value": ...}``, matching the tool's output
  schema, plus the same JSON as text. A failed run is a tool result with
  ``isError`` and the error's safe message, as are results over
  ``HostConfig.max_response_bytes``. Every result's ``_meta`` carries
  ``agentframe/invocation_id`` and an ``agentframe/usage`` snapshot.

Runs are logged like hosted A2A runs, and the request's valid W3C trace
context is forwarded to remote agents. With ``Runtime(tracing=...)``, each
tool call is recorded like an A2A run (a server ``invoke_agent`` span and
run metrics), and busy or shutting-down refusals are counted. The SDK's own
OpenTelemetry middleware is removed, so it adds no spans of its own.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

try:
    from mcp import MCPError
    from mcp.server import Server, ServerRequestContext
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.types import (
        INVALID_PARAMS,
        CallToolRequestParams,
        CallToolResult,
        ListToolsResult,
        PaginatedRequestParams,
        TextContent,
        Tool,
    )
except ImportError as error:
    raise ImportError(
        "MCPConfig needs the optional 'mcp' package; install it with: "
        "pip install 'agentframe-py[mcp]'"
    ) from error
from a2a.types import AgentCard
from pydantic_core import to_json, to_jsonable_python
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from agentframe.authoring.agent import Agent
from agentframe.authoring.definitions import AgentDefinition, SkillDefinition
from agentframe.discovery.identity import UserToken, UserTokenExchange
from agentframe.errors import AgentFrameError, DefinitionError, OutputValidationError
from agentframe.hosting.a2a_executor import (
    SKILL_FAILED_MESSAGE,
    check_response_size,
    log_failed_run,
    run_metadata,
    safe_message,
)
from agentframe.hosting.admission import RunAdmission, RunSlot
from agentframe.hosting.card import skill_arguments_schema
from agentframe.hosting.endpoints import (
    caller_check,
    too_many_requests,
    unavailable,
)
from agentframe.hosting.host import HostConfig, MCPConfig
from agentframe.hosting.lifecycle import Lifecycle
from agentframe.hosting.rate_limit import CallerRateLimiter
from agentframe.hosting.request_size import with_body
from agentframe.runtime.accounting import RunAccounting
from agentframe.runtime.correlation import (
    TRACEPARENT_HEADER,
    TRACESTATE_HEADER,
    RunCorrelation,
    read_trace_context,
)
from agentframe.runtime.options import RunOptions
from agentframe.runtime.runtime import Runtime
from agentframe.runtime.telemetry import Span
from agentframe.security.principal import Principal

MCP_PATH = "/mcp"
"""Where the MCP endpoint is served."""

_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
"""The tool names MCP allows."""

logger = logging.getLogger(__name__)


class MCPEndpoint:
    """The ASGI endpoint serving ``mcp.exposed_skills`` as MCP tools.

    Serve it only inside ``running()``, which runs the SDK's request
    handling; outside it, requests get HTTP 503.

    Args:
        agent: The hosted agent instance.
        runtime: The runtime that validates, authorizes and limits every run.
        host: The host's authentication and limits, shared with ``/a2a``.
        mcp: Which skills to publish.
        user_token_exchange: Keeps a signed-in user's verified token for
            the run's ``OnBehalfOf`` remotes, as for ``/a2a``.
        card: The served Agent Card, whose security schemes give the 401
            challenge.
        admission: The app's run slots, shared with ``/a2a``.
        lifecycle: The app's readiness and shutdown.
        rate_limiter: Each caller's request allowance, shared with ``/a2a``,
            or ``None`` to limit nothing.

    Raises:
        DefinitionError: A listed skill is not one of the agent's, its ID is
            not a valid MCP tool name, or its result cannot be described as
            a JSON schema.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        runtime: Runtime,
        host: HostConfig,
        mcp: MCPConfig,
        user_token_exchange: UserTokenExchange | None,
        card: AgentCard,
        admission: RunAdmission,
        lifecycle: Lifecycle,
        rate_limiter: CallerRateLimiter | None = None,
    ) -> None:
        definition = type(agent).definition()
        self._skills = _exposed_skills(definition, mcp)
        self._tools = {skill.id: _tool(skill) for skill in self._skills.values()}
        self._agent = agent
        self._agent_name = definition.name
        self._runtime = runtime
        self._host = host
        self._own_interface_url = card.supported_interfaces[0].url
        self._admission = admission
        self._lifecycle = lifecycle
        self._rate_limiter = rate_limiter
        self._check_caller = caller_check(host, user_token_exchange, card)
        self._server: Server[Any] = Server(
            definition.name,
            version=definition.version,
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )
        # AgentFrame creates no spans; the application owns tracing.
        self._server.middleware = []
        self._manager: StreamableHTTPSessionManager | None = None

    @property
    def path(self) -> str:
        """Where the endpoint is served: ``/mcp``."""
        return MCP_PATH

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Run the SDK's request handling until the block exits.

        Exiting cancels any tool call still running.
        """
        manager = StreamableHTTPSessionManager(
            self._server,
            json_response=True,
            stateless=True,
            max_request_body_size=self._host.max_request_bytes,
        )
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the request, then hand it to the SDK.

        A ``tools/call`` holds a run slot, as an A2A ``SendMessage`` does:
        the call takes it over when its run starts and releases it when the
        run ends; otherwise it is released here.
        """
        checked = await self._check_caller(Request(scope, receive))
        if isinstance(checked, Response):
            await checked(scope, receive, send)
            return
        manager = self._manager
        if manager is None:
            await unavailable("The agent is not ready; retry later.")(
                scope, receive, send
            )
            return
        body = await checked.body()
        method = _method(body)
        # Streamable HTTP has no batches; refusing them keeps every
        # tools/call behind the admission check below.
        if method is _BATCH:
            response: Response = JSONResponse(
                {"error": "Batch requests are not supported."}, 400
            )
            await response(scope, receive, send)
            return
        # The body was read for the check above, so the SDK gets a copy
        # that replays it.
        request = with_body(checked, body)
        if method != "tools/call":
            await manager.handle_request(request.scope, request.receive, send)
            return
        if self._rate_limiter is not None:
            principal: Principal | None = request.state.principal
            wait = self._rate_limiter.try_acquire(principal.id if principal else None)
            if wait is not None:
                await too_many_requests(wait)(scope, receive, send)
                return
        telemetry = self._runtime.telemetry
        if self._lifecycle.shutting_down:
            telemetry.record_rejection(agent=self._agent_name, reason="shutting_down")
            response = unavailable("The agent is shutting down; retry later.")
            await response(scope, receive, send)
            return
        slot = self._admission.try_admit()
        if slot is None:
            telemetry.record_rejection(agent=self._agent_name, reason="busy")
            await unavailable("The agent is busy; retry later.")(scope, receive, send)
            return
        request.state.run_slot = slot
        try:
            with self._lifecycle.track_send():
                await manager.handle_request(request.scope, request.receive, send)
        finally:
            if not slot.started:
                slot.release()

    async def _list_tools(
        self, ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        """List the published tools whose permissions the caller holds."""
        principal = _request(ctx).state.principal
        permissions = principal.permissions if principal else frozenset()
        return ListToolsResult(
            tools=[
                tool
                for tool in self._tools.values()
                if self._skills[tool.name].requires_permissions <= permissions
            ]
        )

    async def _call_tool(
        self, ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        """Run the published skill ``params.name`` as one hosted run.

        Raises:
            MCPError: The tool is not published (``Invalid params``).
        """
        skill = self._skills.get(params.name)
        if skill is None:
            raise MCPError(INVALID_PARAMS, "Unknown tool.")
        request = _request(ctx)
        slot: RunSlot | None = getattr(request.state, "run_slot", None)
        if slot is not None:
            slot.start()
        try:
            return await self._run(skill, params.arguments or {}, request)
        finally:
            if slot is not None:
                slot.release()

    async def _run(
        self, skill: SkillDefinition, arguments: dict[str, Any], request: Request
    ) -> CallToolResult:
        """Run ``skill`` for the request's caller and return its tool result.

        The run is recorded as a server ``invoke_agent`` span continuing the
        request's ``traceparent``, as for an A2A message.
        """
        invocation_id = str(uuid.uuid4())
        correlation = RunCorrelation(
            self._agent_name,
            invocation_id,
            root_invocation_id=invocation_id,
            trace=read_trace_context(
                request.headers.getlist(TRACEPARENT_HEADER),
                request.headers.getlist(TRACESTATE_HEADER),
            ),
        )
        with self._runtime.telemetry.run(
            agent=self._agent_name,
            invocation_id=invocation_id,
            kind="server",
            parent=correlation.trace,
        ) as span:
            span.set_skill(skill.id)
            return await self._run_traced(skill, arguments, request, correlation, span)

    async def _run_traced(
        self,
        skill: SkillDefinition,
        arguments: dict[str, Any],
        request: Request,
        correlation: RunCorrelation,
        span: Span,
    ) -> CallToolResult:
        """Run ``skill`` inside the run's span, which records any failure."""
        principal: Principal | None = request.state.principal
        user_token: UserToken | None = request.state.user_token
        invocation_id = correlation.invocation_id
        accounting = RunAccounting()
        started_at = asyncio.get_running_loop().time()
        logger.info(
            "Run started (invocation_id=%s, root_invocation_id=%s)",
            invocation_id,
            invocation_id,
            extra=correlation.log_extra(),
        )
        # noinspection PyBroadException
        try:
            run = await self._runtime.run_invocation(
                invocation_id,
                self._agent,
                skill.id,
                arguments,
                principal,
                # MCP callers cannot send per-run options.
                RunOptions(),
                started_at,
                accounting,
                self._host.limits_ceiling,
                user_token=user_token,
                correlation=correlation,
                own_interface_url=self._own_interface_url,
            )
            result = _tool_result(
                run.value,
                as_text=self._tools[skill.id].output_schema is None,
                invocation_id=invocation_id,
                max_bytes=self._host.max_response_bytes,
            )
        except AgentFrameError as error:
            error.invocation_id = invocation_id
            span.fail(error)
            log_failed_run(error, correlation)
            return _with_meta(_error(safe_message(error)), invocation_id, accounting)
        except Exception as error:
            # Skill exceptions may carry submitted values or secrets, so the
            # caller gets a generic message and only the log keeps the details.
            span.fail(error)
            logger.exception(
                "Skill failed (invocation_id=%s)",
                invocation_id,
                extra=correlation.log_extra(),
            )
            return _with_meta(_error(SKILL_FAILED_MESSAGE), invocation_id, accounting)
        logger.info(
            "Run completed (invocation_id=%s)",
            invocation_id,
            extra=correlation.log_extra(),
        )
        return _with_meta(result, invocation_id, accounting)


def _exposed_skills(
    definition: AgentDefinition, mcp: MCPConfig
) -> dict[str, SkillDefinition]:
    """Return the skills ``mcp`` publishes, by ID, in its order.

    Raises:
        DefinitionError: A listed ID is not one of the agent's skills or is
            not a valid MCP tool name.
    """
    exposed = {}
    for skill_id in mcp.exposed_skills:
        skill = definition.skills.get(skill_id)
        if skill is None:
            raise DefinitionError(
                f"MCPConfig exposed_skills lists '{skill_id}', which is not a "
                f"skill of agent '{definition.name}'."
            )
        if not _TOOL_NAME.fullmatch(skill_id):
            raise DefinitionError(
                f"Skill '{skill_id}' cannot be an MCP tool: tool names are 1 to "
                "128 letters, digits, '_', '-' or '.'."
            )
        exposed[skill_id] = skill
    return exposed


def _tool(skill: SkillDefinition) -> Tool:
    """Return the MCP tool that publishes ``skill``."""
    return Tool(
        name=skill.id,
        title=skill.name,
        description=skill.description,
        input_schema=skill_arguments_schema(skill),
        output_schema=_output_schema(skill),
    )


def _output_schema(skill: SkillDefinition) -> dict[str, Any] | None:
    """Return the schema of ``{"value": ...}`` results, or ``None`` for text.

    Raises:
        DefinitionError: The skill's result has no JSON schema.
    """
    try:
        value = skill.return_adapter.json_schema(mode="serialization")
    except Exception as error:  # Pydantic raises several kinds here.
        raise DefinitionError(
            f"Skill '{skill.id}' cannot be an MCP tool: its result has no JSON schema."
        ) from error
    if _is_text(value):
        return None
    # References point at the root, so the definitions move up to it.
    definitions = value.pop("$defs", None)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"value": value},
        "required": ["value"],
    }
    if definitions:
        schema["$defs"] = definitions
    return schema


def _is_text(schema: dict[str, Any]) -> bool:
    """Return whether a result schema is a plain string, sent as text."""
    return schema.get("type") == "string"


def _tool_result(
    value: Any, *, as_text: bool, invocation_id: str, max_bytes: int
) -> CallToolResult:
    """Return a skill's validated result as an MCP tool result.

    ``as_text`` is for skills returning ``str``, whose tools have no output
    schema.

    Raises:
        OutputValidationError: The value cannot be represented as JSON.
        LimitExceededError: The text or JSON is larger than ``max_bytes``.
    """
    if as_text and isinstance(value, str):
        check_response_size(value.encode(), max_bytes, invocation_id)
        return CallToolResult(content=[TextContent(type="text", text=value)])
    try:
        data = {"value": to_jsonable_python(value)}
    except (TypeError, ValueError):
        raise OutputValidationError(
            "The skill returned a value that cannot be serialized as JSON",
            invocation_id=invocation_id,
        ) from None
    text = to_json(data)
    check_response_size(text, max_bytes, invocation_id)
    return CallToolResult(
        content=[TextContent(type="text", text=text.decode())],
        structured_content=data,
    )


def _error(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)], is_error=True
    )


def _with_meta(
    result: CallToolResult, invocation_id: str, accounting: RunAccounting
) -> CallToolResult:
    """Add the run's ID and usage snapshot to the result's ``_meta``."""
    metadata = run_metadata(invocation_id, accounting)
    result.meta = {f"agentframe/{key}": value for key, value in metadata.items()}
    return result


def _request(ctx: ServerRequestContext[Any]) -> Request:
    """Return the HTTP request a handler is serving.

    Raises:
        RuntimeError: There is none, which never happens: the endpoint is
            served only over HTTP.
    """
    if not isinstance(ctx.request, Request):
        raise RuntimeError("An MCP request reached the agent without HTTP")
    return ctx.request


_BATCH = object()
"""What ``_method`` returns for a JSON array body."""


def _method(body: bytes) -> object:
    """Return a JSON-RPC body's ``method``, ``_BATCH`` for an array, else ``None``.

    Malformed JSON is left for the SDK to report.
    """
    try:
        message = json.loads(body)
    except (ValueError, RecursionError):  # Deeply nested JSON raises the latter.
        return None
    if isinstance(message, list):
        return _BATCH
    return message.get("method") if isinstance(message, dict) else None
