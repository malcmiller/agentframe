"""Run cases against a target and score each output."""

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from agentframe.errors import (
    AgentFrameError,
    DefinitionError,
    TimeoutLimitExceededError,
)
from agentframe.evaluation.budget import TokenBudget
from agentframe.evaluation.cases import Case
from agentframe.evaluation.report import (
    CaseResult,
    CaseStatus,
    EvalReport,
    ScoreResult,
)
from agentframe.evaluation.scoring import Scorer, run_scorer, scorer_name
from agentframe.evaluation.targets import LocalSkill
from agentframe.runtime.runtime import RunResult

logger = logging.getLogger(__name__)


_BUDGET_STOP = "The evaluation's token budget could not fit this attempt."


class Evaluation:
    """Runs fixed cases against a target and scores every output.

    Up to ``concurrency`` attempts run at once, started in case order. A case
    passes when every scorer passes. A failing target or scorer marks only
    that attempt ``errored``; the run goes on. ``asyncio.CancelledError``
    from outside still propagates.

    Example::

        def mentions_order(case: Case[str], output: str) -> bool:
            arguments = case.arguments or {}
            return str(arguments.get("order_id")) in output


        evaluation = Evaluation(
            cases=cases,
            target=LocalSkill(Assistant(model=model), skill="answer"),
            scorers=[mentions_order],
        )
        report = await evaluation.run()

    Every case is checked against the target here, before any case runs, so
    dataset mistakes fail fast.

    Args:
        cases: The cases, each with a unique ID.
        target: What runs each case.
        scorers: Sync or async callables ``(case, output) -> bool | Score``.
        capture_content: Whether results keep case inputs, expected values,
            outputs and score reasons. Off by default; a report with content
            is sensitive. The harness itself logs only case IDs, statuses
            and invocation IDs. Tracebacks of target and scorer exceptions
            that are not AgentFrame errors go to the operator log with their
            messages, so keep content out of exception messages, as for
            skills.
        concurrency: The most attempts that run at once. ``1`` runs them in
            order.
        repeats: How many times each case runs, to show nondeterminism.
            Pass rates count every attempt; a case whose attempts end
            differently is listed in ``EvalReport.flaky_cases``.
        max_total_tokens: An optional token budget for the whole evaluation.
            Before an attempt starts it holds its full per-run ceiling
            (``target.token_ceiling``), so the holds never add up to more
            than the budget; when the rest of the budget cannot fit an
            attempt, it waits for running ones, and once nothing is running
            it is ``not_run``. A finished attempt's reported usage replaces
            its hold; unknown usage (including a failed run) keeps the whole
            hold charged. Per-run limits are never raised.
        timeout_seconds: An optional wall-clock limit for the whole
            evaluation. When it expires, running attempts are cancelled and
            marked ``errored`` with ``TimeoutLimitExceededError``, and
            attempts not yet started are ``not_run``.

    Raises:
        DefinitionError: There are no cases or no scorers, two cases share an
            ID, a scorer is not callable, a case does not suit the target
            (see ``LocalSkill.check_case``), ``concurrency`` or ``repeats`` is
            not a positive integer, or ``max_total_tokens`` or
            ``timeout_seconds`` is set but not greater than 0.
    """

    def __init__(
        self,
        *,
        cases: Iterable[Case[Any]],
        target: LocalSkill,
        scorers: Iterable[Scorer],
        capture_content: bool = False,
        concurrency: int = 4,
        repeats: int = 1,
        max_total_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        _check_settings(concurrency, repeats, max_total_tokens, timeout_seconds)
        cases = list(cases)
        scorers = list(scorers)
        if not cases:
            raise DefinitionError("An evaluation needs at least one case.")
        if not scorers:
            raise DefinitionError("An evaluation needs at least one scorer.")
        if not all(callable(scorer) for scorer in scorers):
            raise DefinitionError("Every scorer must be callable.")
        _check_unique_ids(cases)
        self._cases = [target.check_case(case) for case in cases]
        self._target = target
        self._scorers = scorers
        self._capture_content = capture_content
        self._concurrency = concurrency
        self._repeats = repeats
        self._max_total_tokens = max_total_tokens
        self._timeout_seconds = timeout_seconds

    async def run(self) -> EvalReport:
        """Run every case ``repeats`` times and score each attempt.

        Returns:
            A report with one result per attempt, in case order, then
            attempt order, whatever order they finished in.
        """
        attempts = [
            (case, attempt)
            for case in self._cases
            for attempt in range(1, self._repeats + 1)
        ]
        results: list[CaseResult | None] = [None] * len(attempts)
        started: set[int] = set()
        budget = TokenBudget(self._max_total_tokens)
        ceiling = self._target.token_ceiling
        # Workers share one iterator, so each attempt is taken exactly once and
        # attempts start in case order.
        queue = iter(enumerate(attempts))

        async def worker() -> None:
            for index, (case, attempt) in queue:
                if await budget.hold(ceiling):
                    started.add(index)
                    result = await self._run_case(case, attempt)
                    await budget.settle(ceiling, _total_tokens(result))
                else:
                    result = self._not_run(case, attempt, _BUDGET_STOP)
                _log(result)
                results[index] = result

        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with asyncio.TaskGroup() as group:
                    for _ in range(min(self._concurrency, len(attempts))):
                        group.create_task(worker())
        except TimeoutError:
            # Only our own timeout ends up here: asyncio.timeout turns its
            # cancellation into TimeoutError, while an outside cancellation
            # stays asyncio.CancelledError and propagates.
            for index, (case, attempt) in enumerate(attempts):
                if results[index] is None:
                    result = self._stopped_by_timeout(
                        case, attempt, running=index in started
                    )
                    _log(result)
                    results[index] = result
        finished = [result for result in results if result is not None]
        if len(finished) < len(results):
            # A worker raised CancelledError itself, which TaskGroup treats as
            # a cancelled task rather than an error; never drop its attempts.
            raise asyncio.CancelledError
        return EvalReport(
            results=tuple(finished),
            max_total_tokens=self._max_total_tokens,
        )

    async def _run_case(self, case: Case[Any], attempt: int) -> CaseResult:
        try:
            run = await self._target.run(case)
        except Exception as error:
            return self._errored(case, attempt, error, "The target", (), run=None)
        scores: list[ScoreResult] = []
        for scorer in self._scorers:
            name = scorer_name(scorer)
            try:
                score = await run_scorer(scorer, case, run.value)
            except Exception as error:
                return self._errored(
                    case, attempt, error, f"Scorer '{name}'", tuple(scores), run
                )
            reason = score.reason if self._capture_content else None
            scores.append(
                ScoreResult(
                    scorer=name, passed=score.passed, value=score.value, reason=reason
                )
            )
        passed = all(score.passed for score in scores)
        status = CaseStatus.PASSED if passed else CaseStatus.FAILED
        return self._result(case, attempt, status, tuple(scores), run)

    def _stopped_by_timeout(
        self, case: Case[Any], attempt: int, *, running: bool
    ) -> CaseResult:
        if not running:
            return self._not_run(
                case, attempt, "The evaluation's timeout expired before this attempt."
            )
        return self._result(
            case,
            attempt,
            CaseStatus.ERRORED,
            (),
            None,
            error_type=TimeoutLimitExceededError.__name__,
            error_message="The evaluation's timeout expired while this attempt ran.",
        )

    def _not_run(self, case: Case[Any], attempt: int, reason: str) -> CaseResult:
        return self._result(
            case, attempt, CaseStatus.NOT_RUN, (), None, error_message=reason
        )

    def _errored(
        self,
        case: Case[Any],
        attempt: int,
        error: Exception,
        source: str,
        scores: tuple[ScoreResult, ...],
        run: RunResult[Any] | None,
    ) -> CaseResult:
        # A failed run has no RunResult, but its AgentFrame error carries the ID.
        invocation_id = run.invocation_id if run else None
        if isinstance(error, AgentFrameError):
            message = error.message
            invocation_id = invocation_id or error.invocation_id
        else:
            # Other exceptions may carry case content in their message, so
            # only the operator log keeps it, with the traceback.
            message = f"{source} raised {type(error).__name__}."
            logger.exception(
                "Evaluation case %s errored: %s (invocation_id=%s)",
                case.id,
                message,
                invocation_id,
                exc_info=error,
            )
        return self._result(
            case,
            attempt,
            CaseStatus.ERRORED,
            scores,
            run,
            error_type=type(error).__name__,
            error_message=message,
            invocation_id=invocation_id,
        )

    def _result(
        self,
        case: Case[Any],
        attempt: int,
        status: CaseStatus,
        scores: tuple[ScoreResult, ...],
        run: RunResult[Any] | None,
        *,
        error_type: str | None = None,
        error_message: str | None = None,
        invocation_id: str | None = None,
    ) -> CaseResult:
        capture = self._capture_content
        return CaseResult(
            case_id=case.id,
            attempt=attempt,
            status=status,
            scores=scores,
            error_type=error_type,
            error_message=error_message,
            invocation_id=invocation_id or (run.invocation_id if run else None),
            usage=run.usage if run else None,
            duration=run.duration if run else None,
            model_calls=run.model_calls if run else None,
            tool_calls=run.tool_calls if run else None,
            finish_reason=run.finish_reason if run else None,
            tags=case.tags,
            arguments=case.arguments if capture else None,
            text=case.text if capture else None,
            expected=case.expected if capture else None,
            output=run.value if capture and run else None,
        )


def _total_tokens(result: CaseResult) -> int | None:
    return result.usage.total_tokens if result.usage else None


def _log(result: CaseResult) -> None:
    logger.info(
        "Evaluation case %s attempt %d %s (invocation_id=%s)",
        result.case_id,
        result.attempt,
        result.status,
        result.invocation_id,
    )


def _check_settings(
    concurrency: int,
    repeats: int,
    max_total_tokens: int | None,
    timeout_seconds: float | None,
) -> None:
    for name, count in (("concurrency", concurrency), ("repeats", repeats)):
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise DefinitionError(f"Evaluation {name} must be an integer of 1 or more.")
    if max_total_tokens is not None and not max_total_tokens > 0:
        raise DefinitionError("Evaluation max_total_tokens must be greater than 0.")
    if timeout_seconds is not None and not timeout_seconds > 0:
        raise DefinitionError("Evaluation timeout_seconds must be greater than 0.")


def _check_unique_ids(cases: list[Case[Any]]) -> None:
    first_position_by_id: dict[str, int] = {}
    for position, case in enumerate(cases, start=1):
        if case.id in first_position_by_id:
            raise DefinitionError(
                f"Case {position} repeats the ID of case "
                f"{first_position_by_id[case.id]}; case IDs must be unique."
            )
        first_position_by_id[case.id] = position
