"""Run cases against a target and score each output."""

import logging
from collections.abc import Iterable
from typing import Any

from agentframe.errors import AgentFrameError, DefinitionError
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


class Evaluation:
    """Runs fixed cases against a target and scores every output.

    Cases run one at a time, in order. A case passes when every scorer
    passes. A failing target or scorer marks only that case ``errored``; the
    run goes on. ``asyncio.CancelledError`` still propagates.

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
            is sensitive. Logs never contain content either way.

    Raises:
        DefinitionError: There are no cases or no scorers, two cases share an
            ID, a scorer is not callable, or a case does not suit the target
            (see ``LocalSkill.check_case``).
    """

    def __init__(
        self,
        *,
        cases: Iterable[Case[Any]],
        target: LocalSkill,
        scorers: Iterable[Scorer],
        capture_content: bool = False,
    ) -> None:
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

    async def run(self) -> EvalReport:
        """Run every case once and score it.

        Returns:
            A report with one result per case, in case order.
        """
        results = []
        for case in self._cases:
            result = await self._run_case(case)
            logger.info(
                "Evaluation case %s %s (invocation_id=%s)",
                result.case_id,
                result.status,
                result.invocation_id,
            )
            results.append(result)
        return EvalReport(results=tuple(results))

    async def _run_case(self, case: Case[Any]) -> CaseResult:
        try:
            run = await self._target.run(case)
        except Exception as error:
            return self._errored(case, error, "The target", (), run=None)
        scores: list[ScoreResult] = []
        for scorer in self._scorers:
            name = scorer_name(scorer)
            try:
                score = await run_scorer(scorer, case, run.value)
            except Exception as error:
                return self._errored(
                    case, error, f"Scorer '{name}'", tuple(scores), run
                )
            reason = score.reason if self._capture_content else None
            scores.append(
                ScoreResult(
                    scorer=name, passed=score.passed, value=score.value, reason=reason
                )
            )
        passed = all(score.passed for score in scores)
        status = CaseStatus.PASSED if passed else CaseStatus.FAILED
        return self._result(case, status, tuple(scores), run)

    def _errored(
        self,
        case: Case[Any],
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


def _check_unique_ids(cases: list[Case[Any]]) -> None:
    first_position_by_id: dict[str, int] = {}
    for position, case in enumerate(cases, start=1):
        if case.id in first_position_by_id:
            raise DefinitionError(
                f"Case {position} repeats the ID of case "
                f"{first_position_by_id[case.id]}; case IDs must be unique."
            )
        first_position_by_id[case.id] = position
