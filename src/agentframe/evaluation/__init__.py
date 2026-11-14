"""Evaluate agents against fixed cases.

Import from here, not from ``agentframe``: ``import agentframe`` does not load
this package.
"""

from agentframe.evaluation.cases import Case, load_cases
from agentframe.evaluation.harness import Evaluation
from agentframe.evaluation.judge import Judge
from agentframe.evaluation.report import (
    CaseResult,
    CaseStatus,
    EvalReport,
    Latency,
    ScoreResult,
    UsageTotals,
)
from agentframe.evaluation.scoring import Score, Scorer, contains, equals, valid
from agentframe.evaluation.targets import A2ATarget, LocalSkill, Target, TargetResult

__all__ = [
    "A2ATarget",
    "Case",
    "CaseResult",
    "CaseStatus",
    "EvalReport",
    "Evaluation",
    "Judge",
    "Latency",
    "LocalSkill",
    "Score",
    "ScoreResult",
    "Scorer",
    "Target",
    "TargetResult",
    "UsageTotals",
    "contains",
    "equals",
    "load_cases",
    "valid",
]
