# Decorators and data contracts

[Reference index and status conventions](README.md). Approved fields below are
decisions. `@agent` and `@skill` are implemented from source as of M1 (model-free,
local `Runtime` only); `@tool` and `ToolDefinition` are implemented as of M5
(#58), without agent registration or tool execution yet. Unspecified details
remain illustrative.
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
[metadata design](../guides/annotations.md#proposed-decorators).

## `@skill`

**Purpose / decision:** register one async method under a stable skill ID, with
descriptive A2A metadata and optional mandatory all-of permission requirements.

**Signature status:** decided and implemented in M1 (2026-11-11). Since M2
(2026-11-12) `tags` must contain at least one non-blank tag; the `()` default
remains only so an omitted value raises a clear `DefinitionError`.

```text
@skill(
    id: str, *, name: str | None = None, description: str | None = None,
    tags: Iterable[str] = (), requires_permissions: Iterable[str] = (),
    limits: RunLimits | None = None, model_settings: ModelSettings | None = None,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `id` | `str` | Required, non-empty stable ID | Unique among registered skills, including inheritance; not necessarily the Python method name. |
| `name` | `str` | Optional; defaults to the title-cased method name | Human-readable advertised skill name (`find_sources` -> `"Find Sources"`). |
| `description` | `str` | Optional; defaults to the method docstring | Describes the ability. No explicit value and no docstring raises `DefinitionError`. |
| `tags` | Iterable of `str` (lists in examples) | Required: at least one non-blank tag (decided 2026-11-12, replacing M1's empty default) | Descriptive discovery tags, not execution instructions or permissions. A2A 1.0 marks `AgentSkill.tags` REQUIRED and required arrays must contain at least one element (spec §5.7), so omitted, empty or blank tags raise `DefinitionError` naming the agent and skill when `@agent` validates the class. A single string is rejected. |
| `requires_permissions` | Iterable of `str` (lists in examples) | Optional; default empty | All listed permissions required. Application-defined exact strings; no inferred wildcard/role semantics. A single string is rejected so `"admin"` cannot become characters. |
| `limits` | `RunLimits` | Optional per-skill overrides | Each field it sets overrides the agent's value; unset fields are inherited (M3 #42, [precedence](configuration.md#precedence)). Per-run options override it in turn. |
| `model_settings` | `ModelSettings` | Optional per-skill overrides (M3 #42) | Merged field by field like `limits`: overrides the agent's `model_settings` only for the fields it sets. |

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
unsupported annotations, missing or blank tags, and duplicate IDs. Undecorated helpers are not skills;
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
[registration versus routing](../guides/annotations.md#skill-registration-versus-request-routing).

## `@tool`

**Purpose / decision:** mark a typed function as an internal model-callable
tool, optional per agent. Decoration validates the function and attaches a
[`ToolDefinition`](#tooldefinition); it neither registers the tool with an
agent, offers it to a model, nor publishes an A2A skill or MCP tool.

**Signature status:** decided and implemented in M5 (#58, decisions confirmed
2026-11-12). Agent registration through `Agent(tools=[...])` and
`ctx.generate(..., tools=[...])` selection are implemented (#60), and so is the
bounded [tool loop](agents.md#implemented-tool-calling-m5-61) (#61) and tool
authorization with `requires_permissions` (#62). Tools may be sync (called inline)
or async, and receive `ctx` only when they declare a `ctx: RunContext`
parameter.

```text
@tool(
    *, description: str | None = None, side_effects: bool = False,
    idempotent: bool = False, requires_permissions: Iterable[str] = (),
)
```

Always call it with parentheses, `@tool()` or `@tool(description=...)`; bare
`@tool` is not supported.

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `description` | `str` | Optional; defaults to the function docstring | Tells the model what the tool does; does not authorize actions. No explicit value and no docstring raises `DefinitionError`. |
| `side_effects` | `bool` | Optional; default `False` | `True` marks consequential effects, such as cancelling an order. |
| `idempotent` | `bool` | Optional; default `False` | `True` declares that repeating a call has no further effect. Not a retry grant or exactly-once guarantee. May be combined with `side_effects=True`. |
| `requires_permissions` | `Iterable[str]` | Optional; default empty | Permissions the caller must all hold, as on [`@skill`](#skill). A tool the caller lacks any of is not offered to the model and fails the run with `PermissionDeniedError` if called anyway. A single string raises `DefinitionError`. |

```python
from typing import Literal, Protocol

from pydantic import BaseModel

from agentframe import Inject, tool


class OrderStatus(BaseModel):
    order_id: str
    status: Literal["open", "shipped", "cancelled"]


class OrderStore(Protocol):
    async def status(self, order_id: str) -> OrderStatus: ...
    async def cancel(self, order_id: str) -> None: ...


@tool(description="Look up an order's status.", idempotent=True)
async def lookup_order(order_id: str, orders: Inject[OrderStore]) -> OrderStatus:
    return await orders.status(order_id)


@tool(
    description="Cancel an order.",
    side_effects=True,
    requires_permissions=["orders:write"],
)
async def cancel_order(order_id: str, orders: Inject[OrderStore]) -> None:
    await orders.cancel(order_id)


@tool(idempotent=True)
def to_fahrenheit(celsius: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return celsius * 9 / 5 + 32
```

`OrderStore` is application-defined and registered through
[`Services`](agents.md#services); the tool receives the run's instance (the same
scoped instances as the skill). It is not model input.

**Behavior / failures:** the decorator returns the same function, so it stays
directly callable and testable. Tools are module-level functions (nested
functions are accepted) or static methods with `@tool` written above
`@staticmethod`; bound or unbound methods, lambdas and other callables are
rejected. `async def` and plain `def` are both accepted; sync tools run inline
on the event loop, so use `async def` for any I/O. Generator and async generator
functions are rejected; a tool returns one value. Every parameter and the
return need annotations; `*args`, `**kwargs` and positional-only parameters
are rejected. Parameters annotated `Inject[T]` are resolved from the run's
services, and an optional `ctx: RunContext` parameter gives access to
`principal` and `invocation_id`; neither appears in the argument schema. A
business parameter cannot be named `principal`. The tool name is the function
name and must be 1-64 ASCII letters, digits, `_` or `-` (the whole name,
`[a-zA-Z0-9_-]{1,64}`). Every violation raises
`DefinitionError` naming the tool when the function is decorated (import time).

Declared permissions say a caller may use a tool at all; they do not check the
resource. For consequential tools, such as `cancel_order` above, also supply a
[`ToolAuthorizationPolicy`](authorization.md#toolauthorizationpolicy) that
checks the actual arguments (for example, that the caller owns the order).
Neither is mandatory for `side_effects=True`, but both are strongly advised.

No tool is ever retried automatically, whatever its flags (#63), so a forgotten
flag cannot cause a replay. No shell, filesystem, or network tools are granted by
default. Validated tool results will feed generation continuations under the
same budgets, deadlines and outbound checks; defaults are 20 tool calls, 10 model
attempts and 120 seconds per run. Application approval workflows are separate;
no built-in human pause/resume in first release. Future AG-UI tool visibility
grants no frontend execution authority.

**Related:** [`Agent`](agents.md#agent), [`ExecutionPolicy`](authorization.md#executionpolicy),
[`MCPConfig`](hosting.md#mcpconfig), [tool design](../guides/annotations.md#optional-tool-example).

## `ToolDefinition`

**Purpose / decision:** the validated, immutable description of a `@tool`
function, built at decoration time. Later stages (registration, the tool loop,
authorization) read it; applications rarely construct one directly.

**Signature status:** implemented in M5 (#58) as a frozen dataclass exported
from `agentframe`.

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | `str` | The function name, the tool name a model calls. |
| `description` | `str` | Explicit description or the docstring. |
| `side_effects` | `bool` | As declared on `@tool`. |
| `idempotent` | `bool` | As declared on `@tool`. |
| `requires_permissions` | `frozenset[str]` | As declared on `@tool`; empty when none. |
| `function` | callable | The decorated function (the underlying function for a static method). |
| `takes_context` | `bool` | Whether the function has a `ctx: RunContext` parameter. |
| `argument_names` | `tuple[str, ...]` | Business parameters, in declaration order. |
| `injected` | `dict[str, object]` | `Inject[T]` parameters mapped to their service types. |
| `arguments_adapter` | `TypeAdapter` | Validates business arguments; unknown names are rejected. |
| `return_adapter` | `TypeAdapter` | Validates the return value. |

`json_schema()` returns the JSON Schema of the business arguments: the Pydantic
schema of those parameters, never including `ctx` or `Inject[T]` parameters.

Read the definitions of an agent's registered tools from its read-only
[`tools`](agents.md#agent) mapping, keyed by tool name:

```python
from agentframe import Agent, RunContext, agent, skill, tool


@tool(description="Convert Celsius to Fahrenheit.")
def to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32


@agent(version="0.1.0")
class Weather(Agent):
    """Answers weather questions."""

    @skill("ask", tags=["weather"])
    async def ask(self, question: str, ctx: RunContext) -> str:
        """Answer a weather question."""
        return await ctx.generate(question, tools=[to_fahrenheit])


weather = Weather(tools=[to_fahrenheit])
schema = weather.tools["to_fahrenheit"].json_schema()
assert schema["required"] == ["celsius"]
```

**Related:** [`@tool`](#tool), [`Inject[T]`](agents.md#injectt).

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
Pydantic result; each model adapter documents its provider's schema subset. A skill return annotation
alone does not configure the provider.

**Related:** [data-contract rationale](../guides/annotations.md#small-data-contract-example),
[`Runtime.invoke`](runtime.md#runtimeinvoke), [hosting](hosting.md).
