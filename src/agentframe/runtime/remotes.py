"""One run's bindings to remote agents, shared by ``Remote`` parameters and
the skill catalog."""

import logging
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from a2a.types import AgentCard

from agentframe.discovery.binding import find_remote, new_binding
from agentframe.discovery.directory import AgentDirectory, DiscoveredAgent
from agentframe.discovery.identity import (
    EntraAppCredential,
    OnBehalfOf,
    OutboundCredential,
    RemoteIdentity,
    Service,
    UserToken,
    UserTokenCredential,
    check_identity,
    requires_credentials,
)
from agentframe.discovery.remote import Remote
from agentframe.errors import DefinitionError, PermissionDeniedError
from agentframe.runtime.accounting import RunAccounting
from agentframe.runtime.correlation import RunCorrelation
from agentframe.runtime.limits import RunLimits
from agentframe.runtime.telemetry import NO_TELEMETRY, Telemetry
from agentframe.security.principal import Principal

if TYPE_CHECKING:
    # Imported only for type checking: the catalog module imports this one.
    from agentframe.orchestration.catalog import SkillCatalog

logger = logging.getLogger(__name__)


class RunRemotes:
    """Creates one run's ``Remote`` bindings, with the run's limits and identity.

    Package-internal. Every binding shares the run's deadline, depth, token
    accounting and correlation, and the agent's ``remotes=`` entry decides its
    credentials. Catalog agents without an entry may use ``default_remote``
    instead; declared ``Remote`` parameters never do. All ``OnBehalfOf``
    agents of a run share one on-behalf-of credential, closed when the run
    ends.

    Args:
        directory: Where agents are looked up.
        identities: The ``remotes=`` entries, by agent name.
        default: The ``default_remote=`` identity for catalog agents, if any.
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
        logged_identities: The ``(agent, skill)`` pairs whose catalog
            identity mode was already logged, shared by the runtime's runs so
            each is logged once.
        telemetry: Records every binding's calls as spans.

    Attributes:
        catalogs: The skill catalogs this run built, so ``ctx.generate`` can
            find the binding of each entry it is given.
    """

    def __init__(
        self,
        directory: AgentDirectory,
        identities: Mapping[str, RemoteIdentity],
        *,
        default: RemoteIdentity | None,
        deadline: float,
        correlation: RunCorrelation,
        principal: Principal | None,
        hops: int,
        limits: RunLimits,
        accounting: RunAccounting,
        user_token: UserToken | None,
        stack: AsyncExitStack,
        own_interface_url: str | None,
        logged_identities: set[tuple[str, str]],
        telemetry: Telemetry = NO_TELEMETRY,
    ) -> None:
        assert limits.max_remote_hops is not None  # DEFAULT_LIMITS sets them.
        assert limits.max_total_tokens is not None
        assert limits.max_tool_calls is not None
        self.directory = directory
        self.correlation = correlation
        self.own_interface_url = own_interface_url
        self._identities = identities
        self._default = default
        self._deadline = deadline
        self._principal = principal
        self._hops = hops
        self._max_hops = limits.max_remote_hops
        self._max_total_tokens = limits.max_total_tokens
        self._max_tool_calls = limits.max_tool_calls
        self._accounting = accounting
        self._user_token = user_token
        self._stack = stack
        self._on_behalf_of: UserTokenCredential | None = None
        self._logged_identities = logged_identities
        self._telemetry = telemetry
        self.catalogs: list[SkillCatalog] = []

    @property
    def hosted(self) -> bool:
        """Whether the run is hosted by an ``AgentApp``, not a local run."""
        return self.own_interface_url is not None

    def log_identity_mode(self, skill_id: str, mode: str) -> None:
        """Log, once per hosted skill, how its catalog agents are called.

        Args:
            skill_id: The skill that built a catalog.
            mode: ``"Service"``, ``"OnBehalfOf"``, ``"mixed"`` or ``"none"``.
                Never a credential.
        """
        key = (self.correlation.agent, skill_id)
        if not self.hosted or key in self._logged_identities:
            return
        self._logged_identities.add(key)
        logger.info(
            "Skill '%s' calls its skill catalog agents with identity: %s",
            skill_id,
            mode,
            extra=self.correlation.log_extra(),
        )

    def tool_calls_left(self) -> int:
        """Return the tool calls the run may still make."""
        return max(self._max_tool_calls - self._accounting.tool_calls, 0)

    def identity(
        self, name: str, card: AgentCard | None = None
    ) -> RemoteIdentity | None:
        """Return how the run authenticates to an agent, or ``None`` for no
        credentials.

        Args:
            name: The agent's registered name.
            card: The agent's card, for a catalog agent. Without it (a
                declared ``Remote`` parameter) only ``remotes=`` counts.

        Returns:
            The agent's ``remotes=`` entry; otherwise, for a catalog agent
            whose card requires credentials, ``default_remote`` when it
            meets that card's security; otherwise ``None``. Open cards never
            get the default, so it is sent only to agents that ask for it.
        """
        own = self._identities.get(name)
        if own is not None or card is None or self._default is None:
            return own
        if not requires_credentials(card):
            return None
        try:
            check_identity(name, card, self._default)
        except DefinitionError:
            return None
        return self._default

    async def bind(self, name: str) -> Remote[Any]:
        """Look a declared remote up, check its identity and bind it.

        Only its ``remotes=`` entry counts, never ``default_remote``.

        Raises:
            DefinitionError: As for ``find_remote``.
            RemoteAgentError: As for ``find_remote``.
            PermissionDeniedError: The agent is called ``OnBehalfOf`` the
                user and the run has no signed-in user.
        """
        identity = self.identity(name)
        found = await find_remote(self.directory, name, identity)
        return self._new_binding(found, identity)

    def bind_found(self, found: DiscoveredAgent) -> Remote[Any]:
        """Bind a catalog agent whose card the caller already checked
        ``identity(name, card)`` against.

        Raises:
            PermissionDeniedError: As for ``bind``.
        """
        identity = self.identity(found.reference.name, found.card)
        return self._new_binding(found, identity)

    def _new_binding(
        self, found: DiscoveredAgent, identity: RemoteIdentity | None
    ) -> Remote[Any]:
        return new_binding(
            self.directory,
            found,
            deadline=self._deadline,
            correlation=self.correlation,
            principal=self._principal,
            credential=self._credential(found.reference.name, identity),
            hops=self._hops,
            max_hops=self._max_hops,
            accounting=self._accounting,
            max_total_tokens=self._max_total_tokens,
            telemetry=self._telemetry,
        )

    def _credential(
        self, name: str, identity: RemoteIdentity | None
    ) -> OutboundCredential | None:
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
