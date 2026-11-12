# Models

[Reference index and status conventions](README.md). These are proposed
AgentFrame-owned abstractions, not wrappers around another agent framework.
Provider dependencies are optional and stay out of skill business logic.
**Decided:** AgentFrame owns normalized messages, tool calls/results, token
usage and finish reasons; adapters translate to/from provider types. M3 #33
implements the text-generation layouts below (`Message`, `ModelSettings`,
`ModelRequest`, `ModelResponse`, `ModelCapabilities`, the `Model` protocol)
in `agentframe.models`, with no provider SDK imported. Tool-call, structured
output and other layouts not specified here remain open, not provider leaks.

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
output (`output=`, M4), tools (`tools=`, M5) and streaming remain later, with
optional [AG-UI](streaming.md) mapping from managed execution, not raw
provider events.

**Related:** [`ModelRequest`](#modelrequest), [`ModelResponse`](#modelresponse),
[model interface rationale](../base-classes.md#model-interfaces).

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
The runtime retries only transient provider failures, default two retries with
backoff and provider-SDK retries disabled (#37).

**Related:** [`Model`](#model), [`RunLimits`](configuration.md#runlimits),
[provider boundaries](../initial-outline.md#initial-adapters).

## `FoundryModel`

**Purpose / decision:** call an explicitly configured Foundry GPT deployment
through the OpenAI v1 **Responses API** using the official `openai` Python
package, not deploy a hosted agent. The Azure AI Inference SDK is retired and
not used.

**Signature status:** implemented (M3 #39); decisions confirmed 2026-11-12.
Requires the optional extra `agentframe[foundry]`, which pins
[`openai`](https://github.com/openai/openai-python) (`3.49.0`) and
[`azure-identity`](https://pypi.org/project/azure-identity/) (`1.27.0`).
`import agentframe` works without it; using `FoundryModel` without it raises
`ImportError` naming the extra.

```text
FoundryModel(
    *, deployment: str, endpoint: str | None = None,
    credential: str | AsyncTokenCredential | None = None,
    client: openai.AsyncOpenAI | None = None,
    context_window: int | None = None, store: bool = False,
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

```python
from azure.identity.aio import DefaultAzureCredential

from agentframe import FoundryModel


async def main(endpoint: str, deployment: str) -> None:
    async with (
        DefaultAzureCredential() as credential,
        FoundryModel(
            endpoint=endpoint, deployment=deployment, credential=credential
        ) as model,
    ):
        ...  # Inject the model into an agent and serve or run it.
```

The application supplies the endpoint, deployment and credential; no
credential chain is selected implicitly.

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
  configured `context_window` (or `None`); structured output and tools are
  unsupported. Without `context_window=`, an agent using this model needs an
  explicit `RunLimits(max_input_tokens=...)`; otherwise `AgentApp` startup
  fails with `DefinitionError` ([limits accounting](../limits.md#implemented-token-accounting-m3), M3 #36).
- Opt-in real test: set `AGENTFRAME_TEST_FOUNDRY=1`,
  `AGENTFRAME_FOUNDRY_ENDPOINT` and `AGENTFRAME_FOUNDRY_DEPLOYMENT`; it signs in
  with the Azure CLI credential. Default test runs never call Foundry.

**Related:** [`ModelSettings`](#modelsettings),
[Foundry guide](../agent-examples.md#switch-to-foundry),
[resolved platform/API choices](../initial-outline.md#resolved-m0-platform-and-api-decisions),
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
- `structured_output` and `tools` are reported as unsupported in M3.
- Connection failures, timeouts, HTTP 408, 429 and 5xx raise a transient
  `ProviderError`; an unknown model (404) or other rejected request raises a
  non-transient one. Messages are safe and do not repeat the server's error
  text. The 0.6 client has no retry loop of its own; AgentFrame retries are
  separate.

**Related:** [`ModelCapabilities`](#modelcapabilities),
[local model guide](../agent-examples.md#choose-local-llama-through-ollama).

## `ScriptedModel`

**Purpose / decision:** a deterministic [`Model`](#model) for testing
model-backed skills offline, quickly and repeatably, without a provider.

**Signature (implemented, M3 #34):** `agentframe.testing.ScriptedModel`.

```text
ScriptedModel(replies: Iterable[str | ModelResponse | Exception] = (), *, capabilities: ModelCapabilities | None = None)
```

| Parameter / attribute | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `replies` | `Iterable[str \| ModelResponse \| Exception]` | Default empty | One reply per `generate` call, in order. A `str` returns a `COMPLETED` response with unknown usage; a `ModelResponse` is returned as given (scripted usage/finish reason); an exception, such as `ProviderError(..., transient=True)`, is raised. |
| `capabilities` | `ModelCapabilities \| None` | Default `ModelCapabilities(context_window=SCRIPTED_CONTEXT_WINDOW)` (128000) | Returned by `capabilities()`. Pass `ModelCapabilities()` to test a model that reports no context window. |
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
[`RunLimits`](configuration.md#runlimits), [limit scopes](../limits.md#separate-the-scopes).

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
| `structured_output` | `bool` | `False` | Whether schema-constrained output is supported (M4). |
| `tools` | `bool` | `False` | Whether tool calling is supported (M5). |

```python
from agentframe import ModelCapabilities


capabilities = ModelCapabilities(context_window=128000)
```

**Behavior / failures:** unknown support counts as unsupported. Unsupported
requested capabilities fail before sending with `UnsupportedCapabilityError`.
Streaming and additional modalities remain later; fields arrive only when a
milestone needs them.

**Related:** [`Model`](#model), [context capacity](../limits.md#separate-the-scopes).

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

**Purpose / decision:** one text message in a model conversation.

**Signature (implemented, M3 #33):** frozen dataclass.

```text
Message(role: Role, content: str)
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `role` | `Role` = `Literal["system", "user", "assistant"]` | Required | `system` holds trusted instructions only; untrusted user text is always a `user` message. |
| `content` | `str` | Required | Text only in M3. |

```python
from agentframe import Message


messages = (
    Message(role="system", content="Answer in one sentence."),
    Message(role="user", content="What is A2A?"),
)
```

**Behavior / failures:** never concatenate untrusted text into a `system`
message. Tool-call/result messages and other content types remain open (M5).

**Related:** [`ModelRequest`](#modelrequest),
[prompt safeguarding](../prompt-safeguarding.md).

## `ModelRequest`

**Purpose / direction:** normalized outbound generation input shared by model
adapters, keeping trusted instructions distinct from untrusted content.

**Signature (implemented, M3 #33):** frozen dataclass.

```text
ModelRequest(messages: tuple[Message, ...], settings: ModelSettings)
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `messages` | `tuple[Message, ...]` | Required | The conversation in order; preserves instruction/user provenance. |
| `settings` | [`ModelSettings`](#modelsettings) | Required, fully resolved | The effective settings for this call. Every field must be set: an unset (`None`) field raises `DefinitionError` (M3 #42), so adapters never guess defaults. The runtime always sends merged settings. |

```python
from agentframe import Message, ModelRequest, ModelSettings


request = ModelRequest(
    messages=(Message(role="user", content="Hello"),),
    settings=ModelSettings(max_output_tokens=100),
)
```

**Behavior / failures:** managed execution applies outbound checks to every
request. Invalid/unsupported requests, denied egress and exceeded allowances
must stop dispatch. Tool definitions and output contracts for
`ctx.generate(..., output=Summary)` and `ctx.generate(..., tools=[...])` are
decided author forms whose request fields remain open (M4, M5).

**Related:** [`ExecutionPolicy`](authorization.md#executionpolicy),
[outbound checks](../prompt-safeguarding.md#check-at-the-boundary-not-just-once).

## `ModelResponse`

**Purpose / direction:** normalized model result with honest usage/completion
information, not an A2A or MCP transport envelope.

**Signature (implemented, M3 #33):** frozen dataclass.

```text
ModelResponse(text: str, usage: Usage, finish_reason: FinishReason)
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `text` | `str` | Required | Generated text; possibly truncated when `finish_reason` is not `COMPLETED`. |
| `usage` | [`Usage`](runtime.md#runresult) | Required | Tokens for this call; unknown counts are `None`, never zero; `estimated` marks estimates. |
| `finish_reason` | [`FinishReason`](runtime.md#runresult) | Required | `COMPLETED`, `LENGTH`, `REFUSED` or `CONTENT_FILTER`. |

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
`IncompleteOutputError` (partial-result opt-in syntax open) and never returns
them as success. Tool-call linkage and typed structured content remain open
(M4, M5). Failed-run usage retrieval remains open; no hard billing guarantee
is implied.

**Related:** [`Model.generate`](#modelgenerate),
[accounting and enforcement](../limits.md#accounting-and-enforcement).
