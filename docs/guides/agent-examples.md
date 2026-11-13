# Agents with and without models

The snippets here are short illustrations. The runnable, tested versions are in
[`examples/`](../../examples/) (listed in the
[README](../../README.md#runnable-examples)); a smoke runner checks each one
against the installed wheel in CI. An agent is an `Agent` subclass with one or more separately
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
`agentframe-py[foundry]`) calls the OpenAI v1 Responses API at
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
text skill, a typed structured-output `summarize` skill and a model-free one.
`tests/integration/test_success_criterion.py` and
`tests/integration/test_structured_example.py` call it over A2A with the
official client using scripted models, and the opt-in real-provider tests run
the same contract against Ollama and Foundry.

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

The built-in router (M6) is opt-in: pass `router=ModelRouter(model)` to
`AgentApp`. [`examples/routed_assistant.py`](../../examples/routed_assistant.py)
serves an agent with a text `answer` skill and a model-free `to_fahrenheit`
skill that returns a typed `Temperature`:

```python
app = AgentApp(
    agent=RoutedAssistant(model=model),
    host=HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous()),
    router=ModelRouter(model),
)
```

A free-text message such as "Convert 25 degrees Celsius to Fahrenheit." takes
two routing model calls (select `to_fahrenheit`, then extract `celsius=25`) and
completes with the skill's typed result; the usage snapshot counts both
routing calls plus any the skill makes. "Convert a temperature to Fahrenheit."
leaves the task `INPUT_REQUIRED` with the router's question; the caller
answers in the same task and the whole conversation is routed again. A
structured skill-call request for `to_fahrenheit` skips the router and its
model calls. The router shares the agent's model only because it is passed
explicitly; any model with structured output works. `AllowAnonymous()` is a
development placeholder for a loopback server only. See
[`ModelRouter`](../api/hosting.md#modelrouter) for every outcome, including
Foundry strict-schema rejections of a skill's arguments, which surface at the
first routing call as `UnsupportedCapabilityError`.

A custom router composes the same way, with the application's runtime and
authentication:

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

`application_router` is any object with the
[`SkillRouter`](../api/hosting.md#skillrouter) `route` method.
`development_keys` is securely supplied application configuration (its entry
schema is still open), not literal secrets. `development_authorization_policy` is application resource
policy. API-key
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
`ModelRouter` (M6) for selection and argument extraction. Ambiguity or missing
arguments asks back through `TASK_STATE_INPUT_REQUIRED`, not a guessed skill;
the answer continues the same task. A clarification is not an approval
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
still be model-free or omit tools. Structured output (M4 #53) and bounded
tool calling (M5) are implemented. Both return complete validated
results; streaming is later.

The decided author API uses one `ctx.generate` method: omit `output` for text,
or pass a Pydantic `BaseModel` subclass for a validated instance (type checkers
infer the class). A model without structured-output support fails with
`UnsupportedCapabilityError` before sending, refusal raises `ModelRefusalError`,
invalid JSON or JSON that does not match raises `OutputValidationError` (field
locations only, no repair or re-ask), and token-limit truncation raises
`IncompleteOutputError`, never a partially parsed object. No provider calls
belong in skill business logic.

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
generation; pass `output=`. Only `BaseModel` subclasses are accepted (not
`RootModel`); wrap a list in a model field. AgentFrame sends the class's JSON
schema as `ModelRequest.output` and validates the reply; adapters translate the
schema to their provider; each adapter documents its schema subset
([Foundry](../api/models.md#structured-output),
[Ollama](../api/models.md#structured-output-ollama)). Hosted, the returned instance becomes one A2A data
part `{"value": {...}}`.

This tool example uses the implemented registration and selection shape (M5
#60): the constructor registers tools, and each `ctx.generate` call names the
ones it offers, and `ctx.generate` runs the model's tool calls until it
answers (#61; see [tool calling](../api/agents.md#implemented-tool-calling-m5-61)).
Each tool call is authorized before it runs (#62; see
[`ToolAuthorizationPolicy`](../api/authorization.md#toolauthorizationpolicy)). The supplied `model`
must support the requested tool capability; instructions do not grant it.

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
Normalized tool-call layouts are settled (M5 #59). Transient model failures retry at most twice
with backoff and provider-SDK retries disabled. `@tool(side_effects=True)`
tools never auto-retry; idempotent declarations are not retry grants.
Application approval workflows are separate; first
release has no built-in human pause/resume. These internal tools are not
automatic A2A skills, MCP publication or future AG-UI frontend tools.

[`examples/orders.py`](../../examples/orders.py) is a runnable hosted version:
a read-only `lookup_order` tool, a side-effecting `cancel_order` tool that needs
the `orders.cancel` permission, and a development tool policy that lets callers
touch only their own orders.

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
unknown usage is never assumed zero. Ordinary remote A2A gets the invocation ID and
a safe usage snapshot (token counts, estimated flag, model and tool call counts), not this internal result object.

## Limits, tracing, and optional MCP

The same skills inherit token budgets/deadlines; use
`@skill(..., limits=RunLimits(...))` for per-skill overrides.
Tracing observes their runtime execution independently of whether a model is used.
An MCP host may explicitly publish selected skill IDs for named tool invocation,
without using the A2A router or exposing internal model tools.

See [limits](limits.md), [tracing](../design/tracing.md), and the
[combined A2A/MCP server example](../design/mcp-skills.md). All three use the same validated
runtime pipeline; the custom REST skill API is deferred.

## Multi-agent composition

[`examples/multi_agent`](../../examples/multi_agent) is two independently
deployed agents. The research agent is an ordinary agent with a structured
skill (`find_sources`) and a model-backed one (`brief`) that free text is
routed to. The planner is an ordinary agent too: its skill finds the research
agent's card with `directory.find(skill="find_sources")`, then researches
every topic at once in one `asyncio.TaskGroup`, a structured `call` and a
free-text `ask` per topic:

```python
type Research = Remote[Literal["research-agent"]]


@agent(name="Planner", version="0.1.0")
class Planner(Agent):
    """Plans a goal with help from a research agent."""

    @skill(id="plan", tags=["planning"])
    async def plan(
        self,
        goal: str,
        topics: list[str],
        research: Research,
        directory: Inject[AgentDirectory],
        ctx: RunContext,
    ) -> Plan:
        """Research every topic of a goal at once: sources and a short brief."""
        researchers = await directory.find(skill="find_sources")
        async with asyncio.TaskGroup() as group:
            sources = {
                topic: group.create_task(
                    research.call(
                        "find_sources",
                        topic=topic,
                        output=list[Source],
                        max_total_tokens=SOURCES_TOKENS,
                    )
                )
                for topic in topics
            }
            briefs = {...}  # research.ask(..., max_total_tokens=BRIEF_TOKENS)
        ...
```

The planner never imports the research agent's code: it validates the result
against its own `Source` model. Its `create_app()` wires the directory, its
API key for `research-agent` (`Service(APIKeyCredential(...))`) and its own
authentication from `dev_settings()`; replace those with real sources and
authentication outside development. Run both with `agentframe dev` from the
example's directory (see its `agentframe.yaml` and
[local development](development.md#multiple-agents-m8)).

Each remote call carries the caller's remaining deadline, its token ceiling,
the hop count (at most 5 by default), the root invocation ID and any W3C trace
context; the peer's reported usage is charged to the caller's run once. Good
to know:

- Give each concurrent call an explicit `max_total_tokens`. A call with the
  default ceiling holds everything the run has left until it answers, so the
  next concurrent call, and the run's own model calls, cannot use those tokens.
- A configured remote's credential is always sent, even to a peer whose card
  declares no security.
- A receiver checks incoming hops against its agent-level
  `RunLimits.max_remote_hops`; a skill-level value limits only that skill's
  outbound calls.
- The `a2a` SDK's DEBUG logs include request bodies, so keep its loggers at
  INFO or above where messages may hold sensitive data.

An orchestrator is ordinary Python: no orchestration helpers or workflow
engine. Remote services authenticate callers and enforce their own policy and
limits; a found card is not permission to call an agent. See
[discovery](../design/discovery.md), its
[reference](../api/discovery.md) and [local dev/containers](development.md).

## Future streaming and AG-UI

First-release examples await complete validated results, not token streams.
Future [AG-UI support](../design/streaming.md) is a scoped optional frontend-protocol
adapter over managed execution events, not an OTel trace stream, automatic
frontend tool execution, human approval workflow or durable resume mechanism.
The later configuration is `AgentApp(..., ag_ui=AGUIConfig(enabled=True))` at
`/ag-ui`; no runtime stream method is selected. Stream lifecycle/text deltas,
tool names/status (not raw arguments by default) and safe errors. Whole-answer
checks release complete text at end; disconnect cancels, no reconnect/resume.
