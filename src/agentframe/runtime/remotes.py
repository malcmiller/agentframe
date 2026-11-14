"""One run's bindings to remote agents, shared by ``Remote`` parameters and
the skill catalog."""

from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from agentframe.discovery.directory import AgentDirectory, DiscoveredAgent
from agentframe.discovery.identity import (
    EntraAppCredential,
    OnBehalfOf,
    OutboundCredential,
    RemoteIdentity,
    Service,
    UserToken,
    UserTokenCredential,
)
from agentframe.discovery.remote import Remote, find_remote, new_binding
from agentframe.errors import PermissionDeniedError
from agentframe.runtime.correlation import RunCorrelation
from agentframe.runtime.generation import RunAccounting
from agentframe.runtime.limits import RunLimits
from agentframe.security.principal import Principal

if TYPE_CHECKING:
    # Imported only for type checking: the catalog module imports this one.
    from agentframe.orchestration.catalog import SkillCatalog


class RunRemotes:
    """Creates one run's ``Remote`` bindings, with the run's limits and identity.

    Package-internal. Every binding shares the run's deadline, depth, token
    accounting and correlation, and the agent's ``remotes=`` entry decides its
    credentials. All ``OnBehalfOf`` agents of a run share one on-behalf-of
    credential, closed when the run ends.

    Args:
        directory: Where agents are looked up.
        identities: The ``remotes=`` entries, by agent name.
        deadline: Event-loop time when the run times out.
        correlation: The run and the request it is part of.
        principal: The run's verified caller, if any.
        hops: The run's depth in a chain of calls between agents.
        limits: The run's effective limits.
        accounting: The run's accounting, which every call is charged to.
        user_token: The signed-in caller's token, for ``OnBehalfOf`` agents.
        stack: Closes the on-behalf-of credential when the run ends.
        own_interface_url: The A2A interface URL the hosting app advertises,
            or ``None`` for a local run.

    Attributes:
        catalogs: The skill catalogs this run built, so ``ctx.generate`` can
            find the binding of each entry it is given.
    """

    def __init__(
        self,
        directory: AgentDirectory,
        identities: Mapping[str, RemoteIdentity],
        *,
        deadline: float,
        correlation: RunCorrelation,
        principal: Principal | None,
        hops: int,
        limits: RunLimits,
        accounting: RunAccounting,
        user_token: UserToken | None,
        stack: AsyncExitStack,
        own_interface_url: str | None,
    ) -> None:
        assert limits.max_remote_hops is not None  # DEFAULT_LIMITS sets them.
        assert limits.max_total_tokens is not None
        self.directory = directory
        self.correlation = correlation
        self.own_interface_url = own_interface_url
        self._identities = identities
        self._deadline = deadline
        self._principal = principal
        self._hops = hops
        self._max_hops = limits.max_remote_hops
        self._max_total_tokens = limits.max_total_tokens
        self._accounting = accounting
        self._user_token = user_token
        self._stack = stack
        self._on_behalf_of: UserTokenCredential | None = None
        self.catalogs: list[SkillCatalog] = []

    def identity(self, name: str) -> RemoteIdentity | None:
        """Return the agent's ``remotes=`` entry, if it has one."""
        return self._identities.get(name)

    async def bind(self, name: str) -> Remote[Any]:
        """Look a declared remote up, check its identity and bind it.

        Raises:
            DefinitionError: As for ``find_remote``.
            RemoteAgentError: As for ``find_remote``.
            PermissionDeniedError: The agent is called ``OnBehalfOf`` the
                user and the run has no signed-in user.
        """
        found = await find_remote(self.directory, name, self.identity(name))
        return self.bind_found(found)

    def bind_found(self, found: DiscoveredAgent) -> Remote[Any]:
        """Bind an agent whose card the caller already checked its identity against.

        Raises:
            PermissionDeniedError: As for ``bind``.
        """
        return new_binding(
            self.directory,
            found,
            deadline=self._deadline,
            correlation=self.correlation,
            principal=self._principal,
            credential=self._credential(found.reference.name),
            hops=self._hops,
            max_hops=self._max_hops,
            accounting=self._accounting,
            max_total_tokens=self._max_total_tokens,
        )

    def _credential(self, name: str) -> OutboundCredential | None:
        identity = self.identity(name)
        if isinstance(identity, Service):
            return identity.credential
        if isinstance(identity, OnBehalfOf):
            if self._on_behalf_of is None:
                self._on_behalf_of = _user_credential(name, self._user_token)
                self._stack.push_async_callback(self._on_behalf_of.close)
            return EntraAppCredential(self._on_behalf_of, scope=identity.scope)
        return None


def _user_credential(name: str, user_token: UserToken | None) -> UserTokenCredential:
    """Start this run's on-behalf-of credential for the signed-in user.

    Raises:
        PermissionDeniedError: The run has no signed-in user, so there is no
            one to act for. Calling as the agent instead is never a fallback.
    """
    if user_token is None:
        raise PermissionDeniedError(
            f"Remote '{name}' is called on behalf of the signed-in user, but "
            "this run has none: the caller is not an Entra user"
        )
    return user_token.credential()
