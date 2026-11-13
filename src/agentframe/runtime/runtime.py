"""Run skills locally with validation, permissions, services and limits."""

import asyncio
import time
import uuid
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from agentframe.authoring.agent import Agent
from agentframe.authoring.context import RunContext
from agentframe.authoring.definitions import SkillDefinition
from agentframe.authoring.services import Services
from agentframe.errors import (
    AgentFrameError,
    DefinitionError,
    InputValidationError,
    OutputValidationError,
    PermissionDeniedError,
    PolicyUnavailableError,
    ProviderError,
    TimeoutLimitExceededError,
    describe_validation_error,
)
from agentframe.models.base import DEFAULT_MODEL_SETTINGS, ModelSettings
from agentframe.runtime.generation import (
    GenerationState,
    RunAccounting,
    input_token_limit,
)
from agentframe.runtime.limits import DEFAULT_LIMITS, RunLimits
from agentframe.runtime.options import RunOptions, merge_settings
from agentframe.runtime.usage import FinishReason, Usage
from agentframe.security.authorization import (
    AuthorizationPolicy,
    ToolAuthorizationPolicy,
)
from agentframe.security.principal import Principal


@dataclass(frozen=True)
class RunResult[T]:
    """The validated value of a run plus its execution metadata.

    ``tool_calls`` counts every tool call the model asked for, including ones
    that did not run because of an unknown tool name or invalid arguments.
    """

    value: T
    invocation_id: str
    usage: Usage
    model_calls: int
    tool_calls: int
    duration: timedelta
    finish_reason: FinishReason


class Runtime:
    """Runs agent skills locally through validation, permissions and limits.

    Calling a skill method directly bypasses all of these checks.

    Args:
        services: Services available to ``Inject[T]`` parameters.
        authorization: Application policy that checks each run against the
            requested resource. ``None`` means no resource checks.
        tool_authorization: Application policy that checks every tool call
            a model asks for, with its validated arguments, before it runs.
            ``None`` means only the tools' ``requires_permissions`` are
            checked. Strongly advised for tools with side effects.
    """

    def __init__(
        self,
        *,
        services: Services | None = None,
        authorization: AuthorizationPolicy | None = None,
        tool_authorization: ToolAuthorizationPolicy | None = None,
    ) -> None:
        self._services = services or Services()
        self._authorization = authorization
        self._tool_authorization = tool_authorization

    def validate(self, agent: Agent) -> None:
        """Check that ``agent`` can run here before accepting any work.

        Args:
            agent: Instance of an ``@agent``-decorated class.

        Raises:
            DefinitionError: The agent's class is not decorated with ``@agent``,
                or an ``Inject[T]`` parameter of a skill or registered tool has
                no registered service.
        """
        for skill in type(agent).definition().skills.values():
            self._check_services(f"Skill '{skill.id}'", skill.injected)
        for tool in agent.tools.values():
            self._check_services(f"Tool '{tool.name}'", tool.injected)

    async def validate_model(self, agent: Agent) -> None:
        """Check that every skill knows its input token limit before any runs.

        A skill's input limit is its effective ``RunLimits.max_input_tokens``,
        else the model's reported context window. ``AgentApp`` calls this at
        startup; local runs that skip it fail the same way at their first
        model call. Agents without a model pass.

        Reading the model's capabilities may do I/O (for example, an
        ``OllamaModel`` without ``context_window=`` asks its server), so it is
        bounded by the agent's timeout.

        Args:
            agent: Instance of an ``@agent``-decorated class.

        Raises:
            DefinitionError: The model reports no context window and a skill
                has no ``max_input_tokens``. A context size is never guessed.
            ProviderError: The model's capabilities could not be read, or
                reading them took longer than the agent's timeout.
        """
        if agent.model is None:
            return
        agent_limits = merge_settings(DEFAULT_LIMITS, agent.limits)
        try:
            async with asyncio.timeout(agent_limits.timeout_seconds):
                capabilities = await agent.model.capabilities()
        except TimeoutError:
            raise ProviderError(
                "Reading the model's capabilities timed out", transient=True
            ) from None
        for skill in type(agent).definition().skills.values():
            limits, _ = _effective_settings(agent, skill, RunOptions())
            try:
                input_token_limit(limits, capabilities)
            except DefinitionError as error:
                raise DefinitionError(f"Skill '{skill.id}': {error.message}") from None

    async def invoke(
        self,
        agent: Agent,
        *,
        skill: str,
        arguments: Mapping[str, Any] | None = None,
        principal: Principal | None = None,
        options: RunOptions | None = None,
    ) -> Any:
        """Run a skill and return only its validated value.

        Takes the same arguments and raises the same errors as ``run``.
        """
        result = await self.run(
            agent,
            skill=skill,
            arguments=arguments,
            principal=principal,
            options=options,
        )
        return result.value

    async def run(
        self,
        agent: Agent,
        *,
        skill: str,
        arguments: Mapping[str, Any] | None = None,
        principal: Principal | None = None,
        options: RunOptions | None = None,
    ) -> RunResult[Any]:
        """Run a skill and return its value with execution metadata.

        Args:
            agent: Instance of an ``@agent``-decorated class.
            skill: Id of the skill to run.
            arguments: Business arguments for the skill. ``None`` means none.
            principal: Verified caller identity supplied by trusted code.
            options: Limits and model settings for this run only. Each field
                they set overrides the skill's, agent's and library's value.

        Returns:
            A ``RunResult`` holding the validated value.

        Raises:
            InputValidationError: Unknown skill or invalid arguments.
            DefinitionError: Undecorated agent, or a service needed by the
                skill or a registered tool is not registered.
            PermissionDeniedError: The principal lacks a permission the skill
                or a tool the model called requires, or an authorization
                policy denied the run or the tool call.
            PolicyUnavailableError: An authorization policy raised an error
                or did not answer before the run's deadline.
            LimitExceededError: The run exceeded a limit. The subclass names
                which one: ``TimeoutLimitExceededError``,
                ``TokenLimitExceededError``, ``ModelCallLimitExceededError``
                or ``ToolCallLimitExceededError``.
            OutputValidationError: The skill or a tool returned an invalid value.

            Every ``AgentFrameError`` carries this run's ``invocation_id``.
            Exceptions raised by the skill, or by a tool it lets the model
            call, propagate unchanged.
        """
        return await self._run_invocation(
            str(uuid.uuid4()),
            agent,
            skill,
            arguments or {},
            principal,
            options or RunOptions(),
        )

    async def _run_invocation(
        self,
        invocation_id: str,
        agent: Agent,
        skill: str,
        arguments: Mapping[str, Any],
        principal: Principal | None,
        options: RunOptions,
        started_at: float | None = None,
        accounting: RunAccounting | None = None,
    ) -> RunResult[Any]:
        """Run like ``run``, under an invocation ID chosen by the caller.

        Package-internal: the A2A host allocates the ID itself so it can report
        the run's ID for skill exceptions, which propagate without one. It
        passes ``started_at``, the event-loop time its run began, so time spent
        routing the request counts against the skill's timeout, and its own
        ``accounting`` so it can report usage when the run fails or is cancelled.
        """
        started = time.perf_counter()
        accounting = accounting or RunAccounting()
        try:
            value = await self._run(
                invocation_id,
                agent,
                skill,
                arguments,
                principal,
                options,
                started_at,
                accounting,
            )
        except AgentFrameError as error:
            error.invocation_id = invocation_id
            raise
        return RunResult(
            value=value,
            invocation_id=invocation_id,
            usage=accounting.usage,
            model_calls=accounting.model_calls,
            tool_calls=accounting.tool_calls,
            duration=timedelta(seconds=time.perf_counter() - started),
            finish_reason=FinishReason.COMPLETED,
        )

    async def _run(
        self,
        invocation_id: str,
        agent: Agent,
        skill_id: str,
        arguments: Mapping[str, Any],
        principal: Principal | None,
        options: RunOptions,
        started_at: float | None,
        accounting: RunAccounting,
    ) -> Any:
        """Run the skill and return its validated value."""
        definition = type(agent).definition()
        skill = definition.skills.get(skill_id)
        if skill is None:
            # The ID is not echoed: remote callers get this message too.
            raise InputValidationError(
                f"Agent '{definition.name}' has no skill with the requested id"
            )
        self._check_services(f"Skill '{skill.id}'", skill.injected)
        for tool in agent.tools.values():
            self._check_services(f"Tool '{tool.name}'", tool.injected)
        _check_permissions(skill, principal)
        validated_arguments = _validate_arguments(skill, arguments)

        limits, model_settings = _effective_settings(agent, skill, options)
        timeout_seconds = limits.timeout_seconds
        assert timeout_seconds is not None  # DEFAULT_LIMITS sets every field.
        method = getattr(agent, skill.method_name)
        if started_at is None:
            started_at = asyncio.get_running_loop().time()
        deadline = started_at + timeout_seconds
        await self._check_policy(skill, principal, validated_arguments, deadline)
        try:
            async with (
                asyncio.timeout_at(deadline) as timeout,
                AsyncExitStack() as stack,
            ):
                services = self._services.scope(stack)
                generation = GenerationState(
                    model=agent.model,
                    instructions=agent.instructions,
                    settings=model_settings,
                    limits=limits,
                    services=services,
                    accounting=accounting,
                    tools=agent.tools,
                    tool_authorization=self._tool_authorization,
                )
                ctx = RunContext(invocation_id, principal, deadline, generation)
                injected = await services.resolve(skill.injected)
                result = await method(**validated_arguments, **injected, ctx=ctx)
        except TimeoutError:
            if not timeout.expired():
                raise
            raise TimeoutLimitExceededError(
                f"Skill '{skill.id}' exceeded its timeout of {timeout_seconds} seconds"
            ) from None
        return _validate_output(skill, result)

    async def _check_policy(
        self,
        skill: SkillDefinition,
        principal: Principal | None,
        arguments: dict[str, Any],
        deadline: float,
    ) -> None:
        """Ask the authorization policy about this run, failing closed."""
        if self._authorization is None:
            return
        try:
            # The policy shares the run's deadline; running out of time while
            # waiting for it is a policy outage, not permission.
            async with asyncio.timeout_at(deadline):
                allowed = await self._authorization.check(
                    principal, skill, MappingProxyType(arguments)
                )
        except Exception as error:
            raise PolicyUnavailableError(
                f"Authorization policy for skill '{skill.id}' is unavailable"
            ) from error
        # Fail closed: only an explicit True allows the run, not any truthy value.
        # noinspection PySimplifyBooleanCheck
        if allowed is not True:
            # Deliberately generic so a denial never reveals whether the
            # requested resource exists.
            raise PermissionDeniedError(
                f"Skill '{skill.id}' was denied by the authorization policy"
            )

    def _check_services(self, where: str, injected: Mapping[str, object]) -> None:
        for parameter_name, service_type in injected.items():
            if not self._services.is_registered(service_type):
                raise DefinitionError(
                    f"{where}: no service registered for parameter '{parameter_name}'"
                )


def _effective_settings(
    agent: Agent, skill: SkillDefinition, options: RunOptions
) -> tuple[RunLimits, ModelSettings]:
    """Merge library defaults, agent, skill and run settings, field by field."""
    limits = merge_settings(DEFAULT_LIMITS, agent.limits, skill.limits, options.limits)
    model_settings = merge_settings(
        DEFAULT_MODEL_SETTINGS,
        agent.model_settings,
        skill.model_settings,
        options.model_settings,
    )
    return limits, model_settings


def _check_permissions(skill: SkillDefinition, principal: Principal | None) -> None:
    if not skill.requires_permissions:
        return
    if principal is None:
        raise PermissionDeniedError(f"Skill '{skill.id}' requires a caller identity")
    missing = skill.requires_permissions - principal.permissions
    if missing:
        raise PermissionDeniedError(
            f"Skill '{skill.id}' requires permissions: {', '.join(sorted(missing))}"
        )


def _validate_arguments(
    skill: SkillDefinition, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        model = skill.arguments_adapter.validate_python(arguments)
    except ValidationError as error:
        raise InputValidationError(
            f"Invalid arguments for skill '{skill.id}': "
            f"{describe_validation_error(error)}"
        ) from None
    return {name: getattr(model, name) for name in skill.argument_names}


def _validate_output(skill: SkillDefinition, result: Any) -> Any:
    try:
        return skill.return_adapter.validate_python(result)
    except ValidationError as error:
        raise OutputValidationError(
            f"Skill '{skill.id}' returned an invalid value: "
            f"{describe_validation_error(error)}"
        ) from None
