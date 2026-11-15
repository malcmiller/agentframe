"""Host configuration for serving an agent remotely."""

import ipaddress
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from a2a.server.tasks import TaskStore

from agentframe.errors import DefinitionError
from agentframe.hosting.dedup import MessageDedupStore
from agentframe.runtime.limits import RunLimits
from agentframe.security.authentication import Authenticator


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
        max_concurrent_runs: How many messages may run at once. Another
            ``SendMessage`` is refused immediately with HTTP 503 and
            ``Retry-After: 1``; nothing is queued. A task waiting for input
            holds no slot. Defaults to 32.
        max_request_bytes: The largest ``/a2a`` request body accepted, in
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
            restart and not shared with other instances.
        task_store: Where A2A tasks are kept: any ``a2a-sdk`` ``TaskStore``,
            such as a database store. It owns its own retention, so the two
            settings below do not apply to it. ``None`` (the default) keeps
            tasks in this process's memory, lost on restart.
        task_retention_seconds: How long the default in-memory store keeps a
            finished (completed, failed, cancelled or rejected) task. After
            that, ``GetTask`` and ``CancelTask`` treat it as unknown. Tasks
            still running or waiting for input are never deleted. Must be at
            least ``dedup_window_seconds``, so a retry within the window still
            finds its task. Defaults to 3600 (1 hour).
        max_retained_tasks: How many finished tasks the default in-memory store
            keeps at most; past it, the oldest finished task is deleted first.
            Defaults to 10,000.

    Raises:
        DefinitionError: ``dedup_window_seconds`` or ``task_retention_seconds``
            is not positive; ``max_retained_tasks`` is less than 1; or, with
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


def is_plain_http_url(url: str) -> bool:
    """Return whether ``url`` is an absolute http(s) URL with nothing secret in it.

    Credentials, a query or a fragment are refused because such URLs are
    published (on an Agent Card) or shared (in a registry source).

    Args:
        url: The URL to check.

    Returns:
        ``True`` for an absolute http or https URL with a host and without
        credentials, query or fragment; otherwise ``False``.
    """
    try:
        parts = urlsplit(url)
        # Reading the port validates it: a non-number or out-of-range port raises.
        parts.port  # noqa: B018
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and not parts.query
        and not parts.fragment
    )


def is_loopback_host(host: str) -> bool:
    """Return whether ``host`` names this machine: ``localhost`` or a loopback IP.

    Args:
        host: A host name or IP address, without brackets or port.

    Returns:
        ``True`` for ``localhost`` (any case) and loopback addresses such as
        ``127.0.0.1`` and ``::1``; otherwise ``False``.
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
