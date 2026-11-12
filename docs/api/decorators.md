# Decorators and data contracts

[Reference index and status conventions](README.md). Approved fields below are
decisions. `@agent` and `@skill` are implemented from source as of M1 (model-free,
local `Runtime` only); `@tool` and unspecified details remain illustrative.
Decorators describe/register behavior; Python
types describe validated data. Neither replaces runtime enforcement.

## `@agent`

**Purpose / decision:** declare an `Agent` subclass's identity and collect its
declared/inherited skills without I/O, inference, or process-global registration.

**Signature status:** decided and implemented in M1 (2026-11-11). Version
format constraints remain open.

```text
@agent(version: str, *, name: str | None = None, description: str | None = None)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `version` | `str` | Required | Agent identity version, not the A2A protocol version; format constraints open. Omitting it raises `TypeError`. |
| `name` | `str` | Optional; defaults to the class name | Descriptive agent name, not a network route. |
| `description` | `str` | Optional; defaults to the docstring written on the class itself | A non-empty description is required: no explicit value and no own docstring raises `DefinitionError`. `Agent`'s docstring is never inherited. |

```python
from agentframe import Agent, RunContext, agent, skill


@agent(name="Echo", description="Returns supplied text.", version="0.1.0")
class Echo(Agent):
    @skill(id="echo", name="Echo", description="Return text.", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        return text
```

**Behavior / failures:** preserve class identity, inheritance, signatures, and
introspection; the decorator returns the same class. Definitions are validated
when the class is defined (import time), without I/O. Require at least one valid
declared/inherited skill; reject malformed metadata and duplicate IDs rather than
dropping declarations, raising `DefinitionError` that names the class, method and
problem. Only `Agent` subclasses can be decorated. Subclasses inherit their
parents' skills but must be decorated themselves: `definition()` on an
undecorated subclass raises `DefinitionError` instead of reusing the parent's. Optional
documentation/provider identity metadata has no finalized parameter contract.
Cards also need truthful host settings; decoration does not start a server.

**Related:** [`Agent`](agents.md#agent), [`AgentApp`](hosting.md#agentapp),
[metadata design](../annotations.md#proposed-decorators).

## `@skill`

**Purpose / decision:** register one async method under a stable skill ID, with
descriptive A2A metadata and optional mandatory all-of permission requirements.

**Signature status:** decided and implemented in M1 (2026-11-11).

```text
@skill(
    id: str, *, name: str | None = None, description: str | None = None,
    tags: Iterable[str] = (), requires_permissions: Iterable[str] = (),
    limits: RunLimits | None = None,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `id` | `str` | Required, non-empty stable ID | Unique among registered skills, including inheritance; not necessarily the Python method name. |
| `name` | `str` | Optional; defaults to the title-cased method name | Human-readable advertised skill name (`find_sources` -> `"Find Sources"`). |
| `description` | `str` | Optional; defaults to the method docstring | Describes the ability. No explicit value and no docstring raises `DefinitionError`. |
| `tags` | Iterable of `str` (lists in examples) | Optional; default empty | Descriptive discovery tags, not execution instructions or permissions. A single string is rejected. |
| `requires_permissions` | Iterable of `str` (lists in examples) | Optional; default empty | All listed permissions required. Application-defined exact strings; no inferred wildcard/role semantics. A single string is rejected so `"admin"` cannot become characters. |
| `limits` | `RunLimits` | Optional per-skill overrides | In M1 the skill's `limits` replace the agent class `limits` as a whole. Library -> agent -> skill -> per-run precedence within host ceilings; partial-field merge mechanics remain open. |

```python
from typing import Protocol
from agentframe import Agent, Inject, RunContext, RunLimits, agent, skill


class DocumentReader(Protocol):
    async def read(self, document_id: str) -> str: ...


@agent(name="Documents", description="Reads documents.", version="0.1.0")
class Documents(Agent):
    @skill(
        id="read",
        name="Read document",
        description="Read an authorized document.",
        tags=["documents"],
        requires_permissions=["documents:read"],
        limits=RunLimits(timeout_seconds=30),
    )
    async def read(
        self, document_id: str, reader: Inject[DocumentReader], ctx: RunContext,
    ) -> str:
        return await reader.read(document_id)
```

`DocumentReader` is application-defined, registered through `Services.add`
or `add_scoped`; it is not caller input or a built-in reader. The
authoritative read must enforce current resource policy.

**Behavior / failures:** every skill accepts `ctx: RunContext`, even model-free
ones. Business parameters and returns use supported Python/Pydantic annotations;
context, `Inject[T]` and M8+ `Remote["name"]` parameters are injected, not
published in caller/card/MCP schemas. Validate configured remote names through
the app directory at startup. Reject invalid definitions at startup with
`DefinitionError`, including missing required services,
unsupported annotations, and duplicate IDs. Undecorated helpers are not skills;
no mandatory `handle()` or manual dispatch is needed. Inherited overrides must
not accidentally discard permission requirements: to change an inherited skill,
redefine the same method with `@skill` again (the same id is allowed); overriding
a skill method without `@skill` raises `DefinitionError`, as do two different
methods with the same skill id.

Skills must be `async def`, take a `ctx: RunContext` parameter and annotate every
parameter and the return. Parameters must be named (no `*args`, `**kwargs` or
positional-only parameters), and a business parameter cannot be named
`principal`. The decorator only records metadata; `@agent` inspects the method
once the class exists.

This metadata is not an A2A RPC endpoint or an invented Agent Card permission
field. Empty/omitted requirements do not grant anonymous remote access.
Managed invocation enforces requirements and resource policy separately from
MCP exposure and internal model-tool registration.

**Related:** [`Runtime.invoke`](runtime.md#runtimeinvoke),
[`AuthorizationPolicy`](authorization.md#authorizationpolicy),
[registration versus routing](../annotations.md#skill-registration-versus-request-routing).

## `@tool`

**Purpose / decision:** decoration for typed internal model-callable functions,
optional per agent, with bounded execution/continuations in the first usable
release. Decoration alone neither registers model access nor
publishes an A2A skill or MCP tool.

**Signature status:** the `side_effects` / `idempotent` declarations are decided;
flag defaults/combinations and description fallback details remain open.

```text
@tool(description: str, side_effects: bool = ..., idempotent: bool = ...)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `description` | `str` | Explicit in current example; fallback/default open | Describes an eligible callable; does not authorize actions. |
| `side_effects` | `bool` | Explicit declaration supported; omitted semantics open | `True` marks consequential effects; never auto-retry these tools. |
| `idempotent` | `bool` | Explicit declaration supported; omitted semantics open | `True` declares idempotence; not an automatic retry grant or exactly-once guarantee. Combination/validation rules remain open. |

```python
from agentframe import tool


@tool(description="Convert Celsius to Fahrenheit.", idempotent=True)
def to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32
```

**Behavior / failures:** the proposed agent constructor accepts an explicit
`tools=[to_fahrenheit]` registration. No shell, filesystem, or network tools are
granted by default. Validate tool arguments, enforce action authorization and
content checks, and bound iterations before execution. Exact callable support,
registration types and detailed normalized layouts remain open; the
synchronous example does not settle synchronous *skill* support. Never
automatically replay side-effecting tools on retry.
Validated tool results feed generation continuations under the same budgets,
deadlines and outbound checks. Authors select registered tools via
`ctx.generate(..., tools=[to_fahrenheit])`. The defaults are 20 tool calls,
10 model attempts and 120 seconds per run; model retries use one layer with
two retries for transient provider failures only. Tool retry policy beyond
the prohibition on auto-retrying side effects remains open. Application
approval workflows are separate; no built-in human pause/resume in first
release. Future AG-UI tool visibility grants no frontend execution authority.

**Related:** [`Agent`](agents.md#agent), [`ExecutionPolicy`](authorization.md#executionpolicy),
[`MCPConfig`](hosting.md#mcpconfig), [tool design](../annotations.md#optional-tool-example).

## Python and Pydantic data contracts

These are existing Python/Pydantic constructs, **not AgentFrame decorators or
new public classes**. No AgentFrame-specific field language is proposed.

| Construct | Required/default status | Meaning and constraints |
| --- | --- | --- |
| Python parameter/return annotations | Needed for validated supported contracts; exact supported subset open | Describe business data; `RunContext` and `Inject[T]` are excluded from external input. |
| `BaseModel` | Optional | Defines structured validated data, not another agent superclass. |
| `Annotated[T, Field(...)]` | Optional | Adds Pydantic descriptions/constraints; defaults follow the chosen validation-library contract. |

```python
from typing import Annotated
from pydantic import BaseModel, Field


class SummaryInput(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
```

**Behavior / failures:** managed calls validate supported input/output contracts
and fail explicitly on invalid data. M1 uses Pydantic in lax mode (for example
`"5"` converts to `int`, and dicts convert to `BaseModel` arguments) with one
validator per skill built at definition time. Unknown or missing arguments raise
`InputValidationError`; a return value that fails its annotation raises
`OutputValidationError`. Messages list field locations and problems, never
submitted values. Python schemas do not establish standard
A2A parameter dispatch; media types are not full schemas. Structured A2A part
mapping and supported MCP schemas need version-specific verification.
Typed model structured generation is a first-release requirement, not merely
skill return validation. Use `ctx.generate(..., output=Summary)` for a validated
Pydantic result; supported schema subset remains open. A skill return annotation
alone does not configure the provider.

**Related:** [data-contract rationale](../annotations.md#small-data-contract-example),
[`Runtime.invoke`](runtime.md#runtimeinvoke), [hosting](hosting.md).
