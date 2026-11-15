"""Remote skill catalog entries offered to a model as tools.

``ctx.generate(tools=[*catalog])`` turns each ``RemoteSkill`` into one model
tool. When the model calls it, the call is authorized and passes the
execution policy's tool checks, then the run's binding to the entry's agent
sends the request, with the run's deadline, depth, identity and token budget.
"""

import copy
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from agentframe.discovery.remote import Remote
from agentframe.errors import (
    ClarificationNeededError,
    DefinitionError,
    PermissionDeniedError,
    RemoteAgentError,
)
from agentframe.models.base import ToolSpec
from agentframe.orchestration.catalog import RemoteSkill, safe_tool_name
from agentframe.runtime.correlation import RunCorrelation
from agentframe.runtime.remotes import RunRemotes
from agentframe.runtime.tools import error_result, is_permitted
from agentframe.security.execution_policy import ExecutionPolicy, enforce_tool_checks
from agentframe.security.principal import Principal

if TYPE_CHECKING:
    # Imported only for type checking, to avoid an import cycle.
    from agentframe.authoring.definitions import SkillDefinition

logger = logging.getLogger(__name__)

ASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "The request, in plain text."}
    },
    "required": ["text"],
    "additionalProperties": False,
}
"""The arguments of an ``ask`` entry's tool: the free-text request."""

_MAX_TOOL_NAME = 64
"""Providers accept tool names of 1-64 letters, digits, ``_`` or ``-``."""


@dataclass(frozen=True)
class RemoteTool:
    """One catalog entry offered to the model, bound to the run.

    Attributes:
        name: The provider-safe tool name the model sees and calls.
        entry: The catalog entry.
        binding: The run's binding to the entry's agent.
        max_total_tokens: The tokens each call may use, or ``None`` for
            everything the run has left.
        correlation: The run, for the logs of failed calls.
        principal: The run's verified caller, or ``None``.
        policy: The execution policy whose tool checks run before every
            call, or ``None`` for no checks.
        skill: The running skill, passed to the tool checks: the same object
            ``@tool`` calls pass.
        deadline: Event-loop time by which every tool check must answer.
    """

    name: str
    entry: RemoteSkill
    binding: Remote[Any]
    max_total_tokens: int | None
    correlation: RunCorrelation
    principal: Principal | None
    policy: ExecutionPolicy | None
    skill: "SkillDefinition | None"
    deadline: float

    def spec(self) -> ToolSpec:
        """Return the tool as the model sees it.

        The schema is a copy, so nothing the request goes through can change
        the catalog's entry. Card text is untrusted: it goes only in the tool
        definition, never in trusted instructions.
        """
        schema = self.entry.arguments_schema
        return ToolSpec(
            name=self.name,
            description=self.entry.description,
            parameters=copy.deepcopy(ASK_SCHEMA if schema is None else schema),
        )

    async def run(self, arguments: str) -> str:
        """Send one model-chosen call to the remote agent and return its result.

        The caller must hold the entry's ``requires_permissions``, checked
        before the arguments so a model learns nothing about an entry it may
        not call. Arguments that are not a JSON object (or, for ``ask``, not
        just a ``text`` string) send nothing; the model is told. Other
        argument checks are the remote agent's. The execution policy's tool
        checks then see the entry's name and the arguments about to be sent.
        A permission or check failure fails the run; the model is never
        told. A ``RemoteAgentError`` or
        ``ClarificationNeededError`` becomes a tool error naming only its
        class, so the model can try something else; the error is logged.
        The call is never retried.

        Args:
            arguments: The model's raw JSON arguments.

        Returns:
            The remote agent's text for ``ask``, otherwise its result as JSON.
            Either is untrusted content.

        Raises:
            PermissionDeniedError: The caller lacks a permission the entry
                requires, or a tool check denied the call. Nothing is sent.
            PolicyUnavailableError: A tool check raised an error or did not
                answer before the run's deadline. Nothing is sent.
            LimitExceededError: A limit of the run stopped the call, for
                example ``TokenLimitExceededError`` when the call's tokens do
                not fit what the run has left.
            AuthenticationError: The credential could not provide its headers.
        """
        if not is_permitted(self.entry, self.principal):
            raise PermissionDeniedError(
                f"Remote skill '{self.entry.name}' requires permissions the "
                "caller does not hold"
            )
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            return error_result("Invalid arguments: expected a JSON object.")
        if self.entry.skill is None and not _is_ask(parsed):
            return error_result("Invalid arguments: expected one string 'text'.")
        await self._check(parsed)
        try:
            if self.entry.skill is None:
                return await self.binding.ask(
                    parsed["text"], max_total_tokens=self.max_total_tokens
                )
            value = await self.binding.call_with(
                self.entry.skill, parsed, self.max_total_tokens
            )
        except (RemoteAgentError, ClarificationNeededError) as error:
            # The model can choose another call; the operator still sees why.
            logger.warning(
                "Remote tool call '%s' failed with %s: %s (invocation_id=%s)",
                self.entry.name,
                type(error).__name__,
                error.message,
                self.correlation.invocation_id,
                extra=self.correlation.log_extra(),
            )
            return error_result(f"The remote call failed: {type(error).__name__}")
        return json.dumps(value)

    async def _check(self, arguments: dict[str, Any]) -> None:
        """Run the execution policy's tool checks on the call about to be sent."""
        if self.policy is None or not self.policy.tool_checks:
            return
        # Remote tools are offered only inside a skill; routing never offers them.
        assert self.skill is not None
        await enforce_tool_checks(
            self.policy.tool_checks,
            self.skill,
            self.principal,
            self.entry.name,
            arguments,
            self.deadline,
        )


def remote_tools(
    entries: Iterable[RemoteSkill],
    run: RunRemotes | None,
    *,
    principal: Principal | None,
    policy: ExecutionPolicy | None,
    skill: "SkillDefinition | None",
    deadline: float,
) -> Mapping[str, RemoteTool]:
    """Bind each entry to the run and return the tools by provider-safe name.

    Every entry is returned, including those the caller lacks permissions
    for: the generation layer leaves those out of what the model sees, and
    a call naming one still finds it, so the run fails instead of the model
    being told the name is unknown.

    Args:
        entries: Catalog entries the skill passed to ``ctx.generate``.
        run: The run's remote bindings, or ``None`` without a directory.
        principal: The run's verified caller, or ``None``.
        policy: The run's execution policy, or ``None``.
        skill: The running skill, as ``@tool`` calls pass it to tool checks.
        deadline: Event-loop time when the run times out.

    Raises:
        DefinitionError: An entry is not from a catalog this run built, is
            given twice, or its tool name is too long or the same as another
            entry's.
    """
    tools: dict[str, RemoteTool] = {}
    for entry in entries:
        if run is None:
            raise _not_from_this_run(entry)
        binding, max_tokens = _bound(entry, run)
        name = tool_name(entry)
        if name in tools:
            if tools[name].entry is entry:
                raise DefinitionError(
                    f"Remote skill '{entry.name}' is selected more than once"
                )
            raise DefinitionError(
                f"Remote skills '{tools[name].entry.name}' and '{entry.name}' "
                f"both become the model tool '{name}'; narrow the catalog or "
                "pass only one of them."
            )
        tools[name] = RemoteTool(
            name,
            entry,
            binding,
            max_tokens,
            run.correlation,
            principal,
            policy,
            skill,
            deadline,
        )
    return MappingProxyType(tools)


def tool_name(entry: RemoteSkill) -> str:
    """Return the entry's provider-safe tool name.

    Every character other than letters, digits, ``_`` and ``-`` becomes
    ``_``, so ``"research-agent.find_sources"`` is
    ``"research-agent_find_sources"``.

    Raises:
        DefinitionError: The name is longer than 64 characters.
    """
    name = safe_tool_name(entry.name)
    if len(name) > _MAX_TOOL_NAME:
        raise DefinitionError(
            f"Remote skill '{entry.name}' cannot be a model tool: its name is "
            f"longer than {_MAX_TOOL_NAME} characters."
        )
    return name


def _bound(entry: RemoteSkill, run: RunRemotes) -> tuple[Remote[Any], int | None]:
    """Return the entry's binding and token ceiling from this run's catalogs."""
    for catalog in run.catalogs:
        binding = catalog.binding(entry)
        if binding is not None:
            return binding, catalog.max_tokens_per_call
    raise _not_from_this_run(entry)


def _not_from_this_run(entry: RemoteSkill) -> DefinitionError:
    return DefinitionError(
        f"Remote skill '{entry.name}' is not from a catalog this run built; "
        "pass entries from ctx.remote_skills(...) to ctx.generate."
    )


def _is_ask(arguments: dict[str, Any]) -> bool:
    return arguments.keys() == {"text"} and isinstance(arguments["text"], str)
