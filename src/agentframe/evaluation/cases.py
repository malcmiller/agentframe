"""Evaluation cases, written in Python or loaded from a JSONL file."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentframe.errors import DefinitionError

_MAX_FILE_BYTES = 10 * 1024 * 1024
_CASE_FIELDS = frozenset({"id", "arguments", "text", "expected", "tags"})


@dataclass(frozen=True, kw_only=True)
class Case[E]:
    """One fixed input for an evaluation, with an optional expected result.

    A case has exactly one input: ``arguments`` for a named skill, or ``text``
    for a free-text message. Error messages never repeat a case's content.

    Example::

        Case(id="short", arguments={"text": "one two three"}, expected=3)
        Case(id="refund", text="Can I get a refund for order A1?", tags=("support",))

    Args:
        id: A stable, non-empty name for the case, unique within its dataset.
            Reports and CI output name cases by it.
        arguments: The JSON arguments for a named skill.
        text: A free-text message.
        expected: The expected result, for scorers that compare against one.
        tags: Labels to break results down by, for example ``"edge-case"``.

    Raises:
        DefinitionError: ``id`` is empty, both or neither of ``arguments`` and
            ``text`` are given, or a field has the wrong type.
    """

    id: str
    arguments: Mapping[str, Any] | None = None
    text: str | None = None
    expected: E | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        case_id: object = self.id
        if not isinstance(case_id, str) or not case_id.strip():
            raise DefinitionError("A case ID must be a non-empty string.")
        if (self.arguments is None) == (self.text is None):
            raise DefinitionError("A case must have exactly one of arguments or text.")
        arguments: object = self.arguments
        if arguments is not None and not _is_json_object(arguments):
            raise DefinitionError(
                "Case arguments must be a mapping of names to values."
            )
        text: object = self.text
        if text is not None and not isinstance(text, str):
            raise DefinitionError("Case text must be a string.")
        # Accept any sequence of strings (a JSON list, for example) but store a
        # tuple so the frozen case stays immutable.
        tags: object = self.tags
        if isinstance(tags, str) or not isinstance(tags, list | tuple):
            raise DefinitionError("Case tags must be a list of strings.")
        if not all(isinstance(tag, str) for tag in tags):
            raise DefinitionError("Case tags must be a list of strings.")
        object.__setattr__(self, "tags", tuple(tags))


def load_cases(path: str | os.PathLike[str]) -> list[Case[Any]]:
    """Read evaluation cases from a JSONL file.

    Each non-blank line is one JSON object with the ``Case`` fields, for
    example::

        {"id": "short", "arguments": {"text": "one two three"}, "expected": 3}
        {
            "id": "refund",
            "text": "Can I get a refund for order A1?",
            "tags": ["support"],
        }

    Loading is strict: the file is read with ``json.loads`` only, never
    executed, and must be UTF-8 (a leading byte order mark is ignored) and at
    most 10 MiB. Error messages name the
    file and line but never repeat its content.

    Args:
        path: The JSONL file.

    Returns:
        The cases, in file order.

    Raises:
        DefinitionError: The file cannot be read, is larger than 10 MiB, is
            not UTF-8, has no cases, or a line is not valid JSON, is not an
            object, has an unknown or duplicate field, repeats an earlier case
            ID or is not a valid ``Case``.
    """
    source = f"Evaluation cases file '{Path(path)}'"
    text = _read_text(Path(path), source)
    cases: list[Case[Any]] = []
    first_line_by_id: dict[str, int] = {}
    # Split on newlines only: JSON strings may contain other line separators.
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        where = f"{source} line {number}"
        case = _parse_case(line, where)
        if case.id in first_line_by_id:
            raise DefinitionError(
                f"{where} repeats the case ID of line {first_line_by_id[case.id]}."
            )
        first_line_by_id[case.id] = number
        cases.append(case)
    if not cases:
        raise DefinitionError(f"{source} has no cases.")
    return cases


def _read_text(path: Path, source: str) -> str:
    try:
        with path.open("rb") as file:
            data = file.read(_MAX_FILE_BYTES + 1)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise DefinitionError(f"{source} cannot be read: {reason}.") from None
    if len(data) > _MAX_FILE_BYTES:
        raise DefinitionError(f"{source} is larger than {_MAX_FILE_BYTES} bytes.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DefinitionError(f"{source} is not UTF-8 text.") from None


def _parse_case(line: str, where: str) -> Case[Any]:
    try:
        entry = json.loads(
            line,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except _DuplicateFieldError:
        raise DefinitionError(f"{where} has a duplicate field.") from None
    except (ValueError, RecursionError):
        # The parser's message can quote the line, so it is not passed on.
        raise DefinitionError(f"{where} is not valid JSON.") from None
    if not isinstance(entry, dict):
        raise DefinitionError(f"{where} is not a JSON object.")
    if not entry.keys() <= _CASE_FIELDS:
        allowed = ", ".join(sorted(_CASE_FIELDS))
        raise DefinitionError(f"{where} has an unknown field; allowed: {allowed}.")
    if "id" not in entry:
        raise DefinitionError(f"{where} has no id.")
    try:
        return Case(**entry)
    except DefinitionError as error:
        raise DefinitionError(f"{where}: {error.message}") from None


class _DuplicateFieldError(ValueError):
    """A JSON object names the same field twice; ``json.loads`` keeps the last."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    entry = dict(pairs)
    if len(entry) != len(pairs):
        raise _DuplicateFieldError
    return entry


def _reject_constant(name: str) -> Any:
    # NaN and Infinity are not JSON, though ``json.loads`` accepts them.
    raise ValueError(f"{name} is not valid JSON")


def _is_json_object(value: object) -> bool:
    return isinstance(value, Mapping) and all(isinstance(key, str) for key in value)
