import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentframe import DefinitionError
from agentframe.evaluation import Case, load_cases


def write(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_case_holds_skill_arguments_or_text() -> None:
    structured = Case(id="short", arguments={"text": "one two"}, expected=2)
    free_text: Case[str] = Case(
        id="refund", text="Can I get a refund?", tags=("support",)
    )

    assert structured.arguments == {"text": "one two"}
    assert structured.expected == 2
    assert free_text.text == "Can I get a refund?"
    assert free_text.tags == ("support",)


@pytest.mark.parametrize(
    "fields",
    [
        {"id": "", "text": "hi"},
        {"id": "both", "arguments": {}, "text": "hi"},
        {"id": "neither"},
        {"id": "bad-arguments", "arguments": ["a"]},
        {"id": "bad-text", "text": 3},
        {"id": "bad-tags", "text": "hi", "tags": "support"},
    ],
)
def test_invalid_case_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(DefinitionError):
        Case(**fields)


def test_load_cases_reads_each_line_in_order(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '{"id": "short", "arguments": {"text": "one two three"}, "expected": 3}',
        "",
        '{"id": "refund", "text": "Can I get a refund?", "tags": ["support"]}',
    )

    cases = load_cases(path)

    assert cases == [
        Case(id="short", arguments={"text": "one two three"}, expected=3),
        Case(id="refund", text="Can I get a refund?", tags=("support",)),
    ]


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ('{"id": "a", "text": "secret"', "is not valid JSON"),
        ('{"id": "a", "text": "secret", "score": NaN}', "is not valid JSON"),
        ('["secret"]', "is not a JSON object"),
        ('{"id": "a", "text": "secret", "secret": 1}', "has an unknown field"),
        ('{"id": "a", "text": "secret", "text": "secret"}', "has a duplicate field"),
        ('{"text": "secret"}', "has no id"),
        ('{"id": "a", "arguments": {}, "text": "secret"}', "exactly one of"),
        ('{"id": "a"}', "exactly one of"),
        ('{"id": "a", "text": "secret", "tags": [1]}', "tags must be"),
    ],
)
def test_invalid_line_names_file_and_line_without_content(
    tmp_path: Path, line: str, problem: str
) -> None:
    path = write(tmp_path, '{"id": "first", "text": "fine"}', line)

    with pytest.raises(DefinitionError) as raised:
        load_cases(path)

    message = raised.value.message
    assert str(path) in message
    assert "line 2" in message
    assert problem in message
    assert "secret" not in message


def test_duplicate_id_names_both_lines(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '{"id": "same", "text": "one"}',
        '{"id": "other", "text": "two"}',
        '{"id": "same", "text": "three"}',
    )

    with pytest.raises(DefinitionError, match="line 3 repeats the case ID of line 1"):
        load_cases(path)


def test_oversize_file_fails_before_parsing(tmp_path: Path) -> None:
    # Not valid JSON, so only the size check can explain the error.
    path = tmp_path / "big.jsonl"
    path.write_bytes(b"x" * (10 * 1024 * 1024 + 1))

    with pytest.raises(DefinitionError, match="larger than 10485760 bytes"):
        load_cases(path)


def test_a_leading_utf8_byte_order_mark_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "bom.jsonl"
    path.write_bytes('{"id": "short", "text": "hi"}\n'.encode("utf-8-sig"))

    assert [case.id for case in load_cases(path)] == ["short"]


def test_file_problems_are_definition_errors(tmp_path: Path) -> None:
    empty = write(tmp_path, "")
    not_utf8 = tmp_path / "latin1.jsonl"
    not_utf8.write_bytes('{"id": "café", "text": "x"}'.encode("latin-1"))

    with pytest.raises(DefinitionError, match="has no cases"):
        load_cases(empty)
    with pytest.raises(DefinitionError, match="is not UTF-8"):
        load_cases(not_utf8)
    with pytest.raises(DefinitionError, match="cannot be read"):
        load_cases(tmp_path / "missing.jsonl")


def test_import_agentframe_does_not_load_evaluation() -> None:
    check = (
        "import sys, agentframe; "
        "assert 'agentframe.evaluation' not in sys.modules, 'evaluation imported'"
    )
    subprocess.run([sys.executable, "-c", check], check=True)
