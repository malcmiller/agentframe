# Agents and invocation context

[Reference index and status conventions](README.md). The decisions below are
approved contracts. `Agent`, `RunContext`, `Inject[T]` and `Services` are
implemented from source as of M1; M3 #35 adds model injection, agent
`instructions` / `model_settings` and the text form of `ctx.generate`; M4 #53
adds the structured `output=` form. M5 #60 adds tool registration
(`Agent(tools=[...])`) and per-call selection (`ctx.generate(..., tools=[...])`);
M5 #61 adds the bounded tool-calling loop and M5 #62 tool action
authorization. `AgentConfig` is not implemented yet.
Unspecified details remain open.
Models are dependencies, not a second kind of agent base class.

## `Agent`

**Purpose / decision:** the common base for behavior expressed as separately
decorated async skills. One valid declared or inherited skill is required;
there is no required abstract `handle()`.

**Proposed construction shape** (shown on an author-defined subclass):

```text
Echo(*, model: Model = ..., config: AgentConfig = ..., tools = ...)
```

`Echo` is defined below, not a library class. The `config` argument arrives
with its milestone. Implemented members:

| Member | Type | Default | Meaning |
| --- | --- | --- | --- |
| `Agent(*, model=None, tools=())` | keyword-only constructor (M3 #35, `tools` M5 #60) | `model=None`, `tools=()` | Stores the injected model as `self.model` and the registered tools as `self.tools`. Subclasses with their own `__init__` keep working without calling it; they simply have no model and no tools. |
| `model` | `Model \| None` | `None` | The model `ctx.generate` calls. Application-owned: AgentFrame never closes it. |
| `tools` | `Mapping[str, ToolDefinition]` (M5 #60) | empty | The registered [`@tool`](decorators.md#tool) definitions by tool name, read-only. |
| `limits` | `ClassVar[RunLimits]` | `RunLimits()` (all unset) | Agent-wide execution limits. Fields it sets override the library defaults; a skill or [per-run options](configuration.md#runoptions) override only the fields they set ([precedence](configuration.md#precedence)). |
| `instructions` | `ClassVar[str \| None]` (M3 #35) | `None` | Trusted shared instructions, sent as the first `system` message of every `ctx.generate` call. `None` or empty sends none. |
| `model_settings` | `ClassVar[ModelSettings]` (M3 #35) | `ModelSettings()` (all unset) | Default settings for this agent's model calls, merged field by field with the library defaults, skill and per-run settings ([precedence](configuration.md#precedence)). |
| `definition()` | class method returning the validated agent definition | — | Returns metadata built by `@agent`; raises `DefinitionError` if this exact class was not decorated. |

| Parameter/configuration | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `model` | `Model` implementation | Optional; no implicit provider | Needed only for generation. Model-free and mixed skills use this same base. |
| `config` | `AgentConfig` | Optional; exact constructor default open | Instance configuration refines class/library defaults, subject to host ceilings and security policy. |
| `tools` | `Iterable[Callable[..., Any]]` of `@tool` functions (implemented, M5 #60) | Optional; default `()` registers none | The tools this agent's `ctx.generate` calls may select. Registering grants nothing by itself: each call names the tools it offers. An undecorated function or two tools with the same name raise `DefinitionError` at construction. `Runtime.validate` (and so `AgentApp` startup), and every local `Runtime.run`, fail with `DefinitionError` when a registered tool's `Inject[T]` service is not registered. Tools are internal: never on the Agent Card, in skill lists, router candidates, MCP publication or future AG-UI frontend tools. |
| Class `instructions` | `str \| None` (implemented) | Optional; `None` sends no agent instructions | Trusted shared instructions. Typed configuration/trusted callable is also intended; callable signature and merge details open. |

```python
from agentframe import Agent, RunContext, agent, skill


@agent(name="Echo", description="Returns supplied text.", version="0.1.0")
class Echo(Agent):
    @skill(id="echo", name="Echo", description="Return text.", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        return text


echo = Echo()
```

**Behavior / failures:** preserve ordinary Python inheritance and immutable
authoring configuration. Do not keep mutable caller/conversation state on the
shared agent. Reject invalid skill definitions and duplicate IDs. Subclasses
inherit skills; redefining a skill requires `@skill` again, and an override
without it raises `DefinitionError` (see [`@skill`](decorators.md#skill)). Construction/import performs no inference,
download, server startup, or cloud fallback. Missing model configuration must
fail when generation is attempted, not when model-free work is invoked.
Outside `Services`, injected clients remain application-owned unless ownership
is transferred; library-created clients are library-owned. `Services` singleton
and scoped ownership follows the explicit rules below.

**Related:** [`@agent`](decorators.md#agent), [`@skill`](decorators.md#skill),
[`AgentConfig`](configuration.md#agentconfig),
[mixed-skill guide](../guides/agent-examples.md#mixing-both-kinds-of-skill).

## `RunContext`

**Purpose / decision:** fresh library-managed invocation context. Authors receive
it as `ctx: RunContext`; they do not construct/subclass it or accept it from
request arguments.

**Signature status:** applications do not construct it. M1 implements a frozen
dataclass with the attributes below; M3 #35 adds the text form of
[`generate`](#runcontextgenerate). The context also holds a private per-run
generation state (model, effective settings and limits, usage totals, call
count), so concurrent runs never share accounting. Further accessors remain open.

| Attribute/concern | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `invocation_id` | `str` (UUID) | Always set | Same value as `RunResult.invocation_id` and error `invocation_id`; correlation, never authentication. |
| `principal` | `Principal \| None` | Runtime-supplied; `None` when no identity was supplied | Verified immutable identity, required for protected work. Never supplied as business data or inferred from model output, roles, session IDs, or tracing. |
| `deadline` | `float` | Always set | Event-loop time (`loop.time()`) when the run's timeout expires, as used by `asyncio.timeout_at`. |
| Service injection | Skill parameters annotated `Inject[T]` | Services registered by trusted application | No untyped context service bag; injected parameters are excluded from caller input and all published schemas/cards. |
| Model access | Injected `Model` through `generate` | Model optional until used | No implicit model or fallback. No additional public model accessor is specified. |
| Cancellation, run state | Exact accessors/types open | Managed per invocation | Enforce effective limits and isolation; IDs/accounting work without tracing. |

```python
from agentframe import Agent, RunContext, agent, skill


@agent(name="Counter", description="Counts words.", version="0.1.0")
class Counter(Agent):
    @skill(id="count", name="Count", description="Count words.", tags=["text"])
    async def count(self, text: str, ctx: RunContext) -> int:
        return len(text.split())
```

**Behavior / failures:** unused context is valid for model-free skills.
The runtime creates and supplies it as part of managed invocation.
Attempts to pass `ctx` through invocation arguments fail; context must not be
reused as mutable global state. Context IDs do not authorize task/state access.
Use [`Inject[T]`](#injectt) for services and
[`Runtime.run` / `RunResult`](runtime.md#runtimerun) for invocation IDs, usage
and completion metadata independently of tracing. Exact additional context
accessors remain open.

Next-core-release discovery can inject an appropriately scoped `AgentDirectory`
through an `Inject[AgentDirectory]` parameter backed by `Services`.
`Remote["name"]` injects managed remote bindings resolved by
`AgentApp(directory=...)`, with configured names validated at startup and excluded
from caller/card/MCP schemas. No new `Agent` base class is implied. A singleton directory's
lifespan is application-owned, not invocation cleanup. See
[discovery injection](../design/discovery.md#agent-injection-and-separate-invocation).

**Related:** [`Runtime.invoke`](runtime.md#runtimeinvoke),
[`Principal`](authorization.md#principal),
[context responsibilities](../design/base-classes.md#agent-authoring-and-execution).

## `RunContext.generate`

**Purpose / decision:** generate through the injected model while preserving
instruction/data boundaries and invocation-wide policy/accounting.

**Decided call forms:** one method handles text, Pydantic output and explicitly
registered tools. The accepted `tools` collection type remains open; the names
`output` and `tools` and their behavior are decided.

```text
await ctx.generate(text, instructions=...) -> str       # implemented (M3 #35)
await ctx.generate(text, output=Summary) -> Summary     # implemented (M4 #53)
await ctx.generate(text, tools=[lookup_order]) -> str   # implemented (M5 #60, #61)
```

**Implemented signature (M3 #35, M4 #53, `tools` M5 #60/#61):** two
`typing.overload`s, so type checkers infer `str` without `output` and the
requested class with it.

```text
async def generate(
    self,
    text: str,
    *,
    instructions: str | None = None,
    tools: Iterable[Callable[..., Any]] = (),
) -> str
async def generate[T: BaseModel](
    self,
    text: str,
    *,
    instructions: str | None = None,
    output: type[T],
    tools: Iterable[Callable[..., Any]] = (),
) -> T
```

Messages are assembled in this order: the agent's `instructions` as a `system`
message, then the call's `instructions` as a second `system` message, then
`text` as a `user` message. Untrusted `text` is never merged into a `system`
message. This separation does not guarantee resistance to prompt injection.

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `text` | `str` | Required in current text proposal | Untrusted user content, never promoted to privileged instructions. |
| `instructions` | `str \| None` | Optional; default `None` sends no call instructions | Trusted call-specific instructions combine with shared agent instructions without replacing shared constraints. Must not interpolate untrusted content as trusted instructions. |
| `output` | `type[T]`, `T` a Pydantic `BaseModel` subclass (implemented, M4 #53) | Optional; `None` returns text | Sends the class's JSON schema as [`OutputSchema`](models.md#outputschema) and returns the reply validated with `model_validate_json` in Pydantic's default (lax) mode; a model's own `ConfigDict(strict=True)` is respected. `RootModel`, `list[...]` and other types raise `DefinitionError` before sending: wrap lists in a model field. Provider schema subsets are documented per adapter ([Foundry](models.md#structured-output), [Ollama](models.md#structured-output-ollama)). With `tools`, the final answer after the tool calls is validated (M5 #61). |
| `tools` | `Iterable[Callable[..., Any]]` of `@tool` functions registered on the agent (selection implemented, M5 #60) | Optional; default `()` offers no tools | Names the tools this call offers the model; omitting `tools=` or passing an empty selection offers none, even when the agent registers tools. A function not registered on the agent, or selected twice, raises `DefinitionError` naming the tool before anything is sent. The model's tool calls run within the run's limits until it answers; see [tool calling](#implemented-tool-calling-m5-61) and the [Foundry mapping](models.md#tool-calling). Decoration or a model suggestion alone does not grant execution. |

```python
from agentframe import Agent, RunContext, agent, skill


@agent(name="Writer", description="Summarizes text.", version="0.1.0")
class Writer(Agent):
    instructions = "Preserve the source meaning. Do not invent facts."

    @skill(id="summarize", name="Summarize", description="Summarize text.", tags=["text"])
    async def summarize(self, text: str, ctx: RunContext) -> str:
        return await ctx.generate(text, instructions="Use three sentences.")
```

With `output=`, the same skill returns typed data without parsing model text
or importing a provider SDK:

```python
from pydantic import BaseModel, Field

from agentframe import Agent, RunContext, agent, skill


class Summary(BaseModel):
    text: str = Field(min_length=1)
    key_points: list[str]


@agent(name="Summarizer", description="Summarizes text.", version="0.1.0")
class Summarizer(Agent):
    @skill(id="summarize", name="Summarize", description="Summarize text.", tags=["text"])
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        return await ctx.generate(text, output=Summary)
```

Construct `Writer(model=...)` with a configured [`FoundryModel`](models.md#foundrymodel),
[`OllamaModel`](models.md#ollamamodel), or structural `Model` implementation.
Tests can use [`ScriptedModel`](models.md#scriptedmodel):

```python
from agentframe import Runtime
from agentframe.testing import ScriptedModel

async def test_summarize() -> None:
    model = ScriptedModel(["A three-sentence summary."])
    summary = await Runtime().invoke(
        Writer(model=model), skill="summarize", arguments={"text": "Long text."}
    )
    assert summary == "A three-sentence summary."
```

**Implemented outcome mapping (M3 #35).** Each model call is counted, and its
usage is added to the run's totals (`RunResult.usage`, `RunResult.model_calls`)
before any error is raised. Unknown usage stays unknown, never zero.

| Situation | Result |
| --- | --- |
| A selected tool is not registered on the agent | `DefinitionError` naming the tool; nothing is sent (M5 #60) |
| Agent has no model | `UnsupportedCapabilityError` ("This agent has no model configured"); nothing is sent |
| `COMPLETED` with text | The text is returned |
| `COMPLETED` with empty text | `IncompleteOutputError` |
| `LENGTH` | `IncompleteOutputError` with `partial_text` set to the text received (M4 #54); never returned directly |
| `REFUSED` | `ModelRefusalError` |
| `CONTENT_FILTER` | `ModelRefusalError` (blocked by a content filter) |
| Model raises another `ModelError` (such as `ProviderError`) | Re-raised unchanged; the usage it carries, or unknown usage, is recorded |
| Model raises `UnsupportedCapabilityError` | Re-raised; not counted, because nothing was sent |
| Model reports no context window and no `max_input_tokens` is set | `DefinitionError`; nothing is sent (M3 #36) |
| Input, context window or remaining `max_total_tokens` too small for the call | `TokenLimitExceededError`; nothing is sent (M3 #36) |
| `max_model_calls` reached | `ModelCallLimitExceededError`; nothing is sent (M3 #36) |
| Run timeout expires during the call | The call is cancelled; `TimeoutLimitExceededError` |
| Run cancelled during the call | The call is cancelled |

**Implemented structured outcomes (M4 #53).** With `output=`, the rows above
still apply; these come first or are added:

| Situation | Result |
| --- | --- |
| `output` is not a `BaseModel` subclass, or is a `RootModel` | `DefinitionError`; nothing is sent or reserved |
| Model's `capabilities().structured_output` is `False` | `UnsupportedCapabilityError` ("The model does not support structured output"); nothing is sent or reserved, `model_calls` unchanged |
| Schema makes the call too large | The serialized schema counts toward the input estimate, so `TokenLimitExceededError` before sending |
| `COMPLETED` with JSON matching the schema | A validated instance of `output` |
| `COMPLETED` with invalid JSON or JSON that does not match | `OutputValidationError` listing field locations and problems, never the model's text (location parts that are not schema property names, such as model-chosen dictionary keys, appear as `<key>`); the call's usage is recorded; no repair or re-ask |
| `LENGTH` | `IncompleteOutputError` with `partial_text` `None`; the text is never parsed, even if it happens to be valid JSON |
| `REFUSED` / `CONTENT_FILTER` | `ModelRefusalError` |

Transient-provider retries are unchanged and never retry a validation failure.
Hosted, a returned instance becomes one A2A data part `{"value": {...}}`, and
`OutputValidationError` fails the task with its safe message.

#### Implemented tool calling (M5 #61)

With `tools=`, `ctx.generate` runs a small loop: call the model; if the
response asks for tool calls, append it as an `assistant` message (keeping its
`tool_calls` and `provider_items`), run each call one at a time in the model's
order, append each result as a `tool` message with the matching
`tool_call_id`, and call the model again. The first response without tool
calls is the final answer, handled by the tables above (text, or `output=`
validation). Tool results are untrusted content and only ever go in `tool`
messages, never `system` messages. The provider's parallel tool calling is not
disabled; parallel calls simply run sequentially.

```python
from agentframe import Agent, Inject, RunContext, agent, skill, tool


class OrderStore:  # Application-supplied placeholder.
    async def status(self, order_id: str) -> str: ...


@tool(description="Look up an order's status.", idempotent=True)
async def lookup_order(order_id: str, orders: Inject[OrderStore]) -> str:
    return await orders.status(order_id)


@agent(name="Orders", description="Answers order questions.", version="0.1.0")
class Orders(Agent):
    @skill(id="ask", description="Answer an order question.", tags=["orders"])
    async def ask(self, question: str, ctx: RunContext) -> str:
        return await ctx.generate(question, tools=[lookup_order])


# Orders(model=..., tools=[lookup_order]), run with
# Runtime(services=...) providing OrderStore.
```

Each tool call is validated and run like this: `call.arguments` (raw JSON) is
validated against the tool's parameters; `Inject[T]` services are resolved
from the run's scope, so a scoped service is shared with the skill and closed
when the run ends; `ctx` is passed only when the tool declares it; sync tools
are called inline and async tools awaited; the return value is validated
against the annotation and sent as JSON text.

| Situation | Result |
| --- | --- |
| The model calls a tool that was offered, with valid arguments | It runs; its JSON result is sent back and the loop continues |
| Several calls in one response | Run one at a time in the model's order; every result is sent back |
| Unknown tool name, or a tool that was not selected (including when none were) | Nothing runs; the model gets `{"error": "Unknown tool. ..."}` |
| A selected tool whose `requires_permissions` the caller lacks (M5 #62) | Never offered (left out of the request's `tools`); if the model calls it anyway, nothing runs and the run fails with `PermissionDeniedError`, checked before the arguments |
| Invalid JSON or arguments | Nothing runs; the model gets `{"error": "Invalid arguments: ..."}` naming field locations and problems, never values |
| The [tool authorization policy](authorization.md#toolauthorizationpolicy) returns anything but `True` (M5 #62) | Nothing runs; `PermissionDeniedError` with a generic message. The model is never told |
| The policy raises or misses the run's deadline (M5 #62) | Nothing runs; `PolicyUnavailableError` |
| The tool raises | The exception propagates unchanged and fails the run like a skill exception (hosted: generic `FAILED`, details logged with the `invocation_id`); its text is never sent to the model |
| The tool returns a value that does not match its annotation | `OutputValidationError` ("Tool '...' returned an invalid value: ...") |
| `max_tool_calls` reached | `ToolCallLimitExceededError` before the extra call runs. Every model-requested call counts, including unknown and invalid ones |
| A continuation would exceed `max_model_calls` or the token budget | `ModelCallLimitExceededError` / `TokenLimitExceededError`; each continuation is an ordinary counted, reserved model call |
| The run's timeout expires during a tool | The tool is cancelled; `TimeoutLimitExceededError`; scoped services are still cleaned up |
| The run is cancelled during a tool | The tool is cancelled and cancellation propagates; scoped services are cleaned up |
| The model does not report tool support | `UnsupportedCapabilityError`; nothing is sent |
| An [`ExecutionPolicy`](authorization.md#executionpolicy) model-request check blocks a request, including a continuation (M7 #77) | Nothing is sent and no tokens are reserved; `PermissionDeniedError` (`PolicyUnavailableError` when the check raises) |

Tools are never automatically retried; a model retry re-sends the same request,
which already holds earlier tool results. Each call is authorized after its
arguments are validated and before services are resolved or the tool runs; see
[`ToolAuthorizationPolicy`](authorization.md#toolauthorizationpolicy).

Hosted runs map a tool denial to task `REJECTED` and the other errors to task `FAILED` with their safe message and the
run's `invocation_id` (see [result and failure mapping](hosting.md#result-and-failure-mapping)). Token limits and
reservations work as described in
[implemented token accounting](../guides/limits.md#implemented-token-accounting-m3);
transient provider failures are
[retried](../guides/limits.md#retries-and-duplicate-requests) up to
`RunLimits.max_model_retries` times. Every call uses
the run's effective `model_settings`, merged field by field (see
[precedence](configuration.md#precedence)).

**Behavior / failures:** apply outbound checks to every model request, including
continuations; reserve/reconcile token budgets and count dispatched attempts
including retries. Unsupported requested features/settings, missing model,
input/context overflow, exhausted budgets, provider failure, deadline, and
cancellation must be explicit failures. Do not truncate silently or switch
models. AgentFrame owns normalized messages, tool calls/results, token usage
and finish reasons; adapters translate provider types. Unsupported requested
capability raises `UnsupportedCapabilityError` before sending. Refusal raises
`ModelRefusalError`, never fabricated text; token-limit truncation raises
`IncompleteOutputError`; a skill opts into truncated text by catching it and
reading `partial_text`, which cannot bypass Pydantic/output validation.
Invalid structured output raises `OutputValidationError`. Tools use the same
run limits, authorization and retry rules. See the
[examples](../guides/agent-examples.md#typed-structured-output-and-bounded-tools).
Streaming is later with [optional AG-UI](streaming.md), not an implicit provider
or A2A capability. Instructions do not authorize actions or guarantee injection
resistance. Cancellation does not guarantee no provider charges.

**Related:** [`Model.generate`](models.md#modelgenerate),
[`RunLimits`](configuration.md#runlimits),
[`ExecutionPolicy`](authorization.md#executionpolicy),
[instruction design](../guides/annotations.md#proposed-authoring-shape).

## `Inject[T]`

**Purpose / decision:** mark a skill parameter as a trusted service dependency,
not caller-supplied business data.

**Signature status:** decided and implemented in M1 as a PEP 695 type alias over
`Annotated[T, marker]`, so type checkers see `T`. It is not a service constructor.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `T` | Service type/key | Required | Resolve from `Services`; missing registration fails startup with `DefinitionError`. Never emit the parameter in Agent Cards, MCP schemas or caller arguments. |

```python
from typing import Protocol
from agentframe import Agent, Inject, RunContext, agent, skill


class DocumentStore(Protocol):
    async def publish(self, doc_id: str) -> str: ...


@agent(name="Writer", description="Publishes documents.", version="0.1.0")
class Writer(Agent):
    @skill(id="publish", name="Publish", description="Publish a document.", tags=["docs"])
    async def publish(
        self, doc_id: str, store: Inject[DocumentStore], ctx: RunContext,
    ) -> str:
        return await store.publish(doc_id)
```

`DocumentStore` is application-defined. Production publishing also needs skill
permissions and current resource/action policy; DI is not authorization.

**Behavior / failures:** resolve and validate service registrations before
serving/invocation; reject caller attempts to supply injected parameters with
`InputValidationError`. Local `Runtime(services=...)` uses the same injection;
in M1 it checks every `Inject[T]` registration before running the skill and raises
`DefinitionError` naming the parameter if one is missing.

**Related:** [`Services`](#services), [`Runtime`](runtime.md#runtime),
[ownership rationale](../design/base-classes.md#authoring-and-extension-decisions).

## `Services`

**Purpose / decision:** application registrations for singleton and per-run
services. `AgentApp(services=...)` and `Runtime(services=...)` accept this container.

**Decided construction:** `Services()`; no constructor parameters are selected.

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Registrations | Service type to instance or scoped factory | Explicit application registration | Missing required services fail at startup, not during skill execution. App owns/closes singleton services; AgentFrame closes only scoped instances it creates. |

```python
from agentframe import Runtime, Services


services = Services()
runtime = Runtime(services=services)
```

**Behavior / failures:** registrations cannot be populated from request data.
For local use the application owns singleton lifetime too; a run never closes
shared singletons. Registering the same type twice, across `add` and
`add_scoped`, raises `DefinitionError`. M1 also provides
`is_registered(service_type) -> bool`. A startup/preflight API for hosts remains
open.

**Related:** [`Inject[T]`](#injectt), [`AgentApp`](hosting.md#agentapp).

## `Services.add`

**Purpose / decision:** register an app-wide singleton.

**Decided call:** `services.add(DocumentStore, my_store)`; returns `None`.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Service type | `T` | Required | Key matched by `Inject[T]`. |
| Instance | Service implementation | Required | Application owns/closes the singleton; no per-run cleanup by AgentFrame. |

```python
from agentframe import Services


def register_store[T](services: Services, store_type: type[T], my_store: T) -> None:
    services.add(store_type, my_store)
```

This generic registration helper receives an application type/instance; it does
not settle runtime compatibility checks or permit arbitrary mismatched services.

**Behavior / failures:** validate required registrations before running.
A duplicate registration raises `DefinitionError`; there is no replacement.

**Related:** [`Services`](#services), [`Inject[T]`](#injectt).

## `Services.add_scoped`

**Purpose / decision:** register a factory for a fresh service per run, closed
automatically afterward by AgentFrame.

**Decided call:** `services.add_scoped(DbSession, make_session)`; returns `None`.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Service type | `T` | Required | Key matched by `Inject[T]`. |
| Factory | Zero-argument sync function, async function, or async generator function | Required | Created once per type per run; only AgentFrame-created scoped instances are closed by AgentFrame. For a generator, code after its single `yield` is cleanup and always runs after success, skill failure or timeout. |

```python
from collections.abc import AsyncIterator
from agentframe import Services


class DbSession:
    async def close(self) -> None: ...


async def open_session() -> AsyncIterator[DbSession]:
    session = DbSession()
    yield session
    await session.close()


services = Services()
services.add_scoped(DbSession, open_session)
```

`DbSession` is an application-supplied placeholder. Unlike
`contextlib.asynccontextmanager`, the run's exception is not thrown into the
generator, so cleanup after `yield` runs without `try`/`finally`; a generator
that yields more than once raises `DefinitionError`. Factories take no
arguments; factory dependency resolution remains open.

**Behavior / failures:** never reuse a scoped instance across runs; preflight
missing service registrations before accepting work. Factory execution failures
must remain explicit; startup validation is not proof every later resource is available.

**Related:** [`Services`](#services), [`RunContext`](#runcontext).
