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
policy=None, router=None, a2a=None, directory=None)`, with `policy=` from M7
#77, `a2a=` from M7 #75 and `directory=` from M8+ #87; the other parameters
below arrive with their stories (`mcp=` later, later `ag_ui=`). `services=` and
`ag_ui=` are decided; unspecified omission details remain illustrative.

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
| `runtime` | `Runtime` | Optional; default `Runtime(services=services, authorization=authorization, policy=policy, directory=directory)` | The single authoritative runtime and service container for every request. Passing it together with `services=`, `authorization=`, `policy=` or `directory=` raises `DefinitionError`. Pass a runtime to authorize tool calls: `AgentApp(runtime=Runtime(services=..., tool_authorization=...))`; there is no separate host setting (M5 #62). |
| `a2a` | [`A2AConfig`](#a2aconfig) | Optional; `None` means `A2AConfig()` (M7 #75) | A2A endpoint settings; today only [message deduplication](#a2aconfig). A2A is always served. |
| `mcp` | `MCPConfig` | MCP disabled by default; exact object default open | Explicit optional allowlisted skill publication. |
| `router` | [`SkillRouter`](#skillrouter) implementation | Optional; default `None` refuses free text, even when the agent has a model | Selects the skill and arguments for free-text A2A messages or asks a question. Custom router in M2 (#6); built-in opt-in [`ModelRouter`](#modelrouter) in M6 (#68), checked at construction and startup (#69). This is the only way to enable routing; an agent model never does. Activated structured selection bypasses it, not runtime controls. The app never closes a router's model. |
| `services` | `Services` | Optional; only without `runtime=` | Dependencies for `Inject[T]`. Construction validates every skill's injected services; app owns/closes singletons, AgentFrame creates/closes per-run scoped instances. |
| `authorization` | `AuthorizationPolicy` | Optional; only without `runtime=` | Resource policy applied to every run, as on [`Runtime`](runtime.md#runtime). |
| `policy` | [`ExecutionPolicy`](authorization.md#executionpolicy) | Optional; only without `runtime=` (M7 #77) | Input, model-request and output checks for every run, as on [`Runtime`](runtime.md#runtime). Model-request checks also cover routing model calls, with `skill=None`. |
| `directory` | [`AgentDirectory`](discovery.md#agentdirectory) | Optional; only without `runtime=`; required when a skill takes a `Remote` parameter (M8+ #87) | Where [`Remote[Literal["name"]]`](discovery.md#remoteliteralname) parameters find their agents. Construction fails without it; server startup looks up every name with [`Runtime.validate_remotes`](runtime.md#runtimevalidate_remotes) and fails for an unknown name or an invalid or credential-requiring card. The application opens and closes it; the app never does. |
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
- With [`Remote`](discovery.md#remoteliteralname) parameters (M8+ #87),
  construction needs `directory=`, and startup then runs
  [`Runtime.validate_remotes`](runtime.md#runtimevalidate_remotes): an unknown
  name or an invalid or credential-requiring card fails startup.
- With a [`ModelRouter`](#modelrouter) (M6 #69), construction also builds its
  selection and extraction schemas, and startup checks its model: see
  [`ModelRouter` hosting](#modelrouter-hosting). Custom routers are not
  checked; they own their own validation.
- The app never closes the agent's or the router's model; the application
  owns them. The same model instance may serve both.
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
- At most `HostConfig.max_concurrent_runs` messages run at once (M7 #72).
  Every `/a2a` request is checked in a fixed order before the SDK sees it:
  body size (M7 #73), authentication, [deduplication](#a2aconfig) (M7 #75),
  then admission. A repeated message takes no slot, so it gets its original
  task even while the host is full. A `SendMessage` beyond the limit, blocking or
  `returnImmediately`, gets HTTP 503 with `Retry-After: 1` and the body
  `{"error": "The agent is busy; retry later."}`: it is never queued, no task is
  created and no skill runs. A2A 1.0 defines no busy JSON-RPC error, so the
  official SDK client raises `A2AClientError` whose cause is an
  `httpx.HTTPStatusError` with status 503. A slot is held from admission until
  the run ends (completed, failed, timed out, cancelled or asking a question),
  including after a non-blocking response; a message the SDK refuses before
  running frees it at once. A task waiting for input (`INPUT_REQUIRED`) holds
  no slot, so its answer is admitted like a new message. `GetTask`,
  `ListTasks`, `CancelTask` and the Agent Card are never counted or refused.
- `GET /health` (liveness) answers `200 {"status": "ok"}` whenever the app is
  serving. `GET /ready` (readiness) answers `200 {"status": "ready"}` only
  after lifespan startup, including the model checks above, succeeded and
  before shutdown starts; otherwise `503 {"status": "not ready"}`. Both need
  no credentials, reveal nothing about the agent, are not on the Agent Card
  and have fixed paths (M7 #74).
- Graceful shutdown (M7 #74) is bounded by
  `HostConfig.shutdown_grace_seconds`, measured once from when shutdown
  starts. From then on `/ready` is 503 and a new `SendMessage` gets HTTP 503
  with `Retry-After: 1` and `{"error": "The agent is shutting down; retry
  later."}`, with no task; `GetTask`, `ListTasks`, `CancelTask` and the card
  keep working. Running runs that finish within the grace period complete
  normally, and the app's shutdown ends as soon as none is left. Runs still
  going after it are cancelled as `CancelTask` would: their tasks end
  `CANCELED` and their per-run services are cleaned up. Tasks waiting for
  input have no run and are left as they are. Shutdown starts on the shutdown
  signal under [`agentframe run`](#agentframe-run). Under another ASGI server
  it starts at lifespan shutdown, after that server's own wait for open
  requests, so set that server's graceful timeout to at least
  `shutdown_grace_seconds` (for example Uvicorn's
  `--timeout-graceful-shutdown`); the total can then reach about twice the
  grace period.

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
| Request body over `max_request_bytes` (M7 #73), checked before authentication | HTTP 413 with `{"error": "Request too large."}`, no task |
| Missing or invalid credentials | HTTP 401 with `WWW-Authenticate`, no task |
| `SendMessage` while `max_concurrent_runs` runs are running (M7 #72) | HTTP 503 with `Retry-After: 1`, no task |
| `SendMessage` once shutdown has started (M7 #74) | HTTP 503 with `Retry-After: 1`, no task |
| `SendMessage` repeating a verified caller's `messageId` within the dedup window (M7 #75) | The original task, whatever its state; nothing runs and no slot is taken |
| `SendMessage` reusing a verified caller's `messageId` for a different message (M7 #75) | JSON-RPC `InvalidParams`, no task |
| Malformed A2A request or extension misuse | Standard A2A/JSON-RPC protocol error, no task |
| Unknown skill, invalid arguments, permission or policy denied, including a tool call (M5 #62) or an execution policy check on the input, a model request, a tool call or the output (M7 #77, #78) | Task `REJECTED`, safe message; a blocked output is never published |
| Clarification needed | Task `INPUT_REQUIRED` |
| Timeout, output validation failure, skill or router exception, router timeout, policy unavailable (skill or tool policy, M5 #62; execution policy check, M7 #77, #78) | Task `FAILED`, safe message |
| Model error: unsupported capability, refusal, incomplete output, provider failure (M3 #33; structured output proven end to end in M4 #57) | Task `FAILED`, safe message; never the model's text |
| Token, model-call or tool-call limit exceeded (M3 #36, M5 #61) | Task `FAILED`, safe message |
| Result or clarification question over `max_response_bytes` (M7 #73) | Task `FAILED` with `LimitExceededError`'s "The result exceeds the host's response size limit", `invocation_id` and usage; no artifact or question is sent |
| A tool raises (M5 #61) | Task `FAILED`, reported like a skill exception: "The skill failed.", details logged with the `invocation_id` |
| Cancelled | Task `CANCELED` |

- A malformed [skill call](#structured-skill-call-wire-format) is refused with
  JSON-RPC `InvalidParams` (`-32602`) before the SDK creates a task. Malformed
  JSON-RPC is rejected by the SDK the same way.
- `REJECTED` and `FAILED` tasks carry the error's safe message as the status
  message: no stack traces, provider payloads, submitted values (including the
  requested skill ID) or secrets. Exceptions raised by a skill are logged
  with their details and reported only as "The skill failed."; router
  exceptions likewise as "The skill router failed.", except `AgentFrameError`s
  such as model or limit errors, which keep their safe messages (M6 #67).
  A `DefinitionError` names agent internals, so it is logged and
  reported only as "The agent is not configured correctly."
- Every task status carries the run's `invocation_id` in the task metadata,
  including failures. The host allocates the ID before the run, so the caller,
  the skill's `ctx.invocation_id` and the log entry for a skill exception all
  share it. A failure raised before a run starts gets a new ID.
- Server logs (M7 #79): every `FAILED` run logs one warning with its
  `invocation_id`, error class and safe message; never the error's cause,
  arguments, prompts or results. Skill, tool and router exceptions are also
  logged with their traceback under the same ID, for operators only, so skill
  authors must not put secrets in exception messages. Rejections and cancels
  are not logged. `tests/integration/test_hardening.py` checks that no
  argument values, prompts or credentials reach callers or logs.
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
      "model_calls": 2,
      "tool_calls": 1
    }
  }
  ```

  Token counts are `null` when unknown, never `0`: for example a cancelled task
  whose model call was still running. `estimated` is `true` when any count is
  a character-based estimate. `tool_calls` (M5 #61) counts every tool call
  the model asked for, as in `RunResult.tool_calls`. Model-free runs report zeros. No provider
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

**Message deduplication (implemented, M7 #75):** a `SendMessage` from the
same verified caller with the same `messageId` within
[`A2AConfig.dedup_window_seconds`](#a2aconfig) (default 10 minutes) returns the
original task, whatever its state, instead of running again; see
[`A2AConfig`](#a2aconfig) for the full behavior and its limits.

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

For [discovery](discovery.md#agentdirectory), `directory=` does not transfer
ownership: the application opens the directory before serving and closes it
after. Directory refresh/removal is not A2A task cancellation or agent
deployment.
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
pinned in the optional `server` extra: `pip install 'agentframe-py[server]'`.

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
traceback. Importing agents never starts serving. If the server then fails
to start, for example because a startup model check fails, the command also
exits with code 1.

On SIGTERM or Ctrl+C (M7 #74), the app's
[graceful shutdown](#agentapp) starts at once, from Uvicorn's signal handler:
`/ready` turns 503 and new messages are refused. Uvicorn stops accepting
connections and waits for open requests, at most
`HostConfig.shutdown_grace_seconds` (rounded up to whole seconds); the app
then waits only for what remains of the same grace period before cancelling
leftover runs. The process exits within about one grace period plus a small
margin. With `--reload`, Uvicorn's wait is bounded the same way but the app's
shutdown starts only after it.
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
and is stored read-only. `limits_ceiling` is implemented (M7 #71): an optional
`RunLimits` that caps every hosted run, described in the table below.
`max_concurrent_runs` is implemented (M7 #72), and `max_request_bytes` and
`max_response_bytes` (M7 #73); a value below 1 for any of them raises
`DefinitionError`. `shutdown_grace_seconds` is implemented (M7 #74); a value
that is not positive raises `DefinitionError`. Existing URL/ceiling
composition is illustrative where not specified.

```text
HostConfig(
    *, public_url: str, auth: Authenticator, role_permissions = ...,
    limits_ceiling: RunLimits | None = None,
    max_concurrent_runs: int = 32, max_request_bytes: int = 1048576,
    max_response_bytes: int = 4194304, shutdown_grace_seconds: float = 30,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `public_url` | `str` | Explicit in current shape | Advertised origin/endpoint configuration, not server binding or TLS setup. Do not derive it from untrusted forwarded headers. |
| `auth` | `Authenticator` | Required for remote hosting; no default | Built-in Entra/API key/explicit anonymous or custom; omitted choice refuses startup. |
| `role_permissions` | `Mapping[str, Iterable[str]]` | Optional; default no roles (implemented) | Maps verified roles, e.g. `{"Editor": ["documents:write"]}`; never trusts roles from skill arguments. Permissions are the union over the caller's verified roles; unknown roles grant nothing. `EntraIDAuth` supplies app roles and delegated scopes as roles (M7 #76). |
| `limits_ceiling` | `RunLimits \| None` | Optional; default `None` caps nothing (implemented) | Independent host constraint, not another default layer. Each field it sets caps that limit after library -> agent -> skill -> per-run merging; unset fields cap nothing. A library default above the ceiling is lowered to it; an agent or skill limit explicitly set above it fails `AgentApp` construction with `DefinitionError` naming the skill and field. Hosted runs, free-text routing (deadline and budget) and startup model checks use the capped limits; a `max_input_tokens` ceiling stands in for an unknown or larger context window. A `max_total_tokens` ceiling also bounds `ModelSettings.max_output_tokens`: a call whose output cap does not fit fails with `TokenLimitExceededError`, never silently shrunk. Local `Runtime` takes no ceiling. |
| `max_concurrent_runs` | `int` | `32`, configurable; at least 1 (implemented) | How many messages may run at once. Excess `SendMessage` requests get HTTP 503 with `Retry-After: 1` and the body `{"error": "The agent is busy; retry later."}` before the SDK sees them, never queued and with no task created. A task waiting for input holds no slot. Task reads, lists and cancels are not counted. See [`AgentApp`](#agentapp). |
| `max_request_bytes` | `int` | `1048576` (1 MiB), configurable; at least 1 (implemented) | Largest `/a2a` request body, in bytes as received. Checked first, before authentication: a declared `Content-Length` above it is refused without reading the body, and a chunked body is refused as soon as it passes the limit, so it is never held in full or parsed. Refusal: HTTP 413 with `{"error": "Request too large."}`, no task. A body exactly at the limit is accepted. Independent of token limits. |
| `max_response_bytes` | `int` | `4194304` (4 MiB), configurable; at least 1 (implemented) | Largest result or clarification question a task returns: a text result's or question's UTF-8 bytes, or the compact JSON of a data part's `{"value": ...}`; the JSON-RPC envelope is not counted. A larger one fails the task with `LimitExceededError` and the safe message "The result exceeds the host's response size limit"; none of it is sent. Never truncated. Task history is bounded by `max_request_bytes`. |
| `shutdown_grace_seconds` | `float` | `30`, configurable; positive (implemented) | How long shutdown waits for running work, measured once from when it starts: stop accepting new messages, drain running runs up to the grace period, then cancel the rest (tasks `CANCELED`, scoped services cleaned up). See [`AgentApp`](#agentapp) and [`agentframe run`](#agentframe-run). |

```python
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
shutdown fields are enforced independently
of token limits (M7 #72, #73, #74). `/health` is liveness; `/ready` reports
startup checks passed and not shutting down. Startup includes auth/definition/
service validation. The probe bodies (`{"status": "ok"}`, `{"status":
"ready"}`, `{"status": "not ready"}`), the busy and shutting-down errors
(HTTP 503) and the size errors (HTTP 413 and `LimitExceededError`) are
decided.

**Related:** [`Principal`](authorization.md#principal),
[`AgentConfig`](configuration.md#agentconfig),
[host ceilings](../guides/limits.md#defaults-versus-ceilings).

## `A2AConfig`

**Purpose / decision:** configure the first-class A2A adapter, using official
protocol/SDK types and actual supported endpoint behavior. Accept preferred
structured selection plus JSON arguments through a declared/activated A2A 1.0
extension, and free-text requests through custom or opt-in model routing.

**Constructor status:** implemented in M7 #75 as
`A2AConfig(*, dedup_window_seconds=600, dedup_store=None)`, passed as
`AgentApp(a2a=...)`. `enabled` and `path` stay proposed and unimplemented:
A2A is always served at `/a2a`.

```text
A2AConfig(*, dedup_window_seconds: float = 600, dedup_store: MessageDedupStore | None = None)
```

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `dedup_window_seconds` | `float` | `600` (10 minutes), configurable; must be positive or `DefinitionError` (implemented) | How long a verified caller's `messageId` is remembered. The same caller resending it within the window gets the original task back. |
| `dedup_store` | [`MessageDedupStore`](#messagededupstore) or `None` | `None`: in-process memory (implemented) | Where `messageId` claims are kept. Supply a shared store for multi-instance hosts. |
| `enabled` | `bool` | Proposed, not implemented | Would control the adapter, not caller authorization. |
| `path` | `str` | Proposed, not implemented; today always `/a2a` | Selected A2A binding endpoint, not a route per skill. |
| Binding | No constructor field | JSON-RPC over HTTP, A2A 1.0 only (decided M2) | Advertised as `protocolBinding: "JSONRPC"`, `protocolVersion: "1.0"`. No gRPC, HTTP+JSON/REST or 0.3 compatibility; requests without `A2A-Version: 1.0` are rejected. |
| Task-store integration, supported capabilities | Exact fields/types open | Select/verify before release | Reuse SDK interfaces; first-release complete results only. Do not advertise streaming/push/task features the host cannot execute. |
| Structured requests | A2A extension declaration/activation, data-part arguments; [wire format implemented (M2 #5)](#structured-skill-call-wire-format) | Preferred request style; no constructor keyword | Card declares support; caller selects a skill under that extension, bypassing routing but not authorization/typed validation/limits/accounting/tracing. |
| Prompt clarification | Native A2A task/status message; exact contract open | Required for ambiguous/missing prompt input | Return `TASK_STATE_INPUT_REQUIRED` with an ask-back, not a guessed invocation or successful final result. |
| Response correlation/errors | Native response metadata/task states; exact mappings open | The invocation ID and a safe usage snapshot (token counts, estimated flag, model and tool call counts) (M3 #36) | Safe failed/rejected states as appropriate; no `RunResult`, raw provider exception, stack trace or secrets. |
| Long-running tasks | Native A2A task store; constructor setting open | In-memory default, persistent store pluggable | Task persistence is not Python skill checkpointing, human approval resume or exactly-once execution. |

```python
from agentframe import A2AConfig, Agent, AgentApp, HostConfig


def build_app(agent: Agent, host: HostConfig) -> AgentApp:
    # Remember each caller's messageIds for 5 minutes instead of 10.
    return AgentApp(agent=agent, host=host, a2a=A2AConfig(dedup_window_seconds=300))
```

**Message deduplication (implemented, M7 #75):** a caller that retries after a
network failure resends its message with the same `messageId`; the retry gets
the original task instead of running the skill again.

- The key is the verified caller's ID plus the `messageId`. Each `/a2a` request
  is checked in order: body size, authentication, deduplication, then
  admission. A repeat therefore takes no run slot and gets its task even while
  `max_concurrent_runs` is reached or shutdown has started.
- A repeat within the window gets the original task as it is now, whatever its
  state: working, completed, failed, cancelled, rejected or waiting for input.
  Failed and cancelled tasks are not re-run.
- Answers to input-required tasks (messages with a `taskId`) are deduplicated
  with the same key, so a repeated answer is not routed twice.
- Reusing a `messageId` for a different message, compared by a SHA-256 hash of
  its parts and metadata (or naming a different task), is refused with JSON-RPC
  `InvalidParams` ("messageId was already used for a different message").
- Two copies sent at once run the skill once: repeats of one caller's message
  are handled one at a time within a process, and the later copy gets the
  task the first one created.
- A message refused before it ran (busy, shutting down, or a protocol error)
  is not a repeat: its retry runs normally.
- **Anonymous callers are not deduplicated.** They share one namespace, so a
  guessable `messageId` would let one anonymous caller fetch another's task.
- **No bypass of task authorization.** The original task is looked up as the
  caller, exactly as `GetTask` would; another caller using the same
  `messageId` gets its own task and never sees the first caller's.
- **Not exactly-once.** With the default in-memory store, claims are **lost on
  restart** and not shared between instances. After the window expires the
  same `messageId` runs again. A shared store across instances still needs the
  tasks themselves to be visible to every instance, and concurrent copies on
  different instances can both run. Applications that need exactly-once side
  effects use idempotent backends ([retries](../guides/limits.md#retries-and-duplicate-requests)).

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

## `MessageDedupStore`

**Purpose / decision:** the pluggable store behind
[message deduplication](#a2aconfig), so hosts with several instances can share
`messageId` claims. Implemented in M7 #75 as a structural protocol; implement
it without inheriting from anything.

```python
class MessageDedupStore(Protocol):
    async def put_if_absent(
        self, key: str, task_id: str, ttl_seconds: float
    ) -> str | None:
        """Store key -> task_id unless present; return the existing task_id if it was."""
```

| Parameter | Type | Meaning |
| --- | --- | --- |
| `key` | `str` | Identifies one verified caller's `messageId`. Treat it as opaque. |
| `task_id` | `str` | The task the message will run in. |
| `ttl_seconds` | `float` | How long to keep the entry: `A2AConfig.dedup_window_seconds`. |

Returns `None` when it stored `task_id`, or the task ID already stored under
`key`. **It must be atomic:** of two concurrent calls with one key, exactly one
may store its task ID. The default keeps entries in a dict with expiry times,
pruned on use, and loses them on restart.

```python
from agentframe import A2AConfig, MessageDedupStore


class SharedDedupStore:  # Application-defined, for example over Redis SET NX EX.
    async def put_if_absent(
        self, key: str, task_id: str, ttl_seconds: float
    ) -> str | None: ...


store: MessageDedupStore = SharedDedupStore()
a2a = A2AConfig(dedup_store=store)
```

**Related:** [`A2AConfig`](#a2aconfig),
[retries and duplicate requests](../guides/limits.md#retries-and-duplicate-requests).

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
M6 (#67) added the required `ctx: RoutingContext` parameter (a pre-release
breaking change) so router model calls are charged to the run. The M6 model
router is [`ModelRouter`](#modelrouter) (#68).

```python
class SkillRouter(Protocol):
    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification: ...
```

| Parameter / return | Type | Meaning and constraints |
| --- | --- | --- |
| `conversation` | `Sequence[a2a.types.Message]` | The task's native A2A messages so far, oldest first: the caller's messages and the agent's earlier questions. The last message is the one being routed. |
| `skills` | `Sequence[SkillDefinition]` | The agent's skills whose `requires_permissions` the caller holds, recomputed for every message. |
| `ctx` | [`RoutingContext`](#routingcontext) | The message's routing context. Make model calls with `ctx.generate(model, ...)` so they are charged to the run. |
| Returns | `SkillCall(skill, arguments)` | Run skill `skill` with the business `arguments` dict. The skill must be one of `skills`; the arguments are validated like any caller's. |
| Returns | `Clarification(question)` | A non-empty question for the caller when the request is ambiguous or misses input. Never guess or pick the first skill. |

`SkillCall`, `Clarification`, `SkillRouter` and `RoutingContext` are exported
from `agentframe`.

```python
from collections.abc import Sequence

from a2a.helpers import get_message_text
from a2a.types import Message, Role

from agentframe import (
    Clarification,
    RoutingContext,
    SkillCall,
    SkillDefinition,
    SkillRouter,
)


class GreetingRouter(SkillRouter):
    """Routes "greet <name>" to the greet skill; asks for a missing name."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
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
| No router configured, free text | JSON-RPC `UnsupportedOperationError` (`-32004`), no task and no model call, even when the agent has a model (M6 #69). |
| `SkillCall` to an offered skill | The skill runs through the runtime: argument validation, permissions and the authorization policy all apply. |
| `SkillCall` to a skill not offered (unknown, or the caller lacks its permissions), invalid arguments, an empty question or any other return value | Task `REJECTED` with a safe message that does not echo the skill ID. |
| `Clarification` | Task `INPUT_REQUIRED` with the question as the status message. |
| Router raises an `AgentFrameError` (M6 #67), such as `ModelRefusalError`, `ProviderError` or a `LimitExceededError` from `ctx.generate` | Task in that error's state (`FAILED` for these) with the error's own safe message. A `DefinitionError` still gets the generic "not configured correctly" message. |
| Router raises any other exception | Task `FAILED` with "The skill router failed."; details are logged with the `invocation_id`. |
| Router does not answer in time, including while a routing model call is running | Task `FAILED` with a timeout message (`TimeoutLimitExceededError`). |

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
- **Routing budget (M6 #67):** model calls made through `ctx.generate` use
  the run's accounting, so routing tokens and model calls count against the
  selected skill's `max_total_tokens` and `max_model_calls`; the skill only
  gets what remains. For example, with `max_model_calls=2` on the agent, a
  router that makes two calls leaves a model-backed skill none
  (`ModelCallLimitExceededError`), while a model-free skill still runs. Task
  usage metadata on `COMPLETED`, `INPUT_REQUIRED` and `FAILED` includes routing
  calls. Structured skill calls never create a `RoutingContext` and make no
  routing calls.
- A router cannot grant permissions; selection is not authorization.
  Clarification is not human approval or durable workflow resumption.

**Related:** [`RoutingContext`](#routingcontext),
[`Runtime.invoke`](runtime.md#runtimeinvoke),
[result and failure mapping](#result-and-failure-mapping),
[routing design](../guides/annotations.md#skill-registration-versus-request-routing).

## `RoutingContext`

**Purpose / decision:** per-message context passed to
[`SkillRouter.route`](#skillrouter), so model calls made while routing are
charged to the same run as the selected skill (M6 #67). Created by the host
for free-text messages only; never shared between messages.

**Signature status:** implemented in M6 (#67) as a frozen dataclass. Routers
read its fields and call `generate`; they do not construct it.

| Field | Type | Meaning |
| --- | --- | --- |
| `invocation_id` | `str` | The run being routed; the selected skill runs under the same ID. |
| `principal` | `Principal \| None` | Verified caller identity, or `None`. |
| `deadline` | `float` | Event-loop time (`loop.time()`) when routing times out: the agent's `timeout_seconds` from the start of the run. |

### `RoutingContext.generate`

```python
async def generate(
    self,
    model: Model,
    text: str | Sequence[Message],
    *,
    instructions: str | None = None,
    output: type[T] | None = None,
    model_settings: ModelSettings | None = None,
) -> str | T: ...
```

| Parameter | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `model` | `Model` | Required | The router's own model. The agent's model is never used implicitly; pass the same instance explicitly to share it. |
| `text` | `str \| Sequence[Message]` | Required | Untrusted content: one `user` message, or a conversation of AgentFrame [`Message`](models.md#message)s with only plain `user` (caller) and `assistant` (agent's earlier questions) roles, sent after the instructions (M6 #68). Any other role or tool calls raise `DefinitionError` before sending. |
| `instructions` | `str \| None` | `None` | Trusted routing instructions, sent as a `system` message. The agent's own `instructions` are not sent; they are for its skills. |
| `output` | `type[BaseModel] \| None` | `None` | Validate the reply as this Pydantic model, as in [`RunContext.generate`](agents.md#runcontextgenerate). |
| `model_settings` | `ModelSettings \| None` | `None` | Router override; each field set replaces the library and agent value. |

- **Settings and limits:** the skill is not known yet, so routing calls use
  the library defaults merged with the agent's `model_settings` and `limits`,
  plus the router's `model_settings` override. No tools are offered.
- **Accounting:** each call reserves and settles against the run's shared
  accounting before the skill runs, with the same input, context-window,
  token, model-call and retry rules as a skill's `ctx.generate`.
- **Failures:** raises the same errors as `RunContext.generate`. They reach the
  task with their own safe messages (see [`SkillRouter`](#skillrouter)).

```python
from collections.abc import Sequence

from a2a.helpers import get_message_text
from a2a.types import Message
from pydantic import BaseModel

from agentframe import (
    Clarification,
    Model,
    RoutingContext,
    SkillCall,
    SkillDefinition,
    SkillRouter,
)


class Choice(BaseModel):
    skill: str


class OneShotRouter(SkillRouter):
    """Asks its own model which skill to run; each skill takes ``text``."""

    def __init__(self, model: Model) -> None:
        self.model = model

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        text = get_message_text(conversation[-1])
        choice = await ctx.generate(
            self.model,
            text,
            instructions="Reply with the id of one of: "
            + ", ".join(skill.id for skill in skills),
            output=Choice,
        )
        if choice.skill not in {skill.id for skill in skills}:
            return Clarification("What would you like me to do?")
        return SkillCall(choice.skill, {"text": text})
```

`OneShotRouter` is an illustration; `Model` is any configured model, such as
an `OllamaModel`. The built-in router (#68) adds argument extraction and never
guesses; see [`ModelRouter`](#modelrouter).

**Related:** [`SkillRouter`](#skillrouter),
[limits: defaults versus ceilings](../guides/limits.md#defaults-versus-ceilings).

## `ModelRouter`

**Purpose / decision:** the library-supplied, opt-in [`SkillRouter`](#skillrouter)
for free-text multi-skill A2A, alongside custom routers (M6 #68). A model picks
the skill and extracts its arguments, or the router asks the caller a question;
it never guesses. Structured extension requests bypass it and its cost.
Injecting a model into an agent never enables it.

**Signature status:** implemented in M6 (#68); hosting checks and outcomes in
#69. The only opt-in is `AgentApp(..., router=ModelRouter(model))`.

```python
ModelRouter(
    model: Model,
    *,
    instructions: str | None = None,
    model_settings: ModelSettings | None = None,
)
```

| Parameter | Type | Required/default | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `model` | `Model` | Required | The router's own model; it must support structured output. The agent's model is never used implicitly; pass the same instance explicitly to share it. |
| `instructions` | `str \| None` | `None` | Extra trusted instructions (such as domain hints) appended to the router's own for both calls. Never put untrusted content here. |
| `model_settings` | `ModelSettings \| None` | `None` | Settings for both routing calls; each field set overrides the library and agent value (see [`RoutingContext.generate`](#routingcontextgenerate)). |

```python
from agentframe import AgentApp, ModelRouter, OllamaModel

router = ModelRouter(OllamaModel(model="llama3.2:3b", context_window=8192))
app = AgentApp(agent=assistant, host=host, router=router)
```

`assistant` and `host` are the application's agent and
[`HostConfig`](#hostconfig), with the application's authentication.

**Behavior:** each routed message takes two structured-output calls through
[`RoutingContext.generate`](#routingcontextgenerate), so both are charged to
the run before the skill runs (a routed model-backed skill reports 2 routing
model calls plus its own).

1. **Select:** trusted `system` instructions list each eligible skill's ID,
   name, tags and description. The conversation is sent as untrusted messages:
   the caller's text as `user`, the agent's earlier questions as `assistant`.
   The reply is a `Selection`: `skill`, limited by its schema to the eligible
   IDs, or `question`. Selection always runs, even with one eligible skill, so
   off-topic requests get a question.
2. **Extract:** for the chosen skill only, the reply is an `Extraction`:
   `arguments` following the skill's argument schema, or `question`. Every
   optional argument is required and nullable in that schema, with no
   default; the instructions tell the model to use only values the caller
   stated and never to invent one.

The result is a [`SkillCall`](#skillrouter) with only the arguments the model
supplied: nulls for optional arguments are dropped, so the skill's defaults
apply (an optional argument that is nullable with a non-`None` default
therefore cannot be set to `None` through routing). The arguments are
validated against the skill first; the runtime validates them again and
checks permissions and policy when the skill runs. A follow-up answer in the
same task is routed again with the whole conversation.

| Situation | Result |
| --- | --- |
| Model names a skill and gives valid arguments | `SkillCall(skill, arguments)`; task runs the skill. |
| Model asks a question in either call (even alongside an answer), or gives neither | `Clarification` with its question (or a generic one); task `INPUT_REQUIRED`. |
| Extracted arguments fail the skill's validation | `Clarification` naming the invalid argument names only, never their values. |
| The caller's latest message has no text part | `Clarification` asking for text; no model call. Only text parts are ever used. |
| No skill is available to the caller | `InputValidationError`; no model call. |
| Model lacks structured output, or its adapter cannot express a skill's argument schema (such as Foundry strict mode and a `dict` argument) | `UnsupportedCapabilityError`; that routing attempt fails. |
| Selection names a skill that is not eligible, or the reply is not valid JSON for its schema | `OutputValidationError`; never returned as a skill. |
| Model refusal, limit or provider error | Propagates; task `FAILED` with its safe message (see [`SkillRouter`](#skillrouter)). |

Keeping roles separate does not guarantee resistance to prompt injection; see
[prompt safeguarding](../design/prompt-safeguarding.md). Selection is not
authorization: only skills the caller may run are offered, and every run is
still authorized.

### `ModelRouter` hosting

**Implemented behavior (M6 #69):** `AgentApp` checks a `ModelRouter` before
serving. Custom routers skip these checks.

| When | Check | Failure |
| --- | --- | --- |
| `AgentApp(...)` construction | Build the `Selection` schema and every skill's `Extraction` schema once. | `DefinitionError` naming the skill whose arguments have no JSON schema (such as a `Callable` argument). |
| Server startup (ASGI lifespan), after [`Runtime.validate_model`](runtime.md#runtimevalidate_model) | Read the router model's capabilities, bounded by the agent's `timeout_seconds` capped by any host ceiling. The model must support structured output and have a known input limit: the agent's `RunLimits.max_input_tokens` (capped by any host ceiling), else the model's context window. | `DefinitionError` (no request is served); `ProviderError` if the capabilities cannot be read in time. |

Whether a model's adapter accepts each schema (for example Foundry strict
mode and a `dict` argument) is only known at the first routing call, which
fails with `UnsupportedCapabilityError`; there is no adapter schema check at
startup. The router model is application-owned: `AgentApp` never closes it,
and it may be the agent's own model instance. The Agent Card does not change:
routing is not a capability claim.

Hosted outcomes of a free-text message:

| Situation | A2A result |
| --- | --- |
| No `router=`, even when the agent has a model | `UnsupportedOperationError` (`-32004`), no task, no model call. |
| Routed and the skill succeeds | Task `COMPLETED`; usage counts both routing calls plus the skill's. |
| The model asks a question, or arguments are missing or invalid | Task `INPUT_REQUIRED` with the question; the caller answers in the same task and the whole conversation is routed again. |
| No skill is available to the caller, or the skill rejects the run (invalid arguments, permission or policy denied) | Task `REJECTED`, safe message. |
| Routing limit, refusal, provider error, unsupported capability, invalid model reply or timeout | Task `FAILED` with the error's safe message. |
| Structured skill-call extension request | Bypasses the router: no routing calls and no routing cost. |

**Example and proof (M6 #70):** [`examples/routed_assistant.py`](../../examples/routed_assistant.py)
serves two skills with `router=ModelRouter(model)`, sharing the agent's model.
`tests/integration/test_routing_example.py` drives it offline with the
official A2A client: routed completion with routing plus skill usage, a
clarification answered in the same task, a structured call with no router
model call, a model-call limit failing routing before the skill runs, and a
custom `SkillRouter` on another app. The opt-in Ollama and Foundry tests in
`tests/integration/test_providers.py` route one unambiguous request and check
only the selected skill and that its argument is present. With `FoundryModel`,
a skill whose argument schema strict mode rejects fails each routed task
(`FAILED`, `UnsupportedCapabilityError`) before its extraction call is
sent, not at startup; call each routed skill once against the deployment before serving.

**Related:** [`SkillRouter`](#skillrouter), [`RoutingContext`](#routingcontext),
[routing and extraction](../guides/annotations.md#skill-registration-versus-request-routing),
[A2A request styles](../design/mcp-skills.md#a2a-request-styles-and-extension-boundary).

## Native A2A and MCP types

Reuse official A2A `AgentCard`, `AgentSkill`, `Message`, `Part`, `Task`, and
`Artifact`, task stores, and request-handling components where possible.
Reuse native MCP tool results and SDK protocol handling. These are **not new
AgentFrame classes** or separately maintained wire schemas.

A2A protocol 1.0 and the official `a2a-sdk` are selected (specification
release v1.0.1). M2 pins `a2a-sdk[http-server]==1.3.0` as a core dependency
because A2A hosting is the default interface (the optional `agentframe-py[server]`
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
