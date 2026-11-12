"""Application-supplied authorization policies for skills and tool calls."""

from collections.abc import Mapping
from typing import Protocol

from agentframe.authoring.definitions import SkillDefinition, ToolDefinition
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


class ToolAuthorizationPolicy(Protocol):
    """Decides whether a caller may run one tool call the model asked for.

    Checked before every tool call, after its arguments are validated and its
    ``requires_permissions`` are met, so a model (or a prompt injection) can
    never make a tool act beyond the caller's authority. Any class with a
    matching ``check_tool`` method works; no inheritance is needed.
    """

    async def check_tool(
        self,
        principal: Principal | None,
        tool: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        """Return ``True`` to allow the tool call and ``False`` to deny it.

        Args:
            principal: Verified caller identity, or ``None`` when there is none.
            tool: Definition of the tool about to run.
            arguments: The tool's validated business arguments, read-only.
                Never ``ctx`` or injected services.

        Returns:
            ``True`` only when the caller may run the tool with these
            arguments. Any other value denies the call and fails the run.
        """
        ...
