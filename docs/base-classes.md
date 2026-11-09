# Base classes and interfaces

These are proposed responsibilities, not stable class signatures. Start with
the smallest working surface; add supporting types when implementation needs
them, rather than scaffolding an entire platform in advance.

## Agent authoring and execution

| Type | Definition |
| --- | --- |
| `Agent` | Base class for behavior and immutable authoring configuration. Supports model-free handlers and optional injected models. Does not own transport or provider SDK details. |
| `RunContext` | Per-invocation model access, verified caller context, deadline, cancellation, and correlation data. Provides convenience helpers such as `generate`. Never reused as mutable global state. |
| `AgentConfig` | Validated defaults for instructions and execution limits. A default is distinct from a non-overridable host policy ceiling. |
| `AgentApp` | ASGI integration that adapts supported A2A requests to agent behavior, publishes the native card, and coordinates task lifecycle and owned resources through the official SDK. |
| `HostConfig` | Advertised endpoint, supported binding, authentication integration, task-store choice, and actual optional protocol capabilities. Separate from model and agent settings. |

`AgentApp` must own the mapping from A2A messages to handlers explicitly. An
internal Python handler name must not become an undocumented remote RPC method.
Task state transitions and cancellation must follow the selected A2A version.
Disconnecting a stream is not automatically equivalent to canceling a task.

## Model interfaces

| Type | Definition |
| --- | --- |
| `Model` | A Python `Protocol` for asynchronous generation through normalized requests and responses. Does not require inheritance from a vendor-specific class. |
| `FoundryModel` | Adapter for a configured Foundry model deployment and an explicitly supported inference API. Accepts injected credentials or a configured SDK client. |
| `OllamaModel` | Adapter for an explicit Ollama endpoint and model tag, initially targeting locally executed Llama models. Never downloads or switches models implicitly. |
| `ModelSettings` | Validated shared generation settings with typed provider-specific options. Unsupported requested settings fail explicitly. |
| `ModelCapabilities` | Supported features of the actual model/deployment, including streaming, tools, structured output, and accepted modalities. Unknown support is not assumed to be available. |
| `ModelRequest` | Provider-neutral messages, optional tool definitions, output contract, and permitted generation settings. Keeps instructions distinct from user content. |
| `ModelResponse` | Normalized generated content, optional tool calls, available usage data, and completion status. Missing usage remains unknown, not zero. |

The minimal contract needs non-streaming text generation first. Streaming events
and richer content should be introduced only with corresponding adapter tests.
There is no claim that every model host supports the same features or produces
identical results.

Resource ownership must be explicit: library-created clients are closed by the
library; injected clients remain caller-owned unless ownership is explicitly
transferred. Clients and credential providers must not be constructed at module
import time.

## Native A2A types

Reuse `AgentCard`, `AgentSkill`, `Message`, `Part`, `Task`, and `Artifact` from
the selected official A2A SDK release. Reuse SDK task-store interfaces and
request-handling components where possible.

AgentFrame should not create competing wire models, rename protocol states, or
copy SDK schemas into a separately maintained contract.

## Errors and extension points

Distinguish definition/configuration errors, input/output validation failures,
authentication/authorization failures, unsupported model features, provider
failures, deadline expiration, and cancellation. Exact exception names remain
an implementation decision.

Keep detailed diagnostics in redacted, correlated application logs. Return safe
protocol errors to remote callers, not stack traces, credentials, or raw provider
responses.

Allow explicit injection of model implementations, provider clients, credential
providers, supported task stores, and authentication integration. Avoid a
global plugin registry or custom configuration language in the first release.
