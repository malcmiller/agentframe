"""Evaluations that the ``agentframe eval`` tests point the command at."""

from agentframe import Agent, RunContext, agent, skill
from agentframe.evaluation import Case, Evaluation, LocalSkill, equals


@agent(version="0.1.0")
class WordCounter(Agent):
    """Counts words without a model."""

    @skill(id="count_words", tags=["text"])
    async def count_words(self, text: str, ctx: RunContext) -> int:
        """Count the words in some text."""
        return len(text.split())


def _evaluation(expected_words: int, *, capture_content: bool = False) -> Evaluation:
    return Evaluation(
        cases=[
            Case(id="three", arguments={"text": "one two three"}, expected=3),
            Case(id="secret", arguments={"text": "classified words"}, expected=2),
            Case(id="other", arguments={"text": "a b c d"}, expected=expected_words),
        ],
        target=LocalSkill(WordCounter(), skill="count_words"),
        scorers=[equals],
        capture_content=capture_content,
    )


def create_passing_evaluation() -> Evaluation:
    return _evaluation(expected_words=4)


def create_failing_evaluation() -> Evaluation:
    """Two of three cases pass."""
    return _evaluation(expected_words=5)


def create_capturing_evaluation() -> Evaluation:
    return _evaluation(expected_words=4, capture_content=True)


def create_invalid_evaluation() -> Evaluation:
    return Evaluation(
        cases=[],
        target=LocalSkill(WordCounter(), skill="count_words"),
        scorers=[equals],
    )


def create_nothing() -> None:
    return None
