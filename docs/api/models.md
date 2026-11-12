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
through OpenAI v1 **Responses API** using the official `openai` Python package,
not deploy a hosted agent. The API/SDK choice is confirmed; the AgentFrame
constructor below remains a proposal.

**Proposed credential-based constructor shape:**

```text
FoundryModel(*, endpoint: str, deployment: str, credential: ...)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `endpoint` | `str` | Explicit in current shape | OpenAI v1 base URL `https://<resource>.openai.azure.com/openai/v1/`; final AgentFrame endpoint normalization remains open. Not a Foundry project agents endpoint. |
| `deployment` | `str` | Explicit selection required | Deployment identifier; no automatic provisioning or fallback. |
| `credential` | Async Azure token credential in existing examples; final wrapper types open | Required for this shape | Trusted application injection; chosen auth paths are Entra ID token provider or API key. Mapping this proposed parameter to the OpenAI client remains open; no invented `client=` or API-key constructor keyword. |

```python
from azure.core.credentials_async import AsyncTokenCredential
from agentframe import FoundryModel


def build_model(
    endpoint: str, deployment: str, credential: AsyncTokenCredential
) -> FoundryModel:
    return FoundryModel(
        endpoint=endpoint, deployment=deployment, credential=credential,
    )
```

Azure imports are from optional provider dependencies. The application supplies
the endpoint, deployment, and supported credential; no credential chain is
implicitly selected by this example.
The selected SDK accepts an API key or an Entra token provider. Microsoft's
[migration guide](https://learn.microsoft.com/en-us/azure/foundry/how-to/model-inference-to-openai-migration)
uses `azure-identity`'s `get_bearer_token_provider` with scope
`https://ai.azure.com/.default`. The async credential above is an illustrative
AgentFrame input, not a claim that the OpenAI client directly accepts that
object; async wrapping and final accepted types remain to be implemented.
Keep `openai` and `azure-identity` optional provider extras. The Azure AI
Inference SDK is retired and is not the selected path. No dependency is
installed or pinned by this reference.

**Behavior / failures:** implements the [`Model.generate`](#modelgenerate)
contract. Reject unsupported API/features/settings explicitly; preserve
authentication versus provider/configuration failure distinctions without
leaking credential/provider payloads. Runtime bounds retries and attempts.
Exact client typing, ownership transfer, and lifecycle APIs remain open.
API selection does not prove a deployment supports every Responses feature;
capability checks, normalized results/usage and Python 3.13-3.15 compatibility
still need implementation evidence.

**Related:** [`ModelSettings`](#modelsettings),
[Foundry guide](../agent-examples.md#switch-to-foundry),
[resolved platform/API choices](../initial-outline.md#resolved-m0-platform-and-api-decisions).

## `OllamaModel`

**Purpose / decision:** call an explicit Ollama endpoint and installed model tag,
initially for locally executed Llama. Llama is a model family, not a host.

**Proposed constructor shape:**

```text
OllamaModel(*, model: str, endpoint: str)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `model` | `str` | Explicit selection required | Already-installed model tag; illustrative tags are not a version/support guarantee. |
| `endpoint` | `str` | Explicit in current examples; default open | Application-selected Ollama server. Localhost alone does not prove a tag executes locally. |

```python
from agentframe import OllamaModel


model = OllamaModel(model="llama3.2:3b", endpoint="http://localhost:11434")
```

**Behavior / failures:** implements [`Model.generate`](#modelgenerate).
Never download a model, start a server, silently switch models, or enable cloud
execution. Unavailable endpoint/model, unsupported features/settings, and
provider errors fail explicitly. Local deployment verification, supported
client versions, and lifecycle details remain open.

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
| `capabilities` | `ModelCapabilities \| None` | Default `ModelCapabilities()` | Returned by `capabilities()`. |
| `requests` | `list[ModelRequest]` | Attribute | Every request `generate` received, for asserting on assembled messages and settings. |
| `closed` | `bool` | Attribute | Whether `close()` (or `async with` exit) ran. |

```python
from agentframe import Message, ModelRequest, ModelSettings, ProviderError
from agentframe.testing import ScriptedModel


async def test_retry_after_rate_limit() -> None:
    model = ScriptedModel([ProviderError("Rate limited.", transient=True), "Hello!"])
    request = ModelRequest(
        messages=(Message(role="user", content="Hi"),), settings=ModelSettings()
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

**Signature (implemented, M3 #33):** frozen dataclass.

```text
ModelSettings(max_output_tokens: int = 4096)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `max_output_tokens` | `int` | `4096`, configurable | Canonical per-model-call output allowance, not a second `RunLimits` field. Must be greater than zero (`DefinitionError` otherwise). Must fit model context, host ceilings and remaining run budget; reasoning-token inclusion is provider/API-specific. |

```python
from agentframe import ModelSettings


settings = ModelSettings(max_output_tokens=800)
```

**Behavior / failures:** 800 is an override; the default is 4096. Validate
settings against the actual deployment and effective limits; unsupported
requested settings fail, not disappear. Typed provider-specific options are
intended, but no universal temperature/reasoning fields or option registry are
settled. Precedence is library → agent → skill → per-run within host ceilings,
merged field by field (#42). Do not duplicate output tokens in `RunLimits`.

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
| `settings` | [`ModelSettings`](#modelsettings) | Required | The effective settings for this call. |

```python
from agentframe import Message, ModelRequest, ModelSettings


request = ModelRequest(
    messages=(Message(role="user", content="Hello"),),
    settings=ModelSettings(),
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
