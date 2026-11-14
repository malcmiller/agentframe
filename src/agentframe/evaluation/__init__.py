"""Evaluate agents against fixed cases.

Import from here, not from ``agentframe``: ``import agentframe`` does not load
this package.
"""

from agentframe.evaluation.cases import Case, load_cases
from agentframe.evaluation.harness import Evaluation
from agentframe.evaluation.report import (
    CaseResult,
    CaseStatus,
    EvalReport,
    Latency,
    ScoreResult,
    UsageTotals,
)
from agentframe.evaluation.scoring import Score, Scorer, contains, equals, valid
from agentframe.evaluation.targets import LocalSkill

__all__ = [
    "Case",
    "CaseResult",
    "CaseStatus",
    "EvalReport",
    "Evaluation",
    "Latency",
    "LocalSkill",
    "Score",
    "ScoreResult",
    "Scorer",
    "UsageTotals",
    "contains",
    "equals",
    "load_cases",
    "valid",
]
