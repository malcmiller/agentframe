"""Per-run context passed to skills."""

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, overload

from pydantic import BaseModel

from agentframe.discovery.remote import Remote
from agentframe.errors import DefinitionError
from agentframe.orchestration.catalog import RemoteSkill, SkillCatalog, build_catalog
from agentframe.orchestration.execution import (
    DEFAULT_MAX_PARALLEL,
    OnFailure,
    PlanResult,
    run_plan,
)
from agentframe.orchestration.plan import DEFAULT_MAX_STEPS, Plan
from agentframe.orchestration.planning import make_plan
from agentframe.orchestration.remote_tools import remote_tools
from agentframe.runtime.generation import GenerationState
from agentframe.runtime.remotes import RunRemotes
from agentframe.security.principal import Principal


@dataclass(frozen=True)
class RunContext:
    """Per-run information passed to every skill. Never shared between runs.

    Attributes:
        invocation_id: Library-generated identifier of this run.
        principal: Verified caller identity, or ``None`` when none was supplied.
        deadline: Event-loop time (``loop.time()``) when the run times out.
        hops: The run's depth in a chain of calls between agents: 0 when
            started locally or by a caller that sent no depth, and one more
            than the calling run's depth when another agent sent it.
    """

    invocation_id: str
    principal: Principal | None
    deadline: float
    # Private per-run model state; the context is frozen but its accounting
    # must change as the run makes model calls.
    _generation: GenerationState = field(repr=False, compare=False)
    hops: int = 0
    # Private per-run bindings to remote agents; None without a directory.
    _remotes: RunRemotes | None = field(default=None, repr=False, compare=False)

    @overload
    async def generate(
        self,
        text: str,
        *,
        instructions: str | None = None,
        tools: Iterable[Callable[..., Any] | RemoteSkill] = (),
    ) -> str: ...

    @overload
    async def generate[T: BaseModel](
        self,
        text: str,
        *,
        instructions: str | None = None,
        output: type[T],
        tools: Iterable[Callable[..., Any] | RemoteSkill] = (),
    ) -> T: ...

    async def generate(
        self,
        text: str,
        *,
        instructions: str | None = None,
        output: type[BaseModel] | None = None,
        tools: Iterable[Callable[..., Any] | RemoteSkill] = (),
    ) -> str | BaseModel:
        """Generate text, or a validated ``output`` instance, with the agent's model.

        The agent's ``instructions`` and then ``instructions`` are sent as
        trusted ``system`` messages; ``text`` is sent as a separate ``user``
        message and never merged into them. Keeping them separate does not
        guarantee resistance to prompt injection.

        Args:
            text: Untrusted content for the model, such as caller input.
            instructions: Trusted instructions for this call only. Never put
                untrusted content here.
            output: A Pydantic ``BaseModel`` subclass. When given, the model
                is asked for JSON matching its schema and the reply is
                validated into an instance. Invalid output is never repaired
                or re-requested.
            tools: ``@tool`` functions to offer the model, each registered on
                the agent and selected once. Omitted or empty offers no
                tools; registered tools are never offered unless selected
                here. The model's tool calls run one at a time, in its
                order, and their results are sent back until it answers.
                A call naming a tool that was not offered, or with invalid
                arguments, runs nothing; the model is told what was wrong.
                Tools whose ``requires_permissions`` the caller lacks are
                not offered, and every call is authorized before it runs.
                ``RemoteSkill`` entries from this run's ``remote_skills``
                catalogs may be mixed in: see "Remote skills" below.

        Returns:
            The model's complete answer, or an ``output`` instance when
            ``output`` is given. With tools, the answer after the tool calls.

        Raises:
            DefinitionError: ``output`` is not a ``BaseModel`` subclass, or is
                a ``RootModel``, or a selected tool is not registered on the
                agent or is selected twice. Raised before anything is sent.
            UnsupportedCapabilityError: The agent has no model configured, or
                the model lacks something the request needs, such as
                structured output or tool calling. Raised before anything is
                sent.
            OutputValidationError: The structured reply is not valid JSON or
                does not match ``output``, or a tool returned a value that
                does not match its return annotation.
            ModelRefusalError: The model refused, or a content filter blocked
                its output.
            IncompleteOutputError: The output was cut short at the token
                limit or was empty. Partial text or objects are never returned;
                for text cut short at the limit, the error's ``partial_text``
                holds what arrived, and a skill may catch it and use it.
            ProviderError: The model provider call failed.
            PermissionDeniedError: The model called a tool or remote entry
                the caller lacks a required permission for, the runtime's
                tool authorization policy denied a call, or an execution
                policy tool check denied a call. The model is never told.
                Also raised, before anything is sent, when an execution
                policy model-request check blocks a request.
            PolicyUnavailableError: The tool authorization policy or an
                execution policy model-request or tool check raised an error
                or did not answer before the run's deadline.
            LimitExceededError: A model call does not fit the run's limits,
                the run has made ``max_tool_calls`` tool calls, or the run's
                timeout expired during a model or tool call.

        Exceptions raised by a tool propagate unchanged and fail the run;
        their text is never sent to the model.

        Remote skills: each catalog entry becomes one model tool named after
        ``entry.name``, with every character other than letters, digits,
        ``_`` and ``-`` replaced by ``_`` (``"research-agent.find_sources"``
        becomes ``"research-agent_find_sources"``), the entry's bounded
        description and a copy of its argument schema (``{"text": string}``
        for an ``ask`` entry). A call runs ``Remote.call`` or ``Remote.ask``
        on the catalog's binding, after the same permission and execution
        policy tool checks as a ``@tool`` call: entries whose
        ``requires_permissions`` the caller lacks are not offered, and
        before every call the permissions are checked again and each tool
        check gets the entry's name (``"agent.skill"`` or ``"agent"``, not
        the tool name) and the arguments about to be sent. The tool
        authorization policy is for ``@tool`` functions only. The run's
        deadline, depth, identity, correlation and token budget apply; a
        call holds the catalog's ``max_tokens_per_call``, or everything the
        run has left. Calls run
        one at a time, count toward ``max_tool_calls`` and are never retried.
        The result goes back to the model as untrusted tool output: the
        remote result as JSON, or the ``ask`` answer as text. A
        ``RemoteAgentError`` or ``ClarificationNeededError`` goes back as a
        tool error naming only its class (and is logged); limit,
        authentication and other errors fail the run. A remote tool whose
        name matches another tool, an entry selected twice, or an entry
        from another run's catalog raises ``DefinitionError`` before
        anything is sent; an argument schema the model's provider cannot use
        raises ``UnsupportedCapabilityError`` before sending.
        """
        selected = list(tools)  # May be a one-shot iterator.
        functions = [item for item in selected if not isinstance(item, RemoteSkill)]
        entries = [item for item in selected if isinstance(item, RemoteSkill)]
        remote = remote_tools(
            entries,
            self._remotes,
            principal=self.principal,
            policy=self._generation.policy,
            skill=self._generation.skill,
            deadline=self.deadline,
        )
        if output is None:
            return await self._generation.generate_text(
                self, text, instructions, functions, remote
            )
        return await self._generation.generate_output(
            self, text, instructions, output, functions, remote
        )

    async def remote_skills(
        self,
        *remotes: Remote[Any],
        agents: Collection[str] | None = None,
        skill: str | None = None,
        tags: Collection[str] = (),
        ask: bool = False,
        max_tokens_per_call: int | None = None,
        permissions: Mapping[str, Collection[str]] | None = None,
    ) -> SkillCatalog:
        """Return a catalog of the remote skills this run may offer a model.

        With no ``remotes``, every agent the directory's sources list is
        considered: the registry is the allowlist. An agent is included only
        when its card can be fetched and is valid (with the directory's
        usual checks and cache, at most eight fetches at a time) and its
        card's security is open or met by its ``remotes=`` entry. Any other
        agent, and any source that cannot list its agents, is skipped with
        one WARNING log naming only the agent (or the source's position and
        class) and the reason. An empty catalog is valid. When the run is
        hosted by an ``AgentApp``, the agent itself is left out (matched by
        its advertised A2A URL); a local run leaves nothing out.

        With ``remotes``, the skill's own ``Remote`` parameters, the catalog
        holds exactly those agents, and a problem with one raises instead.

        An agent whose card declares AgentFrame's skill-call extension gives
        one entry per card skill, with its argument schema; any other agent
        gives one free-text ``ask`` entry describing it. Card text is
        untrusted: descriptions (also inside schemas) are cut to 500
        characters, at most ten tags are kept, control characters are
        removed and ``examples`` are dropped. Nothing calls a model.

        Args:
            *remotes: ``Remote`` parameters of the running skill.
            agents: Agent names to keep. ``None`` keeps every agent.
            skill: Keep only card skills with this ID.
            tags: Keep only card skills that have all of these tags. Matched
                with ``skill`` on the same card skill, as
                ``AgentDirectory.find``; an ``ask`` entry is kept when any of
                its agent's card skills matches.
            ask: Also add an ``ask`` entry for agents with the extension.
            max_tokens_per_call: The most tokens each model-chosen call to
                an entry may use (see ``generate``). ``None`` holds
                everything the run has left, which is safe because those
                calls run one at a time.
            permissions: Permissions a caller must hold to use an entry, by
                entry name, for example ``{"billing-agent.refund":
                {"refunds:write"}}``: the caller needs all of them, as for
                ``@tool(requires_permissions=...)``. Entries the caller
                lacks are never shown to a model, and a call naming one
                fails the run with ``PermissionDeniedError`` before anything
                is sent. An agent's ``ask`` entry also requires every
                permission given for that agent's skills, since free text
                can ask for anything they do. Keys are entry names, not
                tool names: a tool name raises ``DefinitionError``, as does
                a key of a declared ``Remote`` that names none of its card's
                entries. Any other key matching no entry in this catalog
                (for example because its agent is down) logs one WARNING;
                use tool checks as defense in depth for consequential
                entries. Hidden entries still count toward the 20-entry
                limit, so whether a catalog is too large never depends on
                the caller.

        Returns:
            The catalog, holding the run's binding to each of its agents.

        Raises:
            DefinitionError: The runtime has no directory, the directory is
                not open, a name is listed by more than one source, no
                source lists a name in ``agents`` (unless a source failed),
                ``agents`` or ``tags`` is a single string, a positional
                argument is not a ``Remote``, two entries share a name, or
                more than 20 entries remain after narrowing and skipping. A
                catalog is never truncated. Also raised when
                ``max_tokens_per_call`` is not a whole number above 0, or
                ``permissions`` is not a mapping of names to lists of
                strings, has a tool name as a key, or has a key of a
                declared agent that names none of its entries.
            RemoteAgentError: A ``Remote`` parameter's card has an unsafe
                or repeated skill ID, or a missing or over-8-KiB argument
                schema.
            PermissionDeniedError: An included agent is called
                ``OnBehalfOf`` the user, and the run has no signed-in user.
        """
        if self._remotes is None:
            raise DefinitionError(
                "remote_skills needs an AgentDirectory; pass directory= to "
                "Runtime or AgentApp"
            )
        return await build_catalog(
            self._remotes,
            remotes,
            agents=agents,
            skill=skill,
            tags=tags,
            ask=ask,
            max_tokens_per_call=max_tokens_per_call,
            permissions=permissions,
        )

    async def plan(
        self,
        goal: str,
        skills: SkillCatalog,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        instructions: str | None = None,
    ) -> Plan:
        """Ask the agent's model for a checked plan to reach ``goal`` with ``skills``.

        Makes one structured-output call with the agent's model, like
        ``generate(output=...)``: it counts toward the run's model calls and
        tokens, and the execution policy's model-request checks see it.
        Library-owned planning instructions, then ``instructions``, are the
        trusted ``system`` messages. The goal and the catalog (entry names,
        bounded descriptions and tags, argument schemas) go together in the
        untrusted ``user`` message as JSON data, never in instructions.

        Only entries the caller may use (see ``remote_skills(permissions=
        ...)``) are shown to the model and accepted in its plan; a step
        using any other entry fails exactly as one using an unknown name.
        The plan is checked with ``SkillCatalog.check`` and never repaired
        or re-requested: call ``plan`` again, perhaps with a reviewed
        summary of what went wrong, to try again. Nothing is sent to
        another agent.

        Args:
            goal: What the plan should achieve. Untrusted content, such as
                caller input.
            skills: A catalog this run built with ``remote_skills``.
            max_steps: The most steps the plan may have, never more than the
                tool calls the run has left. The model is told the limit.
            instructions: Extra trusted planning instructions, such as
                domain hints. Never put untrusted content here.

        Returns:
            The checked plan.

        Raises:
            DefinitionError: ``max_steps`` is not a whole number above 0.
                Raised before anything is sent.
            InputValidationError: The catalog has no entry the caller may
                use, raised before anything is sent; or the model declined
                to plan: the message is always "No plan for this goal with
                the available skills", without the model's reason.
            OutputValidationError: The model's reply is not a valid plan:
                not the requested JSON, a step's arguments not a JSON object,
                or a plan ``SkillCatalog.check`` rejects. The message names
                the step and field, never a value.
            UnsupportedCapabilityError: The agent has no model, or the model
                does not support structured output. Raised before sending.

        Raises everything ``generate`` raises for its model call, such as
        ``LimitExceededError``, ``ModelRefusalError`` or ``ProviderError``.
        """
        return await make_plan(self, goal, skills, max_steps, instructions)

    async def run_plan(
        self,
        plan: Plan,
        skills: SkillCatalog,
        *,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        max_tokens_per_step: int | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        on_failure: OnFailure = "stop",
    ) -> PlanResult:
        """Check ``plan`` against ``skills`` again, then run it.

        The plan is checked first, as ``ctx.plan`` checks it: with
        ``SkillCatalog.check`` against only the entries the caller may use,
        so a step using a hidden entry fails like one using an unknown name.
        This happens even if the plan came from ``ctx.plan``: a
        caller-supplied plan is untrusted. Steps then run under one
        ``asyncio.TaskGroup``: a step starts when every step it depends on
        has completed, with at most ``max_parallel`` running at once. No
        model sees any result between steps.

        Just before a step is sent:

        1. It counts as one tool call toward ``max_tool_calls``.
        2. Its references are replaced by copies of earlier values:
           ``{"$ref": "step"}`` by the step's whole value and
           ``{"$ref": "step.field"}`` by one top-level field, keeping types;
           in ``ask`` text, ``{step}`` and ``{step.field}`` by the value,
           strings as they are and anything else as compact JSON with
           sorted keys.
        3. The caller must hold the entry's ``requires_permissions``.
        4. The execution policy's tool checks get the entry's name and the
           arguments about to be sent, after substitution (``{"text": ...}``
           for an ``ask`` entry): the data-exfiltration gate.

        Each step is then ``Remote.call`` or ``Remote.ask`` on the catalog's
        binding to its agent, so the run's deadline, depth, identity,
        correlation and token accounting apply as for any remote call. Each
        step holds ``max_tokens_per_step`` of the run's token budget while it
        runs, replaced by the usage its agent reports; an agent that does
        not report usage holds nothing and makes the run's usage unknown.

        By default the first failing step stops the plan: running steps are
        cancelled (their agents get a best-effort ``CancelTask``), later
        steps are never sent and its error is raised as itself, never as an
        ``ExceptionGroup``. With ``on_failure="continue"``, a step whose
        agent fails (``RemoteAgentError``) or asks a question
        (``ClarificationNeededError``) is kept as a ``"failed"``,
        ``"cancelled"`` or ``"input_required"`` step result, the steps
        depending on it are ``"skipped"`` and never sent, and the other
        steps still run. Every other error, including limits, permissions,
        tool checks, invalid references, authentication and cancellation,
        always stops the plan: they protect the whole run, not one step.
        No step is retried. Errors name steps, fields and entries, never
        values.

        Args:
            plan: The plan, from ``ctx.plan`` or built or received by the
                skill.
            skills: The catalog the plan was made for, built by this run.
            max_parallel: The most steps that may run at once.
            max_tokens_per_step: The tokens each step may use. ``None``
                shares the tokens the run has left when the plan starts
                equally among its steps. Never chosen by a model.
            max_steps: The most steps the plan may have, as for
                ``SkillCatalog.check``; never above the tool calls the run
                has left.
            on_failure: ``"stop"`` raises a remote agent's failure or
                question; ``"continue"`` keeps it as its step's result and
                returns a partial result.

        Returns:
            The ``result`` step's value and every step's result. With
            ``on_failure="continue"``, ``complete`` is ``False`` when a step
            did not complete, and ``value`` is ``None`` when the ``result``
            step did not.

        Raises:
            DefinitionError: ``skills`` was not built by this run, or
                ``max_parallel``, ``max_tokens_per_step`` or ``max_steps``
                is not a whole number above 0, or ``on_failure`` is neither
                ``"stop"`` nor ``"continue"``. Nothing is sent.
            InputValidationError: The plan is invalid for the entries the
                caller may use (see ``SkillCatalog.check``; nothing is sent),
                or a step refers to a field that the referenced step's value
                is not an object with.
            PermissionDeniedError: The caller lacks a permission a step's
                entry requires, or a tool check denied a step. That step is
                not sent.
            PolicyUnavailableError: A tool check raised or did not answer in
                time. That step is not sent.
            TokenLimitExceededError: A step's tokens do not fit what the run
                has left, or fewer tokens are left than the plan has steps.
            ToolCallLimitExceededError: The run has made ``max_tool_calls``.
            LimitExceededError: The run is at its ``max_remote_hops`` depth,
                or another limit of the run stopped a step.
            RemoteAgentError: A step's agent failed, as for ``Remote.call``
                (only with ``on_failure="stop"``).
            ClarificationNeededError: A step's agent asked a question (only
                with ``on_failure="stop"``).
            AuthenticationError: A credential could not provide its headers.
        """
        return await run_plan(
            plan,
            skills,
            self._remotes,
            generation=self._generation,
            principal=self.principal,
            deadline=self.deadline,
            max_parallel=max_parallel,
            max_tokens_per_step=max_tokens_per_step,
            max_steps=max_steps,
            on_failure=on_failure,
        )
