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
from agentframe.discovery.binding import find_remote
from agentframe.discovery.directory import AgentDirectory
from agentframe.discovery.identity import (
    OnBehalfOf,
    Service,
    UserToken,
    checked_default_remote,
    checked_remotes,
    on_behalf_of_names,
)
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
from agentframe.models.base import DEFAULT_MODEL_SETTINGS, ModelSettings, TextSink
from agentframe.runtime.accounting import RunAccounting
from agentframe.runtime.correlation import RunCorrelation, root_run
from agentframe.runtime.generation import GenerationState
from agentframe.runtime.limits import DEFAULT_LIMITS, RunLimits, cap_limits
from agentframe.runtime.options import RunOptions, merge_settings
from agentframe.runtime.remotes import RunRemotes
from agentframe.runtime.state import (
    InMemoryStateStore,
    RunConversation,
    StateStore,
    run_conversation,
)
from agentframe.runtime.tokens import input_token_limit
from agentframe.security.authorization import (
    AuthorizationPolicy,
    ToolAuthorizationPolicy,
)
from agentframe.security.execution_policy import ExecutionPolicy, enforce_checks
from agentframe.security.principal import Principal
from agentframe.usage import FinishReason, Usage


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


@dataclass(frozen=True)
class _Invocation:
    """What one run was asked to do, as ``Runtime.run_invocation`` received it."""

    invocation_id: str
    agent: Agent
    skill_id: str
    arguments: Mapping[str, Any]
    principal: Principal | None
    options: RunOptions
    started_at: float | None
    accounting: RunAccounting
    limits_ceiling: RunLimits | None
    hops: int
    user_token: UserToken | None
    correlation: RunCorrelation
    own_interface_url: str | None
    conversation_id: str | None
    on_text: TextSink | None = None


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
        remotes: How to authenticate to each remote agent, by name, such as
            ``{"research-agent": Service(credential=APIKeyCredential(key))}``
            or ``OnBehalfOf(scope=...)``. A remote without an entry is called
            without credentials, which only works when its card declares no
            security. ``OnBehalfOf`` needs a signed-in Entra user, so it
            works only in runs hosted by an ``AgentApp`` with ``EntraIDAuth``.
        default_remote: How to authenticate to skill-catalog agents
            (``ctx.remote_skills(...)``) that have no ``remotes=`` entry. It is
            sent only to agents whose card requires credentials it meets;
            others whose card requires credentials are skipped. It is never
            used for ``Remote`` parameters. Every such registered agent
            receives it, so use it only when each is trusted with it.
            ``None`` (the default) includes only open agents and agents with
            ``remotes=`` entries.
        state_store: Where conversation history is kept between runs
            (``ctx.history()`` and ``ctx.save_turn()``). ``None`` uses a new
            ``InMemoryStateStore()``, which is bounded and lost on restart.

    Raises:
        DefinitionError: ``remotes`` is not a mapping of names to ``Service``
            or ``OnBehalfOf``, or ``default_remote`` is neither of them nor
            ``None``.
    """

    def __init__(
        self,
        *,
        services: Services | None = None,
        authorization: AuthorizationPolicy | None = None,
        tool_authorization: ToolAuthorizationPolicy | None = None,
        policy: ExecutionPolicy | None = None,
        directory: AgentDirectory | None = None,
        remotes: Mapping[str, Service | OnBehalfOf] | None = None,
        default_remote: Service | OnBehalfOf | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        self._services = services or Services()
        self._authorization = authorization
        self._tool_authorization = tool_authorization
        self._policy = policy
        self._directory = directory
        self._remotes = checked_remotes(remotes)
        self._default_remote = checked_default_remote(default_remote)
        # Each (agent, skill) logs its catalog identity mode once.
        self._logged_identities: set[tuple[str, str]] = set()
        self._state_store = (
            state_store if state_store is not None else InMemoryStateStore()
        )

    @property
    def policy(self) -> ExecutionPolicy | None:
        """The execution policy given at construction, if any.

        Package-internal: the A2A host runs its model-request checks on
        routing requests too.
        """
        return self._policy

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
                credentials its ``remotes=`` entry cannot provide.
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
            await find_remote(self._directory, name, self._remotes.get(name))

    def on_behalf_of_remotes(self) -> tuple[list[str], bool]:
        """Say which remotes are called ``OnBehalfOf`` the signed-in user.

        Package-internal: ``AgentApp`` refuses them when its host cannot
        exchange user tokens.

        Returns:
            The names of the ``OnBehalfOf`` entries in ``remotes=``, sorted,
            and whether ``default_remote`` is ``OnBehalfOf``.
        """
        default = isinstance(self._default_remote, OnBehalfOf)
        return on_behalf_of_names(self._remotes), default

    async def invoke(
        self,
        agent: Agent,
        *,
        skill: str,
        arguments: Mapping[str, Any] | None = None,
        principal: Principal | None = None,
        options: RunOptions | None = None,
        conversation_id: str | None = None,
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
            conversation_id=conversation_id,
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
        conversation_id: str | None = None,
        on_text: TextSink | None = None,
    ) -> RunResult[Any]:
        """Run a skill and return its value with execution metadata.

        Args:
            agent: Instance of an ``@agent``-decorated class.
            skill: Id of the skill to run.
            arguments: Business arguments for the skill. ``None`` means none.
            principal: Verified caller identity supplied by trusted code.
            options: Limits and model settings for this run only. Each field
                they set overrides the skill's, agent's and library's value.
            conversation_id: The conversation this run continues, for
                ``ctx.history()`` and ``ctx.save_turn()``. Needs a
                ``principal``: conversations are kept per agent, caller and
                ID. ``None`` means the run has no conversation.
            on_text: Awaited with each piece of model text the skill
                generates with ``ctx.generate(..., stream=True)``, as it
                arrives, within the run's deadline. Not called when the
                execution policy has output checks, so nothing is released
                before they pass. Streamed text is a preview: the result is
                still the skill's validated value. Its exceptions fail the
                run unchanged. ``None`` streams nothing.

        Returns:
            A ``RunResult`` holding the validated value.

        Raises:
            InputValidationError: Unknown skill, invalid arguments, or a
                ``conversation_id`` that is empty or over 256 characters.
            DefinitionError: Undecorated agent, a service needed by the
                skill or a registered tool is not registered, a ``Remote``
                parameter's agent is unknown or requires credentials, or a
                ``conversation_id`` is given without a ``principal``.
            RemoteAgentError: A ``Remote`` parameter's agent card cannot be
                fetched or is invalid.
            PermissionDeniedError: The principal lacks a permission the skill
                or a tool the model called requires, a ``Remote`` parameter's
                agent is called ``OnBehalfOf`` a user and the run has no
                signed-in user (local runs never do), an authorization policy
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
        return await self.run_invocation(
            str(uuid.uuid4()),
            agent,
            skill,
            arguments or {},
            principal,
            options or RunOptions(),
            conversation_id=conversation_id,
            on_text=on_text,
        )

    async def run_invocation(
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
        hops: int = 0,
        user_token: UserToken | None = None,
        correlation: RunCorrelation | None = None,
        own_interface_url: str | None = None,
        conversation_id: str | None = None,
        on_text: TextSink | None = None,
    ) -> RunResult[Any]:
        """Run like ``run``, under an invocation ID chosen by the caller.

        Package-internal: the A2A host allocates the ID itself so it can report
        the run's ID for skill exceptions, which propagate without one. It
        passes ``started_at``, the event-loop time its run began, so time spent
        routing the request counts against the skill's timeout, its own
        ``accounting`` so it can report usage when the run fails or is
        cancelled, its ``limits_ceiling``, which caps the merged limits (and
        carries the caller's deadline), the request's ``hops``, the
        signed-in caller's ``user_token`` for ``OnBehalfOf`` remotes, the
        request's ``correlation`` with the agents before it (``None`` makes
        the run a root run), the A2A interface URL it advertises, which
        leaves the agent out of its own skill catalog, the task's
        ``contextId`` as ``conversation_id`` when the caller is verified, and
        ``on_text`` for a streaming request.
        """
        started = time.perf_counter()
        accounting = accounting or RunAccounting()
        if correlation is None:
            correlation = root_run(type(agent).definition().name, invocation_id)
        invocation = _Invocation(
            invocation_id=invocation_id,
            agent=agent,
            skill_id=skill,
            arguments=arguments,
            principal=principal,
            options=options,
            started_at=started_at,
            accounting=accounting,
            limits_ceiling=limits_ceiling,
            hops=hops,
            user_token=user_token,
            correlation=correlation,
            own_interface_url=own_interface_url,
            conversation_id=conversation_id,
            on_text=on_text,
        )
        try:
            value = await self._run(invocation)
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

    async def _run(self, run: _Invocation) -> Any:
        """Run the skill and return its validated value."""
        skill = self._checked_skill(run.agent, run.skill_id)
        _check_permissions(skill, run.principal)
        validated_arguments = _validate_arguments(skill, run.arguments)
        conversation = run_conversation(
            self._state_store,
            type(run.agent).definition().name,
            run.principal.id if run.principal is not None else None,
            run.conversation_id,
        )

        limits, model_settings = _effective_settings(
            run.agent, skill, run.options, run.limits_ceiling
        )
        timeout_seconds = limits.timeout_seconds
        assert timeout_seconds is not None  # DEFAULT_LIMITS sets every field.
        started_at = run.started_at
        if started_at is None:
            started_at = asyncio.get_running_loop().time()
        deadline = started_at + timeout_seconds
        await self._check_policy(skill, run.principal, validated_arguments, deadline)
        if self._policy is not None:
            await enforce_checks(
                self._policy.input_checks,
                skill,
                run.principal,
                MappingProxyType(validated_arguments),
                deadline,
            )
        try:
            async with asyncio.timeout_at(deadline) as timeout:
                result = await self._call_skill(
                    run,
                    skill,
                    validated_arguments,
                    limits,
                    model_settings,
                    deadline,
                    conversation,
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
                self._policy.output_checks, skill, run.principal, value, deadline
            )
        return value

    def _checked_skill(self, agent: Agent, skill_id: str) -> SkillDefinition:
        """Return the skill to run, once its services and directory are known.

        Raises:
            InputValidationError: The agent has no skill ``skill_id``.
            DefinitionError: A service the skill or a registered tool needs
                is not registered, or the skill takes a ``Remote`` parameter
                and the runtime has no directory.
        """
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
        return skill

    async def _call_skill(
        self,
        run: _Invocation,
        skill: SkillDefinition,
        arguments: dict[str, Any],
        limits: RunLimits,
        model_settings: ModelSettings,
        deadline: float,
        conversation: RunConversation | None,
    ) -> Any:
        """Call the skill's method with its services, remotes and ``RunContext``.

        Scoped services and anything else the run opens are closed when the
        method returns or raises.
        """
        method = getattr(run.agent, skill.method_name)
        async with AsyncExitStack() as stack:
            services = self._services.scope(stack)
            generation = GenerationState(
                model=run.agent.model,
                instructions=run.agent.instructions,
                settings=model_settings,
                limits=limits,
                services=services,
                accounting=run.accounting,
                tools=run.agent.tools,
                tool_authorization=self._tool_authorization,
                skill=skill,
                policy=self._policy,
                text_sink=self._text_sink(run.on_text),
            )
            run_remotes = self._run_remotes(run, limits, deadline, stack)
            ctx = RunContext(
                run.invocation_id,
                run.principal,
                deadline,
                generation,
                hops=run.hops,
                _remotes=run_remotes,
                _conversation=conversation,
            )
            injected = await services.resolve(skill.injected)
            remotes: dict[str, object] = {}
            for parameter_name, name in skill.remotes.items():
                assert run_remotes is not None  # Checked before the run starts.
                remotes[parameter_name] = await run_remotes.bind(name)
            return await method(**arguments, **injected, **remotes, ctx=ctx)

    def _text_sink(self, on_text: TextSink | None) -> TextSink | None:
        """Return ``on_text``, unless output checks must see the answer first."""
        if self._policy is not None and self._policy.output_checks:
            return None
        return on_text

    def _run_remotes(
        self,
        run: _Invocation,
        limits: RunLimits,
        deadline: float,
        stack: AsyncExitStack,
    ) -> RunRemotes | None:
        """Return the run's view of remote agents, or ``None`` without a directory."""
        if self._directory is None:
            return None
        return RunRemotes(
            self._directory,
            self._remotes,
            default=self._default_remote,
            deadline=deadline,
            correlation=run.correlation,
            principal=run.principal,
            hops=run.hops,
            limits=limits,
            accounting=run.accounting,
            user_token=run.user_token,
            stack=stack,
            own_interface_url=run.own_interface_url,
            logged_identities=self._logged_identities,
        )

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
