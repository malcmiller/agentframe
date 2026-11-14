"""Evaluation results: one ``CaseResult`` per case, collected in an ``EvalReport``."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from agentframe.runtime.usage import FinishReason, Usage


class CaseStatus(StrEnum):
    """How one case ended.

    Attributes:
        PASSED: The target ran and every scorer passed.
        FAILED: The target ran and at least one scorer failed.
        ERRORED: The target or a scorer raised, or a scorer returned
            something other than a ``bool`` or ``Score``.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"


@dataclass(frozen=True, kw_only=True)
class ScoreResult:
    """One scorer's verdict on one case.

    Attributes:
        scorer: The scorer's name.
        passed: Whether the output passed.
        value: The scorer's optional number.
        reason: The scorer's explanation; ``None`` unless the evaluation
            captures content, since it may quote the output.
    """

    scorer: str
    passed: bool
    value: float | None = None
    reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class CaseResult:
    """The outcome of one case: its status, scores and run metadata.

    Run metadata comes from the target's ``RunResult`` and is ``None`` when
    the target raised. Content fields (``arguments``, ``text``, ``expected``,
    ``output`` and score reasons) are ``None`` unless the evaluation was
    created with ``capture_content=True``.

    Attributes:
        case_id: The case's ID.
        status: Whether the case passed, failed or errored.
        scores: Each scorer's verdict, in scorer order. Scorers after one
            that raised do not run.
        error_type: The class name of the error that made the case errored.
        error_message: A safe description of that error: AgentFrame's own
            message for AgentFrame errors, otherwise which part raised. Any
            other exception's message is never kept, since it may hold
            content; its traceback is logged instead.
        invocation_id: The run's invocation ID, also kept when the run failed
            with an AgentFrame error.
        usage: Token usage; fields may be ``None`` (unknown, not zero).
        duration: How long the run took.
        model_calls: Model calls the run made.
        tool_calls: Tool calls the model asked for.
        finish_reason: Why the run finished.
        tags: The case's tags.
        arguments: The case's arguments, when captured.
        text: The case's text, when captured.
        expected: The case's expected value, when captured.
        output: The target's output, when captured.
    """

    case_id: str
    status: CaseStatus
    scores: tuple[ScoreResult, ...] = ()
    error_type: str | None = None
    error_message: str | None = None
    invocation_id: str | None = None
    usage: Usage | None = None
    duration: timedelta | None = None
    model_calls: int | None = None
    tool_calls: int | None = None
    finish_reason: FinishReason | None = None
    tags: tuple[str, ...] = ()
    arguments: Mapping[str, Any] | None = None
    text: str | None = None
    expected: Any = None
    output: Any = None


@dataclass(frozen=True, kw_only=True)
class EvalReport:
    """The results of one evaluation run.

    Attributes:
        results: One result per case, in case order.
    """

    results: tuple[CaseResult, ...]
