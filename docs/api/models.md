# Models

[Reference index and status conventions](README.md). These are proposed
AgentFrame-owned abstractions, not wrappers around another agent framework.
Provider dependencies are optional and stay out of skill business logic.
**Decided:** AgentFrame owns normalized messages, tool calls/results, token
usage and finish reasons; adapters translate to/from provider types. Concrete
field layouts and enum values not specified here remain open, not provider leaks.

## `Model`

**Purpose / decision:** a structural Python protocol for asynchronous generation.
Custom implementations do not need to inherit from `Agent` or a vendor base.

**Signature status:** no constructor; the known method is
[`generate(request)`](#modelgenerate). Capability reporting and lifecycle
requirements remain open.

| Contract concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Generation | Async method accepting `ModelRequest`, returning `ModelResponse` | Proposed complete-result operation | First release covers text, typed structured outputs, and tool calls/results with bounded continuations; translate provider data/errors explicitly. |
| Capabilities/lifecycle | Exact members open | Must be specified before adapter support | Unknown features are not supported by assumption; injected clients remain caller-owned unless explicitly transferred. |

```python
from agentframe import Model, ModelRequest, ModelResponse


class CustomModel:
    def __init__(self, client: Model) -> None:
        self.client = client

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return await self.client.generate(request)
```

This structural delegation example wraps an already normalized `Model`, not an
arbitrary vendor client.

**Behavior / failures:** a real adapter must translate requests, responses,
feature support, and failures. Do not silently drop requested settings, assume
usage is known, or perform inference on import. Exact validation and provider
exception mappings remain open.
Normalized types are AgentFrame-owned; detailed layouts and provider mappings
still need implementation evidence. Unsupported requested capabilities raise
`UnsupportedCapabilityError` before sending. Managed generation returns text,
validated Pydantic output via `output=`, or registered tool continuations via
`tools=`. Refusal raises `ModelRefusalError`; token-limit truncation raises
`IncompleteOutputError` unless a skill explicitly opts into partial results
(syntax open). Streaming remains later, with optional [AG-UI](streaming.md) mapping
from managed execution, not raw provider events.

**Related:** [`ModelRequest`](#modelrequest), [`ModelResponse`](#modelresponse),
[model interface rationale](../base-classes.md#model-interfaces).

## `Model.generate`

**Purpose / direction:** low-level provider-neutral generation used beneath
the managed `RunContext.generate` helper.

**Proposed minimal signature:**

```python
async def generate(self, request: ModelRequest) -> ModelResponse: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `request` | `ModelRequest` | Required | Normalized messages/settings and selected typed-output/tool contracts for first-release features; concrete fields open. |

```python
from agentframe import Model, ModelRequest, ModelResponse


async def delegate(model: Model, request: ModelRequest) -> ModelResponse:
    return await model.generate(request)
```

**Behavior / failures:** returns normalized content, available usage, and
completion status through `ModelResponse`. Unsupported features/settings and
provider failures must be explicit. A raw adapter call is not a managed skill
invocation and does not by itself establish authorization, invocation budgets,
or context; skills should use [`ctx.generate`](agents.md#runcontextgenerate).
Complete validated results are the first-release path; no streaming method/
event contract is finalized. Refused/incomplete/invalid structured output and
invalid tool calls/results must be distinguished from successful completion;
exact normalized layouts remain open. Translate provider outcomes for the
managed helper's decided [typed errors](runtime.md#modelerror); do not return
refusal as fake text. Runtime retries only transient provider failures, default
two retries with backoff and provider-SDK retries disabled; exact retry
configuration syntax/backoff schedule remain open.

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

## `ModelSettings`

**Purpose / decision:** typed generation settings for individual model requests,
separate from aggregate invocation budgets and actual model context capacity.

**Decided canonical field/default:**

```text
ModelSettings(*, max_output_tokens: int = 4096)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `max_output_tokens` | `int` | `4096`, configurable | Canonical per-model-call output allowance, not a second `RunLimits` field. Must fit model context, host ceilings and remaining run budget; reasoning-token inclusion is provider/API-specific. |

```python
from agentframe import ModelSettings


settings = ModelSettings(max_output_tokens=800)
```

**Behavior / failures:** 800 is an override; the default is 4096. Validate
settings against the actual deployment and effective limits; unsupported
requested settings fail, not disappear. Typed provider-specific options are
intended, but no universal temperature/reasoning fields or option registry are
settled. Precedence is library -> agent -> skill -> per-run within host ceilings.
The exact skill/per-run carrier for model settings remains open; skill run
budgets use `@skill(limits=RunLimits(...))`. Do not duplicate output tokens in
that limits object.

**Related:** [`AgentConfig`](configuration.md#agentconfig),
[`RunLimits`](configuration.md#runlimits), [limit scopes](../limits.md#separate-the-scopes).

## `ModelCapabilities`

**Purpose / decision:** describe actual model/deployment support, not
caller-configured promises.

**Signature status:** constructor, field names, and reporting mechanism open.
The rows below are conceptual information, not proposed keyword arguments.

| Information | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Context capacity | Representation open | Must be established for relevant preflight checks | Input plus reserved output must fit actual context, including provider-specific rules; unknown is not unlimited. |
| Streaming, tools, structured output, modalities | Feature representation open | Only claim verified support | Do not assume feature parity across hosts or models. |

```python
from agentframe import ModelCapabilities


def describe_capabilities(capabilities: ModelCapabilities) -> str:
    return type(capabilities).__name__
```

**Behavior / failures:** no capability-construction API or property names are
implied here. Unsupported requested capabilities fail before sending with
`UnsupportedCapabilityError`; unknown is not supported by assumption.
First-release text/structured/tool capabilities require corresponding adapter
evidence on supported deployments. Streaming and additional modalities remain
later; feature reporting and schema support cannot be assumed from host name.

**Related:** [`Model`](#model), [context capacity](../limits.md#separate-the-scopes).

## `ModelRequest`

**Purpose / direction:** normalized outbound generation input shared by model
adapters, keeping trusted instructions distinct from untrusted content.

**Signature status:** constructor and exact fields open.

| Information | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Messages/content | Provider-neutral representation open | Needed for generation | Preserve instruction/user/retrieval/tool provenance; never concatenate untrusted text into privileged instructions. |
| Tool definitions/output contract | Types open | Optional per request; first-release capability | Only explicitly registered, authorized tools; typed structured output requires a supported schema/capability contract, not a text fallback. |
| Generation settings | Permitted normalized settings; layout open | Effective settings apply | Validate actual model support, input size, output reservation, and remaining allowance before dispatch. |

```python
from agentframe import Model, ModelRequest, ModelResponse


async def submit(model: Model, request: ModelRequest) -> ModelResponse:
    return await model.generate(request)
```

The caller supplies an already assembled request; no guessed wire fields or
constructor are needed.

**Behavior / failures:** managed execution applies outbound checks to every
request, including model-backed routing and later requests with tool/history
content. Invalid/unsupported requests, denied egress, and exceeded allowances
must stop dispatch. Exact validation types and hook decisions remain open.
Messages, tool call/result linkage, usage and finish reasons use AgentFrame-owned
normalized types. The separate author forms `ctx.generate(..., output=Summary)`
and `ctx.generate(..., tools=[...])` are decided; exact normalized constructors,
schema subset and combinations of
typed output/tools remain open. No provider-specific skill business logic.

**Related:** [`ExecutionPolicy`](authorization.md#executionpolicy),
[outbound checks](../prompt-safeguarding.md#check-at-the-boundary-not-just-once).

## `ModelResponse`

**Purpose / direction:** normalized model result with honest usage/completion
information, not an A2A or MCP transport envelope.

**Signature status:** constructor and exact fields open.

| Information | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Generated content | Normalized text/typed structured representation open | Depends on selected first-release operation/status | Return complete validated results; no partial JSON as validated output. |
| Tool calls/results linkage | Representation open | Optional per request; first-release bounded loop capability | Model proposals are not permission; validate calls/results and feed authorized results into bounded generation continuations. |
| Usage/completion/refusal/incomplete status | Typed representation open | Distinct outcomes needed; unknown usage stays unknown | Refusal or truncated output is not successful completion. Distinguish actuals, estimates and unresolved reservations without double-counting subdivisions. |

```python
from agentframe import ModelResponse


def describe_response(response: ModelResponse) -> str:
    return type(response).__name__
```

**Behavior / failures:** provider failures cannot be disguised as successful
generated text. Missing final usage after cancellation/failure is not zero or
proof that a call was free. Exact normalized field names still need design;
no hard billing guarantee is implied.
The first release must not return success on a provider refusal, incomplete
generation, invalid structured value or denied tool action. Refusal raises
`ModelRefusalError`, invalid structured output `OutputValidationError`, and
token-limit truncation `IncompleteOutputError` by default. A skill may explicitly
allow partial results (opt-in syntax open), never invalid structured output.
`Runtime.run` returns `RunResult` on success; errors carry `invocation_id`.
Failed-run usage retrieval remains open, not a success-shaped response.

**Related:** [`Model.generate`](#modelgenerate),
[accounting and enforcement](../limits.md#accounting-and-enforcement).
