# Evaluation

[Reference status conventions](README.md). Planned M9 harness for running fixed
cases against an agent and scoring the results; see the
[evaluation design](../design/evaluation.md) for rationale and the rest of the
proposed API. Implemented so far: [`Case`](#case) and
[`load_cases`](#load_cases) (#152); [`Evaluation`](#evaluation),
[`LocalSkill`](#localskill), [`Score`](#score) and [`CaseResult`](#caseresult)
(#153); [`EvalReport`](#evalreport) aggregates and console summary,
[`UsageTotals`](#usagetotals) and [`Latency`](#latency) (#155). Everything else
in the design is unimplemented.

Import from `agentframe.evaluation`, not `agentframe`. The package ships in the
main wheel with no extra dependency, and `import agentframe` does not load it.

## `Case`

**Purpose / decision:** one fixed input for an evaluation, with an optional
expected result. One type serves structured skill calls and free-text targets:
a case has exactly one of `arguments` or `text`
([decision 3](../design/evaluation.md#decisions-confirmed-2026-11-13)).

**Signature status:** implemented in M9 #152 as a frozen, keyword-only generic
dataclass.

```text
Case[E](*, id: str, arguments: Mapping[str, Any] | None = None,
        text: str | None = None, expected: E | None = None,
        tags: tuple[str, ...] = ())
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `id` | `str` | Required | Stable, non-empty case name, unique within a dataset; reports and CI output use it. |
| `arguments` | `Mapping[str, Any] \| None` | Exactly one of `arguments` / `text` | A named skill's JSON arguments, keyed by string. |
| `text` | `str \| None` | Exactly one of `arguments` / `text` | A free-text message. |
| `expected` | `E \| None` | Optional; default `None` | Expected result for scorers that compare against one. |
| `tags` | `tuple[str, ...]` | Optional; default `()` | Labels for breaking results down, for example `"edge-case"`. A list is accepted and stored as a tuple. |

```python
from agentframe.evaluation import Case

cases: list[Case[int]] = [
    Case(id="short", arguments={"text": "one two three"}, expected=3),
    Case(id="empty", arguments={"text": ""}, expected=0, tags=("edge-case",)),
]
```

**Behavior / failures:** construction raises `DefinitionError` when `id` is
empty or not a string, when both or neither of `arguments` and `text` are given,
or when a field has the wrong type. Messages never repeat case content. A
single `Case` cannot check that its ID is unique;
[`load_cases`](#load_cases) checks it for files.

**Related:** [cases and datasets](../design/evaluation.md#cases-and-datasets).

## `load_cases`

**Purpose / decision:** read cases from a [JSONL](https://jsonlines.org/) file,
the dataset format Foundry and OpenAI evals read; Python and JSONL are the only
formats ([decision 4](../design/evaluation.md#decisions-confirmed-2026-11-13)).

**Signature status:** implemented in M9 #152.

```text
load_cases(path: str | os.PathLike[str]) -> list[Case[Any]]
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `path` | `str \| os.PathLike[str]` | Required | A UTF-8 JSONL file of at most 10 MiB. |

A leading UTF-8 byte order mark is ignored. Each non-blank line is one JSON
object with only the `Case` fields; `tags` is a
JSON list of strings:

```json
{"id": "short", "arguments": {"text": "one two three"}, "expected": 3}
{"id": "refund", "text": "Can I get a refund for order A1?", "tags": ["support"]}
```

```python
from agentframe.evaluation import load_cases

cases = load_cases("evals/assistant.jsonl")
```

**Behavior / failures:** returns the cases in file order; blank lines are
skipped. The file is read synchronously with `json.loads` only, never executed.
Every problem raises `DefinitionError` naming the file and, for a bad line, its
line number, without repeating the line's content:

- the file cannot be read, is larger than 10 MiB (checked before parsing), is
  not UTF-8 or has no cases;
- a line is not valid JSON (including `NaN` / `Infinity`) or not a JSON object;
- a line has an unknown or repeated field, or no `id`;
- a line repeats an earlier case ID (the message names both lines);
- a line is not a valid [`Case`](#case), for example both or neither of
  `arguments` and `text`.

**Related:** [cases and datasets](../design/evaluation.md#cases-and-datasets),
[security and privacy](../design/evaluation.md#security-and-privacy).

## `Evaluation`

**Purpose / decision:** run fixed cases against a target and score every
output with plain functions. A case passes when every scorer passes
([decisions 5-7](../design/evaluation.md#decisions-confirmed-2026-11-13)).

**Signature status:** implemented in M9 #153. Concurrency, repeats and an
evaluation-wide budget (#157) and A2A targets (#158) are proposed.

```text
Evaluation(*, cases: Iterable[Case[Any]], target: LocalSkill,
           scorers: Iterable[Scorer], capture_content: bool = False)
async Evaluation.run() -> EvalReport
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `cases` | `Iterable[Case[Any]]` | Required | At least one case; IDs must be unique. |
| `target` | [`LocalSkill`](#localskill) | Required | What runs each case. |
| `scorers` | `Iterable[Scorer]` | Required | At least one sync or async callable `(case, output) -> bool \| Score`. |
| `capture_content` | `bool` | Optional; default `False` | Keep case inputs, expected values, outputs and score reasons in results. A report with content is sensitive. |

```python
from agentframe.evaluation import Case, Evaluation, LocalSkill, Score


def is_three(case: Case[int], output: int) -> bool:
    return output == 3


async def at_least_two(case: Case[int], output: int) -> Score:
    return Score(passed=output >= 2, value=float(output))


evaluation = Evaluation(
    cases=[Case(id="short", arguments={"text": "one two three"})],
    target=LocalSkill(Assistant(), skill="count_words"),  # application agent
    scorers=[is_three, at_least_two],
)
report = await evaluation.run()
```

**Behavior / failures:** construction raises `DefinitionError`, before any case
runs, when there are no cases or no scorers, a scorer is not callable, two
cases share an ID (the message names positions, not the ID), or a case does not
suit the target (see [`LocalSkill`](#localskill)). `run` runs cases one at a
time in order and returns one [`CaseResult`](#caseresult) per case:

- `passed` when every scorer passes, `failed` when any scorer fails;
- `errored` when the target raises (for example `InputValidationError`,
  `PermissionDeniedError` or a `LimitExceededError`), a scorer raises, or a
  scorer returns anything but a `bool` or `Score`. A bare number is refused
  with `DefinitionError`, so thresholds are explicit:
  `Score(passed=value >= 0.8, value=value)`. Scorers after one that raised do
  not run. There is no `expected_error=`: any target error errors the case.

Errors never stop the run; `asyncio.CancelledError` propagates. A scorer's
name is its `__name__`, or its class name for callable objects. The harness
itself logs only one INFO line per case (case ID, status, invocation ID)
under logger `agentframe.evaluation.harness`, never case inputs or outputs.
Tracebacks of target and scorer exceptions that are not AgentFrame errors go
to the operator log with their messages (results never keep those messages),
so keep content out of exception messages, as for skills.

**Related:** [scorers](../design/evaluation.md#scorers),
[running](../design/evaluation.md#running-concurrency-and-cost).

## `LocalSkill`

**Purpose / decision:** the local target: runs each case's `arguments` through
one skill with [`Runtime.run`](runtime.md), so every case gets argument
validation, permissions, authorization and execution policies, and limits.

**Signature status:** implemented in M9 #153.

```text
LocalSkill(agent: Agent, *, skill: str, runtime: Runtime | None = None,
           principal: Principal | None = None,
           options: RunOptions | None = None)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agent` | `Agent` | Required | Instance of an `@agent`-decorated class. |
| `skill` | `str` | Required | ID of the skill to run. |
| `runtime` | `Runtime \| None` | Optional; default `None` | Runtime with the application's services and policies; `None` creates `Runtime()`. |
| `principal` | `Principal \| None` | Optional; default `None` | Verified caller identity from trusted code, used for every case. Never invented. |
| `options` | `RunOptions \| None` | Optional; default `None` | Limits and model settings for every case's run. |

```python
from agentframe import Principal, RunLimits, RunOptions
from agentframe.evaluation import LocalSkill

target = LocalSkill(
    Assistant(model=model),  # application agent and model
    skill="answer",
    principal=Principal(id="evaluator", permissions=frozenset({"orders.read"})),
    options=RunOptions(limits=RunLimits(max_total_tokens=20_000)),
)
```

**Behavior / failures:** construction raises `DefinitionError` when the agent
is not decorated or has no such skill. When an `Evaluation` is created, a case
with `text` instead of `arguments` is refused, and each case's `expected` is
validated against the skill's return type and converted to it (a JSONL object
becomes the skill's Pydantic model, so scorers compare like with like); an
invalid `expected` raises `DefinitionError` naming the case ID and fields but
not the values. Each run gets a deep copy of the case's arguments, so a skill
cannot change the dataset. A protected skill without `principal=` errors the
case with `PermissionDeniedError`.

**Related:** [targets](../design/evaluation.md#targets).

## `Score`

**Purpose / decision:** a scorer's verdict with optional detail; return a plain
`bool` when pass or fail is all there is to say.

**Signature status:** implemented in M9 #153 as a frozen, keyword-only
dataclass.

```text
Score(*, passed: bool, value: float | None = None, reason: str | None = None)
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `passed` | `bool` | Required | Whether the output passes this check. |
| `value` | `float \| None` | Optional; default `None` | A number for reports, such as a similarity of 0.83. |
| `reason` | `str \| None` | Optional; default `None` | An explanation; may quote the output, so results keep it only with `capture_content=True`. |

```python
from agentframe.evaluation import Case, Score


def similar(case: Case[str], output: str) -> Score:
    value = similarity(output, case.expected)  # application function
    return Score(passed=value >= 0.8, value=value)
```

**Behavior / failures:** raises `DefinitionError` when `passed` is not a `bool`
or `value` is not a number.

**Related:** [scorers](../design/evaluation.md#scorers).

## `CaseResult`

**Purpose / decision:** the outcome of one case. Content is off by default
([decision 13](../design/evaluation.md#decisions-confirmed-2026-11-13)).

**Signature status:** implemented in M9 #153 as a frozen dataclass; built by
the harness, not by applications. `CaseStatus` is a `StrEnum` of `passed`,
`failed` and `errored` (`not_run` arrives with #157).

| Field | Type | Meaning |
| --- | --- | --- |
| `case_id` | `str` | The case's ID. |
| `status` | `CaseStatus` | `passed`, `failed` or `errored`. |
| `scores` | `tuple[ScoreResult, ...]` | Each scorer's `scorer` name, `passed`, `value` and `reason` (`None` unless captured), in scorer order. |
| `error_type` | `str \| None` | Class name of the error that errored the case. |
| `error_message` | `str \| None` | AgentFrame's safe message, or which part raised (for example `"Scorer 'mentions_order' raised KeyError."`); other exceptions' messages are never kept. |
| `invocation_id` | `str \| None` | The run's ID, also when it failed with an AgentFrame error. |
| `usage`, `duration`, `model_calls`, `tool_calls`, `finish_reason` | from `RunResult` | `None` when the target raised; usage fields may be `None` (unknown, not zero). |
| `tags` | `tuple[str, ...]` | The case's tags. |
| `arguments`, `text`, `expected`, `output` | content | `None` unless `capture_content=True`. |

**Related:** [reports](../design/evaluation.md#reports).

## `EvalReport`

**Purpose / decision:** the results of one evaluation run, with aggregates and
a readable console summary.

**Signature status:** `results` implemented in M9 #153; aggregates and
`str(report)` in #155. JSON and `require` (#156) are proposed.

| Field or property | Type | Meaning |
| --- | --- | --- |
| `results` | `tuple[CaseResult, ...]` | One [`CaseResult`](#caseresult) per case, in case order. |
| `passed_count`, `failed_count`, `errored_count` | `int` | Cases per status; errored (target or scorer raised) is counted apart from failed (a scorer failed). |
| `pass_rate` | `float` | Passed cases / cases that ran, from `0.0` to `1.0`; errored cases count against it. `0.0` with no cases. |
| `scorer_pass_rates` | `dict[str, float]` | Each scorer's pass rate over the cases it scored, in scorer order. A scorer that did not run for a case (the target or an earlier scorer raised) does not count for it. |
| `tag_pass_rates` | `dict[str, float]` | Pass rate of the cases carrying each tag, in first-seen order. |
| `total_usage` | [`UsageTotals`](#usagetotals) | Summed tokens; unknown is never counted as zero. |
| `latency` | [`Latency`](#latency) `\| None` | p50, p95 and max case duration; `None` when no case has a duration ([decision 1 of #155](https://github.com/malcmiller/agentframe/issues/155)). |
| `errors_by_class` | `dict[str, int]` | Errored cases per error class, for example `{"TimeoutLimitExceededError": 2}`. |

All aggregates are read-only properties computed from `results`.

```python
from agentframe.evaluation import EvalReport


def check(report: EvalReport) -> None:
    print(report)
    if report.errored_count or report.pass_rate < 0.9:
        raise SystemExit(1)
```

`str(report)` (and `print(report)`) is a plain-text summary with no extra
dependency: one line each for counts and pass rate, tokens, latency, errors,
scorer and tag pass rates, then a table of case ID, status, scorer verdicts
(`name=pass` / `name=fail`, with the value in brackets, or `error=<class>`),
tokens (`~` marks estimates, `unknown` when not reported) and duration,
failing cases first (errored, then failed, then passed):

```text
Evaluation: 3 cases, 1 passed, 1 failed, 1 errored (pass rate 33.3%)
Tokens: unknown; 45 reported, 1 cases with unknown usage
Latency: p50 1.00s, p95 3.00s, max 3.00s
Errors: TimeoutLimitExceededError 1
Scorers: exact 50.0%, short 100.0%
Tags: billing 50.0%, refunds 0.0%

case    status   scores                           tokens   duration
broken  errored  error=TimeoutLimitExceededError  unknown  -
wrong   failed   exact=fail short=pass(0.5)       ~30      3.00s
ok      passed   exact=pass                       15       1.00s
```

**Behavior / failures:** the summary holds IDs, statuses, scorer names,
numbers and error classes only: no case inputs, expected values, outputs,
score reasons or error messages. With `capture_content=True`, a "Failure
reasons" section lists each failing scorer's reason; treat that output as
sensitive.

**Related:** [reports](../design/evaluation.md#reports).

## `UsageTotals`

**Purpose / decision:** token usage summed over an evaluation without ever
treating unknown usage as zero.

**Signature status:** implemented in M9 #155 as a frozen dataclass returned by
`EvalReport.total_usage`.

| Field | Type | Meaning |
| --- | --- | --- |
| `input_tokens`, `output_tokens`, `total_tokens` | `int \| None` | Sums over every case, or `None` when any case has no usage or that count unknown. |
| `known_total_tokens` | `int` | Sum of the total token counts cases did report; a lower bound when `unknown_cases` is not zero. |
| `unknown_cases` | `int` | Cases with no usage (the target raised) or any unknown count. |
| `estimated` | `bool` | Whether any reported usage is an estimate. |

**Related:** [`Usage`](runtime.md), [reports](../design/evaluation.md#reports).

## `Latency`

**Purpose / decision:** case durations at p50, p95 and max only, enough for CI
without a statistics dependency.

**Signature status:** implemented in M9 #155 as a frozen dataclass returned by
`EvalReport.latency`.

| Field | Type | Meaning |
| --- | --- | --- |
| `p50` | `timedelta` | Median duration (nearest rank). |
| `p95` | `timedelta` | 95th percentile duration (nearest rank). |
| `max` | `timedelta` | Longest duration. |

Cases whose target raised have no duration and are left out.

**Related:** [reports](../design/evaluation.md#reports).
