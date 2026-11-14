"""Scorers: plain functions that check one case's output."""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agentframe.errors import DefinitionError
from agentframe.evaluation.cases import Case

type Scorer = Callable[[Case[Any], Any], bool | Score | Awaitable[bool | Score]]
"""A sync or async callable taking a case and its output, returning a verdict."""


@dataclass(frozen=True, kw_only=True)
class Score:
    """A scorer's verdict with optional detail.

    Return a plain ``bool`` when pass or fail is all there is to say. A bare
    number is refused, so a threshold is always explicit::

        def similar(case: Case[str], output: str) -> Score:
            value = similarity(output, case.expected)
            return Score(passed=value >= 0.8, value=value)

    Args:
        passed: Whether the output passes this check.
        value: An optional number for reports, such as a similarity of 0.83.
        reason: An optional explanation. It may quote the output, so reports
            keep it only with ``capture_content=True``.

    Raises:
        DefinitionError: ``passed`` is not a ``bool`` or ``value`` is not a
            number.
    """

    passed: bool
    value: float | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        passed: object = self.passed
        if not isinstance(passed, bool):
            raise DefinitionError("Score passed must be True or False.")
        value: object = self.value
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int | float)
        ):
            raise DefinitionError("Score value must be a number.")


def scorer_name(scorer: Scorer) -> str:
    """Return the name reports use for ``scorer``: its ``__name__``.

    Callable objects without one, such as configured scorer instances, are
    named after their class.
    """
    name = getattr(scorer, "__name__", None)
    return name if isinstance(name, str) else type(scorer).__name__


async def run_scorer(scorer: Scorer, case: Case[Any], output: Any) -> Score:
    """Call a sync or async scorer and return its verdict as a ``Score``.

    Raises:
        DefinitionError: The scorer returned something other than a ``bool``
            or ``Score``, such as a bare number.
        Exception: Whatever the scorer raised, unchanged.
    """
    verdict = scorer(case, output)
    if inspect.isawaitable(verdict):
        verdict = await verdict
    if isinstance(verdict, Score):
        return verdict
    if isinstance(verdict, bool):
        return Score(passed=verdict)
    raise DefinitionError(
        f"Scorer '{scorer_name(scorer)}' returned {type(verdict).__name__}, "
        "not a bool or Score. For a number, return "
        "Score(passed=value >= threshold, value=value)."
    )
