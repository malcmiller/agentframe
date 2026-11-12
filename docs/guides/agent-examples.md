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
instructions specialize the task without replacing shared constraints.
AgentFrame sends the shared instructions, then the skill's instructions, as
separate `system` messages and the supplied text as a `user` message; it never
merges the text into instructions. This does not guarantee resistance to prompt
injection, and prompt wording does not replace authorization or other enforced
policy.

The agent above and text `ctx.generate` run from source today (M3 #35), with
the Ollama and Foundry adapters below (M3 #38, #39). Tests can use the scripted
test model instead of a provider:

```python
from agentframe import Runtime
from agentframe.testing import ScriptedModel


async def test_summarize() -> None:
    model = ScriptedModel(["A three-sentence summary."])
    writer = WritingAssistant(model=model)

    summary = await Runtime().invoke(
        writer, skill="summarize", arguments={"text": "Long text."}
    )

    assert summary == "A three-sentence summary."
    assert [message.role for message in model.requests[0].messages] == [
        "system",
        "system",
        "user",
    ]
```

### Choose local Llama through Ollama

```python
from agentframe import OllamaModel

local_model = OllamaModel(
    model="llama3.2:3b",
    endpoint="http://localhost:11434",
    context_window=8192,
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

The application supplies the endpoint, deployment and an API key or async
Azure token credential; `FoundryModel` (implemented in M3 #39, extra
`agentframe[foundry]`) calls the OpenAI v1 Responses API at
`https://<resource>.openai.azure.com/openai/v1/` through the official `openai`
package, wrapping a token credential for scope `https://ai.azure.com/.default`.
Do not use the retired Azure AI Inference SDK. See
[`FoundryModel`](../api/models.md#foundrymodel) and the
[API choice and source](../roadmap.md#resolved-m0-platform-and-api-decisions).
No skill code changes when the configured model host changes.

### Choose the host by configuration

[`examples/assistant.py`](../../examples/assistant.py) is the runnable version of
this switch (the [README success criterion](../../README.md#first-success-criterion)).
Its `create_app()` builds `OllamaModel` (with `context_window=8192`) or
`FoundryModel` (signed in through the Azure CLI's Entra ID credential) from
environment variables and serves one unchanged agent class, which mixes a
model-backed skill with a model-free one. `tests/integration/test_success_criterion.py`
calls it over A2A with the official client using two scripted models, and the
opt-in real-provider tests run the same contract against Ollama and Foundry.

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
`ctx` cannot be supplied through the `arguments` mapping. Undecorated helper
methods and unknown IDs are not invocable skills.

Direct Python calls to decorated methods bypass this managed pipeline; use the
runtime for application invocation. Runtime construction performs no model calls.

## A2A hosting and selection

```python
from agentframe import APIKeyAuth, AgentApp, HostConfig, Runtime

app = AgentApp(
    agent=text_agent,
    runtime=Runtime(authorization=development_authorization_policy),
    host=HostConfig(
        public_url="http://127.0.0.1:8000",
        auth=APIKeyAuth(keys=development_keys),
    ),
    router=application_router,
)
```

`application_router` is an application-supplied dependency placeholder, not a
finalized router interface. AgentFrame will also supply a built-in opt-in
model-based router; its public name/construction are not settled, so this
example does not fabricate them. `development_keys` is securely supplied
application configuration (its entry schema is still open), not literal secrets.
`development_authorization_policy` is application resource policy. API-key
auth is an explicit development choice, not a production-safe default;
omitting `auth` refuses startup and anonymous access requires `AllowAnonymous()`.
The snippet illustrates
composition only; the ASGI server separately binds to the matching loopback
address. Non-local hosting needs explicit authentication and HTTPS.

One server/shared runtime exposes an endpoint per caller audience, not per
skill: A2A for agent callers, optional allowlisted MCP for LLM apps/tools and
AG-UI later for frontends. The card's skills alone are descriptive, not remote
Python-method RPC endpoints.

Prefer structured A2A requests: name the skill and send JSON arguments in a
native data part under the declared/activated AgentFrame extension. This
bypasses routing and its model-selection cost; it does not skip typed
validation, authorization, limits, accounting or tracing. The skill itself
may still call a model. Unknown skill/invalid arguments fail explicitly.

Free-text callers use the configured custom router (M2) or opt-in built-in
model router (M6) for selection and argument extraction. Ambiguity or missing
arguments asks back through `TASK_STATE_INPUT_REQUIRED`, not a guessed skill.
Clarification messages/follow-up handling remain open and are not an approval
workflow or a successful completed result.

Local `skill="word-count"` is not a base A2A wire field. Over A2A the caller
uses the AgentFrame skill-call extension; see its
[wire format](../api/hosting.md#structured-skill-call-wire-format) and
[A2A request styles and native mechanisms](../design/mcp-skills.md#a2a-request-styles-and-extension-boundary).
Optional MCP uses native named tools and an empty-by-default skill allowlist.
Quick typed/single-shot tool-like skills may also use MCP; judgment, multi-turn,
clarification or long-running skills usually stay A2A-only, as guidance rather
than a restriction.
Input/output A2A part mappings must be documented and tested, including
non-string outputs such as the integer count above.

Model-backed routing is separate from model use inside skills. It must not be
enabled just because an agent has a model, and a selector does not bypass policy
or implicitly grant access to skills as model tools. Requests needing multiple
skills require explicit composition, not automatic orchestration.

## Typed structured output and bounded tools

Both capabilities belong in the first usable release, while each agent may
still be model-free or omit tools. All examples remain proposals and return
complete validated results; streaming is later.

The decided author API uses one `ctx.generate` method: omit `output` for text,
or pass a Pydantic class for a validated instance. Unsupported capability fails
before sending, refusal raises `ModelRefusalError`, invalid structured output
raises `OutputValidationError`, and token-limit truncation raises
`IncompleteOutputError` unless the skill explicitly permits partial results
(opt-in syntax still open). No provider calls belong in skill business logic.

```python
from pydantic import BaseModel, Field
from agentframe import Agent, RunContext, agent, skill


class Summary(BaseModel):
    text: str = Field(min_length=1)
    key_points: list[str]


@agent(name="StructuredWriter", description="Creates typed summaries.", version="0.1.0")
class StructuredWriter(Agent):
    @skill(
        id="summarize", name="Summarize",
        description="Return a validated summary.", tags=["text"],
    )
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        return await ctx.generate(text, output=Summary)
```

Declaring a Pydantic skill return type alone does not configure model structured
generation. AgentFrame owns normalized messages, tool calls/results, token
usage and finish reasons; adapters translate provider types. Exact layouts
and supported Pydantic schema subset remain open, not the `output=` syntax
or fail-before-send capability rule.
The A2A representation of typed results also needs a tested native part mapping.

This tool example uses the existing proposed registration shape. The supplied
`model` must support the requested tool capability; instructions do not grant it.

```python
from agentframe import Agent, Model, RunContext, agent, skill, tool


@tool(description="Convert Celsius to Fahrenheit.", idempotent=True)
def to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32


@agent(name="TemperatureHelper", description="Converts temperatures.", version="0.1.0")
class TemperatureHelper(Agent):
    @skill(
        id="convert", name="Convert temperature",
        description="Convert a supplied Celsius temperature.", tags=["units"],
    )
    async def convert(self, text: str, ctx: RunContext) -> str:
        return await ctx.generate(
            text, tools=[to_fahrenheit],
            instructions="Use the registered conversion tool when needed.",
        )


def build_temperature_helper(model: Model) -> TemperatureHelper:
    return TemperatureHelper(model=model, tools=[to_fahrenheit])
```

The managed loop validates model tool calls, applies mandatory authorization/
action checks, validates results and includes them in generation continuations
under the decided defaults: 20 tool calls, 10 model attempts, 100000 total
tokens and 120 seconds per run; each model call defaults to 4096 output tokens.
Normalized layouts remain open. Transient model failures retry at most twice
with backoff and provider-SDK retries disabled. `@tool(side_effects=True)`
tools never auto-retry; idempotent declarations are not retry grants.
Application approval workflows are separate; first
release has no built-in human pause/resume. These internal tools are not
automatic A2A skills, MCP publication or future AG-UI frontend tools.

`Runtime.invoke` returns the complete validated value. For local metadata:

```python
from agentframe import Agent, RunResult, Runtime


async def summarize_with_metadata(runtime: Runtime, writer: Agent) -> RunResult:
    return await runtime.run(
        writer, skill="summarize", arguments={"text": "Summarize this document."},
    )
```

`RunResult` contains `value`, `invocation_id`, `usage`, `model_calls`,
`tool_calls`, `duration` and `finish_reason`, even with tracing off. Usage
reports input/output/total tokens with completeness/estimate information;
unknown usage is never assumed zero. Ordinary remote A2A gets only invocation ID
metadata, not this internal result object.

## Limits, tracing, and optional MCP

The same skills inherit token budgets/deadlines; use
`@skill(..., limits=RunLimits(...))` for per-skill overrides.
Tracing observes their runtime execution independently of whether a model is used.
An MCP host may explicitly publish selected skill IDs for named tool invocation,
without using the A2A router or exposing internal model tools.

See [limits](limits.md), [tracing](../design/tracing.md), and the
[combined A2A/MCP server example](../design/mcp-skills.md). All three use the same validated
runtime pipeline; the custom REST skill API is deferred.

## Next-release multi-agent dependencies

The [discovery guide](../design/discovery.md#agent-injection-and-separate-invocation)
shows trusted `Inject[AgentDirectory]` parameters registered in `Services`.
Its [lifecycle example](../design/discovery.md#managed-directory-lifecycle) keeps card
fetch/cache/refresh and owned-resource cleanup in the library. Named
`Remote["research-agent"]` skill parameters resolve via `AgentApp(directory=...)`.
Use `research.call("find_sources", topic=goal, output=list[Source])` for structured
A2A, or `research.ask(text)` for any compatible A2A agent. Await task completion
within caller deadline; input-required raises `ClarificationNeededError`, not an
automatic conversation. See the [worked delegation, TaskGroup fan-out and search examples](../design/discovery.md#worked-composition-examples).
No orchestration helper library in the first multi-agent release.
Discovery and remote invocation
are core to the next multi-agent release (M8+) after M7; they do not change
the skill authoring contract. An orchestrator is an ordinary AgentFrame agent
whose skills call independently deployed specialists over A2A. Cross-hop
identity/permissions, deadlines, remaining token allowance and trace/correlation
use per-target OBO/service identity, remaining deadline/token allowance and
maximum 5 hops; detailed wire/configuration remains open. Remote services enforce
their own policy/limits. `directory.find(skill=...)` / `find(tags=...)` returns
matching cards, not invocation permission. See [local dev/containers](development.md).

## Future streaming and AG-UI

First-release examples await complete validated results, not token streams.
Future [AG-UI support](../design/streaming.md) is a scoped optional frontend-protocol
adapter over managed execution events, not an OTel trace stream, automatic
frontend tool execution, human approval workflow or durable resume mechanism.
The later configuration is `AgentApp(..., ag_ui=AGUIConfig(enabled=True))` at
`/ag-ui`; no runtime stream method is selected. Stream lifecycle/text deltas,
tool names/status (not raw arguments by default) and safe errors. Whole-answer
checks release complete text at end; disconnect cancels, no reconnect/resume.
