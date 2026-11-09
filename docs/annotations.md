# Decorators and type annotations

All interfaces below are proposals. AgentFrame uses ordinary Python decorators
for metadata and registration, and Python type annotations for data contracts.

## Proposed decorators

| Decorator | Applies to | Definition |
| --- | --- | --- |
| `@agent(...)` | An `Agent` subclass | Declares name, description, version, and optional documentation/provider identity. Collects explicitly declared skills without performing I/O. |
| `@skill(...)` | An agent handler method | Associates an advertised A2A skill with behavior. Declares a stable ID, name, description, tags, examples, and optional input/output media types. Does not create an A2A RPC endpoint per method. |
| `@tool(...)` | An explicitly registered callable | Makes a typed callable eligible for the agent's model toolbox. Does not publish the callable as an A2A skill or authorize its execution. This is a later optional authoring feature. |

Require a non-empty agent description, an explicit agent version, and stable,
unique skill IDs. Docstrings may supply descriptions when unambiguous. Reject
duplicate IDs, unsupported annotations, and malformed metadata instead of
silently dropping declarations.

Agent decorators should preserve class identity, normal inheritance, signatures,
and Python introspection. Inheritance and overrides must have documented,
deterministic behavior; no process-global auto-registration is required.

## Type annotations

- Standard Python types describe local parameters and return values.
- Pydantic `BaseModel` types describe structured, validated data.
- `Annotated[T, Field(...)]` supplies descriptions and constraints without a
  separate AgentFrame validation language.
- `RunContext` is runtime-injected context, never a user-supplied model/tool argument.
- Async handlers are the primary execution path. Support for synchronous
  handlers must document blocking and cancellation behavior before release.

Agent Card input/output modes are media types, not complete Python schemas.
If typed schema discovery is added later, use a documented, negotiated extension
rather than claiming that a standard A2A skill encodes method parameters.

## Proposed authoring shape

This is illustrative design syntax, not executable code from a released package.
The initial example deliberately uses one text handler and one skill to avoid
introducing a custom multi-method routing protocol.

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
    async def handle(self, text: str, ctx: RunContext) -> str:
        return await ctx.generate(text)
```

The host converts supported A2A text parts into handler input and converts the
validated return into native A2A output. Unsupported part types fail explicitly.
The text argument represents supplied data; the model adapter should keep it
separate from agent instructions rather than concatenate everything into a
single privileged prompt.

A model-free handler can return a computed value without using `ctx.generate`.
Model configuration is supplied when constructing the agent, not encoded in
`@agent` or provider-specific decorators.

## Card generation boundaries

Agent identity and skill metadata come from declarations. Supported interfaces,
public URLs, security requirements, streaming, and push-notification capabilities
come from the host's actual configuration and implementation.

Card generation must not require inference. It must not publish instructions,
secrets, private tools, or capabilities the host cannot execute. Declaring a
security scheme in a card does not enforce authentication; the host must do so.
