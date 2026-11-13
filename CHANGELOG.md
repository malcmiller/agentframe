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
