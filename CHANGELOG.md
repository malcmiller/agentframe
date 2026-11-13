# Changelog

All notable changes to AgentFrame are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Pre-1.0: public APIs may change in minor releases; changes are listed in the
changelog.

## [Unreleased]

### Added

- **Discovery (M8+):** read-only `AgentRegistry` base with `AgentReference`
  (agent name and Agent Card URL) and `AgentQuery` (name filter), plus
  `FileAgentRegistry` (YAML `name`/`url` list) and `EnvironmentAgentRegistry`
  (comma-separated `name=url` pairs). PyYAML is a new core dependency.
- **Discovery (M8+):** `AgentDirectory(sources=[...])` returns
  `DiscoveredAgent` results with validated official SDK Agent Cards, cached
  for 300 seconds, with `allow_stale`, `refresh`, `allow_http_hosts` and an
  optional caller-owned `http_client`. New `RemoteAgentError` for failures
  caused by another agent. `httpx` is now an exactly pinned core dependency.
- **Discovery (M8+):** `AgentDirectory.find(skill=..., tags=...)` returns the
  Agent Cards of listed agents with a skill matching the ID and every tag,
  in source order. It fetches at most eight cards at a time through the cache
  and raises on any source or card failure instead of returning fewer cards.
- **Discovery (M8+):** `FoundryAgentRegistry(project_endpoint=...,
  credential=...)` lists a Foundry project's A2A-enabled agents, mapped to
  their A2A 1.0 Agent Card URLs. New optional `AgentRegistry.card_headers`
  lets a source send its own credentials with its card requests; Foundry
  sends a bearer token from its credential. The `foundry` extra now pins
  `azure-ai-projects==2.11.0`.
- **Remote agents (M8+):** skills declare another A2A agent as a
  `Remote[Literal["name"]]` parameter (excluded from arguments, the Agent Card
  and schemas), found through `Runtime(directory=...)` or
  `AgentApp(directory=...)`; server startup checks every name with
  `Runtime.validate_remotes`. `await remote.ask(text)` sends plain A2A text and
  returns the text answer within the run's deadline, cancelling the remote
  task on deadline or cancellation. New `ClarificationNeededError` when the
  remote agent asks a question.
- **Remote agents (M8+):** `await remote.call("skill", output=Type, **arguments)`
  runs a remote AgentFrame agent's skill by ID through the skill-call
  extension and validates its result into `output` (`OutputValidationError`
  on mismatch). A peer whose card lacks the extension or the skill raises
  `RemoteAgentError` before anything is sent, with no free-text fallback.
  `output` and `max_total_tokens` are reserved argument names.
- **Outbound identity (M8+):** `remotes={name: Service(credential=...)}` on
  `Runtime` and `AgentApp` authenticates remote calls with the calling
  agent's own identity: `APIKeyCredential(key, header="X-API-Key")` or
  `EntraAppCredential(credential, scope=...)` (any `azure.core` async token
  credential, sent as a bearer token), or any `OutboundCredential`.
  Credentials go per request only to the card's validated interface origin,
  never with redirects, and incoming caller credentials are never forwarded.
  Agents whose card declares security, including Foundry A2A agents, can now
  be called; startup fails with `DefinitionError` when the configured
  credential cannot satisfy the card's security.

### Fixed

- URLs with a non-numeric or out-of-range port are refused as
  `HostConfig.public_url` and registry card URLs.
- `FileAgentRegistry` strips whitespace around names and URLs, like
  `EnvironmentAgentRegistry`.

## [0.1.0] - 2026-11-13

The first usable single-agent release, distributed on PyPI as `agentframe-py`
and imported as `agentframe`.

### Added

- **Authoring:** `Agent` subclasses with `@agent` identity and one async
  `@skill` method per advertised skill; Python types and Pydantic models as
  validated input and output contracts; `Inject[T]` services registered with
  `Services.add` / `add_scoped`; per-invocation `RunContext`.
- **Local runtime:** `Runtime.invoke` returns a validated value and
  `Runtime.run` a `RunResult` with invocation ID, usage, model and tool calls,
  duration and finish reason. `RunLimits`, `ModelSettings` and `RunOptions`
  resolve field by field from library to agent, skill and run.
- **A2A 1.0 hosting:** `AgentApp` serves a generated native Agent Card and a
  JSON-RPC endpoint through the official `a2a-sdk`. Structured requests name a
  skill through the Agent Card's skill-call extension; free text uses a custom
  `SkillRouter` or the opt-in `ModelRouter`, which asks back with
  `TASK_STATE_INPUT_REQUIRED` when a request is ambiguous.
  `agentframe run module:create_app` serves an application (`server` extra).
- **Models:** `OllamaModel` (local Llama, `ollama` extra) and `FoundryModel`
  (Microsoft Foundry GPT deployments, `foundry` extra), switched by
  configuration only. One `ctx.generate` returns text, a validated Pydantic
  instance with `output=`, or runs registered `@tool` functions with
  `tools=[...]` within `max_tool_calls`. Refusal, truncation and unsupported
  capabilities raise typed errors. `ScriptedModel` in `agentframe.testing`
  scripts model replies for tests.
- **Security:** `HostConfig(auth=...)` with `APIKeyAuth`, `EntraIDAuth`
  (`entra` extra), explicit `AllowAnonymous` or a custom `Authenticator`;
  skill `requires_permissions`, role mapping and an injected
  `AuthorizationPolicy`, failing closed; optional `ExecutionPolicy` input,
  model-request, tool and output checks.
- **Hardening:** host ceilings for concurrent runs, request and response size,
  and graceful shutdown; `/health` and `/ready`; A2A message deduplication by
  caller and message ID; safe, correlated errors with redacted operator logs.
- **Examples:** runnable examples in `examples/`, each checked against the
  installed wheel by `python -m examples.smoke`.

[0.1.0]: https://github.com/malcmiller/agentframe/releases/tag/v0.1.0
