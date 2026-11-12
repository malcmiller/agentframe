# Hosting

[Reference index and status conventions](README.md). These are design contracts,
not implemented servers or compatibility claims. Approved auth/protection
fields/defaults below are decided; unspecified composition details stay open. ASGI
binding/TLS and external infrastructure remain application responsibilities.

## `AgentApp`

**Purpose / decision:** compose an agent and common runtime with native A2A
hosting and optional MCP, rather than require a server subclass. Use one server,
one shared runtime and an endpoint per caller audience, not per skill: A2A for
agent callers, MCP for LLM apps/tools, and later AG-UI for frontends.

**Constructor status:** implemented in M2 (#3, #6) as
`AgentApp(*, agent, host, runtime=None, services=None, authorization=None,
router=None)`; the other parameters below arrive with their stories
(`a2a=`/`mcp=` later, M8+ `directory=`, later `ag_ui=`). `services=`, `directory=` and `ag_ui=`
are decided; unspecified omission details remain illustrative.

```text
AgentApp(
    *, agent: Agent, host: HostConfig, runtime: Runtime = ...,
    a2a: A2AConfig = ..., mcp: MCPConfig = ..., router: SkillRouter = ...,
    services: Services = ...,
    directory: AgentDirectory = ..., ag_ui: AGUIConfig = ...,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `agent` | `Agent` instance | Required in current shape | Valid registered skills and identity; no per-method A2A RPC endpoints. |
| `host` | `HostConfig` | Explicit in current examples; omission contract open | Advertised origin, authentication integration, independent resource ceilings. |
| `runtime` | `Runtime` | Optional; default `Runtime(services=services, authorization=authorization)` | The single authoritative runtime and service container for every request. Passing it together with `services=` or `authorization=` raises `DefinitionError`. |
| `a2a` | `A2AConfig` | A2A enabled by default; exact object default open | Selected native binding and implemented features only. |
| `mcp` | `MCPConfig` | MCP disabled by default; exact object default open | Explicit optional allowlisted skill publication. |
| `router` | [`SkillRouter`](#skillrouter) implementation | Optional; default `None` refuses free text | Selects the skill and arguments for free-text A2A messages or asks a question. Custom router in M2 (#6); built-in opt-in model router in M6. Activated structured selection bypasses it, not runtime controls. |
| `services` | `Services` | Optional; only without `runtime=` | Dependencies for `Inject[T]`. Construction validates every skill's injected services; app owns/closes singletons, AgentFrame creates/closes per-run scoped instances. |
| `authorization` | `AuthorizationPolicy` | Optional; only without `runtime=` | Resource policy applied to every run, as on [`Runtime`](runtime.md#runtime). |
| `directory` | `AgentDirectory` | M8+ when remote bindings need discovery | Resolve `Remote["name"]`; validate configured names at startup. Sources/cache lifecycle follows ownership, not automatic transfer. |
| `ag_ui` | `AGUIConfig` | Later explicit opt-in, off unless enabled | `AGUIConfig(enabled=True)` serves `/ag-ui` with the same auth/permissions/limits. |

Supply an instance of [`Echo`](agents.md#agent) for this single-skill
composition. `auth` is an explicit `Authenticator`;
`authorization` supplies applicable permission/resource policy.

```python
from agentframe import (
    Agent, AgentApp, Authenticator, AuthorizationPolicy, HostConfig, Runtime,
)


def build_app(
    echo: Agent, auth: Authenticator, authorization: AuthorizationPolicy,
) -> AgentApp:
    return AgentApp(
        agent=echo,
        runtime=Runtime(authorization=authorization),
        host=HostConfig(
            public_url="https://echo.example.com",
            auth=auth,
        ),
    )
```

Supply `EntraIDAuth`, `APIKeyAuth`, explicit `AllowAnonymous`, or a custom
authenticator; no remote host starts without a choice.

**Implemented behavior (M2 #3):**

- `AgentApp` is an ASGI application; serve it with any ASGI server.
  Constructing it starts nothing and opens no sockets. It validates the agent
  definition, every skill's `Inject[T]` service and the Agent Card first, so an
  invalid agent, a missing service or an authenticator without
  [`card_security()`](authorization.md#declarescardsecurity) raises
  `DefinitionError` before anything is served.
- Server startup (the ASGI lifespan) then runs
  [`Runtime.validate_model`](runtime.md#runtimevalidate_model) (M3 #36): when
  the agent's model reports no context window and a skill has no
  `RunLimits.max_input_tokens`, startup fails with `DefinitionError` and no
  request is served. A context size is never guessed.
- `GET /.well-known/agent-card.json` serves the generated card publicly, without
  credentials.
- `POST /a2a` is the A2A 1.0 JSON-RPC endpoint from the official SDK. Every
  request to it, including every task operation, first passes `host.auth`.
  Missing or invalid credentials get HTTP 401 with the body
  `{"error": "Authentication required."}` and a `WWW-Authenticate` header
  (RFC 9110) naming the scheme the Agent Card declares, for example
  `APIKey location="header", name="X-API-Key"`: no task is created and nothing
  about the agent or its skills is revealed. A verified caller becomes the run's
  `Principal`, with permissions from `HostConfig.role_permissions`; the SDK
  scopes tasks by the principal's ID.
- Each message runs as one `Runtime.run`, so validation, permissions, the
  authorization policy, timeouts and per-run services all apply.
- Messages are skill calls through the AgentFrame
  [skill-call extension](#structured-skill-call-wire-format) (M2 #5). A message
  sent without activating the extension is free text, routed by the
  configured [`SkillRouter`](#skillrouter) (M2 #6). Without a router, free
  text is refused with the JSON-RPC `UnsupportedOperationError` (`-32004`)
  and creates no task.
- A message naming a `taskId` continues that task only while it is
  `INPUT_REQUIRED`; a working or finished task refuses it with
  `UnsupportedOperationError` (`-32004`). See [`SkillRouter`](#skillrouter).
- Results and failures map to native A2A as described in
  [result and failure mapping](#result-and-failure-mapping) (#7).
- Task operations are the SDK's native A2A 1.0 core methods; they are not
  advertised on the card. `SendMessage` blocks and returns the finished task by
  default. With `configuration.returnImmediately: true` it returns the task at
  once while the skill keeps running, and the caller polls with `GetTask`.
  `GetTask`, `ListTasks`, `CancelTask` and follow-up messages that name a
  `taskId` only reach the caller's own tasks: another caller's task gets the
  same `TaskNotFoundError` as an ID that does not exist.
- `CancelTask` on a running task marks it `CANCELED` and cancels the skill with
  ordinary `asyncio` cancellation, so its per-run (scoped) services are still
  cleaned up. Cancelling a finished task returns the standard
  `TaskNotCancelableError`.
- Tasks live only in the SDK's in-memory store and are lost on restart; a
  durable store is pluggable later.
- All `AllowAnonymous()` callers share one task namespace, so any anonymous
  caller holding a task's ID can get or cancel it. IDs are random UUIDs, but
  treat them as bearer secrets. `ListTasks` is refused to anonymous callers with
  `UnsupportedOperationError`, because it would otherwise reveal every
  anonymous caller's tasks, inputs and results. Use an authenticator when
  callers must not see each other's tasks.

### Result and failure mapping

**Implemented behavior (M2 #7):** a completed run's value becomes the single
part of the task's `result` artifact:

- `str` -> one text part.
- Everything else (`int`, `bool`, `None`, `list`, `dict`, Pydantic models and
  nested types) -> one data part `{"value": ...}` in Pydantic JSON mode.
- A value that cannot be serialized -> `OutputValidationError`, task `FAILED`.
  Serialization runs inside the run's error handling, so no partial artifact is
  published.

Every other outcome follows this table:

| Situation | Result |
| --- | --- |
| Missing or invalid credentials | HTTP 401 with `WWW-Authenticate`, no task |
| Malformed A2A request or extension misuse | Standard A2A/JSON-RPC protocol error, no task |
| Unknown skill, invalid arguments, permission or policy denied | Task `REJECTED`, safe message |
| Clarification needed | Task `INPUT_REQUIRED` |
| Timeout, output validation failure, skill or router exception, router timeout, policy unavailable | Task `FAILED`, safe message |
| Model error: unsupported capability, refusal, incomplete output, provider failure (M3 #33) | Task `FAILED`, safe message |
| Token or model-call limit exceeded (M3 #36) | Task `FAILED`, safe message |
| Cancelled | Task `CANCELED` |

- A malformed [skill call](#structured-skill-call-wire-format) is refused with
  JSON-RPC `InvalidParams` (`-32602`) before the SDK creates a task. Malformed
  JSON-RPC is rejected by the SDK the same way.
- `REJECTED` and `FAILED` tasks carry the error's safe message as the status
  message: no stack traces, provider payloads, submitted values (including the
  requested skill ID) or secrets. Exceptions raised by a skill are logged
  with their details and reported only as "The skill failed."; router
  exceptions likewise as "The skill router failed." A `DefinitionError` names agent internals, so it is logged and
  reported only as "The agent is not configured correctly."
- Every task status carries the run's `invocation_id` in the task metadata,
  including failures. The host allocates the ID before the run, so the caller,
  the skill's `ctx.invocation_id` and the log entry for a skill exception all
  share it. A failure raised before a run starts gets a new ID.
- Since M3 #36 every task status also carries a safe `usage` snapshot next to
  `invocation_id`, on success, failure, input-required and cancel:

  ```json
  {
    "invocation_id": "3f2c...",
    "usage": {
      "input_tokens": 17,
      "output_tokens": 8,
      "total_tokens": 25,
      "estimated": false,
      "model_calls": 2
    }
  }
  ```

  Token counts are `null` when unknown, never `0`: for example a cancelled task
  whose model call was still running. `estimated` is `true` when any count is
  a character-based estimate. Model-free runs report zeros. No provider
  payloads, `RunResult`, prompts or outputs are included.
- `INPUT_REQUIRED` with the question as the status message is published when
  a [`SkillRouter`](#skillrouter) asks for clarification (#6).
- `CancelTask` ends a running task in `CANCELED` through AgentFrame's
  [`CancelledError`](runtime.md#cancellederror) mapping, with the status message
  "The task was cancelled." and the run's `invocation_id`.

### Structured skill-call wire format

Implemented in M2 #5. A caller names a skill and sends JSON arguments; the
runtime validates and runs exactly that skill. A router is never consulted.

| Element | Wire format |
| --- | --- |
| Extension | `https://github.com/malcmiller/agentframe/extensions/skill-call/v1`, declared on the Agent Card with `required: false`. Its `params.skills` maps each skill ID to the JSON Schema of its arguments; requests are validated against the same schema. |
| Activation | The caller lists the URI in the `A2A-Extensions` HTTP request header (comma-separated with any others). Activation is per request. |
| Acknowledgement | The response to every authenticated request that activated it carries `A2A-Extensions: <uri>`. Other requested URIs are not supported and are ignored. |
| Skill ID | Message metadata key `agentframe.skill`: a non-empty string. |
| Arguments | Exactly one message part, a data part holding a JSON object (`{}` for a skill without arguments). |
| Other metadata | Other `agentframe.`-prefixed metadata keys are unknown fields. Keys outside that namespace belong to other extensions and are ignored. |

```http
POST /a2a HTTP/1.1
Content-Type: application/json
A2A-Version: 1.0
A2A-Extensions: https://github.com/malcmiller/agentframe/extensions/skill-call/v1
X-API-Key: <development key>

{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "9b2f4c1e-0d3a-4a51-8a54-2f0b9f7f8e10",
      "role": "ROLE_USER",
      "parts": [{"data": {"name": "Ada"}}],
      "metadata": {"agentframe.skill": "greet"}
    }
  }
}
```

| Request | Result |
| --- | --- |
| Extension activated, valid skill ID and arguments | The skill runs through `Runtime.run`; the task completes with its result. |
| Extension activated, but no skill ID, not exactly one part, a part that is not a JSON-object data part, or an unknown `agentframe.` metadata field | JSON-RPC `InvalidParamsError` (`-32602`); no task is created. Never reinterpreted as free text. |
| Extension activated, unknown skill ID or invalid arguments | Task `REJECTED` with a safe message. |
| Extension not activated | Free text, even if `agentframe.skill` is present: routed by the [`SkillRouter`](#skillrouter), or refused with `-32004` when none is configured. |

With the official Python client, activate the extension per call:

```python
from a2a.client import Client, ClientCallContext
from a2a.client.service_parameters import (
    ServiceParametersFactory,
    with_a2a_extensions,
)
from a2a.helpers import new_data_part
from a2a.types import Message, Role, SendMessageRequest

SKILL_CALL = "https://github.com/malcmiller/agentframe/extensions/skill-call/v1"


async def greet(client: Client, name: str) -> None:
    message = Message(
        message_id="m-1",
        role=Role.ROLE_USER,
        parts=[new_data_part({"name": name})],
        metadata={"agentframe.skill": "greet"},
    )
    activate = ClientCallContext(
        service_parameters=ServiceParametersFactory.create(
            [with_a2a_extensions([SKILL_CALL])]
        )
    )
    async for response in client.send_message(
        SendMessageRequest(message=message), context=activate
    ):
        print(response.task.status.state)
```

```python
import os

from agentframe import Agent, AgentApp, APIKey, APIKeyAuth, HostConfig, Services


def create_app(echo: Agent, services: Services) -> AgentApp:
    # Development placeholder: load real keys from secure configuration.
    auth = APIKeyAuth(keys={os.environ["ECHO_KEY"]: APIKey("dev", roles=["User"])})
    return AgentApp(
        agent=echo,
        host=HostConfig(
            public_url="http://127.0.0.1:8000",
            auth=auth,
            role_permissions={"User": ["echo:use"]},
        ),
        services=services,
    )
```

**Behavior / failures:** composing the ASGI application does not start a server.
Generate a native card from identity/skills plus actual supported host features;
do not expose instructions, secrets, private tools, or imaginary capabilities.
Reject invalid definitions/configuration and unsupported required adapters.
Messages, tasks, artifacts, cancellation, and safe failures follow the selected
official protocol/SDK version. Stream disconnection is not automatically task
cancellation; in-memory task storage is ephemeral. First release returns
complete validated results and must not advertise A2A streaming. Protocol tasks
do not establish built-in human approval pause/resume. Ordinary A2A callers receive only
the invocation ID in response metadata, not internal `RunResult` fields.
Safe failures map to failed/rejected task states as applicable; protocol failures
and input-required clarification retain native semantics.

**Not yet available (M7):** request deduplication, described next.

Duplicate A2A requests from the same verified caller with the same `messageId`
within the default 10-minute window return the original task instead of
re-running. Default storage is in-memory; a pluggable store supports multiple
instances. This is not exactly-once execution. Window/store configuration
syntax, atomicity details, conflicting-payload handling and message-only result
interaction remain open; never claim process restarts preserve memory.

**Agent Card (implemented, M2 #2):** the card is built with official SDK types
by a deterministic, I/O-free function from the `@agent`/`@skill` definition and
`HostConfig`:

| Card field | Source |
| --- | --- |
| `name`, `description`, `version` | `@agent` identity. |
| `skills` | Each skill's `id`, `name`, `description` and `tags`; nothing else. |
| `supportedInterfaces` | `HostConfig.public_url` + `/a2a`, `JSONRPC`, protocol `1.0`. |
| `capabilities` | `streaming` and `pushNotifications` false; one extension, `https://github.com/malcmiller/agentframe/extensions/skill-call/v1` (`required: false`), whose `params.skills` maps each skill ID to the JSON Schema of its business arguments. |
| `securitySchemes`, `securityRequirements` | The authenticator's [`card_security()`](authorization.md#declarescardsecurity), all required. `APIKeyAuth`: an `apiKey` scheme in the `X-API-Key` header. `AllowAnonymous`: none (open access). A custom authenticator without `card_security()` makes card generation raise `DefinitionError`, rather than publish a card that looks open. |
| `defaultInputModes`, `defaultOutputModes` | `text/plain` and `application/json`. |

`ctx`, `Inject[T]` parameters, method names, instructions, credentials and other
internals never appear on the card. The card is public discovery data: serving
it never requires credentials. Generation is internal to `AgentApp` (#3), not a
public method.

The proposed HTTP deployment uses `/.well-known/agent-card.json`, `/a2a`, and
optional MCP Streamable HTTP at `/mcp`; binding/version choices still need
verification. No extra public `serve()`, card-generation, or lifecycle method
signature is specified here.

For next-core-release [discovery](discovery.md#agentdirectory), host lifespan may
manage a library-created or explicitly transferred directory. Merely injecting
a directory does not transfer ownership. The decided constructor keyword is
`directory=`; no new lifecycle method is finalized. Directory refresh/removal is not A2A task
cancellation or agent deployment.
Future [AG-UI streaming](streaming.md) is a separate optional frontend adapter;
`ag_ui=AGUIConfig(enabled=True)` and `/ag-ui` are decided, not implemented
conformance. Disconnect cancels the AG-UI run with no first-version resume;
this does not redefine A2A stream-disconnect task semantics.

**Related:** [`HostConfig`](#hostconfig), [`SkillRouter`](#skillrouter),
[combined server example](../design/mcp-skills.md#proposed-server-composition).

## `agentframe run`

**Purpose / decision:** start one agent through the native host with one
command. Implemented in M2 (#9). The application's own factory function
returns a fully configured [`AgentApp`](#agentapp) (agent, services, router,
auth, policy) in plain, type-checked Python; there is no new configuration file
format. Secrets come from environment variables read inside the factory, never
from command-line flags. Serving uses [uvicorn](https://www.uvicorn.org/),
pinned in the optional `server` extra: `pip install 'agentframe[server]'`.

```text
agentframe run TARGET [--host HOST] [--port PORT] [--reload] [--dev]
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `TARGET` | `module:function` string | Required | Function called with no arguments that returns an `AgentApp`. With `--dev`, `module:AgentClass` instead. The working directory is importable. |
| `--host` | string | Default `127.0.0.1` | Address the server binds to. Not the advertised origin: the factory's `HostConfig(public_url=...)` is. |
| `--port` | integer | Default `8000` | Port the server binds to. |
| `--reload` | flag | Off | Restart when source files in the working directory change. Development only. |
| `--dev` | flag | Off | Serve an `@agent` class constructed without arguments, with `AllowAnonymous`, at `http://HOST:PORT`. Only localhost hosts (`localhost`, loopback addresses) are accepted; logs a warning that any local process can call it. |

```python
# myapp/hosting.py
import os

from agentframe import AgentApp, APIKey, APIKeyAuth, HostConfig

from myapp.agents import Echo


def create_app() -> AgentApp:
    keys = {os.environ["ECHO_API_KEY"]: APIKey(caller_id="dev-client")}
    return AgentApp(
        agent=Echo(),
        host=HostConfig(
            public_url="http://127.0.0.1:8000",
            auth=APIKeyAuth(keys=keys),  # Development placeholder credential.
        ),
    )
```

```shell
agentframe run myapp.hosting:create_app --host 127.0.0.1 --port 8000
agentframe run myapp.agents:Echo --dev
```

**Behavior / failures:** the app is built before the server starts, so problems
are reported as one `agentframe: error: ...` line with exit code 1 and nothing
is served: a target that is not `module:name`, a module that cannot be found,
a missing attribute, a target that is not callable, a factory returning
anything other than an `AgentApp` (an agent class without `--dev` suggests
`--dev`), an invalid agent or configuration (`DefinitionError` from
`AgentApp`), `--dev` with a non-localhost host, a non-agent class or one that
needs constructor arguments, and a missing Uvicorn installation. Errors raised by
the application's own code, including imports inside its modules, keep their
traceback. Importing agents never starts serving. Shutdown follows Uvicorn's
default graceful shutdown until the host grace period arrives in M7.
Multi-agent `agentframe.yaml` is M8+ ([`agentframe dev`](#agentframe-dev)).

**Related:** [single-agent development](../guides/development.md#one-agent-m2),
[`HostConfig`](#hostconfig), [authentication](authorization.md).

## `agentframe dev`

**Purpose / decision:** in M8+, start all named agents from `agentframe.yaml`
on localhost with generated development discovery/auth and one combined log.
Approved command: `agentframe dev`; not implemented tooling.

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agentframe.yaml` | YAML named-agent/module-path/port list | Default config file | Detailed keys, loading/bootstrap and overrides open; not the registry file schema. |
| Local registry | Generated YAML name/URL references | Automatic for named discovery | Path/conflict handling open; never overwrite user configuration silently. |
| Development auth | Managed API-key service identity | Automatic between dev agents | No fabricated OBO identity; key storage/rotation/cleanup open. Never put keys in registry or logs. |
| Combined log | Agent and invocation ID tags | Decided output | Preserve redaction/content-capture defaults; not durable workflow state. |

```shell
agentframe dev
```

**Behavior / failures:** report invalid config, port conflicts and partial
startup; bound cleanup of owned processes/resources. Development keys are not
production defaults. Container packaging is one container per agent; Kubernetes/
Foundry templates later, not infrastructure deployment management.

**Related:** [configuration and packaging examples](../guides/development.md),
[`AgentDirectory`](discovery.md#agentdirectory).

## `HostConfig`

**Purpose / decision:** separate actual deployment/security constraints from
agent defaults and adapter-specific settings.

**Constructor status:** `auth`, `role_permissions` and protection fields/defaults
below are decided. `HostConfig(public_url=..., auth=..., role_permissions=...)`
is implemented (M2 #2, #3): `public_url` must be an absolute http(s) URL without
credentials, query or fragment; `auth` must not be `None`; and each
`role_permissions` value must be a list of permission names, not a bare string.
Any violation raises `DefinitionError`. `role_permissions` defaults to no roles
and is stored read-only.

**Not yet available (M7):** `limits_ceiling`, `max_concurrent_runs`,
`max_request_bytes`, `max_response_bytes`, `shutdown_grace_seconds` and the
`/health` and `/ready` probes. Existing URL/ceiling composition is illustrative
where not specified.

```text
HostConfig(
    *, public_url: str, auth: Authenticator, role_permissions = ...,
    limits_ceiling: RunLimits = ...,
    max_concurrent_runs: int = 32, max_request_bytes: int = 1048576,
    max_response_bytes: int = 4194304, shutdown_grace_seconds: ... = 30,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `public_url` | `str` | Explicit in current shape | Advertised origin/endpoint configuration, not server binding or TLS setup. Do not derive it from untrusted forwarded headers. |
| `auth` | `Authenticator` | Required for remote hosting; no default | Built-in Entra/API key/explicit anonymous or custom; omitted choice refuses startup. |
| `role_permissions` | `Mapping[str, Iterable[str]]` | Optional; default no roles (implemented) | Maps verified roles, e.g. `{"Editor": ["documents:write"]}`; never trusts roles from skill arguments. Permissions are the union over the caller's verified roles; unknown roles grant nothing. Entra scope details remain open. |
| `limits_ceiling` | `RunLimits` | Independent host constraint; omission/default values open | Non-overridable ceilings, not agent defaults. Validate configured agent allowances before accepting work. |
| `max_concurrent_runs` | `int` | `32`, configurable | Excess is rejected immediately with a standard busy error, not indefinitely queued; exact binding error mapping open. |
| `max_request_bytes` | `int` | `1048576` (1 MiB), configurable | Bound ingress before work; protocol/encoding measurement details open. |
| `max_response_bytes` | `int` | `4194304` (4 MiB), configurable | Bound outgoing content; no silent truncation into successful output. Exact error mapping open. |
| `shutdown_grace_seconds` | Numeric seconds; exact type open | `30`, configurable | Stop accepting, drain up to grace, then cancel remaining runs and clean up scoped services. |

```python
# Not yet available: limits_ceiling arrives in M7.
from agentframe import Authenticator, HostConfig, RunLimits


def host_config(auth: Authenticator) -> HostConfig:
    return HostConfig(
        public_url="https://writer.example.com",
        auth=auth,
        limits_ceiling=RunLimits(
            max_input_tokens=8_000,
            max_total_tokens=16_000,
            max_model_calls=6,
            timeout_seconds=45,
        ),
    )
```

The application selects an authenticator. Explicit `limits_ceiling` values
above are example ceilings, not new library defaults; host protection defaults
are the decided values in the table.

**Behavior / failures:** require intentional authentication and HTTPS outside
local development. A card's security declaration is not enforcement.
Host security/ceilings cannot be weakened by agent/run options; reject invalid
or excessive explicit requests instead of silently clamping. Missing identity/
required integration fails protected work. The decided admission, byte and
shutdown fields must be enforced independently
of token limits. `/health` is liveness; `/ready` reports startup checks passed
and not shutting down. Startup includes auth/definition/service validation.
Probe status/body schema and byte-accounting/error details remain open.

**Related:** [`Principal`](authorization.md#principal),
[`AgentConfig`](configuration.md#agentconfig),
[host ceilings](../guides/limits.md#defaults-versus-ceilings).

## `A2AConfig`

**Purpose / decision:** configure the first-class A2A adapter, using official
protocol/SDK types and actual supported endpoint behavior. Accept preferred
structured selection plus JSON arguments through a declared/activated A2A 1.0
extension, and free-text requests through custom or opt-in model routing.

**Proposed known constructor fields:**

```text
A2AConfig(*, enabled: bool = ..., path: str = ...)
```

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `enabled` | `bool` | A2A enabled by default on `AgentApp`; field default open | Controls the adapter, not caller authorization. |
| `path` | `str` | `/a2a` in examples; concrete default open | Selected A2A binding endpoint, not a route per skill. |
| Binding | No constructor field | JSON-RPC over HTTP, A2A 1.0 only (decided M2) | Advertised as `protocolBinding: "JSONRPC"`, `protocolVersion: "1.0"`. No gRPC, HTTP+JSON/REST or 0.3 compatibility; requests without `A2A-Version: 1.0` are rejected. |
| Task-store integration, supported capabilities | Exact fields/types open | Select/verify before release | Reuse SDK interfaces; first-release complete results only. Do not advertise streaming/push/task features the host cannot execute. |
| Structured requests | A2A extension declaration/activation, data-part arguments; [wire format implemented (M2 #5)](#structured-skill-call-wire-format) | Preferred request style; no constructor keyword | Card declares support; caller selects a skill under that extension, bypassing routing but not authorization/typed validation/limits/accounting/tracing. |
| Prompt clarification | Native A2A task/status message; exact contract open | Required for ambiguous/missing prompt input | Return `TASK_STATE_INPUT_REQUIRED` with an ask-back, not a guessed invocation or successful final result. |
| Duplicate requests | Same caller + A2A `messageId`; store/window fields open | 10-minute window, in-memory default; configurable/pluggable | Return original task instead of re-running; multi-instance store support, no exactly-once claim or task-access bypass. |
| Response correlation/errors | Native response metadata/task states; exact mappings open | Only invocation ID exposed from run internals | Safe failed/rejected states as appropriate; no `RunResult`, raw provider exception, stack trace or secrets. |
| Long-running tasks | Native A2A task store; constructor setting open | In-memory default, persistent store pluggable | Task persistence is not Python skill checkpointing, human approval resume or exactly-once execution. |

```python
from agentframe import A2AConfig


a2a = A2AConfig(enabled=True, path="/a2a")
```

**Behavior / failures:** structured selection is deterministic with no routing
model call/cost; the selected skill can still run inference. Unknown skill IDs
or invalid arguments fail explicitly, without falling back to prompt routing.
Extension-unaware callers use free text; ambiguous/missing prompt input asks
back using the native input-required task state, never the first registered
skill or a guess. Native [extension/part/task mechanisms](../design/mcp-skills.md#a2a-request-styles-and-extension-boundary)
are verified against A2A 1.0, and the [skill-call wire format](#structured-skill-call-wire-format)
is implemented; the clarification/follow-up API remains open.
Typed business-input/output mappings and task lifecycle still need
version-specific evidence, including typed structured outputs. Re-authorize protected task/resource access.
Invalid/unsupported configuration and protocol failures must be explicit and
safe; no copied schemas or custom response envelope.
For M8+ peers the negotiated AgentFrame extension also carries deadline,
token ceiling/actual usage and max-5-hop depth accounting. Ordinary callers
still receive no internal result metadata beyond invocation correlation.
Remote client input-required raises `ClarificationNeededError`; the remote task
remains input-required, not automatically failed or conversed with.

**Related:** [`SkillRouter`](#skillrouter),
[native SDK types](#native-a2a-and-mcp-types),
[A2A routing guide](../guides/agent-examples.md#a2a-hosting-and-selection).

## `MCPConfig`

**Purpose / decision:** optional native MCP publication of explicitly
allowlisted registered skills, separate from internal model-callable `@tool`s.

**Proposed constructor shape:**

```text
MCPConfig(*, enabled: bool = ..., path: str = ..., exposed_skills: list[str] = ...)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `enabled` | `bool` | MCP disabled by default on `AgentApp`; field default open | Explicitly opt into the optional adapter/dependency. |
| `path` | `str` | `/mcp` in HTTP examples; default open | Native Streamable HTTP endpoint, not per-skill REST routes. Stdio, if added, is a separate transport. |
| `exposed_skills` | `list[str]` in examples | Empty by default; opt-in per skill | Only registered stable IDs. No wildcard, auto-exposure, or authorization grant. Exact default construction syntax remains proposed. |

```python
from agentframe import MCPConfig


mcp = MCPConfig(
    enabled=True, path="/mcp", exposed_skills=["summarize", "rewrite"],
)
```

Use with an agent registering those IDs, such as the guide's
[`WritingAssistant`](../guides/agent-examples.md#with-a-model).

**Behavior / failures:** initial mapping uses stable skill IDs as native MCP
tool names and excludes context, `Inject[T]` services, credentials and execution internals from
schemas. Reject unknown IDs, naming collisions, unsupported schemas, missing
optional dependencies, and context/policy argument injection; never silently
skip/rename. MCP named selection bypasses A2A routing, not validation,
authorization, limits, checks, or tracing. Apply discovery policy and enforce it
again at invocation; hidden names cannot remain callable through that policy.

Use SDK-native initialization, version negotiation, tool results, and errors.
Exact structured-result mapping (including the design's possible scalar
`{"value": ...}` wrapper), authentication hooks, and async-task support remain
open/version-dependent, not settled wire fields or features. Sharing an A2A
host does not prove MCP authorization conformance. Custom REST remains deferred.
Tool-like deterministic/typed/quick/single-shot skills are good MCP candidates;
judgment, multi-turn, clarification or long-running skills usually stay A2A-only.
This is guidance, not an enforced restriction on model-backed MCP tools.

**Related:** [`@tool`](decorators.md#tool),
[schema mapping](../design/mcp-skills.md#schema-and-call-mapping),
[shared security](../design/mcp-skills.md#shared-execution-and-security).

## `SkillRouter`

**Purpose / decision:** select an eligible skill and extract its arguments for
free-text A2A requests, or ask the caller a question; no author-written
mandatory handler. Custom routers are implemented in M2 (#6); the built-in
opt-in model-based router follows in M6. Activated structured requests select
deterministically and never call the router.

**Signature status:** implemented in M2 (#6) as a structural `Protocol` with
one async method; any class with a matching `route` works without inheritance.
Router budget propagation and the M6 model router's construction remain open.

```python
class SkillRouter(Protocol):
    async def route(
        self, conversation: Sequence[Message], skills: Sequence[SkillDefinition]
    ) -> SkillCall | Clarification: ...
```

| Parameter / return | Type | Meaning and constraints |
| --- | --- | --- |
| `conversation` | `Sequence[a2a.types.Message]` | The task's native A2A messages so far, oldest first: the caller's messages and the agent's earlier questions. The last message is the one being routed. |
| `skills` | `Sequence[SkillDefinition]` | The agent's skills whose `requires_permissions` the caller holds, recomputed for every message. |
| Returns | `SkillCall(skill, arguments)` | Run skill `skill` with the business `arguments` dict. The skill must be one of `skills`; the arguments are validated like any caller's. |
| Returns | `Clarification(question)` | A non-empty question for the caller when the request is ambiguous or misses input. Never guess or pick the first skill. |

`SkillCall`, `Clarification` and `SkillRouter` are exported from `agentframe`.

```python
from collections.abc import Sequence

from a2a.helpers import get_message_text
from a2a.types import Message, Role

from agentframe import Clarification, SkillCall, SkillDefinition, SkillRouter


class GreetingRouter(SkillRouter):
    """Routes "greet <name>" to the greet skill; asks for a missing name."""

    async def route(
        self, conversation: Sequence[Message], skills: Sequence[SkillDefinition]
    ) -> SkillCall | Clarification:
        words = " ".join(
            get_message_text(m) for m in conversation if m.role == Role.ROLE_USER
        ).split()
        if "greet" not in words:
            return Clarification("What would you like me to do?")
        name = words[words.index("greet") + 1 :]
        if not name:
            return Clarification("Whom should I greet?")
        return SkillCall("greet", {"name": name[-1]})
```

Pass it as `AgentApp(agent=..., host=..., router=GreetingRouter())`.

**Implemented behavior (M2 #6):**

| Situation | Result |
| --- | --- |
| No router configured, free text | JSON-RPC `UnsupportedOperationError` (`-32004`), no task. |
| `SkillCall` to an offered skill | The skill runs through the runtime: argument validation, permissions and the authorization policy all apply. |
| `SkillCall` to a skill not offered (unknown, or the caller lacks its permissions), invalid arguments, an empty question or any other return value | Task `REJECTED` with a safe message that does not echo the skill ID. |
| `Clarification` | Task `INPUT_REQUIRED` with the question as the status message. |
| Router raises | Task `FAILED` with "The skill router failed."; details are logged with the `invocation_id`. |
| Router does not answer in time | Task `FAILED` with a timeout message. |

- **Continuation:** the caller answers with a new message naming the task's
  `taskId` (its `contextId` may be omitted). The router runs again with the
  whole conversation, including its earlier question. Each message is a new run
  with its own `invocation_id`, and eligible skills, permissions and the
  [authorization policy](authorization.md#authorizationpolicy) are checked
  again. An answer may also be a structured skill call, which skips the router.
- Only the task's owner can answer: another caller gets `TaskNotFoundError`
  (`-32001`), as for any task operation. All `AllowAnonymous()` callers share
  one owner, so treat task IDs as bearer secrets there.
- A message for a task that is working or finished is refused with
  `UnsupportedOperationError` (`-32004`) and changes nothing. Two answers sent
  at the same moment can both pass that check; the SDK processes one task's
  messages in order, so the second is handled after the first (and refused if
  the task has finished by then).
- **Bounded routing:** the skill is not known while routing, so routing must
  finish within the agent-level `limits.timeout_seconds` (not a skill's own
  limits), and the time it takes counts against the selected
  skill's timeout: the skill's deadline is measured from when routing started.
- A router cannot grant permissions; selection is not authorization.
  Clarification is not human approval or durable workflow resumption.

**Related:** [`Runtime.invoke`](runtime.md#runtimeinvoke),
[result and failure mapping](#result-and-failure-mapping),
[routing design](../guides/annotations.md#skill-registration-versus-request-routing).

## Built-in model-based router (planned implementation)

**Purpose / decision:** first-release library-supplied opt-in selection for
free-text multi-skill A2A, alongside custom `SkillRouter` implementations.
M6 follows M3/M4; injecting a model into an agent never activates it.
Structured extension requests bypass this router and its model-selection cost.

**Signature status:** public implementation name, constructor/factory, selected
model configuration, selection/argument-extraction decisions and clarification
interface remain open. No new class export or constructor fields are settled.

| Concern | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| Model routing opt-in | Explicit router configuration; syntax open | Disabled without explicit selection | Does not follow from `Agent(model=...)`; custom routers remain supported. |
| Model and eligible skills | Normalized model dependency and authorized candidate contracts; open | Needed for model selection | Apply outbound checks and request-wide budget before inference; cannot invent skills or grant permission. |
| Selected skill/business arguments | Typed decision representation; open | Must be validated before invocation | Unknown/invalid decisions fail explicitly; ambiguity/missing arguments asks back through `TASK_STATE_INPUT_REQUIRED`. Exact clarification contract remains open. |

This example accepts an **already explicitly configured library model router**
through the proposed interface. It does not invent its constructor or make the
agent's model double as router configuration.

```python
from agentframe import (
    Agent, AgentApp, Authenticator, AuthorizationPolicy, HostConfig, Runtime, SkillRouter,
)


def build_routed_app(
    agent: Agent, model_router: SkillRouter,
    development_auth: Authenticator,
    development_authorization_policy: AuthorizationPolicy,
) -> AgentApp:
    return AgentApp(
        agent=agent,
        router=model_router,
        runtime=Runtime(authorization=development_authorization_policy),
        host=HostConfig(
            public_url="http://127.0.0.1:8000",
            auth=development_auth,
        ),
    )
```

The application chooses a real authenticator and supplies resource policy.
`APIKeyAuth` is suitable for a development example; `AllowAnonymous()` would
be a deliberate access choice, not an omitted configuration.

**Behavior / failures:** authorize admission/restrict candidates, budget/check
the selection model, validate extraction and re-check skill/resource policy
before execution. Exhaustion, refusal, invalid decisions, ambiguity and policy
failures use the decided typed errors; exact protocol/clarification details
remain open. Never enable
automatic multi-agent orchestration, implicit tools or caller authority.
Input-required clarification is not completed output, human approval workflow
or durable execution resumption.

**Related:** [`SkillRouter`](#skillrouter),
[routing and extraction](../guides/annotations.md#skill-registration-versus-request-routing),
[remaining execution contracts](../design/base-classes.md#decided-execution-contracts-and-remaining-details).

## Native A2A and MCP types

Reuse official A2A `AgentCard`, `AgentSkill`, `Message`, `Part`, `Task`, and
`Artifact`, task stores, and request-handling components where possible.
Reuse native MCP tool results and SDK protocol handling. These are **not new
AgentFrame classes** or separately maintained wire schemas.

A2A protocol 1.0 and the official `a2a-sdk` are selected (specification
release v1.0.1). M2 pins `a2a-sdk[http-server]==1.3.0` as a core dependency
because A2A hosting is the default interface (the optional `agentframe[server]`
extra only adds Uvicorn for `agentframe run`); the `http-server` extra is listed explicitly rather than relied on transitively.
The selected binding is **JSON-RPC over HTTP** for protocol 1.0 only, with the
SDK's 0.3 compatibility disabled. `tests/unit/hosting/test_a2a_binding.py` proves an official
SDK client exchanging a message over that binding and the rejection of pre-1.0
requests; CI imports the server binding from a clean wheel install. `tests/integration/test_interoperability.py`
proves native type mappings and independent official-client interoperability
with AgentFrame hosting end to end over localhost HTTP (M2 #10). MCP version selection is separate. Constructor signatures/imports
are deliberately not copied as stable AgentFrame APIs. See
[resolved platform/API choices](../roadmap.md#resolved-m0-platform-and-api-decisions).
No fabricated
MCP/A2A payload fields, REST envelopes, or unverified capabilities are part of
this reference.

**Related:** [native type boundary](../design/base-classes.md#native-a2a-types),
[delivery milestones](../roadmap.md#delivery-milestones).
