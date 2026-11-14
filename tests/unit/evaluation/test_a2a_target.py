"""``A2ATarget`` refuses identities and cases it cannot run, before any I/O."""

from typing import Any

import pytest
from pydantic import BaseModel

from agentframe import (
    AgentDirectory,
    DefinitionError,
    EnvironmentAgentRegistry,
    OnBehalfOf,
)
from agentframe.evaluation import A2ATarget, Case


class Summary(BaseModel):
    words: int


def directory() -> AgentDirectory:
    return AgentDirectory(sources=[EnvironmentAgentRegistry("AGENTFRAME_AGENTS")])


def test_on_behalf_of_is_refused_at_construction() -> None:
    identity: Any = OnBehalfOf(scope="api://writer/.default")

    with pytest.raises(DefinitionError, match="no signed-in user"):
        A2ATarget(directory(), "writer", identity=identity)


def test_output_needs_a_skill() -> None:
    with pytest.raises(DefinitionError, match="no skill="):
        A2ATarget(directory(), "writer", output=Summary)


def test_a_skill_target_needs_argument_cases() -> None:
    target = A2ATarget(directory(), "writer", skill="summarize")

    with pytest.raises(DefinitionError, match="needs arguments"):
        target.check_case(Case(id="text", text="hello"))


def test_a_free_text_target_needs_text_cases() -> None:
    target = A2ATarget(directory(), "writer")

    with pytest.raises(DefinitionError, match="sends free text"):
        target.check_case(Case(id="args", arguments={"text": "hello"}))


def test_expected_is_converted_to_the_output_type() -> None:
    target = A2ATarget(directory(), "writer", skill="summarize", output=Summary)

    case = target.check_case(Case(id="ok", arguments={}, expected={"words": 2}))

    assert case.expected == Summary(words=2)


@pytest.mark.parametrize(
    ("target", "case"),
    [
        (
            A2ATarget(directory(), "writer", skill="summarize", output=Summary),
            Case(id="bad", arguments={}, expected={"words": "many"}),
        ),
        (A2ATarget(directory(), "writer"), Case(id="bad", text="hi", expected=3)),
    ],
)
def test_an_invalid_expected_value_is_refused(
    target: A2ATarget, case: Case[Any]
) -> None:
    with pytest.raises(DefinitionError, match="Case 'bad' expected value"):
        target.check_case(case)
