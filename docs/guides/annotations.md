# Decorators and type annotations

These are design contracts, not implemented APIs; explicitly decided syntax
and defaults are recorded in the reference. AgentFrame uses ordinary Python decorators
for metadata and registration, and Python type annotations for data contracts.

Decorators are AgentFrame-owned, not re-exports from another agent framework.
They describe behavior; the `Agent` subclass implements it. Configuration and
dependency injection supply instructions, models, policies, and infrastructure.
Do not add provider-specific decorators such as `@foundry` or `@llama`.

## Proposed decorators

| Decorator | Applies to | Definition |
| --- | --- | --- |
| `@agent(...)` | An `Agent` subclass | Declares name, description, version, and optional documentation/provider identity. Collects explicitly declared skills without performing I/O. |
| `@skill(...)` | An individual async agent method | Registers A2A metadata, optional `requires_permissions` and per-skill `limits=RunLimits(...)`. Not a per-method RPC endpoint. |
| `@tool(...)` | An explicitly registered callable | Makes a typed callable eligible for first-release bounded model tool execution and generation continuations. Optional for an individual agent; not an A2A skill, MCP publication or authorization grant. |

Require a non-empty agent description, an explicit agent version, and stable,
unique skill IDs. Docstrings may supply descriptions when unambiguous. Reject
duplicate IDs, unsupported annotations, and malformed metadata instead of
silently dropping declarations.

Agent decorators should preserve class identity, normal inheritance, signatures,
and Python introspection. Inheritance and overrides must have documented,
deterministic behavior; no process-global auto-registration is required.

An agent must have at least one valid decorated skill, defined directly or
inherited. There is no required abstract `handle()` method. Each skill has its
own implementation; do not stack several skill declarations on a single
mandatory handler. Undecorated helpers are not registered skills.

## Permission requirements

Use `@skill(..., requires_permissions=["documents:publish"])` to declare
all-of permission requirements. Roles are application-owned permission groupings,
not hardcoded skill decorators. The runtime evaluates trusted caller grants
and application resource policy before execution; the decorator alone provides
no enforcement. An empty list does not make a remote skill public.

Caller identity is runtime-supplied as `ctx.principal`, never a user/model
argument. Permission requirements are distinct from MCP publication allowlists
and internal tool registration. See [authorization](authorization.md) for a
complete skill example, anonymous-access boundaries, and inherited requirements.

## Type annotations

- Standard Python types describe local parameters and return values.
- Pydantic `BaseModel` types describe structured, validated data.
- `Annotated[T, Field(...)]` supplies descriptions and constraints without a
  separate AgentFrame validation language.
- `RunContext` is runtime-injected context, never a user-supplied model/tool argument.
- `Inject[T]` marks trusted service parameters. Register through `Services.add`
  or `add_scoped`; exclude them from caller arguments, cards and MCP schemas.
  Missing services fail startup with `DefinitionError`.
- M8+ `Remote[Literal["name"]]` marks managed remote bindings, likewise excluded from
  caller/card/MCP schemas. Directory names are validated at startup; `call`
  uses structured A2A and `ask` plain text, never an implicit tool registration.
- Every skill accepts a typed `ctx: RunContext`, even when it does not use a model.
  Context also carries deadlines, cancellation, caller information, and dependencies.
- Async skill methods are the primary execution path. Support for synchronous
  skills must document blocking and cancellation behavior before release.

Agent Card input/output modes are media types, not complete Python schemas.
If typed schema discovery is added later, use a documented, negotiated extension
rather than claiming that a standard A2A skill encodes method parameters.

## Proposed authoring shape

This is illustrative design syntax, not executable code from a released package.
This short example has one skill. See the [full agent examples](agent-examples.md)
for multiple skills, model-free agents, and model injection.

```python
from agentframe import Agent, RunContext, agent, skill


@agent(
    name="Summarizer",
    description="Summarizes supplied text.",
    version="0.1.0",
)
class Summarizer(Agent):
    instructions = "Summarize faithfully. Do not invent missing facts."

    @skill(
        id="summarize",
        name="Summarize text",
        description="Produce a concise summary of supplied text.",
        tags=["text", "summary"],
    )
    async def summarize(self, text: str, ctx: RunContext) -> str:
        return await ctx.generate(
            text,
            instructions="Summarize the supplied text in three sentences.",
        )
```

The host converts supported A2A parts into the selected skill's input and converts
the validated return into native A2A output. Unsupported part types fail explicitly.
The text argument represents supplied data; the model adapter should keep it
separate from agent instructions rather than concatenate everything into a
single privileged prompt.

A model-free skill can return a computed value without using `ctx.generate`.
Model configuration is supplied when constructing the agent, not encoded in
`@agent` or provider-specific decorators.

Shared instructions and trusted skill-specific instructions form the effective
generation instructions; the latter specialize the task without replacing
shared constraints. Supplied text remains user content, not privileged
instructions. Execution policy is enforced independently of prompt wording.
Calling `ctx.generate()` without a configured model fails explicitly.

## Skill registration versus request routing

The runtime registers decorated methods by stable skill ID, derives their local
argument/output contracts, and validates invocation through a common pipeline.
The proposed local `Runtime.invoke(agent, skill=..., arguments=...)` selects an
ID explicitly; Python method names need not equal skill IDs. Direct calls such
as `await instance.summarize(...)` are ordinary Python calls and bypass runtime
validation, policy, context creation, and execution limits.

A2A skill metadata alone does not define remote dispatch. Prefer a structured
request naming a registered skill with JSON arguments in a native data part,
under an A2A 1.0 extension declared on the Agent Card and activated by the caller.
That selection is deterministic and bypasses the router/model-selection cost,
not typed validation, authorization, limits, accounting or tracing. Unknown IDs
and invalid arguments fail explicitly; do not silently reroute them as prompts.

Model-backed routing is distinct from model-backed skill execution. Neither
an injected model nor a decorated skill implicitly enables model selection,
tool access, or multi-skill orchestration. Free-text callers, including clients
not using the extension, use a configured custom router (M2) or opt-in built-in
[`ModelRouter`](../api/hosting.md#modelrouter) (M6 #68). It selects an eligible skill and extracts validated business
arguments; ambiguity or missing input asks back via an A2A
`TASK_STATE_INPUT_REQUIRED` task, never guesses. A matching single skill still
needs valid arguments.

The extension's [wire format](../api/hosting.md#structured-skill-call-wire-format)
is implemented (M2 #5), and so are the router decision and clarification
(`SkillCall` / `Clarification`, answered in the same task; M2 #6, M6 #68). Local
`skill=` is not a standard A2A wire field, and this extension is not claimed as
an official standard skill-dispatch API. See the [verified A2A 1.0 extension,
data-part and task-state mechanisms](../design/mcp-skills.md#a2a-request-styles-and-extension-boundary).
The optional [MCP adapter](../design/mcp-skills.md) instead uses native named tools for
allowlisted skills, empty by default. Tool-like deterministic/typed/quick skills
may also use MCP; agent-like multi-turn/judgment/clarification skills usually
stay A2A-only. This is guidance, not an enforced classifier.

## Small data-contract example

Use existing validation types instead of an AgentFrame-specific field system.
This schema illustrates a local input contract; the A2A structured-part mapping
must be specified before advertising this as an interoperable remote input.

```python
from typing import Annotated
from pydantic import BaseModel, Field


class SummaryInput(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
```

## Optional tool example

Explicitly registered tools, bounded execution and generation continuations are
part of the first usable release; individual agents can still omit tools.
Registration is explicit; decoration alone does not grant model access or
execution permission. This callable is not automatically an advertised A2A skill.
`@tool` and `ToolDefinition` are implemented (M5 #58), as are registration with
`Agent(tools=[...])` and per-call selection with `ctx.generate(..., tools=[...])`
(M5 #60). Tool execution is implemented too: `ctx.generate` runs the model's
tool calls until it answers, within `max_tool_calls` (#61), authorizes each call
before it runs (#62) and never replays or auto-retries a tool (#63); see
[`examples/orders.py`](../../examples/orders.py).

```python
from agentframe import tool


@tool(description="Convert Celsius to Fahrenheit.", idempotent=True)
def to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32
```

Decoration validates the function immediately and returns it unchanged, so it
stays directly callable. Tools are module-level (or static) functions, `async`
or sync; sync tools run inline on the event loop, so use `async def` for I/O.
Like skills, tools take `Inject[T]` services and an optional `ctx: RunContext`,
which never appear in the argument schema the model sees. `description=`
defaults to the docstring; one is required. `side_effects` and `idempotent`
both default to `False` and may both be `True`. See [`@tool`](../api/decorators.md#tool).

The proposed construction shape is `Summarizer(model=model, tools=[to_fahrenheit])`.
The runtime must validate arguments, enforce policy, and bound tool iterations.
Instruction text is not an authorization mechanism.
Tool results feed subsequent generation under the same deadline/token/attempt
budget and outbound checks. Use `ctx.generate(..., tools=[to_fahrenheit])`
to select registered functions. Defaults are 20 tool calls, 10 model attempts,
100000 total tokens and 120 seconds. No tool is ever retried automatically,
whatever its flags; `idempotent=True` is not a retry grant. Model retries are
transient-only, default two with backoff and provider-SDK retries disabled.
Normalized message and tool-call layouts are settled ([`ToolCall`](../api/models.md#toolcall), M5 #59).
Human approval workflows are application-owned, not built-in first-release
pause/resume.

Typed structured model output is also first-release scope, not merely a
Pydantic schema example: `ctx.generate(..., output=Summary)` returns a validated
instance. Unsupported requested capability fails before sending; refusal raises
`ModelRefusalError` and truncation `IncompleteOutputError` by default. A skill
that accepts truncated text catches the error and reads `.partial_text` (text
only); each adapter documents its provider's schema subset; see
[structured generation examples](agent-examples.md#typed-structured-output-and-bounded-tools).
Return complete validated results first; future [streaming/AG-UI](../design/streaming.md)
does not authorize partial output or automatic frontend tools.

Publishing a skill as an MCP tool is a separate host allowlist decision, not a
new decorator and not automatic publication of `@tool` callables. Token budgets
belong in [execution configuration](limits.md); [tracing](../design/tracing.md) belongs on
the runtime. Neither requires changing a skill's signature nor agent inheritance.

## Card generation boundaries

Agent identity and skill metadata come from declarations. Supported interfaces,
public URLs, security requirements, streaming, and push-notification capabilities
come from the host's actual configuration and implementation.

Card generation must not require inference. It must not publish instructions,
secrets, private tools, or capabilities the host cannot execute. Declaring a
security scheme in a card does not enforce authentication; the host must do so.
