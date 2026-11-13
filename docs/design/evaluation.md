# Evaluation

**Status: proposed (M9), not implemented.** Nothing on this page exists in
`agentframe` yet. Names, defaults and shapes below are recommendations awaiting
the maintainer's decisions; each unresolved choice is listed under
[open questions](#open-questions) with a recommendation.

Agents built with AgentFrame are nondeterministic once they call a model. Unit
tests with [`ScriptedModel`](../api/models.md#scriptedmodel) prove the code
paths; they cannot tell whether a prompt change made answers worse, or whether a
switch from a Foundry GPT deployment to local Llama keeps a skill good enough.
Evaluation answers that question: run a fixed set of cases against an agent,
score each result and compare the totals against a threshold, locally and in CI.

## Goals

- **Owned, small harness.** An AgentFrame-owned evaluation API in the same
  style as the rest of the library: typed cases, plain functions as scorers and
  a validated report. It is not a wrapper around another evaluation framework,
  for the same reason AgentFrame does not wrap an agent framework
  ([dependency boundaries](../roadmap.md#independence-and-dependency-boundaries)).
- **Evaluate what is deployed.** The same cases run against a local skill
  through `Runtime.run`, or against any A2A 1.0 agent (AgentFrame or not,
  including Foundry agents) through the existing remote client.
- **Reuse execution controls.** Per-case work is bounded by the existing
  `RunLimits`, deadlines, token ceilings and retries; nothing new to learn and
  no way around host ceilings.
- **Honest numbers.** Pass rates, tokens, latency, model/tool call counts and
  failures come from `RunResult` and the peer's usage snapshot; unknown usage is
  reported as unknown, never zero.
- **CI-friendly.** Ordinary pytest tests and a versioned JSON report make a
  threshold a build gate.
- **Private by default.** Like [tracing](tracing.md#safe-metadata), reports do
  not contain prompts, inputs, outputs or judge reasoning unless explicitly
  enabled.

## Non-goals

- A hosted evaluation service, dashboard, experiment tracker or dataset store.
- Benchmark suites, red-teaming generators, synthetic data generation or safety
  classifiers (content-safety evaluators stay provider services such as
  Foundry's).
- Trajectory evaluation of individual tool calls and arguments. `RunResult`
  exposes call counts, not a tool-call log; scoring trajectories needs a
  trajectory contract first and is later.
- A2A protocol conformance. That is the
  [A2A TCK](https://github.com/a2aproject/a2a-tck)'s job; evaluation scores
  answers, not wire compliance.
- Comparing two reports (baselines, regressions over time). Later, once the
  report format has real use.
- Exporting to or running Foundry cloud evaluation. A
  [later adapter](#later-foundry-evaluation-export).

## What current tools do

The design borrows common ideas, not code or dependencies:

| Tool | Shape | What AgentFrame takes from it |
| --- | --- | --- |
| [Azure AI Evaluation SDK](https://learn.microsoft.com/en-us/python/api/overview/azure/ai-evaluation-readme?view=azure-python) (`azure-ai-evaluation`) | `evaluate(data="x.jsonl", evaluators={...})`; JSONL rows such as `query` / `response` / `ground_truth` mapped to evaluator arguments; AI-assisted, NLP and agent evaluators (`IntentResolution`, `TaskAdherence`, `ToolCallAccuracy`). | JSONL datasets, one row per case; per-case and aggregate results. The Foundry row shape is the target of a later export adapter. |
| [Foundry cloud evaluation](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/cloud-evaluation?view=foundry&tabs=python) | `azure-ai-projects` 2.x drives the OpenAI-compatible Evals API: `evals.create(data_source_config, testing_criteria)` then `evals.runs.create`. | Evals are defined once and run many times; criteria are a list; results live in a service. AgentFrame stays local and leaves the service to Foundry. |
| [OpenAI Evals API](https://platform.openai.com/docs/guides/evals) | Testing criteria `string_check`, `text_similarity`, `label_model`, `score_model`, `python`, over JSONL items. | Deterministic string checks next to model-graded labels; a label (pass/fail) judge is simpler than a numeric one. |
| [pydantic-evals](https://ai.pydantic.dev/evals/) | `Dataset(cases=[Case(name, inputs, expected_output)], evaluators=[...])`, YAML/JSON files, console report. (Part of the Pydantic AI project, so not a permitted dependency.) | Typed `Case` with an expected value; evaluators as small objects; readable console table. |
| [Inspect AI](https://inspect.aisi.org.uk/tasks.html) | `Task(dataset, solver, scorer)`; JSON datasets; structured log per sample. | Separate *what to run* (target) from *how to score* (scorer). |
| [DeepEval](https://deepeval.com/docs/evaluation-unit-testing-in-ci-cd) | `assert_test(test_case, [metrics])` inside pytest; `deepeval test run`. | Evaluations as ordinary pytest tests with a threshold assertion. |
| [promptfoo](https://www.promptfoo.dev/docs/configuration/expected-outputs/) | YAML assertions `equals`, `contains`, `is-json`, `llm-rubric`. | The small built-in scorer set: equality, containment, type validation, rubric judge. |

Model-graded scoring has known failure modes. [Zheng et al., *Judging
LLM-as-a-Judge with MT-Bench and Chatbot Arena* (NeurIPS 2023)](https://proceedings.neurips.cc/paper_files/paper/2023/hash/91f18a1287b398d378ef22505bf41832-Abstract-Datasets_and_Benchmarks.html)
documents position bias, verbosity bias and self-enhancement bias, and finds
reference-guided grading and swapping/aggregating reduce (not remove) them.
[Shi et al., *Judging the Judges* (2024)](https://arxiv.org/abs/2406.07791)
measures position bias systematically. The [judge](#judge) section applies
these findings: one answer per call (no pairwise position), a reference answer
when the case has one, a binary verdict with a written rubric, optional
repeated votes, and the judge charged to the same budget as everything else.

## API sketch

All of this is proposed. It lives in a separate `agentframe.evaluation` module
(like `agentframe.testing`), not in the top-level `agentframe` namespace, so the
core import stays unchanged ([open question 1](#open-questions)).

```python
# test_assistant_eval.py - proposed, not implemented
from agentframe import Runtime
from agentframe.evaluation import Case, Evaluation, LocalSkill, contains, equals
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
    report.require(min_pass_rate=1.0)
```

A model-backed skill against a real model, with a rubric judge and cases from a
file:

```python
# proposed, not implemented
from agentframe.evaluation import Evaluation, Judge, LocalSkill, load_cases, valid

evaluation = Evaluation(
    cases=load_cases("evals/summaries.jsonl"),
    target=LocalSkill(Assistant(model=model), skill="summarize"),
    scorers=[
        valid(Summary),
        Judge(
            judge_model,
            rubric="The summary is one sentence and states only facts in the text.",
        ),
    ],
    concurrency=4,
    max_total_tokens=200_000,  # target and judge calls together
)
report = await evaluation.run()
print(report)  # case IDs, scores, failures; no content by default
report.write_json("eval-report.json")
report.require(min_pass_rate=0.9)
```

The same cases against a deployed agent over A2A, using the existing directory
and outbound identity:

```python
# proposed, not implemented
from agentframe import AgentDirectory, APIKeyCredential, FileAgentRegistry, Service
from agentframe.evaluation import A2ATarget, Evaluation, load_cases, valid

async with AgentDirectory(sources=[FileAgentRegistry("agents.yaml")]) as directory:
    target = A2ATarget(
        directory,
        "assistant",
        skill="summarize",  # structured call; omit to send case text with ask
        output=Summary,
        identity=Service(credential=APIKeyCredential(api_key)),
    )
    report = await Evaluation(cases=cases, target=target, scorers=[valid(Summary)]).run()
```

### Cases and datasets

```python
# proposed, not implemented
@dataclass(frozen=True, kw_only=True)
class Case[E]:
    id: str
    arguments: Mapping[str, Any] | None = None  # for a skill (structured)
    text: str | None = None  # for free text (A2A ask)
    expected: E | None = None
    tags: tuple[str, ...] = ()
```

- A case has exactly one of `arguments` (a named skill's JSON arguments) or
  `text` (a free-text message). The target says which it needs; a mismatch fails
  before any case runs.
- `id` is required and unique, so reports and CI diffs name cases stably.
- `expected` is optional: scorers that need it fail clearly when it is missing;
  a judge without `expected` grades against the rubric alone.
- `tags` let a report break results down (for example `"edge-case"`).

`load_cases(path)` reads [JSONL](https://jsonlines.org/): one JSON object per
line with the same fields, for example:

```json
{"id": "short", "arguments": {"text": "one two three"}, "expected": 3}
{"id": "refund", "text": "Can I get a refund for order A1?", "tags": ["support"]}
```

JSONL matches what Foundry, the OpenAI Evals API and most tools read, diffs
well and can be appended to. Loading is strict: unknown keys, duplicate IDs,
invalid JSON or both/neither of `arguments`/`text` fail with the file name and
line number. Files are read at most 10 MiB, with `json.loads` only (no code
execution). When the target knows the skill's output type, each `expected`
value is validated against it before any case runs, so a typo in a dataset
fails fast instead of producing a confusing score.

### Targets

A target runs one case and returns its output plus metadata. Two are proposed:

| Target | Runs | Output and metadata |
| --- | --- | --- |
| `LocalSkill(agent, skill=..., runtime=None, principal=None, options=None)` | `Runtime.run` with the case's `arguments`. A default `Runtime()` is created when none is given. | The validated value; `usage`, `duration`, `model_calls`, `tool_calls`, `finish_reason` and `invocation_id` from `RunResult`. |
| `A2ATarget(directory, name, skill=None, output=None, identity=None)` | `Remote.call(skill, **arguments)` when `skill` is set, otherwise `Remote.ask(text)`, through the same internal binding a skill's `Remote` uses. | The decoded (and, with `output=`, validated) result; usage from the peer's task snapshot when it advertises it, otherwise unknown; wall-clock latency measured by the harness. |

Because `A2ATarget` reuses the `Remote` binding, every per-case request gets the
same deadline, `max_total_tokens` hold, outbound identity, same-origin and
no-redirect rules, best-effort cancellation and usage reconciliation as a
skill's remote call. Non-AgentFrame agents and Foundry agents work through
`ask`; `call` needs a peer whose card declares AgentFrame's skill-call extension
and skill, exactly as `Remote.call` requires. `OnBehalfOf` identities are not
supported because an evaluation run has no signed-in user.

`LocalSkill` goes through the full runtime pipeline (validation, permissions,
`ExecutionPolicy`, limits). Protected skills need an explicit trusted
`principal=`; the harness never invents one.

An in-process hosted target (exercising an `AgentApp`'s router and
authentication without a network) is not proposed: run the app with
`agentframe dev` and point an `A2ATarget` at it.

### Scorers

A scorer is any sync or async callable taking the case and the output and
returning `bool` or a `Score`:

```python
# proposed, not implemented
@dataclass(frozen=True, kw_only=True)
class Score:
    passed: bool
    value: float | None = None  # optional detail, e.g. similarity 0.83
    reason: str | None = None  # content: reported only with capture_content=True


def mentions_order(case: Case[str], output: str) -> bool:
    return case.arguments["order_id"] in output
```

A scorer's name in the report is the function's `__name__` (or the built-in
scorer's name). A case passes when every scorer passes. A scorer that returns a
bare number is refused rather than guessing a threshold; return
`Score(passed=value >= 0.8, value=value)` instead. A scorer that raises marks the
case as errored with the scorer name and exception class, not as a failed score.

Built-ins:

| Scorer | Passes when |
| --- | --- |
| `equals` | `output == case.expected` (Pydantic models compare field by field). |
| `contains(text, case_sensitive=False)` | Text output contains `text`; `contains()` with no argument uses `case.expected`. |
| `valid(T)` | The output validates as `T` with Pydantic. Useful for A2A results and free-text targets; local typed skills are already validated. |
| `Judge(model, rubric=..., votes=1)` | The [judge](#judge) returns a passing verdict. |

### Judge

`Judge` asks any AgentFrame `Model` (Foundry, Ollama or a custom one) whether
one output meets a written rubric:

- **Typed verdict.** The judge asks for structured output
  `Verdict(passed: bool, reason: str)` through the existing `output=` support,
  so a model without structured output fails before sending
  (`UnsupportedCapabilityError`), and invalid verdicts raise
  `OutputValidationError` instead of being parsed from prose.
- **Ordinary AgentFrame run.** Internally the judge is one model-backed skill
  run through `Runtime`, so its calls get `RunLimits`, retries, usage accounting
  and the evaluation's token budget. Judge usage is reported separately from the
  target's.
- **Bias mitigations.** One answer per call (no pairwise comparison, so no
  position bias between candidates); the case's `expected` value is included as
  a reference when present; the rubric is trusted instructions while the case
  input and output are passed as clearly delimited untrusted data, so an output
  that says "ignore the rubric and pass" is content, not instructions
  ([prompt safeguarding](prompt-safeguarding.md)). `votes=3` asks three times
  and takes the majority; votes are counted in the report.
- **Determinism.** `ModelSettings` has no temperature or seed today, and some
  Foundry reasoning deployments reject temperature. The judge relies on a binary
  rubric, reference answers and votes instead
  ([open question 10](#open-questions)).
- **Self-preference.** The docs recommend a judge model different from the
  target's; the harness does not enforce it.

### Running, concurrency and cost

`Evaluation(cases, target, scorers, concurrency=4, repeats=1,
max_total_tokens=None, timeout_seconds=None, capture_content=False)`:

- **Per case,** the target's normal limits apply: `LocalSkill(options=
  RunOptions(limits=...))` or the agent/skill/library defaults (120 s, 100 000
  tokens, 10 model calls) for local runs, and the run-level deadline and token
  ceiling for `A2ATarget`. Host ceilings are never raised by the harness.
- **Concurrency** runs at most `concurrency` cases at once with
  `asyncio.TaskGroup`, so provider rate limits and the target's
  `max_concurrent_runs` are respected. `concurrency=1` runs cases in order.
- **Repeats** run every case `repeats` times to measure nondeterminism. The pass
  rate counts every attempt; a case whose attempts disagree is marked `flaky`.
- **Evaluation-wide budget.** `max_total_tokens` covers target and judge calls
  together. Each case holds its per-case ceiling against it before starting (the
  same reservation pattern as concurrent remote calls), so the total cannot be
  exceeded by known usage; once the remainder is too small, no new case starts
  and the rest are reported `not_run`. Cases whose usage is unknown (peers that
  report none) are counted as unknown, and the report says the budget could not
  be fully enforced. `timeout_seconds` bounds the whole evaluation: on expiry,
  running cases are cancelled (best effort, as for remote calls) and the rest are
  `not_run`.
- **Failures don't stop the run.** A target error (for example
  `TokenLimitExceededError` or `RemoteAgentError`) marks that case `errored` with
  the error class and AgentFrame's safe message; `asyncio.CancelledError` still
  propagates.

### Reports

`EvalReport` holds one `CaseResult` per case attempt and the aggregates:

| Field | Meaning |
| --- | --- |
| `status` per case | `passed`, `failed` (a scorer failed), `errored` (target or scorer raised) or `not_run` (budget/time). |
| `scores` per case | Each scorer's name, `passed` and optional `value`. |
| `invocation_id`, `usage`, `duration`, `model_calls`, `tool_calls`, `finish_reason` | From `RunResult` (local) or the remote call; `usage` fields may be `None` (unknown). |
| `judge_usage` | The judge's own usage for that case. |
| `pass_rate` | Passed attempts / attempts that ran (`not_run` excluded and counted separately). |
| per-scorer and per-tag pass rates | Which check or slice is failing. |
| `total_usage` | Sums of known counts plus how many cases had unknown usage; never treats unknown as zero. |
| latency | p50, p95 and max of case durations. |
| errors by class | For example `{"TimeoutLimitExceededError": 2}`. |

Output formats:

- `str(report)` / `print(report)`: a plain-text summary table (no new
  dependency), failing cases first.
- `report.write_json(path)`: a versioned document (`"schema":
  "agentframe.evaluation.report/1"`) with the fields above, the AgentFrame
  version, target description (agent and skill name, or remote name) and scorer
  names, for CI artifacts and later comparisons.
- `report.require(min_pass_rate=..., max_errors=...)`: raises `AssertionError`
  with the summary when a threshold is missed, for pytest and scripts.

**Content.** With the default `capture_content=False`, reports contain IDs,
statuses, scorer names, numbers and error classes only: no case arguments or
text, expected values, outputs or judge reasons. `capture_content=True` adds
them to the in-memory report and JSON file for local debugging; treat such a file
as sensitive. Error messages are AgentFrame's safe messages; tracebacks stay in
operator logs with the invocation ID, as for runs.

### pytest and CI

No pytest plugin is proposed: an installed plugin would load in every project
that has AgentFrame and pytest. Evaluations are ordinary async tests (pytest
asyncio auto mode works as in this repository), using `report.require(...)`.

- Model-free or `ScriptedModel` evaluations run in normal CI.
- Real-model evaluations follow the existing opt-in convention: mark them
  `ollama` or `foundry`, skip unless `AGENTFRAME_TEST_OLLAMA=1` or
  `AGENTFRAME_TEST_FOUNDRY=1`, and read endpoints and model names only from the
  documented environment variables. Foundry evaluations cost money; keep
  datasets and output caps small.
- Upload `write_json` output as a CI artifact.

A small CLI for CI without pytest, `agentframe eval module:create_evaluation
[--json PATH] [--min-pass-rate RATE]` exiting non-zero below the threshold, is
proposed as the last, optional story ([open question 14](#open-questions)).

## Packaging and dependencies

Everything above can be built from what AgentFrame already depends on: Pydantic
for cases and verdicts, the A2A SDK through the existing remote client, the
standard library `json` for JSONL and reports, and provider extras for judge
models. So the recommendation is **no new runtime dependency** for M9.

The user suggested `pip install "agentframe-py[eval]"`. Options:

1. **Recommended:** ship `agentframe.evaluation` in the main wheel, imported only
   when used (like `agentframe.testing`), with no `eval` extra until a
   dependency is actually needed. A judge on Ollama or Foundry already installs
   with the `ollama` / `foundry` extras.
2. Declare an `eval` extra now (empty or aliasing provider extras) so the
   install command is stable. An empty extra installs nothing, which can mislead.
3. A separate distribution (`agentframe-evals`). Heavier release process for no
   dependency benefit.

Whichever is chosen: importing `agentframe` never imports the evaluation module,
no I/O happens at import or construction, and any future dependency is pinned
exactly when introduced.

## Security and privacy

- Content is off by default in console and JSON output; enabling it is explicit.
- Datasets are data: loaded with `json.loads`, size-bounded, never executed.
  Custom scorers are trusted application code, like skills.
- Judge prompts keep the rubric (trusted) separate from case input and output
  (untrusted). Judging still sends that content to the judge model's provider;
  choosing a judge provider is a data-egress decision.
- Remote targets use the application's outbound identity; the harness never
  forwards incoming tokens and does not support on-behalf-of.
- Evaluations do not bypass authorization, `ExecutionPolicy` or limits.
- Secrets never belong in datasets, rubrics or reports.

## Later: Foundry evaluation export

A later adapter (with the other [optional adapters](../roadmap.md#optional-follow-ups-after-m7))
could write an AgentFrame report with captured content as Foundry/Azure AI
Evaluation JSONL rows (`query`, `response`, `ground_truth`, and for agent
evaluators message lists with `tool_definitions`) so Foundry's built-in quality,
agent and safety evaluators can score the same runs, or upload them through
`azure-ai-projects` (already in the `foundry` extra) for cloud evaluation. It
needs `capture_content=True`, so it is an explicit export, not a default. Not
in M9.

## Open questions

Each has a recommendation; none is decided yet.

1. **Packaging and install command.** Recommendation: `agentframe.evaluation` in
   the main wheel, no `eval` extra until a dependency is needed (option 1 above).
   Everything needed is already core or a provider extra.
2. **Module name.** Recommendation: `agentframe.evaluation`, not
   `agentframe.eval`, which reads like the `eval` built-in.
3. **Case shape.** Recommendation: one `Case` with exactly one of `arguments` or
   `text`, plus optional `expected` and `tags`. One type covers structured and
   free-text targets.
4. **Dataset formats.** Recommendation: Python lists and JSONL only in M9 (no
   YAML/CSV). JSONL is what Foundry and OpenAI evals read and is easy to append
   and diff.
5. **Scorer form.** Recommendation: plain sync/async functions returning `bool`
   or `Score`, no `@scorer` decorator or base class. Smallest thing a newcomer
   understands.
6. **Pass rule and numeric scores.** Recommendation: a case passes when all its
   scorers pass; bare numbers are refused, so thresholds are always explicit in
   the scorer.
7. **Expected errors.** Recommendation: any target error fails the case in M9;
   no `expected_error=` field yet. Keeps the case model small; limit and refusal
   behavior is already covered by unit tests.
8. **Concurrency default.** Recommendation: `concurrency=4`. Fast enough for
   small datasets while staying under typical provider rate limits and the
   default host admission of 32.
9. **Evaluation-wide budget.** Recommendation: optional `max_total_tokens` and
   `timeout_seconds` on `Evaluation`, unset by default because per-case
   `RunLimits` already bound every case; when set, reserve per case and mark the
   rest `not_run`.
10. **Judge determinism.** Recommendation: do not add `temperature`/`seed` to
    `ModelSettings` for M9; use a binary rubric, reference answers and optional
    `votes`. Provider support is uneven and some reasoning deployments reject
    temperature.
11. **Judge verdict.** Recommendation: binary `passed` plus `reason`, not a 1-10
    scale. Binary labels with a rubric are easier to agree on and need no
    threshold.
12. **Repeats.** Recommendation: `repeats=1` by default; pass rate counts every
    attempt and mixed outcomes mark a case `flaky`. Makes nondeterminism visible
    without a statistics framework.
13. **Content capture.** Recommendation: off by default for console and JSON,
    including judge reasons; one explicit `capture_content=True`. Matches the
    tracing and logging redaction rules.
14. **CLI.** Recommendation: include `agentframe eval` as the last, separable
    story; pytest plus JSON covers CI first, so it can be dropped without
    blocking the milestone.
15. **pytest plugin.** Recommendation: none; ordinary tests plus
    `report.require(...)`. An entry-point plugin would load in every project with
    AgentFrame installed.
16. **A2A target scope.** Recommendation: `A2ATarget(directory, name, skill=...)`
    reusing the `Remote` binding, service identity only (no on-behalf-of), no
    in-process hosted target. Reuses decided outbound rules instead of adding a
    second client.
17. **Foundry export.** Recommendation: a later optional adapter alongside MCP,
    tracing and AG-UI, not M9. M9 should prove the local harness first, and
    export needs captured content.
18. **Milestone placement.** Recommendation: M9, after the M8+ release, with no
    imposed order relative to the later adapters. Evaluation needs the remote
    client (M8+) but no adapter.

See the [roadmap](../roadmap.md#m9-evaluation-planned) for the milestone entry.
