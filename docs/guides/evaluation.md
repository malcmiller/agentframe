# Evaluating agents

This guide adds an evaluation to an agent: fixed cases, a target, scorers, an
optional rubric judge, a token budget and a report that gates CI. It walks
through [`examples/evaluation.py`](../../examples/evaluation.py), which
evaluates [`examples/assistant.py`](../../examples/assistant.py). The
[evaluation API reference](../api/evaluation.md) documents every class; the
[evaluation design](../design/evaluation.md) explains the rationale.

Import from `agentframe.evaluation`. It ships in the main wheel with no extra
and no dependency of its own; `import agentframe` does not load it. A judge on a
real model needs that provider's extra (`ollama` or `foundry`), as for any
agent.

## Cases

A [`Case`](../api/evaluation.md#case) is one fixed input with a unique `id`,
exactly one of `arguments` (a named skill's JSON arguments) or `text` (a
free-text message), and optional `expected` and `tags`:

```python
from agentframe.evaluation import Case

WORD_COUNTS = [
    Case(id="short", arguments={"text": "one two three"}, expected=3),
    Case(id="empty", arguments={"text": ""}, expected=0),
    Case(id="spaces", arguments={"text": "  padded   words  "}, expected=2),
]
```

Larger datasets live in JSONL files, one case per line, read with
[`load_cases`](../api/evaluation.md#load_cases). Loading is strict and never
executes anything: a bad line fails with `DefinitionError` naming the file and
line number, never its content.

```json
{"id": "short", "arguments": {"text": "one two three"}, "expected": 3}
{"id": "refund", "text": "Can I get a refund for order A1?", "tags": ["support"]}
```

## Targets and scorers

A target runs one case. [`LocalSkill`](../api/evaluation.md#localskill) runs a
skill through `Runtime.run`, with the same validation, permissions, policy and
limits as any local run; [`A2ATarget`](../api/evaluation.md#a2atarget) calls a
deployed A2A agent ([below](#agents-not-built-with-agentframe)).

A scorer is any sync or async function taking the case and the output and
returning `bool` or a [`Score`](../api/evaluation.md#score); a case passes when
every scorer passes. The built-ins are [`equals`](../api/evaluation.md#equals)
(output equals `expected`), [`contains`](../api/evaluation.md#contains) (text
contains a string, or `expected`) and [`valid(T)`](../api/evaluation.md#valid)
(output validates as `T`).

The example's first evaluation needs no model: `count_words` never calls one,
and the scripted model has no replies, so it would fail if it were called.

```python
from agentframe.evaluation import Evaluation, LocalSkill, equals
from agentframe.testing import ScriptedModel
from examples.assistant import Assistant


def create_evaluation() -> Evaluation:
    return Evaluation(
        cases=WORD_COUNTS,
        target=LocalSkill(Assistant(model=ScriptedModel()), skill="count_words"),
        scorers=[equals],
    )
```

A target or scorer that raises marks that case `errored` with the error class
and the run goes on; any target error fails the case.

## A rubric judge

Exact checks cannot score open-ended answers. A
[`Judge`](../api/evaluation.md#judge) asks a model whether each output meets a
written rubric and returns a binary verdict. The example's second evaluation
checks the model-backed `answer` skill with `contains()` (the answer names
`expected`) and a judge:

```python
from agentframe import Model, ModelSettings, RunLimits, RunOptions
from agentframe.evaluation import Evaluation, Judge, LocalSkill, contains

RUBRIC = "The output names the expected city as the answer. Extra words are fine."


def create_answer_evaluation(
    model: Model, judge_model: Model, *, votes: int = 1
) -> Evaluation:
    judge = Judge(
        judge_model,
        rubric=RUBRIC,
        votes=votes,
        options=RunOptions(
            limits=RunLimits(max_input_tokens=1000, max_total_tokens=2_000),
            model_settings=ModelSettings(max_output_tokens=512),
        ),
    )
    return Evaluation(
        cases=QUESTIONS,  # "What is the capital of France?", expected "Paris", ...
        target=LocalSkill(
            Assistant(model=model),
            skill="answer",
            options=RunOptions(limits=RunLimits(max_total_tokens=5_000)),
        ),
        scorers=[contains(), judge],
        max_total_tokens=20_000,  # assistant and judge runs together
    )
```

- **Write checkable yes/no rubrics.** The rubric is trusted instructions; the
  case input, output and `expected` reference are sent as data. Say what may
  differ from the reference: small judge models otherwise read it as the exact
  wording required.
- **Use a different judge model** from the target's where you can, since models
  tend to prefer their own answers. The harness does not enforce it.
- **The judge needs structured output.** A model without it errors the case
  before anything is sent. Foundry reports no context window, so give the
  judge a `max_input_tokens`, as above.
- **Judging is data egress.** Case content goes to the judge model's provider.

### Majority votes and their cost

`votes=3` asks the judge up to three times, one run at a time, and takes the
majority, stopping once it is certain: two runs when they agree, three when
they split. Every vote is a separate judge run, so `votes=3` costs up to three
times one vote, and each run holds the judge's `token_ceiling` (its resolved
`max_total_tokens`, 2 000 above) against the evaluation's budget before it
starts. Give the judge a small `options` ceiling, as above, so votes fit the
budget; the library default would hold 100 000 tokens per vote.

With `votes` above 1, the score's `value` is the fraction of votes that
passed, so a split vote shows in the summary and the JSON. A 2-1 pass from
`tests/integration/test_evaluation_release.py`:

```text
france  passed  contains=pass judge=pass(0.666667)  ...
```

### Judge tokens

Judge usage is reported apart from the target's: per attempt as `judge_usage`,
in total as `total_judge_usage`, and as a `Judge tokens:` line in the summary.
The example with scripted models that report usage (the same release test):

```text
Evaluation: 2 cases, 2 passed, 0 failed, 0 errored (pass rate 100.0%)
Tokens: 82 total
Judge tokens: 480 total
Budget: 20000 tokens
```

## Budgets and controls

Each case keeps the target's normal limits (`RunLimits`, deadlines, token
ceilings); the harness never raises a host ceiling. On top of them,
[`Evaluation`](../api/evaluation.md#evaluation) adds:

- `concurrency=4`: at most that many attempts at once, in case order;
- `repeats=1`: run each case more times to measure nondeterminism; cases whose
  attempts disagree are listed as `flaky`;
- `max_total_tokens`: an optional budget for target and judge runs together.
  Each case holds its whole per-case ceiling before starting, and each judge
  run holds its own, so keep per-case ceilings small (the example's 5 000 and
  2 000) or cases wait for each other. An attempt that does not fit waits
  while others run and is `not_run` only once nothing else is running. Usage
  can still go over the budget, for example when a remote agent reports more
  than its hold; the report flags it as over budget;
- `timeout_seconds`: an optional bound on the whole evaluation; running cases
  are cancelled and `errored`, the rest `not_run`.

Unknown usage is never counted as 0: a case whose usage is unknown keeps its
whole hold, and the report says the budget could not be fully enforced.

## Reports

`print(report)` shows counts, pass rate, target and judge tokens, latency,
errors, per-scorer and per-tag pass rates and a table of cases, failing cases
first. [`write_json`](../api/evaluation.md#evalreportwrite_json) writes the
same data as a versioned document. For the model-free evaluation above it
starts:

```json
{
  "schema": "agentframe.evaluation.report/1",
  "agentframe_version": "...",
  "target": {"type": "local_skill", "agent": "Assistant", "skill": "count_words"},
  "scorers": ["equals"],
  "capture_content": false,
  "max_total_tokens": null,
  "aggregates": {"attempt_count": 3, "passed_count": 3, "pass_rate": 1.0, "...": "..."},
  "results": ["..."]
}
```

The `schema` is bumped on breaking changes. Unknown token counts are `null`,
not 0.

## Gate CI on thresholds

### In pytest

There is no pytest plugin: an installed plugin would load in every project that
has AgentFrame and pytest. An evaluation is an ordinary async test
(pytest-asyncio in auto mode, as in this repository) that keeps the JSON
report and asserts thresholds with
[`report.require`](../api/evaluation.md#evalreportrequire):

```python
from examples.evaluation import create_evaluation


async def test_word_counts() -> None:
    report = await create_evaluation().run()
    report.write_json("eval-report.json")
    report.require(min_pass_rate=1.0)
```

`require` raises `AssertionError`, so pytest reports a missed threshold as a
test failure, with each miss listed above the summary. Write the JSON first, so
the report is kept when the test fails.

**Default strictness.** With no arguments, `require` already fails when any
attempt errored (`max_errors=0`), any attempt did not run, or reported usage
went over the evaluation's `max_total_tokens` budget. Opt out explicitly with
`max_errors=None` (or a number), `allow_not_run=True` and
`allow_over_budget=True`. `min_pass_rate=` fails when no attempt ran. A
`max_total_tokens=` threshold counts target and judge tokens together and
never passes when any of that usage is unknown.

### Without pytest: `agentframe eval`

[`agentframe eval`](../api/evaluation.md#agentframe-eval) runs a function
that takes no arguments and returns an `Evaluation`, such as the example's
`create_evaluation`, with the same thresholds:

```shell
agentframe eval examples.evaluation:create_evaluation --json eval-report.json \
  --min-pass-rate 1.0
```

It prints the summary and exits with 0 when every threshold is met, 1 when one
is missed (each miss is listed above the summary) and 2 on a configuration
error, such as a target that cannot be imported or does not return an
`Evaluation`. `--min-pass-rate`, `--max-errors` (default 0),
`--max-total-tokens`, `--allow-not-run` and `--allow-over-budget` go straight
to `require`, so the defaults are as strict as above. `--json` is written
before thresholds are checked. An evaluation created with
`capture_content=True` is refused unless you pass `--capture-content`; the
command never turns capture on itself.

### Keep the report

Upload the JSON file as a CI artifact, even when the check fails; for example,
in GitHub Actions:

```yaml
- run: uv run pytest  # or: uv run agentframe eval ... --json eval-report.json
- uses: actions/upload-artifact@v7
  if: always()
  with:
    name: eval-report
    path: eval-report.json
```

## Real models

Real-model evaluations follow the repository's opt-in convention: mark the test
`ollama` or `foundry`, skip it unless `AGENTFRAME_TEST_OLLAMA=1` or
`AGENTFRAME_TEST_FOUNDRY=1` is set, and read endpoints and model names only
from the documented environment variables. Foundry evaluations cost money, so
keep datasets, output caps and `votes` small. This repository runs the
example's answer evaluation with a judge on real models in
`tests/integration/test_providers.py`; a small local judge such as
`llama3.2:3b` occasionally fails a correct answer, so that test gates at a 50%
pass rate while still failing on any error.

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

## Privacy

By default the console summary, the JSON report and `require` messages hold
case IDs, statuses, scorer names, numbers and error classes only: never case
inputs, expected values, outputs, judge reasons or error messages. The harness
never logs content, whatever the setting; its logs carry case IDs, statuses and
invocation IDs. An evaluation created with `capture_content=True` keeps content
in the report and its JSON file for local debugging; treat that output as
sensitive and do not upload it from shared CI. Keep secrets out of datasets and
rubrics.

## Release acceptance

The M9 release acceptance
([#164](https://github.com/malcmiller/agentframe/issues/164)) maps to these
tests:

| Acceptance | Where |
| --- | --- |
| A model-free skill and a `ScriptedModel` skill evaluated in CI, with judge tokens, a split vote and a JSON report without content | `tests/integration/test_evaluation_release.py` |
| A JSON report threshold gates a build, in pytest and with `agentframe eval` | `tests/integration/test_evaluation_release.py` |
| Real Ollama and Foundry skills with a rubric judge (opt-in) | `tests/integration/test_providers.py` |
| A non-AgentFrame A2A agent over A2A | `tests/integration/test_evaluation_interop.py` (CI), `tests/integration/test_foundry_agents.py` (opt-in Foundry agent) |
| The example from the installed wheel | `examples/smoke.py`, run by CI's wheel check |
| No new runtime dependency | `agentframe.evaluation` uses only existing dependencies |
