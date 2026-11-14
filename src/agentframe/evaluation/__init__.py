"""Evaluate agents against fixed cases.

Import from here, not from ``agentframe``: ``import agentframe`` does not load
this package.
"""

from agentframe.evaluation.cases import Case, load_cases
from agentframe.evaluation.harness import Evaluation
from agentframe.evaluation.report import CaseResult, CaseStatus, EvalReport, ScoreResult
from agentframe.evaluation.scoring import Score, Scorer
from agentframe.evaluation.targets import LocalSkill

__all__ = [
    "Case",
    "CaseResult",
    "CaseStatus",
    "EvalReport",
    "Evaluation",
    "LocalSkill",
    "Score",
    "ScoreResult",
    "Scorer",
    "load_cases",
]
