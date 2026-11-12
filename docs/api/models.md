# Models

[Reference index and status conventions](README.md). These are proposed
AgentFrame-owned abstractions, not wrappers around another agent framework.
Provider dependencies are optional and stay out of skill business logic.
**Decided:** AgentFrame owns normalized messages, tool calls/results, token
usage and finish reasons; adapters translate to/from provider types. M3 #33
implements the text-generation layouts below (`Message`, `ModelSettings`,
`ModelRequest`, `ModelResponse`, `ModelCapabilities`, the `Model` protocol)
in `agentframe.models.base`, with no provider SDK imported. M4 #53 adds the
structured-output contract ([`OutputSchema`](#outputschema) on `ModelRequest`).
M5 #59 settles the tool-call layouts: [`ToolSpec`](#toolspec),
[`ToolCall`](#toolcall), `tool` messages, `ModelRequest.tools`,
`ModelResponse.tool_calls`/`provider_items` and `FinishReason.TOOL_CALLS`.
Layouts not specified here remain open, not provider leaks.

## `Model`

**Purpose / decision:** a structural Python protocol (`typing.Protocol`) for
asynchronous generation. Any class with these methods is a `Model`; custom
implementations do not inherit from an AgentFrame or vendor base.

**Signature status:** implemented (M3 #33). Lifecycle decided 2026-11-12.

| Member | Signature | Meaning and constraints |
| --- | --- | --- |
| [`generate`](#modelgenerate) | `async generate(request: ModelRequest) -> ModelResponse` | One complete, non-streaming call. |
| [`capabilities`](#modelcapabilities-1) | `async capabilities() -> ModelCapabilities` | What the model actually supports; fetched lazily, then cached. |
| [`close`](#modelclose) | `async close() -> None` | Releases only the clients the model created. |
| `__aenter__` / `__aexit__` | `async with model:` | Enter returns the model; exit closes it. |

```python
from types import TracebackType
from typing import Self

from agentframe import (
    FinishReason,
    Model,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Usage,
)


class EchoModel:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        unknown = Usage(None, None, None, estimated=False)
        return ModelResponse(
            text=request.messages[-1].content,
            usage=unknown,
            finish_reason=FinishReason.COMPLETED,
        )

    async def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(context_window=8192)

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()


model: Model = EchoModel()
```

**Behavior / failures:** constructing a model does no I/O: no network calls,
downloads or inference. A model closes only clients it created; clients the
caller injects stay caller-owned. A real adapter translates requests,
responses, feature support and failures explicitly: do not silently drop
requested settings, assume usage is known, or return refusal as text. Provider
exception mappings belong to each adapter (#38, #39). Unsupported requested
capabilities raise [`UnsupportedCapabilityError`](runtime.md#unsupportedcapabilityerror)
before sending. Managed generation (`ctx.generate`, #35) turns `REFUSED` into
`ModelRefusalError` and `LENGTH` into `IncompleteOutputError`. Structured
output (`output=`) is implemented by the managed layer (M4 #53); adapters send
[`ModelRequest.output`](#modelrequest) to their provider. The tool-call types
and the managed tool loop (`tools=`, #61) are implemented (M5 #59, #61); adapter
tool mappings (#64, #65) follow, and until then `FoundryModel` and
`OllamaModel` raise `UnsupportedCapabilityError` for requests with tool
content. Streaming remains later, with
optional [AG-UI](streaming.md) mapping from managed execution, not raw
provider events.

**Related:** [`ModelRequest`](#modelrequest), [`ModelResponse`](#modelresponse),
[model interface rationale](../design/base-classes.md#model-interfaces).

## `Model.generate`

**Purpose / direction:** low-level provider-neutral generation used beneath
the managed `RunContext.generate` helper.

**Signature (implemented, M3 #33):**

```python
async def generate(self, request: ModelRequest) -> ModelResponse: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `request` | [`ModelRequest`](#modelrequest) | Required | Messages plus the effective settings for this call. |

```python
from agentframe import Model, ModelRequest, ModelResponse


async def delegate(model: Model, request: ModelRequest) -> ModelResponse:
    return await model.generate(request)
```

**Behavior / failures:** returns a `ModelResponse` for every finish reason,
including `LENGTH`, `REFUSED` and `CONTENT_FILTER`; the managed helper decides
which are errors. Provider failures raise
[`ProviderError`](runtime.md#providererror) with `transient`, optional
`retry_after` and optional partial `usage`. A raw adapter call is not a managed
skill invocation and does not by itself establish authorization, invocation
budgets, or context; skills should use [`ctx.generate`](agents.md#runcontextgenerate).
The runtime retries only transient provider failures, default two retries
(`RunLimits.max_model_retries`) with backoff and provider-SDK retries disabled;
see [retries](../guides/limits.md#retries-and-duplicate-requests).

**Related:** [`Model`](#model), [`RunLimits`](configuration.md#runlimits),
[provider boundaries](../roadmap.md#initial-adapters).

## `FoundryModel`

**Purpose / decision:** call an explicitly configured Foundry GPT deployment
through the OpenAI v1 **Responses API** using the official `openai` Python
package, not deploy a hosted agent. The Azure AI Inference SDK is retired and
not used.

**Signature status:** implemented (M3 #39; structured output M4 #55);
decisions confirmed 2026-11-12. Requires the optional extra `agentframe[foundry]`, which pins
[`openai`](https://github.com/openai/openai-python) (`3.49.0`),
[`azure-identity`](https://pypi.org/project/azure-identity/) (`1.27.0`) and
[`aiohttp`](https://pypi.org/project/aiohttp/) (`3.14.5`), azure-core's async
HTTP transport for credentials that call Entra ID over the network.
`import agentframe` works without it; using `FoundryModel` without it raises
`ImportError` naming the extra.

```text
FoundryModel(
    *, deployment: str, endpoint: str | None = None,
    credential: str | AsyncTokenCredential | None = None,
    client: openai.AsyncOpenAI | None = None,
    context_window: int | None = None, store: bool = False,
    structured_output: bool = True,
)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `deployment` | `str` | Required | Deployment name; no automatic provisioning or fallback. |
| `endpoint` | `str \| None` | Required unless `client` is given | OpenAI v1 base URL `https://<resource>.openai.azure.com/openai/v1/`. Not a Foundry project agents endpoint. |
| `credential` | `str \| AsyncTokenCredential \| None` | Required unless `client` is given | An API key, or an async Azure token credential (`azure.identity.aio`). The adapter wraps a token credential with `get_bearer_token_provider(credential, "https://ai.azure.com/.default")`. The credential stays application-owned and is never logged. |
| `client` | `openai.AsyncOpenAI \| None` | `None` | An application-created client used instead of `endpoint`/`credential` (giving both fails). Used through `with_options(max_retries=0)`; it stays caller-owned and is never closed by the model. |
| `context_window` | `int \| None` | `None` | Tokens of input plus output the deployment accepts. The API does not report it, so `None` means unknown, never guessed. Must be greater than zero. |
| `store` | `bool` | `False` | Whether Foundry stores responses server-side. Controls response storage only, not every provider retention mechanism. |
| `structured_output` | `bool` | `True` | Whether `capabilities()` reports structured output. The Responses API defines it, so it is on by default (relaxing "unknown = unsupported" for Foundry only); pass `False` for a deployment that rejects it. A rejection fails the call with a non-transient `ProviderError`, never a retry or success. |

```python
from azure.identity.aio import AzureCliCredential

from agentframe import FoundryModel


async def main(endpoint: str, deployment: str) -> None:
    async with (
        AzureCliCredential() as credential,
        FoundryModel(
            endpoint=endpoint, deployment=deployment, credential=credential
        ) as model,
    ):
        ...  # Inject the model into an agent and serve or run it.
```

The application supplies the endpoint, deployment and credential; no
credential chain is selected implicitly. `AzureCliCredential` signs in through
`az login` for local development; production hosts can use
`DefaultAzureCredential` or `ManagedIdentityCredential` with the `foundry` extra.

**Behavior / failures:** implements [`Model.generate`](#modelgenerate) with one
non-streaming Responses call.

- Construction does no I/O. The adapter owns and [`close`](#modelclose)s a
  client it created; an injected client stays open.
- SDK retries are always off (`max_retries=0`), so AgentFrame is the only
  retry layer.
- Leading `system` messages become `instructions`; a later `system` message
  keeps its place in the input as a `developer` message. User and assistant
  messages are sent as input messages.
- `max_output_tokens` is sent as is. For reasoning models it includes
  reasoning tokens, so the visible answer can be shorter than the cap.
- Response mapping: `completed` returns the output text. Any refusal content
  item finishes `REFUSED` with empty text, even beside other text.
  `incomplete` with reason `max_output_tokens` finishes `LENGTH`,
  `content_filter` finishes `CONTENT_FILTER`, and any other reason raises
  `IncompleteOutputError`. `failed` raises `ProviderError`, transient only for
  `server_error` and `rate_limit_exceeded`; `cancelled`, `queued` and
  `in_progress` raise a non-transient `ProviderError`.
- Usage (`input_tokens`, `output_tokens`, `total_tokens`) is parsed whenever
  present, including on a `ProviderError` from a failed response and on the
  `IncompleteOutputError` for an early stop, so the run's accounting settles to
  it; missing usage is unknown (`None`), not zero.
- HTTP 408, 429 and 5xx, connection failures and timeouts are transient
  `ProviderError`s (with the retry hint as `retry_after` in seconds, read from
  `retry-after-ms` when present, else a numeric `Retry-After`);
  401/403, 404, other 4xx and Entra token failures are not. Messages never
  include the service's error text or credentials.
- [`capabilities`](#modelcapabilities-1) does no I/O and reports the
  configured `context_window` (or `None`) and `structured_output`; tools are
  unsupported. Without `context_window=`, an agent using this model needs an
  explicit `RunLimits(max_input_tokens=...)`; otherwise `AgentApp` startup
  fails with `DefinitionError` ([limits accounting](../guides/limits.md#implemented-token-accounting-m3), M3 #36).
- Opt-in real tests (`tests/integration/test_providers.py`, marker `foundry`, M3 #41;
  structured output M4 #55/#57):
  set `AGENTFRAME_TEST_FOUNDRY=1`, `AGENTFRAME_FOUNDRY_ENDPOINT` and
  `AGENTFRAME_FOUNDRY_DEPLOYMENT`; they sign in with the Azure CLI credential.
  Default test runs and CI never call Foundry. See `AGENTS.md`.

### Structured output

`ctx.generate(..., output=Summary)` sends the Responses API's strict JSON-schema
format; without `output=`, requests are unchanged from M3:

```text
text={"format": {"type": "json_schema", "name": <name>,
                 "schema": strict_schema(<json_schema>), "strict": True}}
```

- `name` is the [`OutputSchema`](#outputschema) name with every character
  outside `a-zA-Z0-9_-` replaced by `_` and cut to 64 characters, so a generic
  model such as `Page[int]` becomes `Page_int_` (empty becomes `output`).
- `strict_schema` (`agentframe.models.strict_schema`, AgentFrame-owned, no
  `openai.lib` helpers or `responses.parse`) copies the Pydantic schema and
  makes it strict: every object gets `"additionalProperties": false` and all
  of its properties in `required`; a `$ref` with sibling keywords (such as
  `description`) is wrapped in a one-branch `anyOf`, because strict mode rejects
  `$ref` siblings; and `default` keywords are removed. Other keywords are kept
  as-is: `title`, `description`, `enum`, `const`, `anyOf`, `$defs`/`$ref`
  (including recursion), `pattern`, `format`, `minLength`/`maxLength`,
  `minimum`/`maximum`/`exclusiveMinimum`/`exclusiveMaximum`/`multipleOf` and
  `minItems`/`maxItems`. A real `gpt-5.4` deployment accepted `title`,
  `default` and `minLength`/`maxLength`, and rejected `$ref` with sibling
  keywords (2026-11-12).
- **Defaulted fields:** strict mode makes every property required, which would
  erase Pydantic defaults. A field with a default (including
  `default_factory`) becomes nullable (`anyOf [T, null]`); when the model
  answers `null`, the adapter drops it before returning `ModelResponse.text`,
  so the default applies. A field already nullable in the model
  (`x: int | None = 5`) keeps an explicit `null`. Where a reply object could
  match several `anyOf` object branches, its nulls are left as they are.
- **Rejected before sending** with `UnsupportedCapabilityError`, which names the
  construct and field path, never a value: a root that is not an object;
  `allOf`, `not`, `if`/`then`/`else`, `dependentRequired`, `dependentSchemas`,
  `oneOf` (Pydantic discriminated unions; use a plain union) and
  `patternProperties`; `prefixItems` (Pydantic `tuple[...]` fields; use a
  list or a model); free-form objects (`dict[str, X]`, `extra="allow"`);
  and values of any type (`Any`). No non-strict fallback is attempted.
- Validation stays in the generation layer: the adapter returns the JSON text
  and never validates it. Refusal, `max_output_tokens` and content-filter
  replies finish `REFUSED`, `LENGTH` and `CONTENT_FILTER` as for text, so
  `ctx.generate` raises `ModelRefusalError`, `IncompleteOutputError` and
  `ModelRefusalError` with the call's usage recorded.
- A 4xx rejection of a structured request (for example a deployment without
  structured output, or a schema the converter let through) raises a
  non-transient `ProviderError` saying the deployment may not support
  structured output or this schema; the schema and the service's text are
  never echoed.

**Related:** [`ModelSettings`](#modelsettings),
[Foundry guide](../guides/agent-examples.md#switch-to-foundry),
[resolved platform/API choices](../roadmap.md#resolved-m0-platform-and-api-decisions),
[migration guide](https://learn.microsoft.com/en-us/azure/foundry/how-to/model-inference-to-openai-migration).

## `OllamaModel`

**Purpose / decision:** call an explicit Ollama endpoint and installed model tag,
initially for locally executed Llama. Llama is a model family, not a host.

**Signature status:** implemented (M3 #38); decisions confirmed 2026-11-12.
Requires the optional extra `agentframe[ollama]`, which pins the official
[`ollama`](https://github.com/ollama/ollama-python) client (`0.6.3`).
`import agentframe` works without it; using `OllamaModel` without it raises
`ImportError` naming the extra.

```text
OllamaModel(
    *, model: str, endpoint: str = "http://localhost:11434",
    context_window: int | None = None,
)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `model` | `str` | Required | Already-installed model tag; illustrative tags are not a version/support guarantee. |
| `endpoint` | `str` | `"http://localhost:11434"` | Application-selected Ollama server. Localhost alone does not prove a tag executes locally. |
| `context_window` | `int \| None` | `None` | Tokens of input plus output requested from the server. `None` uses the context length in the model's metadata, which is often the model's maximum and can need far more memory; passing an explicit size such as `8192` is recommended. Must be greater than zero. |

```python
from agentframe import OllamaModel


model = OllamaModel(
    model="llama3.2:3b",
    endpoint="http://localhost:11434",
    context_window=8192,
)
```

**Behavior / failures:** implements [`Model.generate`](#modelgenerate) with
one non-streaming call to Ollama's chat API. Never downloads a model, starts a
server, silently switches models, or enables cloud execution.

- Construction does no I/O. The adapter creates and owns its client;
  [`close`](#modelclose) closes it. A closed model cannot be reused; later
  calls fail with `RuntimeError`.
- Every request sends the context window explicitly (Ollama's `num_ctx`), so
  the server cannot silently truncate input to a smaller default. Without
  `context_window`, the size is read once from the model's metadata (show API)
  on first use and cached; [`capabilities`](#modelcapabilities-1) reports the
  same value, so input checks use what is sent. Metadata without a context
  length raises a non-transient `ProviderError` asking for `context_window=`.
  Because `AgentApp` reads every model's capabilities at startup, an
  `OllamaModel` without `context_window=` contacts its server during startup
  (bounded by the agent's timeout), and startup fails if the server or model
  is unavailable. With an explicit `context_window`, startup does no I/O.
- **Memory cost of the default:** the metadata size is usually the model's
  maximum context. Ollama allocates memory for the whole window, so
  `llama3.2:3b` with its 131072-token maximum loads at about 18 GB instead of
  about 2 GB at 8192. Ollama also reloads the model (about 9 seconds) whenever
  `num_ctx` changes, so models sharing a server should agree on one size. Pass
  an explicit `context_window` (examples use `8192`) sized for your inputs.
- `ModelSettings.max_output_tokens` is sent as `num_predict`.
- `prompt_eval_count` / `eval_count` become input/output usage (missing counts
  stay `None`); `done_reason` `length` becomes `FinishReason.LENGTH`, anything
  else `COMPLETED`.
- `structured_output` is reported as supported for every model (see
  [structured output](#structured-output-ollama)); `tools` is unsupported.
- Connection failures, timeouts, HTTP 408, 429 and 5xx raise a transient
  `ProviderError`; an unknown model (404) or other rejected request raises a
  non-transient one. Messages are safe and do not repeat the server's error
  text. The 0.6 client has no retry loop of its own; AgentFrame retries are
  separate.
- Opt-in real tests (`tests/integration/test_providers.py`, marker `ollama`, M3 #41;
  structured output M4 #56/#57): set
  `AGENTFRAME_TEST_OLLAMA=1` and `AGENTFRAME_OLLAMA_MODEL` (optionally
  `AGENTFRAME_OLLAMA_ENDPOINT`). Default test runs and CI never call Ollama.
  See `AGENTS.md`.

### Structured output (Ollama)

`ctx.generate(..., output=Summary)` sends the [`OutputSchema`](#outputschema)
JSON schema unchanged as Ollama's `format`, which constrains generation to that
schema (M4 #56). Without `output=`, no `format` is sent and requests are
unchanged from M3.

- Because the server, not the model, enforces the schema, `capabilities()`
  reports `structured_output=True` for every model. The opt-in real test proves
  it with `llama3.2:3b`.
- No OpenAI strict-mode conversion is applied: optional fields may simply be
  omitted, so Pydantic defaults apply without any null handling.
- **No hidden prompt text:** the adapter never adds the schema or other
  instructions to the prompt. **Tip:** describe the expected format in the
  agent's or skill's trusted `instructions` (for example "Reply with JSON: the
  country, its capital city and nicknames."); small local models answer better
  when the prompt says what the schema means.
- **No refusal signal:** Ollama cannot report a refusal. A model that declines
  in prose instead of JSON fails validation with `OutputValidationError`; it
  never counts as success and never raises `ModelRefusalError`.
- `done_reason` `length` raises `IncompleteOutputError`, as for text. Usage is
  recorded in both failure cases.
- Validation stays in the generation layer: the adapter returns the JSON text
  and never validates it.

**Related:** [`ModelCapabilities`](#modelcapabilities),
[local model guide](../guides/agent-examples.md#choose-local-llama-through-ollama).

## `ScriptedModel`

**Purpose / decision:** a deterministic [`Model`](#model) for testing
model-backed skills offline, quickly and repeatably, without a provider.

**Signature (implemented, M3 #34):** `agentframe.testing.ScriptedModel`.

```text
ScriptedModel(replies: Iterable[str | ToolCall | tuple[ToolCall, ...] | ModelResponse | Exception] = (), *, capabilities: ModelCapabilities | None = None)
```

| Parameter / attribute | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `replies` | `Iterable[str \| ToolCall \| tuple[ToolCall, ...] \| ModelResponse \| Exception]` | Default empty | One reply per `generate` call, in order. A `str` returns a `COMPLETED` response with unknown usage; a [`ToolCall`](#toolcall), or a tuple of them, returns a `TOOL_CALLS` response with empty text, those calls and unknown usage (M5 #59); a `ModelResponse` is returned as given (scripted usage/finish reason); an exception, such as `ProviderError(..., transient=True)`, is raised. |
| `capabilities` | `ModelCapabilities \| None` | Default `ModelCapabilities(context_window=SCRIPTED_CONTEXT_WINDOW)` (128000), with no structured output or tools | Returned by `capabilities()`. Pass `ModelCapabilities()` to test a model that reports no context window, or `ModelCapabilities(context_window=..., structured_output=True)` to test `ctx.generate(..., output=...)`, or `tools=True` for requests that offer tools. |
| `requests` | `list[ModelRequest]` | Attribute | Every request `generate` received, for asserting on assembled messages and settings. |
| `closed` | `bool` | Attribute | Whether `close()` (or `async with` exit) ran. |

```python
from agentframe import Message, ModelRequest, ModelSettings, ProviderError
from agentframe.testing import ScriptedModel


async def test_retry_after_rate_limit() -> None:
    model = ScriptedModel([ProviderError("Rate limited.", transient=True), "Hello!"])
    request = ModelRequest(
        messages=(Message(role="user", content="Hi"),),
        settings=ModelSettings(max_output_tokens=100),
    )

    try:
        await model.generate(request)
    except ProviderError:
        pass
    response = await model.generate(request)

    assert response.text == "Hello!"
    assert len(model.requests) == 2
```

To mirror a production model that reports no context window, such as a
`FoundryModel` created without `context_window=`, pass `ModelCapabilities()`.
The agent then needs `RunLimits(max_input_tokens=...)`, exactly as in
production; without it, `validate_model` (and `AgentApp` startup) raise
`DefinitionError`:

```python
from agentframe import (
    Agent,
    ModelCapabilities,
    RunContext,
    RunLimits,
    Runtime,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel


@agent(version="1.0.0")
class Writer(Agent):
    """Rewrites text."""

    # Needed when the model reports no context window, as in production.
    limits = RunLimits(max_input_tokens=2000)

    @skill("rewrite", tags=["text"])
    async def rewrite(self, text: str, ctx: RunContext) -> str:
        """Rewrite text."""
        return await ctx.generate(text)


async def test_runs_with_a_model_that_reports_no_context_window() -> None:
    # Mirrors, for example, a FoundryModel created without context_window=.
    model = ScriptedModel(["Shorter."], capabilities=ModelCapabilities())

    await Runtime().validate_model(Writer(model=model))
    result = await Runtime().invoke(
        Writer(model=model), skill="rewrite", arguments={"text": "Long."}
    )

    assert result == "Shorter."
```

For structured output, a scripted string is the JSON text a real adapter would
return; no other reply type is needed:

```python
from pydantic import BaseModel

from agentframe import Agent, ModelCapabilities, RunContext, Runtime, agent, skill
from agentframe.testing import ScriptedModel


class Summary(BaseModel):
    text: str
    key_points: list[str]


@agent(version="1.0.0")
class Summarizer(Agent):
    """Summarizes text."""

    @skill("summarize", tags=["text"])
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        """Summarize text."""
        return await ctx.generate(text, output=Summary)


async def test_summarize_returns_a_summary() -> None:
    model = ScriptedModel(
        ['{"text": "Short.", "key_points": ["one"]}'],
        capabilities=ModelCapabilities(context_window=8192, structured_output=True),
    )

    summary = await Runtime().invoke(
        Summarizer(model=model), skill="summarize", arguments={"text": "Long."}
    )

    assert summary == Summary(text="Short.", key_points=["one"])
    assert model.requests[0].output is not None
```

To script a model that asks for a tool before answering, put tool calls in
the replies. Each later request records what the caller sent back:

```python
from agentframe import (
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelSettings,
    ToolCall,
)
from agentframe.testing import ScriptedModel


async def test_model_asks_for_a_tool_then_answers() -> None:
    lookup = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')
    model = ScriptedModel(
        [lookup, "Order A1 has shipped."],
        capabilities=ModelCapabilities(context_window=8192, tools=True),
    )
    settings = ModelSettings(max_output_tokens=100)
    question = Message(role="user", content="Where is order A1?")

    asked = await model.generate(ModelRequest(messages=(question,), settings=settings))
    answer = await model.generate(
        ModelRequest(
            messages=(
                question,
                Message(role="assistant", content="", tool_calls=asked.tool_calls),
                Message(
                    role="tool",
                    content='{"status": "shipped"}',
                    tool_call_id="call_1",
                    tool_name="lookup_order",
                ),
            ),
            settings=settings,
        )
    )

    assert asked.tool_calls == (lookup,)
    assert answer.text == "Order A1 has shipped."
```

**Behavior / failures:** calling `generate` more times than scripted raises
`AssertionError` naming the call count, so tests fail clearly. No I/O, no
provider SDK; importing `agentframe` does not import `agentframe.testing`.

**Related:** [`Model.generate`](#modelgenerate), [`ModelResponse`](#modelresponse).

## `ModelSettings`

**Purpose / decision:** typed generation settings for individual model requests,
separate from aggregate invocation budgets and actual model context capacity.

**Signature (implemented, M3 #33; unset representation M3 #42):** frozen,
keyword-only dataclass. Every field defaults to `None`, meaning "unset": the
value is inherited from the next lower precedence level. The library defaults
are `DEFAULT_MODEL_SETTINGS = ModelSettings(max_output_tokens=4096)`.

```text
ModelSettings(*, max_output_tokens: int | None = None)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `max_output_tokens` | `int \| None` | `None` (unset); library default `4096` | Canonical per-model-call output allowance, not a second `RunLimits` field. Must be greater than zero when set (`DefinitionError` otherwise). Must fit model context, host ceilings and remaining run budget; reasoning-token inclusion is provider/API-specific. |

```python
from agentframe import ModelSettings


settings = ModelSettings(max_output_tokens=800)
```

**Behavior / failures:** 800 is an override; the default is 4096. Validate
settings against the actual deployment and effective limits; unsupported
requested settings fail, not disappear. Typed provider-specific options are
intended, but no universal temperature/reasoning fields or option registry are
settled. Precedence is library → agent → skill → per-run, merged field by
field (see [precedence](configuration.md#precedence)); host ceilings arrive in
M7. A [`ModelRequest`](#modelrequest) built by the runtime always carries the fully
resolved settings, with every field set. Do not duplicate output tokens in
`RunLimits`.

**Related:** [`AgentConfig`](configuration.md#agentconfig),
[`RunLimits`](configuration.md#runlimits), [limit scopes](../guides/limits.md#separate-the-scopes).

## `ModelCapabilities`

**Purpose / decision:** describe actual model/deployment support, not
caller-configured promises. Returned by [`Model.capabilities()`](#modelcapabilities-1).

**Signature (implemented, M3 #33, minimal for text generation):** frozen dataclass.

```text
ModelCapabilities(context_window: int | None = None, structured_output: bool = False, tools: bool = False)
```

| Field | Type | Default | Meaning and constraints |
| --- | --- | --- | --- |
| `context_window` | `int \| None` | `None` | Most input plus output tokens accepted. `None` is unknown, not unlimited. |
| `structured_output` | `bool` | `False` | Whether schema-constrained output is supported. `ctx.generate(..., output=...)` checks it before reserving tokens or sending; `False` raises `UnsupportedCapabilityError` (M4 #53). |
| `tools` | `bool` | `False` | Whether tool calling is supported (M5). |

```python
from agentframe import ModelCapabilities


capabilities = ModelCapabilities(context_window=128000)
```

**Behavior / failures:** unknown support counts as unsupported. Unsupported
requested capabilities fail before sending with `UnsupportedCapabilityError`.
Streaming and additional modalities remain later; fields arrive only when a
milestone needs them.

**Related:** [`Model`](#model), [context capacity](../guides/limits.md#separate-the-scopes).

## `Model.capabilities`

**Purpose / decision:** report what the model or deployment actually supports.

**Signature (implemented, M3 #33):**

```python
async def capabilities(self) -> ModelCapabilities: ...
```

No parameters.

```python
from agentframe import Model


async def context_window(model: Model) -> int | None:
    return (await model.capabilities()).context_window
```

**Behavior / failures:** fetched lazily on first call (it may do I/O, for
example reading model metadata), bounded by the caller's deadline, then cached.
Failure to fetch raises `ProviderError`.

**Related:** [`ModelCapabilities`](#modelcapabilities).

## `Model.close`

**Purpose / decision:** release clients the model created.

**Signature (implemented, M3 #33):**

```python
async def close(self) -> None: ...
```

No parameters.

```python
from agentframe import Model


async def finish(model: Model) -> None:
    await model.close()
```

**Behavior / failures:** closes only clients the model created; injected
clients stay caller-owned. `async with model:` calls `close()` on exit.

**Related:** [`Model`](#model).

## `Message`

**Purpose / decision:** one message in a model conversation: text, an
assistant's tool calls, or a tool's result.

**Signature (implemented, M3 #33; tool fields M5 #59):** frozen dataclass.

```text
Message(role: Role, content: str, tool_calls: tuple[ToolCall, ...] = (), tool_call_id: str | None = None, tool_name: str | None = None, provider_items: tuple[object, ...] = ())
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `role` | `Role` = `Literal["system", "user", "assistant", "tool"]` | Required | `system` holds trusted instructions only; untrusted user text is always a `user` message; tool results are `tool` messages, never `system`. |
| `content` | `str` | Required | The message text. For a `tool` message, the tool's result as text (M5: validated results serialized as JSON). May be empty for an assistant message that only calls tools. |
| `tool_calls` | `tuple[`[`ToolCall`](#toolcall)`, ...]` | Default `()` | The tools an `assistant` message asked to call, in the model's order. |
| `tool_call_id` | `str \| None` | Default `None`; required for `tool` | The [`ToolCall.id`](#toolcall) this `tool` message answers. |
| `tool_name` | `str \| None` | Default `None` | For `tool` messages, the tool that ran (Ollama needs the name). |
| `provider_items` | `tuple[object, ...]` | Default `()` | Opaque adapter data to replay in the next request, such as Foundry reasoning items with `encrypted_content`. Excluded from `repr` and equality. |

```python
from agentframe import Message, ToolCall


lookup = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')
messages = (
    Message(role="system", content="Answer in one sentence."),
    Message(role="user", content="Where is order A1?"),
    Message(role="assistant", content="", tool_calls=(lookup,)),
    Message(
        role="tool",
        content='{"status": "shipped"}',
        tool_call_id="call_1",
        tool_name="lookup_order",
    ),
)
```

**Behavior / failures:** never concatenate untrusted text into a `system`
message; tool results are untrusted content too. Inconsistent shapes raise
`DefinitionError`: `tool_calls` on a non-`assistant` message, a `tool` message
without `tool_call_id`, or `tool_call_id`/`tool_name` on a non-`tool` message.
`provider_items` is kept in memory for the run only: the runtime copies it
from a response to the assistant message and never inspects, logs, traces or
returns it (decided 2026-11-12). Other content types remain open.

**Related:** [`ModelRequest`](#modelrequest),
[prompt safeguarding](../design/prompt-safeguarding.md).

## `ModelRequest`

**Purpose / direction:** normalized outbound generation input shared by model
adapters, keeping trusted instructions distinct from untrusted content.

**Signature (implemented, M3 #33; `output` M4 #53; `tools` M5 #59):** frozen dataclass.

```text
ModelRequest(messages: tuple[Message, ...], settings: ModelSettings, output: OutputSchema | None = None, tools: tuple[ToolSpec, ...] = ())
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `messages` | `tuple[Message, ...]` | Required | The conversation in order; preserves instruction/user provenance. |
| `settings` | [`ModelSettings`](#modelsettings) | Required, fully resolved | The effective settings for this call. Every field must be set: an unset (`None`) field raises `DefinitionError` (M3 #42), so adapters never guess defaults. The runtime always sends merged settings. |
| `output` | [`OutputSchema`](#outputschema) ` \| None` | Default `None` (plain text) | The JSON shape the reply must have. When set, the adapter constrains generation to it and returns the JSON as `ModelResponse.text`; the managed layer validates it. |
| `tools` | `tuple[`[`ToolSpec`](#toolspec)`, ...]` | Default `()` (no tools) | The tools the model may call. A non-empty `tools` on a model whose `capabilities().tools` is `False` raises `UnsupportedCapabilityError` before tokens are reserved or anything is sent. |

```python
from agentframe import Message, ModelRequest, ModelSettings


request = ModelRequest(
    messages=(Message(role="user", content="Hello"),),
    settings=ModelSettings(max_output_tokens=100),
)
```

**Behavior / failures:** managed execution applies outbound checks to every
request. Invalid/unsupported requests, denied egress and exceeded allowances
must stop dispatch. `ctx.generate(..., output=Summary)` sends the same messages
as the text form plus `output=OutputSchema(name="Summary", json_schema=...)`;
the serialized schema counts toward the call's input token estimate. Tool
specs, tool-call names and arguments, and tool results count toward it too.
`uses_tools()` returns whether the request offers tools or carries tool calls
or results. The managed loop fills `tools` from
`ctx.generate(..., tools=[...])` (#61) and sends the same specs on every
continuation.

**Related:** [`ExecutionPolicy`](authorization.md#executionpolicy),
[outbound checks](../design/prompt-safeguarding.md#check-at-the-boundary-not-just-once).

## `OutputSchema`

**Purpose / decision:** the provider-neutral JSON shape a structured-output call
must return. The managed layer builds it from the `output=` class; adapters
translate it to their provider's format and never validate the reply.

**Signature (implemented, M4 #53):** frozen dataclass, exported from `agentframe`.

```text
OutputSchema(name: str, json_schema: dict[str, Any])
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `name` | `str` | Required | The output class's name, for example `"Summary"`. Adapters may need to sanitize it for their provider. |
| `json_schema` | `dict[str, Any]` | Required | The class's Pydantic `model_json_schema()` (validation mode), including any `$defs`. |

```python
from pydantic import BaseModel

from agentframe import OutputSchema


class Summary(BaseModel):
    text: str
    key_points: list[str]


schema = OutputSchema(name="Summary", json_schema=Summary.model_json_schema())
```

**Behavior / failures:** only `BaseModel` subclasses (not `RootModel`) produce
an `OutputSchema`; anything else raises `DefinitionError` before sending.
Provider strict-mode conversion is the adapter's job (see
[`FoundryModel` structured output](#structured-output);
[`OllamaModel`](#structured-output-ollama) sends the schema unchanged).

**Related:** [`ModelRequest`](#modelrequest),
[`RunContext.generate`](agents.md#runcontextgenerate).

## `ModelResponse`

**Purpose / direction:** normalized model result with honest usage/completion
information, not an A2A or MCP transport envelope.

**Signature (implemented, M3 #33; tool fields M5 #59):** frozen dataclass.

```text
ModelResponse(text: str, usage: Usage, finish_reason: FinishReason, tool_calls: tuple[ToolCall, ...] = (), provider_items: tuple[object, ...] = ())
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `text` | `str` | Required | Generated text, or the JSON text for a structured-output request; possibly truncated when `finish_reason` is not `COMPLETED`. |
| `usage` | [`Usage`](runtime.md#runresult) | Required | Tokens for this call; unknown counts are `None`, never zero; `estimated` marks estimates. |
| `finish_reason` | [`FinishReason`](runtime.md#runresult) | Required | `COMPLETED`, `LENGTH`, `REFUSED`, `CONTENT_FILTER` or `TOOL_CALLS` (the model stopped to ask for tool calls; model calls only, never a run's finish reason). |
| `tool_calls` | `tuple[`[`ToolCall`](#toolcall)`, ...]` | Default `()` | The tools the model asks to call, in its order. `text` may be empty. |
| `provider_items` | `tuple[object, ...]` | Default `()` | Opaque adapter data to replay with the assistant message (see [`Message`](#message)). Excluded from `repr` and equality; never logged, traced or returned. |

```python
from agentframe import FinishReason, ModelResponse, Usage


response = ModelResponse(
    text="A2A is an agent-to-agent protocol.",
    usage=Usage(input_tokens=12, output_tokens=9, total_tokens=21, estimated=False),
    finish_reason=FinishReason.COMPLETED,
)
```

**Behavior / failures:** provider failures raise `ProviderError`, never
success-shaped text. Refusal and truncation are reported through
`finish_reason`; the managed helper raises `ModelRefusalError` and
`IncompleteOutputError` (text cut short is kept on its `partial_text`) and never returns
them as success. Structured replies are JSON in `text`, validated by the
managed layer (M4 #53). Each tool result links to its call through
`Message.tool_call_id` (M5 #59). Failed-run usage retrieval remains open; no hard billing guarantee
is implied.

**Related:** [`Model.generate`](#modelgenerate),
[accounting and enforcement](../guides/limits.md#accounting-and-enforcement).

## `ToolSpec`

**Purpose / decision:** a tool as the model sees it: name, description and the
JSON schema of its business arguments. Adapters translate it to their
provider's tool format.

**Signature (implemented, M5 #59):** frozen dataclass, exported from `agentframe`.

```text
ToolSpec(name: str, description: str, parameters: dict[str, Any])
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `name` | `str` | Required | The tool's name, used by the model to call it. |
| `description` | `str` | Required | What the tool does, for the model. |
| `parameters` | `dict[str, Any]` | Required | JSON schema of the business arguments, as from [`ToolDefinition.json_schema()`](decorators.md#tooldefinition); `ctx` and `Inject[T]` parameters never appear. |

```python
from agentframe import ToolSpec


spec = ToolSpec(
    name="lookup_order",
    description="Look up an order by its ID.",
    parameters={"type": "object", "properties": {"order_id": {"type": "string"}}},
)
```

**Behavior / failures:** a spec grants nothing; only tools registered on the
agent and selected by a `ctx.generate` call are offered (#60, #61). Provider
strict-schema conversion is the adapter's job (#64).

**Related:** [`ModelRequest`](#modelrequest), [`@tool`](decorators.md#tool).

## `ToolCall`

**Purpose / decision:** one tool call requested by a model.

**Signature (implemented, M5 #59):** frozen dataclass, exported from `agentframe`.

```text
ToolCall(id: str, name: str, arguments: str)
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `id` | `str` | Required | The provider's call ID. Adapters generate one when the provider has none (Ollama), so every `tool` message can answer its call. |
| `name` | `str` | Required | The tool to call. |
| `arguments` | `str` | Required | Raw JSON text. The runtime validates it with the tool's `arguments_adapter.validate_json`, so bad JSON and bad fields take one failure path (decided 2026-11-12). |

```python
from agentframe import ToolCall


call = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')
```

**Behavior / failures:** arguments are untrusted model output; nothing runs
until the managed loop (#61) validates the call; unknown tools and invalid
arguments run nothing. Action authorization is #62.

**Related:** [`ModelResponse`](#modelresponse), [`Message`](#message).
