# Evaluation

[Reference status conventions](README.md). The M9 harness for running fixed
cases against an agent and scoring the results, complete in 0.3.0
(#152-#163); see the [evaluation design](../design/evaluation.md) for
rationale, the [evaluation guide](../guides/evaluation.md) for a walkthrough
and `examples/evaluation.py` for a runnable example. Only the later Foundry
evaluation export in the design is unimplemented.

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

**Signature status:** implemented in M9 #153; concurrency, repeats, the
evaluation-wide budget and timeout in #157
([decisions 8, 9 and 12](../design/evaluation.md#decisions-confirmed-2026-11-13)).
`target` accepts any [`Target`](#target) since #158; [`Judge`](#judge) runs
hold their own budget since #160.

```text
Evaluation(*, cases: Iterable[Case[Any]], target: Target,
           scorers: Iterable[Scorer], capture_content: bool = False,
           concurrency: int = 4, repeats: int = 1,
           max_total_tokens: int | None = None,
           timeout_seconds: float | None = None)
async Evaluation.run() -> EvalReport
Evaluation.capture_content -> bool  # read-only property (#162)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `cases` | `Iterable[Case[Any]]` | Required | At least one case; IDs must be unique. |
| `target` | [`Target`](#target) | Required | What runs each case: [`LocalSkill`](#localskill), [`A2ATarget`](#a2atarget) or an application target. |
| `scorers` | `Iterable[`[`Scorer`](#scorer)`]` | Required | At least one sync or async callable `(case, output) -> bool \| Score`, or a [`Judge`](#judge). |
| `capture_content` | `bool` | Optional; default `False` | Keep case inputs, expected values, outputs and score reasons in results. A report with content is sensitive. |
| `concurrency` | `int` | Optional; default `4` | Most attempts running at once, at least 1. `1` runs them in order. |
| `repeats` | `int` | Optional; default `1` | Attempts per case, at least 1. Pass rates count every attempt; cases whose attempts end differently are `flaky`. |
| `max_total_tokens` | `int \| None` | Optional; default `None` (no budget) | Token budget for the whole evaluation, greater than 0. Each attempt holds its full per-run ceiling (the target's `token_ceiling`, see [`Target`](#target)) before it starts. |
| `timeout_seconds` | `float \| None` | Optional; default `None` (no limit) | Wall-clock limit for the whole evaluation, greater than 0. |

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
cases share an ID (the message names positions, not the ID), a case does not
suit the target (its `check_case`; see [`LocalSkill`](#localskill) and
[`A2ATarget`](#a2atarget)), `concurrency` or `repeats`
is not an integer of 1 or more, or `max_total_tokens` or `timeout_seconds` is
not greater than 0. The read-only `capture_content` property returns the
constructor's value, so [`agentframe eval`](#agentframe-eval) can refuse a
capturing evaluation before it runs. `run` runs up to `concurrency` attempts at once, started in
case order, and returns one [`CaseResult`](#caseresult) per attempt, in case
order, then attempt order, however they finish:

- `passed` when every scorer passes, `failed` when any scorer fails;
- `errored` when the target raises (for example `InputValidationError`,
  `PermissionDeniedError` or a `LimitExceededError`), a scorer raises, or a
  scorer returns anything but a `bool` or `Score`. A bare number is refused
  with `DefinitionError`, so thresholds are explicit:
  `Score(passed=value >= 0.8, value=value)`. Scorers after one that raised do
  not run. There is no `expected_error=`: any target error errors the case.
- `not_run` when the attempt never started (below); `error_message` says why.

**Budget and timeout.** Per-case limits are never raised. With
`max_total_tokens`, an attempt holds its per-run token ceiling before it
starts, so the holds of running attempts never add up to more than the budget.
An attempt that does not fit waits while others run (they may use less than
they hold); once nothing is running and it still does not fit, it and later
attempts that do not fit are `not_run`. A finished attempt's reported usage
replaces its hold, even when higher, so an overrun is reported
(`EvalReport.over_budget`) rather than prevented. Unknown usage, including any
attempt whose target raised, keeps its whole hold charged and is counted in
`total_usage.unknown_cases`. Holds are full ceilings: with the default 100 000
tokens per run, a 200 000-token budget runs only two attempts at a time, so set
a smaller per-case limit with `LocalSkill(options=RunOptions(limits=RunLimits(
max_total_tokens=...)))` to use the configured concurrency. When
`timeout_seconds` expires, running attempts are cancelled and marked `errored`
with `TimeoutLimitExceededError`, and attempts not yet started are `not_run`.
An attempt's hold ends when its target finishes, before scoring; each
[`Judge`](#judge) run then holds its own ceiling the same way (waiting while
others run) and, when it cannot fit, errors the attempt with
`TokenLimitExceededError` instead of running unheld.

```python
from agentframe import RunLimits, RunOptions
from agentframe.evaluation import Evaluation, LocalSkill

evaluation = Evaluation(
    cases=cases,  # application cases
    target=LocalSkill(
        Assistant(model=model),  # application agent and model
        skill="answer",
        options=RunOptions(limits=RunLimits(max_total_tokens=5_000)),
    ),
    scorers=[mentions_order],  # application scorer
    concurrency=4,
    repeats=3,
    max_total_tokens=200_000,
    timeout_seconds=600,
)
report = await evaluation.run()
print(report.pass_rate, report.flaky_cases, report.not_run_count)
```

Errors never stop the run; an outside `asyncio.CancelledError` cancels the
running attempts and propagates. A scorer's
name is its `__name__`, or its class name for callable objects. The harness
itself logs only one INFO line per attempt (case ID, attempt, status, invocation ID)
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

**Signature status:** implemented in M9 #153; `token_ceiling` in #157;
`description` in #156.

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

The read-only property `token_ceiling: int` is the most tokens one case's run
may use: its `max_total_tokens` resolved like the run's limits (library default
100 000, then the agent, the skill and `options`). An evaluation-wide budget
holds this much per attempt.

The read-only property `description: dict[str, str]` names the target in
reports: `{"type": "local_skill", "agent": <agent name>, "skill": <skill ID>}`.

**Related:** [targets](../design/evaluation.md#targets).

## `A2ATarget`

**Purpose / decision:** the remote target: runs each case against a deployed
A2A agent found in an [`AgentDirectory`](discovery.md#agentdirectory), through
the same binding a skill's [`Remote`](discovery.md#remoteliteralname) uses, so the
evaluation covers what is actually running, including its routing, auth and
model. Service identity only, no in-process hosted target
([decision 16](../design/evaluation.md#decisions-confirmed-2026-11-13)).

**Signature status:** implemented in M9 #158.

```text
A2ATarget(directory: AgentDirectory, name: str, *, skill: str | None = None,
          output: type[Any] | None = None, identity: Service | None = None,
          options: RunOptions | None = None)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `directory` | `AgentDirectory` | Required | Where the agent is found. The application opens and closes it; cases run while it is closed error with `DefinitionError`. |
| `name` | `str` | Required | The agent's name in the directory. |
| `skill` | `str \| None` | Optional; default `None` | Skill ID: each case's `arguments` go through `Remote.call(skill, ...)`. `None` sends each case's `text` through `Remote.ask`. |
| `output` | `type[Any] \| None` | Optional; default `None` | Type each skill result is validated into, such as a Pydantic model. Needs `skill`. `None` keeps decoded JSON. |
| `identity` | `Service \| None` | Optional; default `None` | Outbound credentials, as in `remotes=`. `None` sends none, which only works when the card declares no security. `OnBehalfOf` is refused. |
| `options` | `RunOptions \| None` | Optional; default `None` | Per-case limits from `options.limits`, then library defaults: `timeout_seconds` (120 s), `max_total_tokens` (100 000) and `max_remote_hops` (5). `options.model_settings` does not apply. |

```python
from agentframe import (
    AgentDirectory,
    APIKeyCredential,
    FileAgentRegistry,
    RunLimits,
    RunOptions,
    Service,
)
from agentframe.evaluation import A2ATarget, Evaluation, valid

async with AgentDirectory(sources=[FileAgentRegistry("agents.yaml")]) as directory:
    target = A2ATarget(
        directory,
        "assistant",
        skill="summarize",
        output=Summary,  # application model
        identity=Service(credential=APIKeyCredential(api_key)),  # application key
        options=RunOptions(limits=RunLimits(timeout_seconds=60)),
    )
    report = await Evaluation(
        cases=cases, target=target, scorers=[valid(Summary)]
    ).run()
```

**Behavior / failures:** construction raises `DefinitionError` for an
`OnBehalfOf` identity (an evaluation has no signed-in user) or `output=`
without `skill=`; it does no I/O. When an `Evaluation` is created, a skill
target refuses `text` cases and a free-text target refuses `arguments` cases;
each `expected` is validated against `output` (or `str` for free text) and
converted to it, as for `LocalSkill`. The read-only property
`token_ceiling: int` is the resolved per-case `max_total_tokens`; an
evaluation-wide budget holds it per attempt. A peer that does not advertise
usage reports it as unknown, so its whole hold stays charged. The read-only
property `description: dict[str, str]` names it in reports as
`{"type": "a2a", "agent": <name>, "skill": <skill ID or "">, "mode": "call"
or "ask"}`, never its URL, identity or credentials (#156).

Each case is its own short root run with a new invocation ID: the binding
holds `max_total_tokens` and sends it as the case's ceiling to peers that
advertise it, sends the remaining deadline and depth 0, applies the outbound
identity, same-origin and no-redirect rules, and reconciles the usage the peer
reports. A peer that does not advertise usage, such as a non-AgentFrame agent,
reports unknown usage, never zero. At the deadline the case errors with
`TimeoutLimitExceededError` and the remote task is cancelled, best effort.
`RemoteAgentError` (including an oversized reply), `ClarificationNeededError`,
`OutputValidationError` and card or identity problems (`DefinitionError`)
error only that case, keeping the error class and AgentFrame's safe message,
never remote content. The evaluation never answers a question back. See
[agents not built with AgentFrame](../guides/evaluation.md#agents-not-built-with-agentframe)
for other frameworks' and Foundry agents (#159).

**Related:** [targets](../design/evaluation.md#targets),
[discovery API](discovery.md).

## `Target`

**Purpose / decision:** what `Evaluation` runs cases against; a small
structural protocol, so `LocalSkill`, `A2ATarget` and application targets need
no base class.

**Signature status:** implemented in M9 #158 as a `typing.Protocol`;
`description` in #156.

```text
Target.description -> dict[str, str]  # read-only property
Target.token_ceiling -> int  # read-only property
Target.check_case(case: Case[Any]) -> Case[Any]
async Target.run(case: Case[Any]) -> TargetResult[Any]
```

**Behavior / failures:** `description` names the target in reports
(`EvalReport.target` and the JSON `target`), with names only, never URLs,
identities or credentials. `token_ceiling` is the most tokens one case may use;
an evaluation-wide `max_total_tokens` holds it per attempt. `Evaluation` calls
`check_case` for every case before any case runs; it returns the case (possibly with `expected` converted) or
raises `DefinitionError`. `run` returns a [`TargetResult`](#targetresult); an
exception errors only that case.

**Related:** [targets](../design/evaluation.md#targets).

## `TargetResult`

**Purpose / decision:** a target's output for one case plus run metadata,
neutral between local and remote targets; unknown values are `None`, never 0.

**Signature status:** implemented in M9 #158 as a frozen, keyword-only generic
dataclass.

| Field | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| `value` | `T` | Required | The output scorers check. |
| `invocation_id` | `str` | Required | The case's invocation ID. |
| `usage` | `Usage` | Required | Token usage; fields may be `None` (unknown). |
| `duration` | `timedelta` | Required | Wall-clock time of the case. |
| `model_calls`, `tool_calls` | `int \| None` | Optional; default `None` | From `RunResult` for `LocalSkill`; `None` for `A2ATarget`. |
| `finish_reason` | `FinishReason \| None` | Optional; default `None` | From `RunResult` for `LocalSkill`; `None` for `A2ATarget`. |

**Related:** [`CaseResult`](#caseresult).

## `Score`

**Purpose / decision:** a scorer's verdict with optional detail; return a plain
`bool` when pass or fail is all there is to say.

**Signature status:** implemented in M9 #153 as a frozen, keyword-only
dataclass.

```text
Score(*, passed: bool, value: float | None = None, reason: str | None = None,
      votes_cast: int | None = None, votes_passed: int | None = None)
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `passed` | `bool` | Required | Whether the output passes this check. |
| `value` | `float \| None` | Optional; default `None` | A number for reports, such as a similarity of 0.83. |
| `reason` | `str \| None` | Optional; default `None` | An explanation; may quote the output, so results keep it only with `capture_content=True`. |
| `votes_cast` | `int \| None` | Optional; default `None` | For a scorer that votes, such as [`Judge`](#judge), how many votes it actually cast: fewer than it may ask for when it stopped early. Set together with `votes_passed`; at least 1 (#213). |
| `votes_passed` | `int \| None` | Optional; default `None` | How many of the votes cast passed, from 0 to `votes_cast` (#213). |

```python
from agentframe.evaluation import Case, Score


def similar(case: Case[str], output: str) -> Score:
    value = similarity(output, case.expected)  # application function
    return Score(passed=value >= 0.8, value=value)
```

**Behavior / failures:** raises `DefinitionError` when `passed` is not a `bool`,
`value` is not a number, or the vote counts are not set together as whole
numbers with at least one vote cast and no more passing than cast.

**Related:** [scorers](../design/evaluation.md#scorers).

## `Scorer`

**Purpose / decision:** the shape of a scorer: any plain function or callable
object that checks one case's output, so scoring needs no base class
([decision 5](../design/evaluation.md#decisions-confirmed-2026-11-13)).

**Signature status:** implemented in M9 #153 as a type alias, exported from
`agentframe.evaluation`.

```text
type Scorer = Callable[[Case[Any], Any], bool | Score | Awaitable[bool | Score]]
```

| Parameter / return | Type | Meaning and constraints |
| --- | --- | --- |
| `case` | [`Case[Any]`](#case) | The case being scored, including `expected` when set. |
| `output` | `Any` | The target's output for the case. |
| Returns | `bool \| Score`, or an awaitable of either | `True` / `False`, or a [`Score`](#score) with detail. Sync and async scorers both work. |

```python
from agentframe.evaluation import Case, Scorer


def mentions(word: str) -> Scorer:
    def mentions_word(case: Case[str], output: str) -> bool:
        return word.casefold() in output.casefold()

    return mentions_word


async def not_empty(case: Case[str], output: str) -> bool:
    return bool(output.strip())


scorers: list[Scorer] = [mentions("refund"), not_empty]
```

**Behavior / failures:** reports name a scorer by its `__name__`, or by its
class name for a callable object without one. A scorer that returns anything
else, such as a bare number, or raises errors that case
([`CaseStatus`](#casestatus) `errored`) and later scorers for it do not run;
other cases still run. Only AgentFrame errors keep their message; for any
other exception the result says which scorer raised.

**Related:** [`Score`](#score), [`equals`](#equals), [`contains`](#contains),
[`valid`](#valid), [`Judge`](#judge), [scorers](../design/evaluation.md#scorers).

## `equals`

**Purpose / decision:** built-in scorer that passes when the output equals the
case's `expected` value
([decision 1 of #154](https://github.com/malcmiller/agentframe/issues/154)).

**Signature status:** implemented in M9 #154 as a plain scorer function, named
`equals` in reports.

```text
equals(case: Case[Any], output: Any) -> bool
```

```python
from agentframe.evaluation import Evaluation, LocalSkill, equals

evaluation = Evaluation(
    cases=cases,  # application-supplied
    target=LocalSkill(Writer(), skill="summarize"),
    scorers=[equals],
)
```

**Behavior / failures:** `LocalSkill` converts each `expected` to the skill's
return type before any case runs, so Pydantic models compare field by field
and an `expected` that does not fit fails the `Evaluation` constructor with
`DefinitionError`. A case without `expected` is `errored` with
`"Scorer 'equals' needs case '<id>' to have an expected value."`.

**Related:** [scorers](../design/evaluation.md#scorers).

## `contains`

**Purpose / decision:** built-in scorer factory for text output containing a
given text, or each case's `expected` text.

**Signature status:** implemented in M9 #154; the returned scorer is named
`contains` in reports.

```text
contains(text: str | None = None, *, case_sensitive: bool = False) -> Scorer
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `text` | `str \| None` | Optional; default `None` | The text to look for; `None` uses each case's `expected`, which must then be a string. |
| `case_sensitive` | `bool` | Optional; default `False` | Whether case must match; otherwise both sides are compared with `str.casefold()`. |

```python
from agentframe.evaluation import contains

scorers = [contains("refund"), contains()]
```

**Behavior / failures:** a `text` that is not a string raises `DefinitionError`
at once. Output that is not a string, a missing `expected` (without `text`) or
an `expected` that is not a string errors the case with a `DefinitionError`
naming the case ID and type, never the content.

**Related:** [scorers](../design/evaluation.md#scorers).

## `valid`

**Purpose / decision:** built-in scorer factory that checks the output
validates as a type with Pydantic; useful for A2A results and free-text
targets, since local typed skills are already validated. It does not use
`expected`.

**Signature status:** implemented in M9 #154; the returned scorer is named
`valid` in reports and returns a `Score`.

```text
valid(output_type: Any) -> Scorer
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `output_type` | any type Pydantic validates | Required | For example a Pydantic model or `list[int]`. |

```python
from agentframe.evaluation import valid

scorers = [valid(Summary)]  # Summary: an application Pydantic model
```

**Behavior / failures:** a type Pydantic cannot validate raises
`DefinitionError` at once. Output is validated as Python data first; string
output that fails is also tried as JSON text. A failing `Score` has the problem
locations as its `reason` (never the invalid values), kept only with
`capture_content=True`.

**Related:** [scorers](../design/evaluation.md#scorers).

## `Judge`

**Purpose / decision:** a scorer that asks a model whether each output meets a
written rubric, for open-ended answers exact checks cannot score. The verdict
is binary `passed` plus `reason`, not a numeric scale, and `ModelSettings`
gets no temperature or seed: use a clear rubric, `expected` references and
majority `votes`
([decisions 10, 11 and 13](../design/evaluation.md#decisions-confirmed-2026-11-13)).

**Signature status:** implemented in M9 #160 as a class whose instances are
scorers named `judge` in reports; `votes=` added in #161.

```text
Judge(
    model: Model,
    *,
    rubric: str,
    votes: int = 1,
    options: RunOptions | None = None,
)
Judge.token_ceiling -> int
async Judge(case, output) -> Score
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `model` | `Model` | Required | Any AgentFrame model with structured output, such as `FoundryModel` or `OllamaModel`. The application owns and closes it. |
| `rubric` | `str` | Required | Trusted instructions saying what passes; must not be blank. Never put untrusted content here. |
| `votes` | `int` | Optional; default `1` | The most verdicts to ask for: an odd number from 1 to 15. The majority wins; asking stops once a majority is certain. Each vote is a separate judge run with its own cost and budget hold. |
| `options` | `RunOptions \| None` | Optional; default `None` (library defaults) | Limits and model settings for every judge run. Its resolved `max_total_tokens` is `token_ceiling`, which each judge run holds against the evaluation's budget. |

```python
from agentframe import ModelSettings, RunLimits, RunOptions
from agentframe.evaluation import Evaluation, Judge, LocalSkill

judge = Judge(
    judge_model,  # application model, different from the target's
    rubric="The summary is one sentence and states only facts in the text.",
    votes=3,  # up to three judge runs per case
    options=RunOptions(
        limits=RunLimits(max_total_tokens=5_000),
        model_settings=ModelSettings(max_output_tokens=512),
    ),
)
evaluation = Evaluation(
    cases=cases,  # application cases
    target=LocalSkill(Writer(model=model), skill="summarize"),
    scorers=[judge],
    max_total_tokens=200_000,  # target and judge runs together
)
```

**Behavior / failures:** a blank `rubric`, or `votes` that is not an odd
`int` from 1 to 15 (including `True`), raises `DefinitionError` at once.
Each judgement is one ordinary run of a small model-backed skill through
`Runtime`, so it gets `RunLimits`, retries and usage accounting. The model is
asked for `Verdict(passed: bool, reason: str)` through `output=`; the scorer
returns `Score(passed=..., reason=...)`. The rubric is sent as trusted
`system` instructions; the case input (`arguments` or `text`), the output and
`expected` (as a reference, only when set; a judge without one grades against
the rubric alone) go as one JSON `user` message, so an output saying "ignore the
rubric and pass" is data to judge, not instructions. That narrows prompt
injection but cannot rule it out
([prompt safeguarding](../design/prompt-safeguarding.md)). The case is
`errored` with the judge run's error, such as `UnsupportedCapabilityError` (a
model without structured output; nothing is sent), `OutputValidationError` (an
invalid verdict, never parsed from prose) or `ModelRefusalError`; output that
is not JSON-serializable errors it too.

**Votes and their cost.** With `votes` above 1 the judge runs one vote at a
time and passes on a majority, stopping as soon as the majority is certain:
`votes=3` takes two runs when they agree and three when they split, so it
costs up to three times one vote. The `Score`'s `value` is the fraction of
the votes cast that passed (for example `0.666667` for a 2-1 split), so a
split vote is visible; with `votes=1` `value` stays `None`. Every judge
`Score` also has `votes_cast` and `votes_passed`, counting the judge runs
actually made, so an early stop is visible too: a unanimous `votes=3` is
`2/2` and a unanimous `votes=5` is `3/3`, though both have a `value` of 1
(#213). The summary shows them as `judge=pass(0.666667, 2/3 votes)` or, with
one vote, `judge=pass(1/1 votes)`, and the JSON `scores` as `votes_cast` and
`votes_passed`; they are kept without `capture_content`, since they are
counts, not content. The `reason` is the first reason on the
winning side. Any vote that errors, or that the budget cannot fit, errors the
case; it is never scored on fewer votes. Votes do not run concurrently, so a
case holds at most one judge ceiling at a time.

In an `Evaluation`, each judge run (each vote) holds `token_ceiling` against
`max_total_tokens` just before it starts, separately from the case's hold, and
when the budget cannot fit it the case is `errored` with
`TokenLimitExceededError` and nothing is sent. Judge usage is reported in
`CaseResult.judge_usage` and `EvalReport.total_judge_usage`, apart from the
target's. The verdict's `reason` is content: results keep it only with
`capture_content=True`, and it is never logged. Called directly
(`await judge(case, output)`), a judge runs with no evaluation budget.

**Bias guidance.** Use a judge model different from the target's, since models
tend to prefer their own answers; the harness does not enforce it. Each call
grades one answer, so there is no position bias between candidates. Write a
rubric with checkable yes/no criteria and give cases `expected` references
where you can. Judging sends case content to the judge model's provider, a
data-egress choice.

**Related:** [judge](../design/evaluation.md#judge),
[scorers](../design/evaluation.md#scorers).

## `CaseResult`

**Purpose / decision:** the outcome of one attempt at a case. Content is off by default
([decision 13](../design/evaluation.md#decisions-confirmed-2026-11-13)).

**Signature status:** implemented in M9 #153 as a frozen dataclass; built by
the harness, not by applications.

| Field | Type | Meaning |
| --- | --- | --- |
| `case_id` | `str` | The case's ID. |
| `attempt` | `int` | Which attempt this is, from 1 to `repeats` (#157). |
| `status` | [`CaseStatus`](#casestatus) | `passed`, `failed`, `errored` or `not_run` (never started: budget or timeout). |
| `scores` | `tuple[`[`ScoreResult`](#scoreresult)`, ...]` | Each scorer's verdict, in scorer order. Scorers after one that raised do not run. |
| `error_type` | `str \| None` | Class name of the error that errored the case. |
| `error_message` | `str \| None` | AgentFrame's safe message, or which part raised (for example `"Scorer 'mentions_order' raised KeyError."`); other exceptions' messages are never kept. For `not_run`, why it did not run. |
| `invocation_id` | `str \| None` | The run's ID, also when it failed with an AgentFrame error. |
| `judge_usage` | `Usage \| None` | The [`Judge`](#judge) runs' usage for this attempt (every vote), summed apart from the target's (#160). `None` when no judge ran; a judge run that failed makes its counts unknown. |
| `usage`, `duration`, `model_calls`, `tool_calls`, `finish_reason` | from [`TargetResult`](#targetresult) | `None` when the target raised or the attempt did not run; `model_calls`, `tool_calls` and `finish_reason` are always `None` for `A2ATarget`; usage fields may be `None` (unknown, not zero). |
| `tags` | `tuple[str, ...]` | The case's tags. |
| `arguments`, `text`, `expected`, `output` | content | `None` unless `capture_content=True`. |

**Related:** [`CaseStatus`](#casestatus), [`ScoreResult`](#scoreresult),
[reports](../design/evaluation.md#reports).

## `CaseStatus`

**Purpose / decision:** how one attempt at a case ended, as a fixed set of
values reports and CI gates can compare against.

**Signature status:** implemented in M9 #153 as a `StrEnum`; `NOT_RUN` added
in #157.

| Value | String | Meaning |
| --- | --- | --- |
| `PASSED` | `"passed"` | The target ran and every scorer passed. |
| `FAILED` | `"failed"` | The target ran and at least one scorer failed. |
| `ERRORED` | `"errored"` | The target or a scorer raised, a scorer returned something other than a `bool` or `Score`, or the evaluation's timeout cancelled the attempt while it ran. |
| `NOT_RUN` | `"not_run"` | The attempt never started: the evaluation's token budget could not fit it, or its timeout expired first. |

```python
from agentframe.evaluation import CaseStatus, EvalReport


def errored_ids(report: EvalReport) -> list[str]:
    return [r.case_id for r in report.results if r.status is CaseStatus.ERRORED]
```

**Behavior / failures:** the JSON report writes the string values. Pass rates
leave out `not_run` attempts; see [`EvalReport`](#evalreport).

**Related:** [`CaseResult`](#caseresult), [`EvalReport.require`](#evalreportrequire).

## `ScoreResult`

**Purpose / decision:** one scorer's verdict on one case, as kept in a
[`CaseResult`](#caseresult). A [`Score`](#score) is what a scorer returns;
a `ScoreResult` adds the scorer's name and drops content that was not
captured.

**Signature status:** implemented in M9 #153 as a frozen, keyword-only
dataclass; vote counts in #213. Built by the harness, not by applications.

| Field | Type | Meaning |
| --- | --- | --- |
| `scorer` | `str` | The scorer's name (see [`Scorer`](#scorer)). |
| `passed` | `bool` | Whether the output passed. |
| `value` | `float \| None` | The scorer's optional number. |
| `reason` | `str \| None` | The scorer's explanation; `None` unless the evaluation captures content, since it may quote the output. |
| `votes_cast` | `int \| None` | How many votes a voting scorer such as [`Judge`](#judge) cast; `None` unless the scorer sets them. Always kept, since counts hold no content. |
| `votes_passed` | `int \| None` | How many of those votes passed. |

```python
from agentframe.evaluation import CaseResult


def failed_scorers(result: CaseResult) -> list[str]:
    return [score.scorer for score in result.scores if not score.passed]
```

**Related:** [`Score`](#score), [`CaseResult`](#caseresult).

## `EvalReport`

**Purpose / decision:** the results of one evaluation run, with aggregates and
a readable console summary.

**Signature status:** `results` implemented in M9 #153; aggregates and
`str(report)` in #155; `max_total_tokens`, `not_run_count`, `flaky_cases` and
`over_budget` in #157; `target`, `scorers`, `capture_content`,
[`write_json`](#evalreportwrite_json) and [`require`](#evalreportrequire) in
#156; `total_judge_usage` in #160; [`write_html`](#evalreportwrite_html) in
#276.

Every aggregate counts attempts: with `repeats=3` each case adds three results.
`not_run` attempts are counted only by `not_run_count` and left out of every
other aggregate (rates, usage, latency, errors), so they never shrink or pad a
rate unseen; check `not_run_count` alongside `pass_rate`.

| Field or property | Type | Meaning |
| --- | --- | --- |
| `results` | `tuple[CaseResult, ...]` | One [`CaseResult`](#caseresult) per attempt, in case order, then attempt order. |
| `max_total_tokens` | `int \| None` | The evaluation-wide token budget, or `None`. |
| `target` | `Mapping[str, str]` | The target's [`description`](#target), for example `{"type": "local_skill", "agent": "Assistant", "skill": "count_words"}`. |
| `scorers` | `tuple[str, ...]` | The scorers' names, in scorer order. |
| `capture_content` | `bool` | Whether the results keep case content. |
| `passed_count`, `failed_count`, `errored_count` | `int` | Attempts per status; errored (target or scorer raised) is counted apart from failed (a scorer failed). |
| `not_run_count` | `int` | Attempts that never started (budget or timeout). |
| `pass_rate` | `float` | Passed attempts / attempts that ran, from `0.0` to `1.0`; errored attempts count against it, `not_run` ones are counted by `not_run_count` instead. `0.0` when none ran. |
| `scorer_pass_rates` | `dict[str, float]` | Each scorer's pass rate over the attempts it scored, in scorer order. A scorer that did not run for an attempt (the target or an earlier scorer raised) does not count for it. |
| `tag_pass_rates` | `dict[str, float]` | Pass rate of the attempts carrying each tag, in first-seen order. |
| `flaky_cases` | `list[str]` | IDs of cases whose attempts that ran ended differently (for example one passed and one failed), in case order. |
| `total_usage` | [`UsageTotals`](#usagetotals) | The targets' summed tokens over attempts that ran; unknown is never counted as zero. |
| `total_judge_usage` | [`UsageTotals`](#usagetotals) `\| None` | [`Judge`](#judge) tokens summed over the attempts a judge ran for; `None` when no judge ran. |
| `over_budget` | `bool` | Whether reported target and judge tokens (the two `known_total_tokens`) exceed `max_total_tokens`. `unknown_cases` above 0 in either means the budget could not be fully enforced. |
| `latency` | [`Latency`](#latency) `\| None` | p50, p95 and max attempt duration; `None` when no attempt has a duration ([decision 1 of #155](https://github.com/malcmiller/agentframe/issues/155)). |
| `errors_by_class` | `dict[str, int]` | Errored attempts per error class, for example `{"TimeoutLimitExceededError": 2}`. |

All aggregates are read-only properties computed from `results`.

```python
from agentframe.evaluation import EvalReport


def check(report: EvalReport) -> None:
    print(report)
    if report.errored_count or report.not_run_count or report.pass_rate < 0.9:
        raise SystemExit(1)
```

`str(report)` (and `print(report)`) is a plain-text summary with no extra
dependency: one line each for counts and pass rate, tokens, latency, errors,
scorer and tag pass rates, then a table of case ID, status, scorer verdicts
(`name=pass` / `name=fail`, with the value and any vote counts in brackets,
such as `judge=pass(0.666667, 2/3 votes)`, or `error=<class>`),
tokens (`~` marks estimates, `unknown` when not reported) and duration,
failing cases first (errored, then failed, then not run, then passed). With a
budget, a `Budget:` line shows it and any overrun or unknown usage (target
and judge together); when a judge ran, a `Judge tokens:` line follows
`Tokens:`; with
`repeats`, the header counts attempts, the table names each `case #attempt`
and a `Flaky:` line lists flaky cases; `not run` appears in the header when any
attempt did not run:

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

For example, with `repeats=2` and `max_total_tokens=20`:

```text
Evaluation: 2 cases, 4 attempts, 2 passed, 0 failed, 1 errored, 1 not run (pass rate 66.7%)
Tokens: unknown; 30 reported, 1 cases with unknown usage
Budget: 20 tokens, over budget (30 reported), not fully enforced (1 attempts with unknown usage)
Latency: -
Flaky: a
Errors: ValueError 1
Tags: slow 0.0%

case  status   scores            tokens   duration
a #2  errored  error=ValueError  unknown  -
b #2  not_run  -                 -        -
a #1  passed   -                 15       -
b #1  passed   -                 15       -
```

**Related:** [reports](../design/evaluation.md#reports).

## `EvalReport.write_json`

**Purpose / decision:** a versioned JSON document of the report, to keep as a CI
artifact and compare later. The schema identifier is
`agentframe.evaluation.report/1`, bumped on breaking changes
([decision 2 of #156](https://github.com/malcmiller/agentframe/issues/156)).

**Signature status:** implemented in M9 #156.

```text
EvalReport.write_json(path: str | Path) -> None
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `path` | `str \| Path` | Required | File to write as UTF-8 JSON; an existing file is replaced. |

```python
from agentframe.evaluation import EvalReport


def keep(report: EvalReport) -> None:
    report.write_json("eval-report.json")
```

The document's top-level keys:

| Key | Meaning |
| --- | --- |
| `schema` | `"agentframe.evaluation.report/1"`. |
| `agentframe_version` | The installed AgentFrame version. |
| `target` | The report's `target`, for example `{"type": "local_skill", "agent": "Assistant", "skill": "count_words"}`. |
| `scorers` | Scorer names, in scorer order. |
| `capture_content` | Whether content was captured. |
| `max_total_tokens` | The evaluation-wide budget, or `null`. |
| `aggregates` | `attempt_count`, `passed_count`, `failed_count`, `errored_count`, `not_run_count`, `pass_rate`, `scorer_pass_rates`, `tag_pass_rates`, `flaky_cases`, `total_usage` (the [`UsageTotals`](#usagetotals) fields), `total_judge_usage` (the same fields, or `null` when no [`Judge`](#judge) ran), `over_budget` (target plus judge tokens), `budget_fully_enforced` (`null` without a budget, `false` when any target or judge usage is unknown), `latency` (`p50_seconds`, `p95_seconds`, `max_seconds`, or `null`) and `errors_by_class`. |
| `results` | One object per attempt, in report order: `case_id`, `attempt`, `status`, `tags`, `scores` (`scorer`, `passed`, `value`, `votes_cast`, `votes_passed`; the vote counts are `null` unless the scorer sets them, as a [`Judge`](#judge) always does, added in #213 without changing the schema identifier), `error_type`, `error_message`, `invocation_id`, `usage` (`input_tokens`, `output_tokens`, `total_tokens`, `estimated`, or `null`), `judge_usage` (the same fields, or `null` when no judge ran), `duration_seconds`, `model_calls`, `tool_calls` and `finish_reason`. |

**Behavior / failures:** unknown token counts are `null`, never 0. Like the
console summary, the document holds no case content and no error messages by
default: `arguments`, `text`, `expected`, `output` and each score's `reason`
are left out, and `error_message` is `null` except for a `not_run` attempt's
fixed reason. With `capture_content=True` they are all written; treat that
file as sensitive. Captured values Pydantic cannot convert to JSON are written
as their `repr`. File system errors propagate.

**Related:** [pytest and CI](../design/evaluation.md#pytest-and-ci),
[evaluation guide](../guides/evaluation.md).

## `EvalReport.write_html`

**Purpose / decision:** one self-contained HTML page of the report for people
to read, optionally compared with an earlier JSON report. It is rendered from
the same document [`write_json`](#evalreportwrite_json) writes, with the
standard library only.

**Signature status:** implemented in #276.

```text
EvalReport.write_html(path: str | Path, *, baseline: str | Path | None = None) -> None
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `path` | `str \| Path` | Required | File to write as UTF-8 HTML; an existing file is replaced. |
| `baseline` | `str \| Path \| None` | Optional; default `None` | An earlier report written by `write_json` (schema `agentframe.evaluation.report/1`) to compare with. |

```python
from agentframe.evaluation import EvalReport


def keep(report: EvalReport) -> None:
    report.write_json("eval-report.json")
    report.write_html("eval-report.html", baseline="main-eval-report.json")
```

The page shows the target, scorers, budget and AgentFrame version; the
aggregates (counts, pass rate, tokens, judge tokens, latency, flaky cases);
scorer and tag pass rates; errors by class; a case table that sorts by any
column and filters by text or status; and per attempt its scores (value and
judge vote counts), error type, invocation ID, usage, duration and call
counts. Attempts that did not pass open expanded.

With `baseline`, a "Compared with baseline" section lists cases that newly
fail and newly pass and the pass-rate (in percentage points) and latency
(p50, p95, max) changes. A case passes when every attempt of it that ran
passed; cases present in only one report are not compared.

**Behavior / failures:** the page loads nothing from the network: CSS and a
small sort-and-filter script are inline, and a Content-Security-Policy allows
only those two (by hash) and blocks every fetch, so it works offline and as a
downloaded CI artifact. Every value is HTML-escaped, so case outputs, judge
reasons and error text are shown as text, never run. Content follows
`capture_content` exactly as in the JSON: without it the page has no case
inputs, expected values, outputs, score reasons or error messages; with it,
treat the file as sensitive. A baseline that is not JSON or not an AgentFrame
evaluation report raises `ValueError`; file system errors propagate.

**Related:** [evaluation guide](../guides/evaluation.md#html-report),
[`agentframe eval`](#agentframe-eval).

## `EvalReport.require`

**Purpose / decision:** fail a pytest test or script when the report misses a
threshold, so quality regressions fail a build like a test. There is no pytest
plugin ([decision 15](../design/evaluation.md#decisions-confirmed-2026-11-13)).

**Signature status:** implemented in M9 #156.

```text
EvalReport.require(*, min_pass_rate: float | None = None,
                   max_errors: int | None = 0,
                   max_total_tokens: int | None = None,
                   allow_not_run: bool = False,
                   allow_over_budget: bool = False) -> None
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `min_pass_rate` | `float \| None` | Optional; default `None` | Lowest acceptable `pass_rate`, from 0.0 to 1.0. Missed when no attempt ran, whatever the value. |
| `max_errors` | `int \| None` | Optional; default `0` | Most errored attempts accepted, 0 or more; `None` accepts any number. |
| `max_total_tokens` | `int \| None` | Optional; default `None` | Most tokens the evaluation may have used, target and [`Judge`](#judge) runs together, greater than 0. Missed when any target or judge usage is unknown. |
| `allow_not_run` | `bool` | Optional; default `False` | Whether attempts that never started are accepted. |
| `allow_over_budget` | `bool` | Optional; default `False` | Whether reported usage over the report's `max_total_tokens` budget is accepted. |

```python
from agentframe.evaluation import Evaluation


async def test_answers(evaluation: Evaluation) -> None:
    report = await evaluation.run()
    report.require(min_pass_rate=0.9)
```

**Behavior / failures:** raises `AssertionError` listing every missed threshold,
then the console summary, for example:

```text
Evaluation requirements not met:
- pass rate 66.7% is below 90.0%
- 1 attempts errored; at most 0 allowed

Evaluation: 3 cases, 2 passed, 0 failed, 1 errored (pass rate 66.7%)
...
```

The listed misses hold counts and thresholds only, and the summary holds no
case content unless the evaluation captured it. Unknown usage never passes a
token threshold. A threshold out of range raises `ValueError`.

**Related:** [pytest and CI](../design/evaluation.md#pytest-and-ci),
[evaluation guide](../guides/evaluation.md).

## `UsageTotals`

**Purpose / decision:** token usage summed over an evaluation without ever
treating unknown usage as zero.

**Signature status:** implemented in M9 #155 as a frozen dataclass returned by
`EvalReport.total_usage`.

| Field | Type | Meaning |
| --- | --- | --- |
| `input_tokens`, `output_tokens`, `total_tokens` | `int \| None` | Sums over every attempt that ran, or `None` when any has no usage or that count unknown. `not_run` attempts used nothing and are left out. |
| `known_total_tokens` | `int` | Sum of the total token counts attempts did report; a lower bound when `unknown_cases` is not zero. |
| `unknown_cases` | `int` | Attempts that ran with no usage (the target raised or was cancelled) or any unknown count. |
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

## `agentframe eval`

**Purpose / decision:** run an evaluation from the command line and gate CI on
its thresholds without pytest. The last, separable M9 story
([decision 14](../design/evaluation.md#decisions-confirmed-2026-11-13)); pytest
plus [`require`](#evalreportrequire) covers the same need.

**Signature status:** implemented in M9 #162, in the existing `agentframe`
command with no new dependency.

```text
agentframe eval TARGET [--json PATH] [--html PATH [--baseline PATH]]
                [--min-pass-rate RATE] [--max-errors N] [--max-total-tokens N]
                [--allow-not-run] [--allow-over-budget] [--capture-content]
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `TARGET` | `module:function` string | Required | Function called with no arguments that returns an [`Evaluation`](#evaluation), loaded like [`agentframe run`](hosting.md#agentframe-run)'s factory. The working directory is importable. |
| `--json` | path | Optional; default none | Write [`write_json`](#evalreportwrite_json) here before thresholds are checked, so the file exists when one is missed. An existing file is replaced. |
| `--html` | path | Optional; default none | Write [`write_html`](#evalreportwrite_html) here, after `--json` and before thresholds are checked. An existing file is replaced. Added in #276. |
| `--baseline` | path | Optional; default none; needs `--html` | An earlier `--json` report to compare with in the HTML report. Read and checked before the evaluation runs. Added in #276. |
| `--min-pass-rate` | float | Optional; default none | Passed to `require(min_pass_rate=)`; from 0.0 to 1.0. |
| `--max-errors` | integer | Optional; default `0` | Passed to `require(max_errors=)`; 0 or more. |
| `--max-total-tokens` | integer | Optional; default none | Passed to `require(max_total_tokens=)`; 1 or more. Unknown usage misses it. |
| `--allow-not-run` | flag | Off | Passed to `require(allow_not_run=True)`. |
| `--allow-over-budget` | flag | Off | Passed to `require(allow_over_budget=True)`. |
| `--capture-content` | flag | Off | Allow an evaluation built with `capture_content=True`. The command never turns capture on; without this flag such an evaluation is refused before it runs. |

```python
# evals/word_counts.py
from agentframe.evaluation import Case, Evaluation, LocalSkill, equals

from myapp.agents import Assistant  # application agent


def create_evaluation() -> Evaluation:
    return Evaluation(
        cases=[Case(id="short", arguments={"text": "one two three"}, expected=3)],
        target=LocalSkill(Assistant(), skill="count_words"),
        scorers=[equals],
    )
```

```shell
agentframe eval evals.word_counts:create_evaluation --json eval-report.json \
  --min-pass-rate 0.9
```

**Behavior / failures:** the evaluation runs once. Exit codes:

| Exit code | Meaning | Output |
| --- | --- | --- |
| `0` | Every threshold met. | The summary (`str(report)`) on stdout. |
| `1` | A threshold missed (`require` raised `AssertionError`). | Each miss above the summary on stdout, then `agentframe: error: The evaluation missed a threshold.` on stderr. |
| `2` | Configuration error; nothing is printed on stdout. | One `agentframe: error: ...` line on stderr. |

Configuration errors are a target that is not `module:name`, a module that
cannot be found, a missing attribute, a target that is not callable, a factory
returning anything but an `Evaluation`, an invalid evaluation
(`DefinitionError` from `Evaluation`), a factory that raises any other
exception (only its class name is printed, since its message may hold case
content), an evaluation that captures content without `--capture-content`, a
`--json` or `--html` path that cannot be written, `--baseline` without
`--html`, and a baseline that cannot be read or is not an AgentFrame
evaluation report. Invalid threshold values are argparse
usage errors, also exit code 2. Errors raised while importing the target
module keep their traceback. Other commands never import
`agentframe.evaluation`. Output holds no case
content (the summary, JSON and HTML never do) unless the evaluation captured it and
`--capture-content` allowed that. Failing, errored and not-run attempts never
stop the run; they are counted against the thresholds.

**Related:** [pytest and CI](../design/evaluation.md#pytest-and-ci),
[evaluation guide](../guides/evaluation.md#without-pytest-agentframe-eval).
