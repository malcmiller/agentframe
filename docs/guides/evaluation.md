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

## Agents not built with AgentFrame

[`A2ATarget`](../api/evaluation.md#a2atarget) evaluates any A2A 1.0 agent, so
one harness scores every agent you depend on, including other frameworks' agents
and Foundry agents (M9 #159). Such an agent's card does not declare
AgentFrame's skill-call extension, so:

- Use free-text cases (`text=`) and leave `skill=` unset. A `skill=` target
  against such an agent errors each case with `RemoteAgentError` before
  anything is sent, exactly as `Remote.call` does; there is no fallback to
  free text.
- Its token usage is unknown, never 0: the report's token cells say `unknown`
  and the JSON has `null`. With an evaluation-wide `max_total_tokens`, each
  case keeps its whole per-case hold, so fewer cases fit, and the summary's
  budget line ends with `not fully enforced (N attempts with unknown usage)`
  (`budget_fully_enforced` is `false` in the JSON). A `require(
  max_total_tokens=...)` threshold never passes.
- A question back (`TASK_STATE_INPUT_REQUIRED`) errors the case with
  `ClarificationNeededError`. The evaluation never answers back; write cases
  the agent can answer in one turn.

A Foundry agent is found through
[`FoundryAgentRegistry`](../api/discovery.md#foundryagentregistry) and needs an
app token for `https://ai.azure.com/.default`; its card declares that security,
so a case without a matching identity errors with `DefinitionError`:

```python
from azure.identity.aio import AzureCliCredential

from agentframe import (
    AgentDirectory,
    EntraAppCredential,
    FoundryAgentRegistry,
    Service,
)
from agentframe.evaluation import A2ATarget, Case, Evaluation, contains

async with AzureCliCredential() as credential:
    source = FoundryAgentRegistry(
        project_endpoint=project_endpoint,  # application setting
        credential=credential,
    )
    token = EntraAppCredential(credential, scope="https://ai.azure.com/.default")
    async with AgentDirectory(sources=[source]) as directory:
        report = await Evaluation(
            cases=[Case(id="ready", text="Reply with: ready")],
            target=A2ATarget(
                directory, "my-foundry-agent", identity=Service(credential=token)
            ),
            scorers=[contains("ready")],
        ).run()
```

This repository evaluates a plain official-SDK agent in CI
(`tests/integration/test_evaluation_interop.py`) and a real Foundry agent in an
opt-in `foundry` test (`tests/integration/test_foundry_agents.py`, enabled with
`AGENTFRAME_TEST_FOUNDRY_AGENTS=1`, `AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT` and
`AGENTFRAME_FOUNDRY_A2A_AGENT` after `az login`). Foundry agents cost money per
case; keep such datasets to a few short prompts.

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
