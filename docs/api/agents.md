# Agents and invocation context

[Reference index and status conventions](README.md). The decisions below are
approved contracts, not an installed SDK. Unspecified details remain open.
Models are dependencies, not a second kind of agent base class.

## `Agent`

**Purpose / decision:** the common base for behavior expressed as separately
decorated async skills. One valid declared or inherited skill is required;
there is no required abstract `handle()`.

**Proposed construction shape** (shown on an author-defined subclass):

```text
Echo(*, model: Model = ..., config: AgentConfig = ..., tools = ...)
```

`Echo` is defined below, not a library class. Exact base constructor, accepted
containers, and omission representations remain open.

| Parameter/configuration | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `model` | `Model` implementation | Optional; no implicit provider | Needed only for generation. Model-free and mixed skills use this same base. |
| `config` | `AgentConfig` | Optional; exact constructor default open | Instance configuration refines class/library defaults, subject to host ceilings and security policy. |
| `tools` | Explicit collection of decorated callables; type open | Optional per agent; first-release bounded execution/continuations | Supplies internal model tools, not A2A skills, MCP publication or future AG-UI frontend tools. No implicit grants. |
| Class `instructions` | `str` in existing examples | Optional class default; omission semantics open | Trusted shared instructions. Typed configuration/trusted callable is also intended; callable signature and merge details open. |

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
shared agent. Reject invalid skill definitions and duplicate IDs; explicit
override semantics remain open. Construction/import performs no inference,
download, server startup, or cloud fallback. Missing model configuration must
fail when generation is attempted, not when model-free work is invoked.
Outside `Services`, injected clients remain application-owned unless ownership
is transferred; library-created clients are library-owned. `Services` singleton
and scoped ownership follows the explicit rules below.

**Related:** [`@agent`](decorators.md#agent), [`@skill`](decorators.md#skill),
[`AgentConfig`](configuration.md#agentconfig),
[mixed-skill guide](../agent-examples.md#mixing-both-kinds-of-skill).

## `RunContext`

**Purpose / decision:** fresh library-managed invocation context. Authors receive
it as `ctx: RunContext`; they do not construct/subclass it or accept it from
request arguments.

**Signature status:** no public constructor is specified. The known public
method is [`generate`](#runcontextgenerate); context accessor details beyond the
illustrated attributes below remain open.

| Attribute/concern | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `principal` | Verified immutable `Principal` when established; absent representation open | Runtime-supplied; required for protected work | Never supplied as business data or inferred from model output, roles, session IDs, or tracing. |
| Service injection | Skill parameters annotated `Inject[T]` | Services registered by trusted application | No untyped context service bag; injected parameters are excluded from caller input and all published schemas/cards. |
| Model access | Injected `Model` through `generate` | Model optional until used | No implicit model or fallback. No additional public model accessor is specified. |
| Deadline, cancellation, correlation, run state | Exact accessors/types open | Managed per invocation | Enforce effective limits and isolation; IDs/accounting work without tracing. |

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
through an `Inject[AgentDirectory]` parameter backed by `Services`; no new
`Agent` constructor keyword or base class is implied. A singleton directory's
lifespan is application-owned, not invocation cleanup. See
[discovery injection](../discovery.md#agent-injection-and-separate-invocation).

**Related:** [`Runtime.invoke`](runtime.md#runtimeinvoke),
[`Principal`](authorization.md#principal),
[context responsibilities](../base-classes.md#agent-authoring-and-execution).

## `RunContext.generate`

**Purpose / decision:** generate through the injected model while preserving
instruction/data boundaries and invocation-wide policy/accounting.

**Decided call forms:** one method handles text, Pydantic output and explicitly
registered tools. Exact overload spelling and accepted collection types remain
open; the names `output` and `tools` and their behavior are decided.

```text
await ctx.generate(text, instructions=...) -> str
await ctx.generate(text, output=Summary) -> Summary
await ctx.generate(text, tools=[lookup_order]) -> str
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `text` | `str` | Required in current text proposal | Untrusted user content, never promoted to privileged instructions. |
| `instructions` | `str` in current examples | Optional specialization; concrete default open | Trusted call-specific instructions combine with shared agent instructions without replacing shared constraints. Must not interpolate untrusted content as trusted instructions. |
| `output` | Pydantic model class | Optional; omitted returns text | Return a validated instance of the requested class; unsupported structured capability fails before sending. Schema subset and combinations with tools remain open. |
| `tools` | Explicit collection of registered `@tool` functions | Optional; no implicit tools | Select authorized registered functions for bounded calls/results and continuations. Decoration or a model suggestion alone does not grant execution. |

```python
from agentframe import Agent, RunContext, agent, skill


@agent(name="Writer", description="Summarizes text.", version="0.1.0")
class Writer(Agent):
    instructions = "Preserve the source meaning. Do not invent facts."

    @skill(id="summarize", name="Summarize", description="Summarize text.", tags=["text"])
    async def summarize(self, text: str, ctx: RunContext) -> str:
        return await ctx.generate(text, instructions="Use three sentences.")
```

Construct `Writer(model=...)` with a configured [`FoundryModel`](models.md#foundrymodel),
[`OllamaModel`](models.md#ollamamodel), or structural `Model` implementation.

**Behavior / failures:** apply outbound checks to every model request, including
continuations; reserve/reconcile token budgets and count dispatched attempts
including retries. Unsupported requested features/settings, missing model,
input/context overflow, exhausted budgets, provider failure, deadline, and
cancellation must be explicit failures. Do not truncate silently or switch
models. AgentFrame owns normalized messages, tool calls/results, token usage
and finish reasons; adapters translate provider types. Unsupported requested
capability raises `UnsupportedCapabilityError` before sending. Refusal raises
`ModelRefusalError`, never fabricated text; token-limit truncation raises
`IncompleteOutputError` unless the skill explicitly opts into partial results.
That opt-in syntax is open and cannot bypass Pydantic/output validation.
Invalid structured output raises `OutputValidationError`. Tools use the same
run limits, authorization and retry rules. See the
[examples](../agent-examples.md#typed-structured-output-and-bounded-tools).
Streaming is later with [optional AG-UI](streaming.md), not an implicit provider
or A2A capability. Instructions do not authorize actions or guarantee injection
resistance. Cancellation does not guarantee no provider charges.

**Related:** [`Model.generate`](models.md#modelgenerate),
[`RunLimits`](configuration.md#runlimits),
[`ExecutionPolicy`](authorization.md#executionpolicy),
[instruction design](../annotations.md#proposed-authoring-shape).

## `Inject[T]`

**Purpose / decision:** mark a skill parameter as a trusted service dependency,
not caller-supplied business data.

**Signature status:** `Inject[T]` annotation is decided; its internal typing
implementation remains open. It is not a service constructor.

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
`InputValidationError`. Local `Runtime(services=...)` uses the same injection.
Exact factory signatures and disposal protocol remain open.

**Related:** [`Services`](#services), [`Runtime`](runtime.md#runtime),
[ownership rationale](../base-classes.md#authoring-and-extension-decisions).

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
For local use the application owns singleton lifetime too; do not make a run
close shared singletons. Startup/preflight API, factory async support and exact
close protocol remain open, not implicit public methods.

**Related:** [`Inject[T]`](#injectt), [`AgentApp`](hosting.md#agentapp).

## `Services.add`

**Purpose / decision:** register an app-wide singleton.

**Decided call:** `services.add(DocumentStore, my_store)`; return value unspecified.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Service type | `T` | Required | Key matched by `Inject[T]`. |
| Instance | Service implementation | Required | Application owns/closes the singleton; no per-run cleanup by AgentFrame. |

```python
from agentframe import Services


def register_store(services: Services, store_type: type, my_store: object) -> None:
    services.add(store_type, my_store)
```

This generic registration helper receives an application type/instance; it does
not settle runtime compatibility checks or permit arbitrary mismatched services.

**Behavior / failures:** validate required registrations at startup.
Duplicate-registration/replacement rules remain open.

**Related:** [`Services`](#services), [`Inject[T]`](#injectt).

## `Services.add_scoped`

**Purpose / decision:** register a factory for a fresh service per run, closed
automatically afterward by AgentFrame.

**Decided call:** `services.add_scoped(DbSession, make_session)`; factory and return
typing remain open.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Service type | `T` | Required | Key matched by `Inject[T]`. |
| Factory | Callable producing a run-scoped service | Required | New per run; only AgentFrame-created scoped instances are closed by AgentFrame, including failure/cancellation cleanup. |

```python
from collections.abc import Callable
from agentframe import Services


def register_session(
    services: Services, session_type: type, make_session: Callable[[], object],
) -> None:
    services.add_scoped(session_type, make_session)
```

`session_type` and factory are application-supplied. Exact factory dependency
resolution and synchronous/asynchronous disposal interfaces are still open.

**Behavior / failures:** never reuse a scoped instance across runs; preflight
missing service registrations before accepting work. Factory execution failures
must remain explicit; startup validation is not proof every later resource is available.

**Related:** [`Services`](#services), [`RunContext`](#runcontext).
