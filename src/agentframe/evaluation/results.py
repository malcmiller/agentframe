"""One evaluation attempt's result, and the types its report sums up into."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from agentframe.usage import FinishReason, Usage


class CaseStatus(StrEnum):
    """How one case ended.

    Attributes:
        PASSED: The target ran and every scorer passed.
        FAILED: The target ran and at least one scorer failed.
        ERRORED: The target or a scorer raised, or a scorer returned
            something other than a ``bool`` or ``Score``, or the
            evaluation's timeout cancelled the attempt while it ran.
        NOT_RUN: The attempt never started: the evaluation's token budget
            could not fit it, or its timeout expired first.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    NOT_RUN = "not_run"


@dataclass(frozen=True, kw_only=True)
class ScoreResult:
    """One scorer's verdict on one case.

    Attributes:
        scorer: The scorer's name.
        passed: Whether the output passed.
        value: The scorer's optional number.
        reason: The scorer's explanation; ``None`` unless the evaluation
            captures content, since it may quote the output.
        votes_cast: How many votes a voting scorer such as ``Judge`` cast;
            ``None`` unless the scorer sets them. Counts are kept without
            captured content, since they hold no content.
        votes_passed: How many of those votes passed.
    """

    scorer: str
    passed: bool
    value: float | None = None
    reason: str | None = None
    votes_cast: int | None = None
    votes_passed: int | None = None


@dataclass(frozen=True, kw_only=True)
class CaseResult:
    """The outcome of one attempt at a case: its status, scores and run metadata.

    Run metadata comes from the target's ``TargetResult`` and is ``None``
    when the target raised or, for a remote agent, cannot be known. Content
    fields (``arguments``, ``text``, ``expected``, ``output`` and score
    reasons) are ``None`` unless the evaluation was created with
    ``capture_content=True``.

    Attributes:
        case_id: The case's ID.
        attempt: Which attempt at the case this is, from 1 to the
            evaluation's ``repeats``.
        status: Whether the attempt passed, failed, errored or did not run.
        scores: Each scorer's verdict, in scorer order. Scorers after one
            that raised do not run.
        error_type: The class name of the error that made the case errored.
        error_message: A safe description of that error: AgentFrame's own
            message for AgentFrame errors, otherwise which part raised. Any
            other exception's message is never kept, since it may hold
            content; its traceback is logged instead. For a ``not_run``
            attempt, why it did not run.
        invocation_id: The run's invocation ID, also kept when the run failed
            with an AgentFrame error.
        usage: Token usage; fields may be ``None`` (unknown, not zero).
        judge_usage: The ``Judge`` runs' usage for this attempt, added up
            separately from the target's; ``None`` when no judge ran. A
            judge run that failed makes it unknown.
        duration: How long the run took.
        model_calls: Model calls the run made; ``None`` for ``A2ATarget``.
        tool_calls: Tool calls the model asked for; ``None`` for ``A2ATarget``.
        finish_reason: Why the run finished; ``None`` for ``A2ATarget``.
        tags: The case's tags.
        arguments: The case's arguments, when captured.
        text: The case's text, when captured.
        expected: The case's expected value, when captured.
        output: The target's output, when captured.
    """

    case_id: str
    attempt: int = 1
    status: CaseStatus
    scores: tuple[ScoreResult, ...] = ()
    error_type: str | None = None
    error_message: str | None = None
    invocation_id: str | None = None
    usage: Usage | None = None
    judge_usage: Usage | None = None
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
class UsageTotals:
    """Token usage summed over the attempts that ran.

    A count is only summed when every attempt that ran reported it, so
    unknown usage is never counted as zero. ``not_run`` attempts used nothing
    and are left out.

    Attributes:
        input_tokens: Total input tokens, or ``None`` when any case's count is
            unknown.
        output_tokens: Total output tokens, or ``None`` when any case's count
            is unknown.
        total_tokens: Total tokens, or ``None`` when any case's count is
            unknown.
        known_total_tokens: The sum of the total token counts cases did
            report; a lower bound when ``unknown_cases`` is not zero.
        unknown_cases: Attempts that ran with no usage (the target raised or
            was cancelled) or with any unknown count.
        estimated: Whether any reported usage is an estimate.
    """

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    known_total_tokens: int
    unknown_cases: int
    estimated: bool


@dataclass(frozen=True, kw_only=True)
class Latency:
    """Case durations, by the nearest-rank percentile.

    Attributes:
        p50: The median duration.
        p95: The 95th percentile duration.
        max: The longest duration.
    """

    p50: timedelta
    p95: timedelta
    max: timedelta
