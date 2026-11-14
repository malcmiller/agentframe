# Evaluation

[Reference status conventions](README.md). Planned M9 harness for running fixed
cases against an agent and scoring the results; see the
[evaluation design](../design/evaluation.md) for rationale and the rest of the
proposed API. Implemented so far: [`Case`](#case) and
[`load_cases`](#load_cases) (#152). Everything else in the design is
unimplemented.

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

Each non-blank line is one JSON object with only the `Case` fields; `tags` is a
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
