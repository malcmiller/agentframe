"""Host configuration for serving an agent remotely."""

import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from a2a.server.tasks import TaskStore

from agentframe.dev import DEV_VARIABLE
from agentframe.errors import DefinitionError
from agentframe.hosting.dedup import MessageDedupStore
from agentframe.hosting.rate_limit import RateLimit
from agentframe.runtime.limits import RunLimits
from agentframe.security.authentication import AllowAnonymous, Authenticator
from agentframe.urls import is_loopback_host, is_plain_http_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostConfig:
    """Deployment settings for a remotely hosted agent.

    Args:
        public_url: The origin callers use to reach the agent, for example
            ``"https://echo.example.com"``. It is advertised on the Agent Card;
            it is not the address the server binds to.
        auth: How remote callers are authenticated. There is no default: use
            ``AllowAnonymous()`` to accept anonymous callers explicitly.
        role_permissions: Maps each verified role to the permissions it grants,
            for example ``{"Editor": ["documents:write"]}``. A caller's
            permissions are the union over its roles; unknown roles grant
            nothing. Stored read-only.
        limits_ceiling: Hard per-host maximums for every hosted run. Each field
            it sets caps that limit; unset fields cap nothing. A library
            default above a ceiling is lowered to it, while an agent or skill
            limit set explicitly above it fails ``AgentApp`` construction.
            A ``max_total_tokens`` ceiling also bounds
            ``ModelSettings.max_output_tokens``: a call whose output cap does
            not fit the remaining budget fails with ``TokenLimitExceededError``.
            ``None`` (the default) caps nothing.
        max_concurrent_runs: How many messages and MCP tool calls may run at
            once. Another ``SendMessage`` or ``tools/call`` is refused
            immediately with HTTP 503 and
            ``Retry-After: 1``; nothing is queued. A task waiting for input
            holds no slot. Defaults to 32.
        max_request_bytes: The largest ``/a2a`` or ``/mcp`` request body accepted, in
            bytes. A larger one is refused with HTTP 413 before
            authentication, without being read in full, and creates no task.
            Defaults to 1 MiB.
        max_response_bytes: The largest result or clarification question a
            task may return, in bytes of its UTF-8 text or compact JSON. A
            larger one fails the task with ``LimitExceededError``; none of it
            is sent. Defaults to 4 MiB.
        shutdown_grace_seconds: How long a shutdown waits for running work to
            finish, measured once from when shutdown starts. New messages are
            refused meanwhile; runs still going after it are cancelled.
            Defaults to 30.
        rate_limit: How many ``SendMessage`` and MCP ``tools/call`` requests,
            together, each verified caller
            may make, for example ``RateLimit(requests=60, per_seconds=60)``.
            Another is refused with HTTP 429 and ``Retry-After`` after
            authentication and before deduplication and admission, and
            creates no task. Anonymous callers share one allowance. The
            count is kept per host instance; for one limit across instances,
            rate-limit at a gateway instead. ``None`` (the default) limits
            nothing.

    Raises:
        DefinitionError: ``auth`` is ``None``; ``public_url`` is not an
            absolute http(s) URL, or it contains credentials, a query or a
            fragment; a ``role_permissions`` value is a single string
            instead of a list; or ``max_concurrent_runs``,
            ``max_request_bytes`` or ``max_response_bytes`` is less than 1;
            or ``shutdown_grace_seconds`` is not positive.
    """

    public_url: str
    auth: Authenticator
    role_permissions: Mapping[str, Iterable[str]] = field(default_factory=dict)
    limits_ceiling: RunLimits | None = None
    max_concurrent_runs: int = 32
    max_request_bytes: int = 1_048_576
    max_response_bytes: int = 4_194_304
    shutdown_grace_seconds: float = 30
    rate_limit: RateLimit | None = None

    def __post_init__(self) -> None:
        _check_public_url(self.public_url)
        auth: object = self.auth
        if auth is None:
            raise DefinitionError(
                "HostConfig requires an explicit auth choice; use "
                "AllowAnonymous() to accept anonymous callers."
            )
        object.__setattr__(
            self, "role_permissions", _frozen_role_permissions(self.role_permissions)
        )
        for name in ("max_concurrent_runs", "max_request_bytes", "max_response_bytes"):
            if getattr(self, name) < 1:
                raise DefinitionError(f"HostConfig {name} must be at least 1")
        if not self.shutdown_grace_seconds > 0:
            raise DefinitionError("HostConfig shutdown_grace_seconds must be positive")


@dataclass(frozen=True)
class A2AConfig:
    """Settings for the A2A endpoint.

    Args:
        dedup_window_seconds: How long a caller's ``messageId`` is remembered.
            The same verified caller resending the same ``messageId`` within it
            gets the original task back instead of running the message again.
            Defaults to 600 (10 minutes).
        dedup_store: Where those ``messageId`` claims are kept. ``None`` (the
            default) keeps them in this process's memory, so they are lost on
            restart and not shared with other instances. Pass a
            ``RedisMessageDedupStore`` (``redis`` extra), or any
            ``MessageDedupStore``, to share them between instances.
        task_store: Where A2A tasks are kept: any ``a2a-sdk`` ``TaskStore``,
            such as a database store. It owns its own retention, so the two
            settings below do not apply to it. ``None`` (the default) keeps
            tasks in this process's memory, lost on restart.
        task_retention_seconds: How long the default in-memory store keeps a
            finished (completed, failed, cancelled or rejected) task. After
            that, ``GetTask`` and ``CancelTask`` treat it as unknown. Tasks in
            any non-terminal state are never deleted. Must be at
            least ``dedup_window_seconds``, so a retry within the window still
            finds its task. Defaults to 3600 (1 hour).
        max_retained_tasks: How many finished tasks the default in-memory store
            keeps at most; past it, the oldest finished task is deleted first.
            Defaults to 10,000.

    Raises:
        DefinitionError: ``task_store`` is not an ``a2a-sdk`` ``TaskStore``;
            ``dedup_window_seconds`` or ``task_retention_seconds`` is not
            positive; ``max_retained_tasks`` is less than 1; or, with
            the default task store, ``task_retention_seconds`` is shorter than
            ``dedup_window_seconds``.
    """

    dedup_window_seconds: float = 600
    dedup_store: MessageDedupStore | None = None
    task_store: TaskStore | None = None
    task_retention_seconds: float = 3600
    max_retained_tasks: int = 10_000

    def __post_init__(self) -> None:
        if not self.dedup_window_seconds > 0:
            raise DefinitionError("A2AConfig dedup_window_seconds must be positive")
        if not self.task_retention_seconds > 0:
            raise DefinitionError("A2AConfig task_retention_seconds must be positive")
        if self.task_store is not None and not isinstance(self.task_store, TaskStore):
            raise DefinitionError(
                "A2AConfig task_store must be an a2a-sdk TaskStore "
                "(a2a.server.tasks.TaskStore)"
            )
        if self.max_retained_tasks < 1:
            raise DefinitionError("A2AConfig max_retained_tasks must be at least 1")
        if (
            self.task_store is None
            and self.task_retention_seconds < self.dedup_window_seconds
        ):
            raise DefinitionError(
                "A2AConfig task_retention_seconds must be at least "
                "dedup_window_seconds, or a retried message could run again"
            )


@dataclass(frozen=True)
class MCPConfig:
    """Settings for the MCP endpoint, which publishes skills as MCP tools.

    Passing one to ``AgentApp(mcp=...)`` serves MCP (Streamable HTTP) at
    ``/mcp`` on the same server as A2A, behind the same authentication and
    host limits. It needs the ``mcp`` extra. Only the skills listed here are
    published, each as the tool named by its skill ID; nothing is published
    by default, and ``@tool`` functions never are.

    Args:
        exposed_skills: IDs of the agent's skills to publish. Each must be a
            registered skill whose ID is a valid MCP tool name (1 to 128
            letters, digits, ``_``, ``-`` or ``.``); ``AgentApp`` checks
            this. Stored as a tuple. Defaults to none.

    Raises:
        DefinitionError: ``exposed_skills`` is a single string instead of a
            list, holds something that is not a string, or lists a skill
            twice.
    """

    exposed_skills: Sequence[str] = ()

    def __post_init__(self) -> None:
        if isinstance(self.exposed_skills, str):
            raise DefinitionError(
                "MCPConfig exposed_skills must be a list of skill IDs, not a "
                "single string."
            )
        exposed = tuple(self.exposed_skills)
        if not all(isinstance(skill_id, str) for skill_id in exposed):
            raise DefinitionError("MCPConfig exposed_skills must be skill ID strings.")
        if len(set(exposed)) != len(exposed):
            raise DefinitionError("MCPConfig exposed_skills lists a skill twice.")
        object.__setattr__(self, "exposed_skills", exposed)


def warn_if_insecure(host: HostConfig) -> None:
    """Log a warning for a legal but insecure ``host`` outside development.

    Warns when callers' credentials would cross plain HTTP (an ``http://``
    ``public_url`` on a non-loopback host with any authenticator other than
    ``AllowAnonymous``), and when ``AllowAnonymous`` is served on a
    non-loopback ``public_url``. Nothing is logged under ``AGENTFRAME_DEV=1``,
    which ``agentframe dev`` and ``agentframe compose`` set.

    Args:
        host: The host configuration to check.
    """
    if os.environ.get(DEV_VARIABLE) == "1":
        return
    parts = urlsplit(host.public_url)
    if is_loopback_host(parts.hostname or ""):
        return
    if isinstance(host.auth, AllowAnonymous):
        logger.warning(
            "HostConfig accepts anonymous callers (AllowAnonymous) on the "
            "non-loopback public_url host %s, so anyone who can reach it can "
            "call the agent. Use real authentication unless this is intended.",
            parts.hostname,
        )
    elif parts.scheme == "http":
        logger.warning(
            "HostConfig public_url uses plain http on the non-loopback host %s, "
            "so callers' credentials and messages cross the network "
            "unencrypted. Use an https public_url.",
            parts.hostname,
        )


def _check_public_url(public_url: str) -> None:
    # The URL is published on the public card, so credentials in it would
    # leak; the error message does not echo it for the same reason.
    if not is_plain_http_url(public_url):
        raise DefinitionError(
            "HostConfig public_url must be an absolute http(s) URL without "
            "credentials, query or fragment."
        )


def _frozen_role_permissions(
    role_permissions: Mapping[str, Iterable[str]],
) -> Mapping[str, frozenset[str]]:
    frozen = {}
    for role, permissions in role_permissions.items():
        # A bare string would otherwise be read as one permission per character.
        if isinstance(permissions, str):
            raise DefinitionError(
                f"HostConfig role_permissions for role '{role}' must be a list "
                "of permission names, not a single string."
            )
        frozen[role] = frozenset(permissions)
    return MappingProxyType(frozen)
