# Evaluation in pytest and CI

This guide shows how to run an evaluation as an ordinary pytest test and gate a
build on it (M9 #156). The [evaluation API reference](../api/evaluation.md)
documents every class; the [evaluation design](../design/evaluation.md) explains
the rationale. Import from `agentframe.evaluation`; it ships in the main wheel
with no extra dependency.

## An evaluation is a test

There is no pytest plugin: an installed plugin would load in every project that
has AgentFrame and pytest. Write an async test (pytest-asyncio in auto mode, as
in this repository), run the evaluation, keep the JSON report and assert
thresholds with [`report.require`](../api/evaluation.md#evalreportrequire):

```python
from agentframe.evaluation import Case, Evaluation, LocalSkill, equals
from agentframe.testing import ScriptedModel
from examples.assistant import Assistant

cases = [
    Case(id="short", arguments={"text": "one two three"}, expected=3),
    Case(id="empty", arguments={"text": ""}, expected=0),
]


async def test_count_words() -> None:
    evaluation = Evaluation(
        cases=cases,
        target=LocalSkill(Assistant(model=ScriptedModel()), skill="count_words"),
        scorers=[equals],
    )
    report = await evaluation.run()
    report.write_json("eval-report.json")
    report.require(min_pass_rate=1.0)
```

`count_words` never calls the model, so this runs in normal CI with no provider.
This repository runs the same evaluation in
`tests/integration/test_evaluation_example.py`.

## Thresholds

`require` raises `AssertionError`, so pytest reports a missed threshold as a test
failure, with each miss listed above the console summary. By default it fails
when any attempt errored, any attempt did not run, or reported usage went over
the evaluation's `max_total_tokens` budget; `max_errors=None`,
`allow_not_run=True` and `allow_over_budget=True` accept them. A
`max_total_tokens=` threshold never passes when any attempt's usage is unknown,
and `min_pass_rate=` fails when no attempt ran. Outside pytest, a script can
call `require` and let the `AssertionError` exit non-zero.

## Real models

Real-model evaluations follow the repository's opt-in convention: mark the test
`ollama` or `foundry`, skip it unless `AGENTFRAME_TEST_OLLAMA=1` or
`AGENTFRAME_TEST_FOUNDRY=1` is set, and read endpoints and model names only
from the documented environment variables. Foundry evaluations cost money, so
keep datasets and output caps small.

## Keep the report

Upload the [`write_json`](../api/evaluation.md#evalreportwrite_json) file as a
CI artifact, even when the test fails; for example, in GitHub Actions:

```yaml
- run: uv run pytest
- uses: actions/upload-artifact@v7
  if: always()
  with:
    name: eval-report
    path: eval-report.json
```

The document's `schema` is `agentframe.evaluation.report/1`, bumped on breaking
changes. By default it holds case IDs, statuses, scorer names, numbers and error
classes only, never case inputs, expected values, outputs, score reasons or
error messages; unknown token counts are `null`, not 0. An evaluation created
with `capture_content=True` writes content too, so treat that file as sensitive
and do not upload it from shared CI.
