"""Evaluate ``examples/assistant.py``: word counts, and answers graded by a judge.

``create_evaluation`` needs no model, so CI can gate a build on it without
pytest::

    agentframe eval examples.evaluation:create_evaluation \\
        --json eval-report.json --min-pass-rate 1.0

``create_answer_evaluation`` runs the model-backed ``answer`` skill and grades
each answer with a rubric ``Judge``. Pass ``ScriptedModel`` instances in CI, or
real models (Ollama, Foundry) in opt-in tests; see docs/guides/evaluation.md.
"""

from agentframe import Model, ModelSettings, RunLimits, RunOptions
from agentframe.evaluation import Case, Evaluation, Judge, LocalSkill, contains, equals
from agentframe.testing import ScriptedModel
from examples.assistant import Assistant

WORD_COUNTS = [
    Case(id="short", arguments={"text": "one two three"}, expected=3),
    Case(id="empty", arguments={"text": ""}, expected=0),
    Case(id="spaces", arguments={"text": "  padded   words  "}, expected=2),
]

QUESTIONS = [
    Case(
        id="france",
        arguments={"question": "What is the capital of France?"},
        expected="Paris",
        tags=("capitals",),
    ),
    Case(
        id="japan",
        arguments={"question": "What is the capital of Japan?"},
        expected="Tokyo",
        tags=("capitals",),
    ),
]

# Checkable yes/no criteria; small judge models read a reference answer as
# the exact wording required unless told otherwise.
RUBRIC = "The output names the expected city as the answer. Extra words are fine."


def create_evaluation() -> Evaluation:
    """Check ``count_words`` against exact expected counts, without a model.

    The scripted model has no replies: it would fail if it were called.
    """
    return Evaluation(
        cases=WORD_COUNTS,
        target=LocalSkill(Assistant(model=ScriptedModel()), skill="count_words"),
        scorers=[equals],
    )


def create_answer_evaluation(
    model: Model, judge_model: Model, *, votes: int = 1
) -> Evaluation:
    """Check ``answer`` names each expected city and passes a rubric judge.

    Args:
        model: The assistant's model.
        judge_model: The judge's model, with structured output; preferably a
            different model from the assistant's.
        votes: The judge's majority votes per case: up to that many judge runs.
    """
    judge = Judge(
        judge_model,
        rubric=RUBRIC,
        votes=votes,
        options=RunOptions(
            # Foundry reports no context window, so cap the judge's input too.
            limits=RunLimits(max_input_tokens=1000, max_total_tokens=2_000),
            # Reasoning deployments count reasoning tokens toward the output cap.
            model_settings=ModelSettings(max_output_tokens=512),
        ),
    )
    return Evaluation(
        cases=QUESTIONS,
        # Each case holds its own ceiling against the budget below, so keep
        # it small enough for the cases to run concurrently.
        target=LocalSkill(
            Assistant(model=model),
            skill="answer",
            options=RunOptions(limits=RunLimits(max_total_tokens=5_000)),
        ),
        scorers=[contains(), judge],
        max_total_tokens=20_000,  # assistant and judge runs together
    )
