# Agents with and without models

These are illustrative API proposals, not executable examples from a released
AgentFrame package. An agent is an `Agent` subclass with one or more separately
decorated async skills. There is no required `handle()` method or manual
dispatch switch. Decorated inherited skills may also satisfy this requirement.

## Without a model

```python
from agentframe import Agent, RunContext, agent, skill


@agent(
    name="TextUtilities",
    description="Transforms and measures text.",
    version="0.1.0",
)
class TextUtilities(Agent):
    @skill(
        id="uppercase",
        name="Uppercase",
        description="Convert text to uppercase.",
        tags=["text"],
    )
    async def uppercase(self, text: str, ctx: RunContext) -> str:
        return text.upper()

    @skill(
        id="word-count",
        name="Word count",
        description="Count whitespace-separated words in text.",
        tags=["text"],
    )
    async def word_count(self, text: str, ctx: RunContext) -> int:
        return len(text.split())


text_agent = TextUtilities()
```

No instructions, provider credentials, inference server, or model downloads are
needed. Each skill accepts `RunContext` even when unused: deadlines, cancellation,
verified caller information, correlation, and dependencies are invocation
concerns, not model-only features.

## With a model

```python
from agentframe import Agent, RunContext, agent, skill


@agent(
    name="WritingAssistant",
    description="Summarizes and rewrites supplied text.",
    version="0.1.0",
)
class WritingAssistant(Agent):
    instructions = "Preserve the source meaning. Do not invent facts."

    @skill(
        id="summarize",
        name="Summarize",
        description="Produce a concise summary.",
        tags=["text", "summary"],
    )
    async def summarize(self, text: str, ctx: RunContext) -> str:
        return await ctx.generate(
            text,
            instructions="Summarize the supplied text in three sentences.",
        )

    @skill(
        id="rewrite",
        name="Rewrite",
        description="Rewrite text for clarity.",
        tags=["text", "editing"],
    )
    async def rewrite(self, text: str, ctx: RunContext) -> str:
        return await ctx.generate(
            text,
            instructions="Rewrite the supplied text in clear, plain language.",
        )
```

Shared instructions apply to every generation call. Trusted skill-specific
instructions specialize the task without replacing shared constraints. The
adapter keeps supplied text separate as user content; prompt wording does not
replace authorization or other enforced policy.

### Choose local Llama through Ollama

```python
from agentframe import OllamaModel

local_model = OllamaModel(
    model="llama3.2:3b",
    endpoint="http://localhost:11434",
)
writer = WritingAssistant(model=local_model)
```

This illustrative tag must already be installed and run locally. Construction
does not download models, perform inference, or silently enable cloud routing.

### Switch to Foundry

```python
from azure.core.credentials_async import AsyncTokenCredential
from agentframe import FoundryModel


def build_writer(
    endpoint: str, deployment: str, credential: AsyncTokenCredential
) -> WritingAssistant:
    return WritingAssistant(
        model=FoundryModel(
            endpoint=endpoint,
            deployment=deployment,
            credential=credential,
        )
    )
```

The application supplies a supported endpoint, deployment, and credential.
This proposes async token credentials from the optional Azure dependencies;
the exact supported inference API remains to be selected. No skill code changes
when the configured model host changes.

## Mixing both kinds of skill

An agent may inherit existing skills and add model-free behavior. Injecting a
model does not force every skill to use it.

```python
@agent(
    name="WritingWorkbench",
    description="Summarizes, rewrites, and measures text.",
    version="0.1.0",
)
class WritingWorkbench(WritingAssistant):
    @skill(
        id="word-count",
        name="Word count",
        description="Count whitespace-separated words in text.",
        tags=["text"],
    )
    async def word_count(self, text: str, ctx: RunContext) -> int:
        return len(text.split())


workbench = WritingWorkbench(model=local_model)
```

The model dependency can be shared, but invocation and conversation state cannot.
Shared injected clients remain application-owned unless ownership is explicitly
transferred. The class inherits `summarize` and `rewrite`;
duplicate IDs must be rejected unless a documented explicit override rule applies.
An instance constructed without a model can still count words. Attempting to
generate without a model produces an explicit configuration error, not fallback
inference.

## Invoke through the local runtime

The snippets below continue the definitions above. Use an async function, not
top-level `await` in a normal Python module.

```python
from agentframe import Runtime


async def run_examples() -> None:
    runtime = Runtime()

    count = await runtime.invoke(
        text_agent,
        skill="word-count",
        arguments={"text": "Build agents with Python"},
    )
    summary = await runtime.invoke(
        writer,
        skill="summarize",
        arguments={"text": "The source document goes here."},
    )
    mixed_count = await runtime.invoke(
        workbench,
        skill="word-count",
        arguments={"text": "This skill does not call a model"},
    )
    print(count, summary, mixed_count)
```

The proposed convenience API returns each validated skill value and raises
explicit typed errors on failure; exact error classes are not finalized.
The runtime resolves a stable skill ID, validates arguments, creates context,
applies policy and limits, executes the method, and validates its output.
`ctx` cannot be supplied through the arguments dictionary. Undecorated helper
methods and unknown IDs are not invocable skills.

Direct Python calls to decorated methods bypass this managed pipeline; use the
runtime for application invocation. Runtime construction performs no model calls.

## A2A hosting and selection

```python
from agentframe import AgentApp, HostConfig

app = AgentApp(
    agent=text_agent,
    host=HostConfig(public_url="http://127.0.0.1:8000"),
    router=application_router,
)
```

`application_router` is an application-supplied dependency placeholder, not a
built-in selector or a finalized router interface. The snippet illustrates
composition only; the ASGI server separately binds to the matching loopback
address. Non-local hosting needs explicit authentication and HTTPS.

The card advertises declared skills using native A2A types. This does not turn
their Python methods into remote RPC endpoints. A matching single skill can be
selected directly; multiple skills require explicit deterministic or optional
model-backed routing. Unknown or ambiguous requests must fail clearly or request
clarification rather than choose the first skill.

Local `skill="word-count"` invocation is not a standard A2A wire operation. Any
remote skill-ID selector requires a documented application contract or negotiated
extension. Input/output A2A part mappings must be documented and tested, including
non-string outputs such as the integer count above.

Model-backed routing is separate from model use inside skills. It must not be
enabled just because an agent has a model, and a selector does not bypass policy
or implicitly grant access to skills as model tools. Requests needing multiple
skills require explicit composition, not automatic orchestration.
