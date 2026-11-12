"""Application-supplied resource authorization policy."""

from collections.abc import Mapping
from typing import Protocol

from agentframe.authoring.definitions import SkillDefinition
from agentframe.security.principal import Principal


class AuthorizationPolicy(Protocol):
    """Decides whether a caller may run a skill on the requested resource.

    Declared ``requires_permissions`` say a caller may use a skill at all; the
    policy checks the actual resource, for example that the caller owns the
    document it asks for. Any class with a matching ``check`` method works; no
    inheritance is needed.
    """

    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        """Return ``True`` to allow the run and ``False`` to deny it.

        Args:
            principal: Verified caller identity, or ``None`` when there is none.
            skill: Definition of the skill about to run.
            arguments: The skill's validated business arguments, read-only.

        Returns:
            ``True`` only when the caller may run the skill on these arguments.
            Any other value denies the run.
        """
        ...
