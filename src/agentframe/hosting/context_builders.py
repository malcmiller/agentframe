"""Build the SDK's call and request contexts from AgentFrame's request state."""

from a2a.auth.user import UnauthenticatedUser, User
from a2a.server.agent_execution import RequestContext, SimpleRequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.routes import DefaultServerCallContextBuilder
from a2a.types import AgentCard, SendMessageRequest, Task
from starlette.requests import Request

from agentframe.hosting.a2a_executor import (
    PRINCIPAL_STATE_KEY,
    TRACE_STATE_KEY,
    USER_TOKEN_STATE_KEY,
)
from agentframe.hosting.admission import RUN_SLOT_STATE_KEY, RunSlot
from agentframe.hosting.dedup import DEDUP_STATE_KEY, DedupOutcome
from agentframe.runtime.correlation import (
    TRACEPARENT_HEADER,
    TRACESTATE_HEADER,
    read_trace_context,
)
from agentframe.security.authentication import API_KEY_HEADER
from agentframe.security.principal import Principal

_CREDENTIAL_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", API_KEY_HEADER.lower()}
)
"""Request headers kept out of the SDK's call context, which it logs."""


class CallerContextBuilder(DefaultServerCallContextBuilder):
    """Add the caller, any run slot, any dedup outcome and any valid W3C trace
    context to the call context.

    Args:
        card: The served Agent Card. Headers its API-key schemes name, such as
            a custom ``APIKeyAuth(header=...)``, are dropped like the
            standard credential headers.
    """

    def __init__(self, card: AgentCard) -> None:
        super().__init__()
        self._credential_headers = _CREDENTIAL_HEADERS | {
            scheme.api_key_security_scheme.name.lower()
            for scheme in card.security_schemes.values()
            if scheme.WhichOneof("scheme") == "api_key_security_scheme"
            and scheme.api_key_security_scheme.location == "header"
        }

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        # The SDK keeps the request headers in the call context and logs that
        # context at debug level; credentials are no longer needed once the
        # caller is verified, so they are dropped rather than logged.
        context.state["headers"] = {
            name: value
            for name, value in context.state.get("headers", {}).items()
            if name.lower() not in self._credential_headers
        }
        context.state[PRINCIPAL_STATE_KEY] = request.state.principal
        context.state[USER_TOKEN_STATE_KEY] = request.state.user_token
        trace = read_trace_context(
            request.headers.getlist(TRACEPARENT_HEADER),
            request.headers.getlist(TRACESTATE_HEADER),
        )
        if trace is not None:
            context.state[TRACE_STATE_KEY] = trace
        slot: RunSlot | None = getattr(request.state, "run_slot", None)
        if slot is not None:
            context.state[RUN_SLOT_STATE_KEY] = slot
        dedup: DedupOutcome | None = getattr(request.state, "dedup", None)
        if dedup is not None:
            context.state[DEDUP_STATE_KEY] = dedup
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


class ClaimedTaskIdBuilder(SimpleRequestContextBuilder):
    """Run a new message in the task ID that deduplication claimed for it.

    The SDK would otherwise generate a fresh ID, and a retry of a message
    whose first copy never ran would not find its claimed task.
    """

    async def build(
        self,
        context: ServerCallContext,
        params: SendMessageRequest | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        task: Task | None = None,
    ) -> RequestContext:
        dedup: DedupOutcome | None = context.state.get(DEDUP_STATE_KEY)
        if task_id is None and dedup is not None:
            task_id = dedup.task_id
        return await super().build(context, params, task_id, context_id, task)
