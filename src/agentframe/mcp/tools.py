"""MCP server tools offered to a model during ``ctx.generate``.

``ctx.generate(tools=[*mcp_tools])`` turns each ``MCPTool`` into one model
tool. When the model calls it, the call is authorized and passes the
execution policy's tool checks; only then does the client send it to the
server, bounded by the client's call timeout and result size.
"""

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from agentframe.errors import DefinitionError, PermissionDeniedError
from agentframe.mcp.client import MCPTool
from agentframe.models.base import ToolSpec
from agentframe.orchestration.catalog import safe_tool_name
from agentframe.runtime.tools import error_result, is_permitted
from agentframe.security.execution_policy import ExecutionPolicy, enforce_tool_checks
from agentframe.security.principal import Principal

if TYPE_CHECKING:
    # Imported only for type checking, to avoid an import cycle.
    from agentframe.authoring.definitions import SkillDefinition


@dataclass(frozen=True)
class BoundMCPTool:
    """One selected MCP tool offered to the model, bound to the run.

    Attributes:
        name: The provider-safe tool name the model sees and calls.
        entry: The selected tool.
        invocation_id: The run's ID, for the logs of failed calls.
        principal: The run's verified caller, or ``None``.
        policy: The execution policy whose tool checks run before every
            call, or ``None`` for no checks.
        skill: The running skill, passed to the tool checks.
        deadline: Event-loop time by which every tool check must answer.
    """

    name: str
    entry: MCPTool
    invocation_id: str
    principal: Principal | None
    policy: ExecutionPolicy | None
    skill: "SkillDefinition | None"
    deadline: float

    def spec(self) -> ToolSpec:
        """Return the tool as the model sees it, with a copy of its schema.

        Server text is untrusted: it goes only in the tool definition, never
        in trusted instructions.
        """
        return ToolSpec(
            name=self.name,
            description=self.entry.description,
            parameters=copy.deepcopy(self.entry.input_schema),
        )

    async def run(self, arguments: str) -> str:
        """Send one model-chosen call to the server and return its result.

        The caller must hold the entry's ``requires_permissions``, checked
        before the arguments. Arguments that are not a JSON object send
        nothing; the model is told. Other argument checks are the server's.
        The execution policy's tool checks then see the entry's name and the
        arguments about to be sent. A permission or check failure fails the
        run; the model is never told. The call is never retried.

        Raises:
            PermissionDeniedError: The caller lacks a permission the entry
                requires, or a tool check denied the call. Nothing is sent.
            PolicyUnavailableError: A tool check raised an error or did not
                answer before the run's deadline. Nothing is sent.
            MCPServerError: The server could not be reached, or the
                connection broke.
        """
        if not is_permitted(self.entry, self.principal):
            raise PermissionDeniedError(
                f"MCP tool '{self.entry.name}' requires permissions the caller "
                "does not hold"
            )
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            return error_result("Invalid arguments: expected a JSON object.")
        await self._check(parsed)
        # The client's call is private to this package: it trusts that the
        # checks above have run.
        return await self.entry.client._call(
            self.entry.tool, parsed, self.invocation_id
        )

    async def _check(self, arguments: dict[str, Any]) -> None:
        """Run the execution policy's tool checks on the call about to be sent."""
        if self.policy is None or not self.policy.tool_checks:
            return
        # MCP tools are offered only inside a skill; routing never offers them.
        assert self.skill is not None
        await enforce_tool_checks(
            self.policy.tool_checks,
            self.skill,
            self.principal,
            self.entry.name,
            arguments,
            self.deadline,
        )


def bind_mcp_tools(
    entries: Iterable[MCPTool],
    *,
    invocation_id: str,
    principal: Principal | None,
    policy: ExecutionPolicy | None,
    skill: "SkillDefinition | None",
    deadline: float,
) -> Mapping[str, BoundMCPTool]:
    """Bind each selected tool to the run and return them by provider-safe name.

    Every entry is returned, including those the caller lacks permissions
    for: the generation layer leaves those out of what the model sees, and
    a call naming one still finds it, so the run fails instead of the model
    being told the name is unknown.

    Raises:
        DefinitionError: An entry is given twice, or two entries become the
            same model tool name.
    """
    tools: dict[str, BoundMCPTool] = {}
    for entry in entries:
        name = safe_tool_name(entry.name)
        if name in tools:
            if tools[name].entry == entry:
                raise DefinitionError(
                    f"MCP tool '{entry.name}' is selected more than once"
                )
            raise DefinitionError(
                f"MCP tools '{tools[name].entry.name}' and '{entry.name}' both "
                f"become the model tool '{name}'; pass only one of them."
            )
        tools[name] = BoundMCPTool(
            name, entry, invocation_id, principal, policy, skill, deadline
        )
    return MappingProxyType(tools)
