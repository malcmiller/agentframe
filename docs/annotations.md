# Decorators and type annotations

All interfaces below are proposals. AgentFrame uses ordinary Python decorators
for metadata and registration, and Python type annotations for data contracts.

Decorators are AgentFrame-owned, not re-exports from another agent framework.
They describe behavior; the `Agent` subclass implements it. Configuration and
dependency injection supply instructions, models, policies, and infrastructure.
Do not add provider-specific decorators such as `@foundry` or `@llama`.

## Proposed decorators

| Decorator | Applies to | Definition |
| --- | --- | --- |
| `@agent(...)` | An `Agent` subclass | Declares name, description, version, and optional documentation/provider identity. Collects explicitly declared skills without performing I/O. |
| `@skill(...)` | An individual async agent method | Registers a typed skill implementation and its advertised A2A metadata. Declares a stable ID, name, description, tags, examples, and optional input/output media types. Does not create an A2A RPC endpoint per method. |
| `@tool(...)` | An explicitly registered callable | Makes a typed callable eligible for the agent's model toolbox. Does not publish the callable as an A2A skill or authorize its execution. This is a later optional authoring feature. |

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

## Type annotations

- Standard Python types describe local parameters and return values.
- Pydantic `BaseModel` types describe structured, validated data.
- `Annotated[T, Field(...)]` supplies descriptions and constraints without a
  separate AgentFrame validation language.
- `RunContext` is runtime-injected context, never a user-supplied model/tool argument.
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

A2A skill metadata does not define remote function dispatch. A single-skill
agent can select that skill when the content matches its contract. Multi-skill
remote agents need an explicitly configured router, using deterministic logic
or an optional model-backed selector. Unknown or ambiguous selection must fail
clearly or request clarification, not select the first registered skill.

Model-backed routing is distinct from model-backed skill execution. Neither
an injected model nor a decorated skill implicitly enables model selection,
tool access, or multi-skill orchestration. Any explicit remote skill-ID
selection format requires a documented application contract or negotiated
extension. No router signature or selection wire format has been finalized.

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

Tools are a later, optional feature, not required for initial text agents.
Registration is explicit; decoration alone does not grant model access or
execution permission. This callable is not automatically an advertised A2A skill.

```python
from agentframe import tool


@tool(description="Convert Celsius to Fahrenheit.")
def to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32
```

The proposed construction shape is `Summarizer(model=model, tools=[to_fahrenheit])`.
The runtime must validate arguments, enforce policy, and bound tool iterations.
Instruction text is not an authorization mechanism.

## Card generation boundaries

Agent identity and skill metadata come from declarations. Supported interfaces,
public URLs, security requirements, streaming, and push-notification capabilities
come from the host's actual configuration and implementation.

Card generation must not require inference. It must not publish instructions,
secrets, private tools, or capabilities the host cannot execute. Declaring a
security scheme in a card does not enforce authentication; the host must do so.
