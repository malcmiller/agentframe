"""Targets: what an evaluation runs each case against."""

import asyncio
import copy
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError

from agentframe.authoring.agent import Agent
from agentframe.authoring.definitions import SkillDefinition
from agentframe.discovery.directory import AgentDirectory
from agentframe.discovery.identity import OnBehalfOf, Service
from agentframe.discovery.remote import Remote, bind_remote
from agentframe.errors import (
    AgentFrameError,
    DefinitionError,
    TimeoutLimitExceededError,
    describe_validation_error,
)
from agentframe.evaluation.cases import Case
from agentframe.runtime.correlation import root_run
from agentframe.runtime.generation import RunAccounting
from agentframe.runtime.limits import DEFAULT_LIMITS
from agentframe.runtime.options import RunOptions, merge_settings
from agentframe.runtime.runtime import Runtime
from agentframe.security.principal import Principal
from agentframe.usage import FinishReason, Usage


@dataclass(frozen=True, kw_only=True)
class TargetResult[T]:
    """What a target returns for one case: its output plus run metadata.

    Attributes:
        value: The output the scorers check.
        invocation_id: The case's invocation ID.
        usage: Token usage; fields may be ``None`` (unknown, not zero).
        duration: Wall-clock time the case took.
        model_calls: Model calls made, or ``None`` when the target cannot
            know, as for a remote agent.
        tool_calls: Tool calls the models asked for, or ``None`` when unknown.
        finish_reason: Why the run finished, or ``None`` when unknown.
    """

    value: T
    invocation_id: str
    usage: Usage
    duration: timedelta
    model_calls: int | None = None
    tool_calls: int | None = None
    finish_reason: FinishReason | None = None


class Target(Protocol):
    """What ``Evaluation`` runs cases against, such as ``LocalSkill`` or
    ``A2ATarget``."""

    @property
    def description(self) -> dict[str, str]:
        """What the target runs, for reports: names only, never URLs, identities
        or credentials."""
        ...

    @property
    def token_ceiling(self) -> int:
        """The most tokens one case may use; an evaluation-wide budget holds
        this much for each case before it starts."""
        ...

    def check_case(self, case: Case[Any]) -> Case[Any]:
        """Check that ``case`` suits the target before any case runs.

        Returns:
            The case, possibly with ``expected`` converted to the output type.

        Raises:
            DefinitionError: The case does not suit the target.
        """
        ...

    async def run(self, case: Case[Any]) -> TargetResult[Any]:
        """Run one case and return its output and metadata."""
        ...


class LocalSkill:
    """Runs each case's ``arguments`` through one skill with ``Runtime.run``.

    Every case goes through the full runtime pipeline: argument validation,
    permissions, authorization and execution policies, and limits.

    Example::

        target = LocalSkill(Assistant(model=model), skill="answer")

    Args:
        agent: Instance of an ``@agent``-decorated class.
        skill: ID of the skill to run.
        runtime: The runtime to run it in, with its services and policies.
            ``None`` creates a default ``Runtime()``.
        principal: Verified caller identity for every case, supplied by
            trusted code. The harness never invents one, so a skill that
            requires permissions fails without it.
        options: Limits and model settings for every case's run.

    Raises:
        DefinitionError: The agent is not decorated with ``@agent`` or has no
            skill with this ID.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        skill: str,
        runtime: Runtime | None = None,
        principal: Principal | None = None,
        options: RunOptions | None = None,
    ) -> None:
        definition = type(agent).definition()
        skill_definition = definition.skills.get(skill)
        if skill_definition is None:
            raise DefinitionError(
                f"Agent '{definition.name}' has no skill '{skill}' to evaluate."
            )
        self._agent = agent
        self._agent_name = definition.name
        self._skill: SkillDefinition = skill_definition
        self._runtime = runtime or Runtime()
        self._principal = principal
        self._options = options

    @property
    def description(self) -> dict[str, str]:
        """What this target runs, for reports: its type, agent name and skill ID."""
        return {
            "type": "local_skill",
            "agent": self._agent_name,
            "skill": self._skill.id,
        }

    @property
    def token_ceiling(self) -> int:
        """The most tokens one case's run may use: its resolved ``max_total_tokens``.

        Resolved like the run's own limits: library defaults, then the agent,
        the skill and ``options``. An evaluation-wide budget holds this much
        for each case before it starts.
        """
        options = self._options or RunOptions()
        limits = merge_settings(
            DEFAULT_LIMITS, self._agent.limits, self._skill.limits, options.limits
        )
        assert limits.max_total_tokens is not None, "DEFAULT_LIMITS sets it"
        return limits.max_total_tokens

    def check_case(self, case: Case[Any]) -> Case[Any]:
        """Check that ``case`` can run against this skill, before any case runs.

        Args:
            case: The case to check.

        Returns:
            The case, with ``expected`` (when set) validated and converted to
            the skill's return type, so scorers compare like with like; for
            example, a JSONL object becomes the skill's Pydantic model.

        Raises:
            DefinitionError: The case has ``text`` instead of ``arguments``,
                or its ``expected`` is not a valid skill result.
        """
        if case.arguments is None:
            raise DefinitionError(
                f"Case '{case.id}' has text, but skill '{self._skill.id}' "
                "needs arguments."
            )
        if case.expected is None:
            return case
        try:
            expected = self._skill.return_adapter.validate_python(case.expected)
        except ValidationError as error:
            raise DefinitionError(
                f"Case '{case.id}' expected value is not a valid result of skill "
                f"'{self._skill.id}': {describe_validation_error(error)}"
            ) from None
        return Case(
            id=case.id,
            arguments=case.arguments,
            expected=expected,
            tags=case.tags,
        )

    async def run(self, case: Case[Any]) -> TargetResult[Any]:
        """Run the skill with the case's arguments.

        The skill gets a copy of the arguments, so it cannot change the
        dataset between runs.

        Returns:
            The skill's value with its ``RunResult`` metadata.

        Raises:
            Exception: Whatever ``Runtime.run`` raises, unchanged.
        """
        run = await self._runtime.run(
            self._agent,
            skill=self._skill.id,
            arguments=copy.deepcopy(dict(case.arguments or {})),
            principal=self._principal,
            options=self._options,
        )
        return TargetResult(
            value=run.value,
            invocation_id=run.invocation_id,
            usage=run.usage,
            duration=run.duration,
            model_calls=run.model_calls,
            tool_calls=run.tool_calls,
            finish_reason=run.finish_reason,
        )


class A2ATarget:
    """Runs each case against a deployed A2A agent, as a skill's ``Remote`` would.

    With ``skill`` set, each case's ``arguments`` go to that skill through
    ``Remote.call``; without it, each case's ``text`` goes through
    ``Remote.ask``. So any A2A 1.0 agent can be evaluated with text cases,
    while skill calls need an agent whose card declares AgentFrame's
    skill-call extension and the skill.

    Each case is its own short root run with the same rules as a skill's
    remote call: a deadline, a token hold sent as ``max_total_tokens``,
    the outbound identity, same-origin and no-redirect rules, best-effort
    cancellation and usage reconciliation. Its limits come from
    ``options.limits``, then the library defaults: ``timeout_seconds``
    (120 s) bounds the case, ``max_total_tokens`` (100 000) is held and sent,
    and ``max_remote_hops`` (5) caps the depth from 0.
    ``options.model_settings`` does not apply, since the target calls no
    model itself.

    Example::

        async with AgentDirectory(sources=[FileAgentRegistry("agents.yaml")]) as d:
            target = A2ATarget(
                d,
                "assistant",
                skill="summarize",
                output=Summary,
                identity=Service(credential=APIKeyCredential(api_key)),
            )

    Args:
        directory: Where the agent is found. The application opens and
            closes it; a case run while it is closed errors.
        name: The agent's name in the directory.
        skill: The skill ID to call with each case's ``arguments``, or
            ``None`` to send each case's ``text``.
        output: The type to validate each skill result into, such as a
            Pydantic model; needs ``skill``. ``None`` keeps the decoded JSON.
        identity: How to authenticate, as in ``remotes=``. ``None`` sends no
            credentials, which only works with a card that declares no
            security.
        options: Per-case limits; see above.

    Raises:
        DefinitionError: ``identity`` is ``OnBehalfOf``, since an evaluation
            has no signed-in user, or ``output`` is set without ``skill``.
    """

    def __init__(
        self,
        directory: AgentDirectory,
        name: str,
        *,
        skill: str | None = None,
        output: type[Any] | None = None,
        identity: Service | None = None,
        options: RunOptions | None = None,
    ) -> None:
        checked_identity: object = identity
        if isinstance(checked_identity, OnBehalfOf):
            raise DefinitionError(
                f"A2ATarget '{name}' cannot call OnBehalfOf a user: an "
                "evaluation has no signed-in user. Use Service instead."
            )
        if output is not None and skill is None:
            raise DefinitionError(
                f"A2ATarget '{name}' has output= but no skill=; free-text "
                "answers are always text."
            )
        self._directory = directory
        self._name = name
        self._skill = skill
        self._output = output
        self._identity = identity
        self._limits = merge_settings(
            DEFAULT_LIMITS, options.limits if options else None
        )

    @property
    def description(self) -> dict[str, str]:
        """What this target calls, for reports: its type, the agent's
        configured name, the skill ID (``""`` for free text) and the mode,
        ``"call"`` or ``"ask"``. Never the URL, identity or credentials."""
        return {
            "type": "a2a",
            "agent": self._name,
            "skill": self._skill or "",
            "mode": "ask" if self._skill is None else "call",
        }

    @property
    def token_ceiling(self) -> int:
        """The most tokens one case may use: its resolved ``max_total_tokens``.

        This is what each case holds and sends to the agent as its ceiling.
        An agent that does not advertise ``max_total_tokens`` is not bound by
        it and reports unknown usage, so an evaluation-wide budget keeps the
        whole ceiling charged for its cases.
        """
        assert self._limits.max_total_tokens is not None  # DEFAULT_LIMITS sets it.
        return self._limits.max_total_tokens

    def check_case(self, case: Case[Any]) -> Case[Any]:
        """Check that ``case`` has the input this target sends, before any case runs.

        Args:
            case: The case to check.

        Returns:
            The case, with ``expected`` (when set) validated and converted to
            ``output``, or checked to be text for a free-text target.

        Raises:
            DefinitionError: A skill target's case has ``text`` instead of
                ``arguments``, a free-text target's case has ``arguments``,
                or its ``expected`` is not a valid output.
        """
        if self._skill is not None and case.arguments is None:
            raise DefinitionError(
                f"Case '{case.id}' has text, but A2ATarget '{self._name}' calls "
                f"skill '{self._skill}', which needs arguments."
            )
        if self._skill is None and case.text is None:
            raise DefinitionError(
                f"Case '{case.id}' has arguments, but A2ATarget '{self._name}' "
                "sends free text; set skill= to call a skill."
            )
        output = self._output if self._skill is not None else str
        if case.expected is None or output is None:
            return case
        try:
            expected = TypeAdapter(output).validate_python(case.expected)
        except ValidationError as error:
            raise DefinitionError(
                f"Case '{case.id}' expected value is not a valid output of "
                f"A2ATarget '{self._name}': {describe_validation_error(error)}"
            ) from None
        return Case(
            id=case.id,
            arguments=case.arguments,
            text=case.text,
            expected=expected,
            tags=case.tags,
        )

    async def run(self, case: Case[Any]) -> TargetResult[Any]:
        """Send the case to the agent and wait for its answer.

        Returns:
            The answer (validated as ``output`` when set) with the usage the
            agent reported, unknown when its card does not advertise usage,
            and the case's wall-clock duration. Model calls, tool calls and
            the finish reason are ``None``: only the agent knows them.

        Raises:
            TimeoutLimitExceededError: The case's deadline passed; the remote
                task is cancelled, best effort.
            AgentFrameError: As for ``Remote.call`` and ``Remote.ask``,
                including ``RemoteAgentError`` and
                ``ClarificationNeededError``, carrying the case's invocation
                ID.
        """
        invocation_id = str(uuid.uuid4())
        started = time.perf_counter()
        accounting = RunAccounting()
        timeout_seconds = self._limits.timeout_seconds
        assert timeout_seconds is not None  # DEFAULT_LIMITS sets every field.
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            async with asyncio.timeout_at(deadline) as timeout:
                remote = await self._bind(invocation_id, deadline, accounting)
                value = await self._send(remote, case)
        except TimeoutError:
            if not timeout.expired():
                raise
            raise TimeoutLimitExceededError(
                f"A2ATarget '{self._name}' exceeded its timeout of "
                f"{timeout_seconds} seconds",
                invocation_id=invocation_id,
            ) from None
        except AgentFrameError as error:
            error.invocation_id = invocation_id
            raise
        return TargetResult(
            value=value,
            invocation_id=invocation_id,
            usage=accounting.usage,
            duration=timedelta(seconds=time.perf_counter() - started),
        )

    async def _bind(
        self, invocation_id: str, deadline: float, accounting: RunAccounting
    ) -> Remote[Any]:
        max_hops = self._limits.max_remote_hops
        assert max_hops is not None  # DEFAULT_LIMITS sets it.
        return await bind_remote(
            self._directory,
            self._name,
            deadline=deadline,
            correlation=root_run("evaluation", invocation_id),
            principal=None,
            identity=self._identity,
            credential=self._identity.credential if self._identity else None,
            hops=0,
            max_hops=max_hops,
            accounting=accounting,
            max_total_tokens=self.token_ceiling,
        )

    async def _send(self, remote: Remote[Any], case: Case[Any]) -> Any:
        max_total_tokens = self.token_ceiling
        if self._skill is None:
            assert case.text is not None  # Checked by check_case.
            return await remote.ask(case.text, max_total_tokens=max_total_tokens)
        # call_with takes the arguments as one dictionary, so a case may have
        # arguments named "output" or "max_total_tokens".
        return await remote.call_with(
            self._skill, dict(case.arguments or {}), max_total_tokens, self._output
        )
