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
from agentframe.discovery.directory import AgentDirectory
from agentframe.discovery.remote import bind_remote, find_remote
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
from agentframe.runtime.limits import DEFAULT_LIMITS, RunLimits, cap_limits
from agentframe.runtime.options import RunOptions, merge_settings
from agentframe.runtime.usage import FinishReason, Usage
from agentframe.security.authorization import (
    AuthorizationPolicy,
    ToolAuthorizationPolicy,
)
from agentframe.security.execution_policy import ExecutionPolicy, enforce_checks
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
        policy: Application content checks on each run's input, every model
            request and the output. They add to authorization, never replace
            it. ``None`` means no content checks.
        directory: Where ``Remote[Literal["name"]]`` parameters find their
            agents. The application opens and closes it. ``None`` means
            skills cannot take ``Remote`` parameters.
    """

    def __init__(
        self,
        *,
        services: Services | None = None,
        authorization: AuthorizationPolicy | None = None,
        tool_authorization: ToolAuthorizationPolicy | None = None,
        policy: ExecutionPolicy | None = None,
        directory: AgentDirectory | None = None,
    ) -> None:
        self._services = services or Services()
        self._authorization = authorization
        self._tool_authorization = tool_authorization
        self._policy = policy
        self._directory = directory

    def validate(self, agent: Agent) -> None:
        """Check that ``agent`` can run here before accepting any work.

        Args:
            agent: Instance of an ``@agent``-decorated class.

        Raises:
            DefinitionError: The agent's class is not decorated with ``@agent``,
                an ``Inject[T]`` parameter of a skill or registered tool has
                no registered service, or a skill takes a ``Remote``
                parameter and the runtime has no directory.
        """
        for skill in type(agent).definition().skills.values():
            self._check_services(f"Skill '{skill.id}'", skill.injected)
            self._check_directory(skill)
        for tool in agent.tools.values():
            self._check_services(f"Tool '{tool.name}'", tool.injected)

    async def validate_model(
        self, agent: Agent, *, limits_ceiling: RunLimits | None = None
    ) -> None:
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
            limits_ceiling: A host's ceiling that caps every limit, as
                ``AgentApp`` passes ``HostConfig.limits_ceiling``. ``None``
                (local use) caps nothing.

        Raises:
            DefinitionError: The model reports no context window and a skill
                has no ``max_input_tokens``. A context size is never guessed.
            ProviderError: The model's capabilities could not be read, or
                reading them took longer than the agent's timeout.
        """
        if agent.model is None:
            return
        agent_limits = cap_limits(
            merge_settings(DEFAULT_LIMITS, agent.limits), limits_ceiling
        )
        try:
            async with asyncio.timeout(agent_limits.timeout_seconds):
                capabilities = await agent.model.capabilities()
        except TimeoutError:
            raise ProviderError(
                "Reading the model's capabilities timed out", transient=True
            ) from None
        for skill in type(agent).definition().skills.values():
            limits, _ = _effective_settings(agent, skill, RunOptions(), limits_ceiling)
            try:
                input_token_limit(limits, capabilities)
            except DefinitionError as error:
                raise DefinitionError(f"Skill '{skill.id}': {error.message}") from None

    async def validate_remotes(self, agent: Agent) -> None:
        """Check that every ``Remote`` parameter's agent can be called.

        Looks each name up in the directory, fetching its card. ``AgentApp``
        calls this at startup; local runs that skip it fail the same way
        when a skill with that parameter runs.

        Args:
            agent: Instance of an ``@agent``-decorated class.

        Raises:
            DefinitionError: A skill takes a ``Remote`` parameter and the
                runtime has no directory, the directory is not open, no
                source lists the name, or the agent's card requires
                credentials.
            RemoteAgentError: An agent's card cannot be fetched or is invalid.
        """
        skills = type(agent).definition().skills.values()
        for skill in skills:
            self._check_directory(skill)
        names = {name for skill in skills for name in skill.remotes.values()}
        if not names:
            return
        assert self._directory is not None  # _check_directory passed.
        for name in sorted(names):
            await find_remote(self._directory, name)

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
            DefinitionError: Undecorated agent, a service needed by the
                skill or a registered tool is not registered, or a ``Remote``
                parameter's agent is unknown or requires credentials.
            RemoteAgentError: A ``Remote`` parameter's agent card cannot be
                fetched or is invalid.
            PermissionDeniedError: The principal lacks a permission the skill
                or a tool the model called requires, an authorization policy
                denied the run or the tool call, or an execution policy check
                blocked the input, a model request or the output.
            PolicyUnavailableError: An authorization policy or execution
                policy check raised an error or did not answer before the
                run's deadline.
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
        limits_ceiling: RunLimits | None = None,
    ) -> RunResult[Any]:
        """Run like ``run``, under an invocation ID chosen by the caller.

        Package-internal: the A2A host allocates the ID itself so it can report
        the run's ID for skill exceptions, which propagate without one. It
        passes ``started_at``, the event-loop time its run began, so time spent
        routing the request counts against the skill's timeout, its own
        ``accounting`` so it can report usage when the run fails or is
        cancelled, and its ``limits_ceiling``, which caps the merged limits.
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
                limits_ceiling,
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
        limits_ceiling: RunLimits | None,
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
        self._check_directory(skill)
        for tool in agent.tools.values():
            self._check_services(f"Tool '{tool.name}'", tool.injected)
        _check_permissions(skill, principal)
        validated_arguments = _validate_arguments(skill, arguments)

        limits, model_settings = _effective_settings(
            agent, skill, options, limits_ceiling
        )
        timeout_seconds = limits.timeout_seconds
        assert timeout_seconds is not None  # DEFAULT_LIMITS sets every field.
        method = getattr(agent, skill.method_name)
        if started_at is None:
            started_at = asyncio.get_running_loop().time()
        deadline = started_at + timeout_seconds
        await self._check_policy(skill, principal, validated_arguments, deadline)
        if self._policy is not None:
            await enforce_checks(
                self._policy.input_checks,
                skill,
                principal,
                MappingProxyType(validated_arguments),
                deadline,
            )
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
                    skill=skill,
                    policy=self._policy,
                )
                ctx = RunContext(invocation_id, principal, deadline, generation)
                injected = await services.resolve(skill.injected)
                remotes = await self._bind_remotes(skill, ctx)
                result = await method(
                    **validated_arguments, **injected, **remotes, ctx=ctx
                )
        except TimeoutError:
            if not timeout.expired():
                raise
            raise TimeoutLimitExceededError(
                f"Skill '{skill.id}' exceeded its timeout of {timeout_seconds} seconds"
            ) from None
        value = _validate_output(skill, result)
        if self._policy is not None:
            await enforce_checks(
                self._policy.output_checks, skill, principal, value, deadline
            )
        return value

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

    async def _bind_remotes(
        self, skill: SkillDefinition, ctx: RunContext
    ) -> dict[str, object]:
        """Create this run's ``Remote`` bindings, within the run's deadline."""
        bindings: dict[str, object] = {}
        for parameter_name, name in skill.remotes.items():
            assert self._directory is not None  # Checked before the run starts.
            bindings[parameter_name] = await bind_remote(
                self._directory,
                name,
                deadline=ctx.deadline,
                invocation_id=ctx.invocation_id,
                principal=ctx.principal,
            )
        return bindings

    def _check_directory(self, skill: SkillDefinition) -> None:
        if skill.remotes and self._directory is None:
            raise DefinitionError(
                f"Skill '{skill.id}' takes a Remote parameter, but no directory "
                "was given; pass directory= to Runtime or AgentApp"
            )

    def _check_services(self, where: str, injected: Mapping[str, object]) -> None:
        for parameter_name, service_type in injected.items():
            if not self._services.is_registered(service_type):
                raise DefinitionError(
                    f"{where}: no service registered for parameter '{parameter_name}'"
                )


def _effective_settings(
    agent: Agent,
    skill: SkillDefinition,
    options: RunOptions,
    limits_ceiling: RunLimits | None,
) -> tuple[RunLimits, ModelSettings]:
    """Merge library defaults, agent, skill and run settings, field by field.

    The merged limits are then capped by the host's ceiling, if any.
    """
    limits = cap_limits(
        merge_settings(DEFAULT_LIMITS, agent.limits, skill.limits, options.limits),
        limits_ceiling,
    )
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
