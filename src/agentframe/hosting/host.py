"""Host configuration for serving an agent remotely."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from agentframe.errors import DefinitionError
from agentframe.runtime.limits import RunLimits
from agentframe.security.authentication import Authenticator


@dataclass(frozen=True)
class HostConfig:
    """Deployment settings for a remotely hosted agent.

    Request size limits and shutdown settings come later (M7).

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

    Raises:
        DefinitionError: ``auth`` is ``None``; ``public_url`` is not an
            absolute http(s) URL, or it contains credentials, a query or a
            fragment; a ``role_permissions`` value is a single string
            instead of a list; or ``max_concurrent_runs`` is less than 1.
    """

    public_url: str
    auth: Authenticator
    role_permissions: Mapping[str, Iterable[str]] = field(default_factory=dict)
    limits_ceiling: RunLimits | None = None
    max_concurrent_runs: int = 32

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
        if self.max_concurrent_runs < 1:
            raise DefinitionError("HostConfig max_concurrent_runs must be at least 1")


def _check_public_url(public_url: str) -> None:
    # The URL is published on the public card, so credentials in it would
    # leak; the error message does not echo it for the same reason.
    parts = urlsplit(public_url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
    ):
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
