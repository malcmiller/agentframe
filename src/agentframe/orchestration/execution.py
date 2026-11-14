"""Running a checked plan: the executor behind ``RunContext.run_plan``.

Steps run under one ``asyncio.TaskGroup``. A step waits for the steps it
depends on, then for one of ``max_parallel`` slots, then is checked and sent
through the run's binding to its agent. No model sees any result between
steps: references are filled in by copying values. By default the first
failure cancels the running steps (their bindings send a best-effort
``CancelTask``) and is raised as itself. With ``on_failure="continue"`` a
remote agent's failure or question is kept as that step's result instead,
and the steps depending on it are skipped.
"""

import asyncio
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from agentframe.discovery.remote import Remote, RemoteReply
from agentframe.errors import (
    ClarificationNeededError,
    DefinitionError,
    PermissionDeniedError,
    RemoteAgentError,
    TokenLimitExceededError,
)
from agentframe.orchestration.catalog import RemoteSkill, SkillCatalog
from agentframe.orchestration.plan import (
    Plan,
    PlanStep,
    dependencies,
    substituted_arguments,
    substituted_text,
)
from agentframe.runtime.generation import GenerationState
from agentframe.runtime.remotes import RunRemotes
from agentframe.runtime.tools import is_permitted
from agentframe.runtime.usage import Usage
from agentframe.security.execution_policy import enforce_tool_checks
from agentframe.security.principal import Principal

DEFAULT_MAX_PARALLEL = 4
"""Steps that may run at once unless the author passes another ``max_parallel``."""

type OnFailure = Literal["stop", "continue"]
"""What a remote agent's failure does to the rest of a plan."""

type StepStatus = Literal[
    "completed", "failed", "skipped", "cancelled", "input_required"
]
"""How one step of a plan ended."""


@dataclass(frozen=True)
class StepResult:
    """What one step of a plan returned.

    Under the default ``on_failure="stop"`` every step of a returned plan
    completed; the other statuses come only with ``"continue"``.

    Attributes:
        status: ``"completed"``; ``"failed"`` or ``"cancelled"`` when the
            remote agent failed or cancelled the step's task;
            ``"input_required"`` when it asked a question instead of
            answering; ``"skipped"`` when a step it depends on did not
            complete, so it was never sent.
        value: The step's result, or ``None`` unless it completed: the
            remote skill's decoded JSON result (``str`` for a text result),
            or the ``ask`` answer's text. Untrusted content from another
            agent.
        usage: The usage the agent reported for the step, or ``None`` when
            it is unknown, for example because the agent does not report it
            or the step did not complete.
        remote_task_id: The remote agent's task ID, which ties the step to
            that agent's logs, or ``None`` when there was no task.
        error_type: The class name of the error that ended a ``"failed"``,
            ``"cancelled"`` or ``"input_required"`` step, else ``None``.
        error_message: That error's safe message, else ``None``. Never the
            remote agent's own status text.
        question: An ``"input_required"`` step's question, else ``None``.
            Untrusted content from another agent; nothing answers it.
    """

    status: StepStatus
    value: Any
    usage: Usage | None
    remote_task_id: str | None
    error_type: str | None = None
    error_message: str | None = None
    question: str | None = None


@dataclass(frozen=True)
class PlanResult:
    """What a plan returned: its ``result`` step's value and every step's result.

    Attributes:
        value: The value of the plan's ``result`` step, or ``None`` when it
            did not complete.
        steps: Every step's result, by step ID, in the order the steps were
            checked to run.
        complete: Whether every step completed. Always ``True`` under the
            default ``on_failure="stop"``.
    """

    value: Any
    steps: Mapping[str, StepResult]
    complete: bool


async def run_plan(
    plan: Plan,
    catalog: SkillCatalog,
    run: RunRemotes | None,
    *,
    generation: GenerationState,
    principal: Principal | None,
    deadline: float,
    max_parallel: int,
    max_tokens_per_step: int | None,
    max_steps: int,
    on_failure: OnFailure,
) -> PlanResult:
    """Check ``plan`` again and run it; see ``RunContext.run_plan``."""
    if on_failure not in ("stop", "continue"):
        raise DefinitionError('on_failure= must be "stop" or "continue"')
    _check_whole_number("max_parallel", max_parallel)
    if max_tokens_per_step is not None:
        _check_whole_number("max_tokens_per_step", max_tokens_per_step)
    if run is None or not any(own is catalog for own in run.catalogs):
        raise DefinitionError(
            "run_plan needs a catalog this run built with ctx.remote_skills(...)"
        )
    # Checked again even if the plan came from ctx.plan: a caller may have
    # supplied it, and the run may have made tool calls since. As in
    # ctx.plan, a hidden entry looks exactly like a missing one.
    # check() lowers max_steps to the tool calls left (_step_limit).
    # noinspection PyProtectedMember
    groups = catalog._visible_to(principal).check(plan, max_steps=max_steps)
    steps = [step for group in groups for step in group]
    tokens = max_tokens_per_step or _equal_share(generation, len(steps))
    execution = _Execution(
        catalog, generation, principal, deadline, max_parallel, tokens, on_failure
    )
    results = await execution.run(steps)
    complete = all(result.status == "completed" for result in results.values())
    return PlanResult(results[plan.result].value, MappingProxyType(results), complete)


def _check_whole_number(name: str, value: int) -> None:
    # bool is an int subclass, but True is not a count.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DefinitionError(f"{name}= must be a whole number greater than 0")


def _equal_share(generation: GenerationState, step_count: int) -> int:
    """Return the run's remaining tokens divided equally among the steps.

    Raises:
        TokenLimitExceededError: Fewer tokens are left than there are steps.
    """
    max_total = generation.limits.max_total_tokens
    assert max_total is not None  # Set by defaults.
    remaining = generation.accounting.remaining_tokens(max_total)
    if remaining < step_count:
        raise TokenLimitExceededError(
            f"Only {remaining} of the run's {max_total} tokens remain, too few "
            f"to share among the plan's {step_count} steps"
        )
    return remaining // step_count


class _Execution:
    """One run of one plan: its slots, its finished steps and first failure.

    Args:
        catalog: The checked plan's catalog, holding the run's bindings.
        generation: The run's state: its limits, accounting, execution
            policy and running skill.
        principal: The run's verified caller, or ``None``.
        deadline: Event-loop time when the run times out; tool checks must
            answer by then.
        max_parallel: The most steps that may run at once.
        tokens_per_step: The tokens each step holds while it runs.
        on_failure: ``"continue"`` keeps a remote agent's failure or
            question as its step's result; ``"stop"`` raises it.
    """

    def __init__(
        self,
        catalog: SkillCatalog,
        generation: GenerationState,
        principal: Principal | None,
        deadline: float,
        max_parallel: int,
        tokens_per_step: int,
        on_failure: OnFailure,
    ) -> None:
        self._catalog = catalog
        self._generation = generation
        self._principal = principal
        self._deadline = deadline
        self._tokens_per_step = tokens_per_step
        self._on_failure = on_failure
        self._slots = asyncio.Semaphore(max_parallel)
        self._finished: dict[str, asyncio.Event] = {}
        self._results: dict[str, StepResult] = {}
        self._failure: Exception | None = None

    async def run(self, steps: Sequence[PlanStep]) -> dict[str, StepResult]:
        """Run ``steps``, given in a dependency order, and return their results.

        Raises:
            Exception: The first step's failure, as itself, never as an
                ``ExceptionGroup``. The other steps were cancelled.
        """
        self._finished = {step.id: asyncio.Event() for step in steps}
        failure: Exception | None = None
        try:
            async with asyncio.TaskGroup() as group:
                for step in steps:
                    group.create_task(self._run_step(step))
        except BaseExceptionGroup:
            failure = self._failure
            assert failure is not None  # Every step failure is recorded.
        # Raised outside the except block, so the group is not its context.
        if failure is not None:
            raise failure
        return {step.id: self._results[step.id] for step in steps}

    async def _run_step(self, step: PlanStep) -> None:
        try:
            for name in dependencies(step):
                await self._finished[name].wait()
            if self._depends_on_unfinished_step(step):
                # Never sent, counted or checked: there is nothing to send.
                self._results[step.id] = StepResult("skipped", None, None, None)
            else:
                async with self._slots:
                    self._results[step.id] = await self._send(step)
            self._finished[step.id].set()
        except Exception as error:
            # The first failure cancels the rest, whose own errors follow it.
            if self._failure is None:
                self._failure = error
            raise

    def _depends_on_unfinished_step(self, step: PlanStep) -> bool:
        return any(
            self._results[name].status != "completed" for name in dependencies(step)
        )

    async def _send(self, step: PlanStep) -> StepResult:
        """Count, resolve, authorize and check one step, then send it."""
        entry = self._catalog.get(step.use)
        assert entry is not None  # The plan was checked against the catalog.
        generation = self._generation
        generation.accounting.count_tool_call(generation.limits)
        values = {step_id: result.value for step_id, result in self._results.items()}
        # noinspection PyProtectedMember
        binding: Remote[Any] | None = self._catalog._binding(entry)
        assert binding is not None
        sending: Coroutine[Any, Any, RemoteReply]
        if entry.skill is None:
            text = substituted_text(step, values)
            await self._check(entry, {"text": text})
            # noinspection PyProtectedMember
            sending = binding._ask_reply(text, self._tokens_per_step)
        else:
            arguments = substituted_arguments(step, values)
            await self._check(entry, arguments)
            # noinspection PyProtectedMember
            sending = binding._call_reply(entry.skill, arguments, self._tokens_per_step)
        # Only the remote agent's own answer may end one step without
        # stopping the plan. Limits, authorization, policy and cancellation
        # protect the whole run and are always raised.
        try:
            reply = await sending
        except (RemoteAgentError, ClarificationNeededError) as error:
            if self._on_failure == "stop":
                raise
            return _unfinished(error)
        return StepResult("completed", reply.value, reply.usage, reply.task_id)

    async def _check(self, entry: RemoteSkill, arguments: dict[str, Any]) -> None:
        """Check the caller may use ``entry`` and the policy allows sending
        ``arguments`` to it, exactly as they will be sent.

        Raises:
            PermissionDeniedError: The caller lacks a permission the entry
                requires, or a tool check denied the step.
            PolicyUnavailableError: A tool check raised or did not answer
                before the run's deadline.
        """
        if not is_permitted(entry, self._principal):
            raise PermissionDeniedError(
                f"Remote skill '{entry.name}' requires permissions the caller "
                "does not hold"
            )
        policy, skill = self._generation.policy, self._generation.skill
        if policy is None or not policy.tool_checks:
            return
        # Plans run only inside a skill; routing never runs one.
        assert skill is not None
        await enforce_tool_checks(
            policy.tool_checks,
            skill,
            self._principal,
            entry.name,
            MappingProxyType(arguments),
            self._deadline,
        )


def _unfinished(error: RemoteAgentError | ClarificationNeededError) -> StepResult:
    """Keep a remote agent's failure or question as its step's result.

    Only the error's class name and safe message are kept, plus the
    question the agent asked.
    """
    error_type, message = type(error).__name__, error.message
    if isinstance(error, ClarificationNeededError):
        return StepResult(
            "input_required",
            None,
            None,
            error.task_id,
            error_type,
            message,
            question=error.question,
        )
    status: StepStatus = (
        "cancelled" if error.task_state == "TASK_STATE_CANCELED" else "failed"
    )
    return StepResult(status, None, None, error.task_id, error_type, message)
