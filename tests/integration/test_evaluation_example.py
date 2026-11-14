"""An evaluation as an ordinary pytest test: ``examples/assistant.py``'s word count.

``count_words`` never calls the model, so this runs in normal CI with no
provider; the scripted model has no replies and would fail if it were called.
"""

import json
from pathlib import Path

from agentframe.evaluation import Case, Evaluation, LocalSkill, equals
from agentframe.testing import ScriptedModel
from examples.assistant import Assistant

cases = [
    Case(id="short", arguments={"text": "one two three"}, expected=3, tags=("text",)),
    Case(id="empty", arguments={"text": ""}, expected=0),
    Case(id="spaces", arguments={"text": "  padded   words  "}, expected=2),
]


async def test_count_words(tmp_path: Path) -> None:
    evaluation = Evaluation(
        cases=cases,
        target=LocalSkill(Assistant(model=ScriptedModel()), skill="count_words"),
        scorers=[equals],
    )

    report = await evaluation.run()
    report.write_json(tmp_path / "eval-report.json")
    report.require(min_pass_rate=1.0)

    document = json.loads((tmp_path / "eval-report.json").read_text())
    assert document["target"] == {
        "type": "local_skill",
        "agent": "Assistant",
        "skill": "count_words",
    }
    assert document["aggregates"]["pass_rate"] == 1.0
