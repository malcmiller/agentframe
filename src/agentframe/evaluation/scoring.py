"""Scorers: plain functions that check one case's output, and the built-ins."""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import PydanticUserError, TypeAdapter, ValidationError

from agentframe.errors import DefinitionError, describe_validation_error
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


def equals(case: Case[Any], output: Any) -> bool:
    """Pass when the output equals the case's ``expected`` value.

    The target converts ``expected`` to the skill's return type before any
    case runs, so Pydantic models compare field by field.

    Raises:
        DefinitionError: The case has no ``expected`` value.
    """
    return bool(output == _expected(case, "equals"))


def contains(text: str | None = None, *, case_sensitive: bool = False) -> Scorer:
    """Return a scorer that passes when the text output contains ``text``.

    Example::

        scorers = [contains("refund"), contains()]

    Args:
        text: The text to look for. ``None`` uses each case's ``expected``.
        case_sensitive: Whether case must match. Off by default.

    Returns:
        A scorer named ``contains`` in reports.

    Raises:
        DefinitionError: ``text`` is not a string. The scorer itself raises
            it, erroring the case, when the output is not a string or, without
            ``text``, the case has no string ``expected`` value.
    """
    if text is not None and not isinstance(text, str):
        raise DefinitionError("contains() text must be a string.")

    def check(case: Case[Any], output: Any) -> bool:
        wanted = _expected(case, "contains") if text is None else text
        if not isinstance(wanted, str):
            raise DefinitionError(
                f"Scorer 'contains' needs a string expected value in case "
                f"'{case.id}', not {type(wanted).__name__}."
            )
        if not isinstance(output, str):
            raise DefinitionError(
                f"Scorer 'contains' needs text output, not {type(output).__name__}."
            )
        if case_sensitive:
            return wanted in output
        return wanted.casefold() in output.casefold()

    check.__name__ = "contains"
    return check


def valid(output_type: Any) -> Scorer:
    """Return a scorer that passes when the output validates as ``output_type``.

    Useful for A2A results and free-text targets; local typed skills are
    already validated. A string output that is not valid as is, such as
    JSON text for a Pydantic model, is also tried as JSON.

    Example::

        scorers = [valid(Summary)]

    Args:
        output_type: Any type Pydantic can validate, such as a model or
            ``list[int]``.

    Returns:
        A scorer named ``valid`` in reports. A failing ``Score`` gives the
        problem locations as its reason, not the invalid values.

    Raises:
        DefinitionError: Pydantic cannot validate ``output_type``.
    """
    try:
        adapter: TypeAdapter[Any] = TypeAdapter(output_type)
    except PydanticUserError:
        raise DefinitionError(
            f"valid() cannot validate {output_type!r} with Pydantic."
        ) from None

    def check(case: Case[Any], output: Any) -> Score:
        try:
            adapter.validate_python(output)
        except ValidationError as error:
            if not isinstance(output, str):
                return Score(passed=False, reason=describe_validation_error(error))
            try:
                adapter.validate_json(output)
            except ValidationError as json_error:
                reason = describe_validation_error(json_error)
                return Score(passed=False, reason=reason)
        return Score(passed=True)

    check.__name__ = "valid"
    return check


def _expected(case: Case[Any], scorer: str) -> Any:
    if case.expected is None:
        raise DefinitionError(
            f"Scorer '{scorer}' needs case '{case.id}' to have an expected value."
        )
    return case.expected
