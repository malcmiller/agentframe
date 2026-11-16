# Streaming and AG-UI

**Confirmed direction:** results are complete and validated by default.
**Implemented in 0.5.0:** opt-in text streaming, from the Ollama and Foundry
adapters through the runtime to A2A `SendStreamingMessage` (#257; see
[A2A text streaming (implemented)](#a2a-text-streaming-implemented)), and a
scoped optional **AG-UI** endpoint for frontends (#258; see
[AG-UI endpoint (implemented)](#ag-ui-endpoint-implemented)). A2A/native Agent
Cards remain first-class; MCP remains a separate tool interface. No UI
framework, other agent framework, automatic orchestration, or blanket AG-UI
conformance is authorized. The [surface reference](../api/streaming.md)
documents both.

## A2A text streaming (implemented)

Implemented in 0.5.0 (#257) with no new dependency. Decisions:

- **Opt-in per call, text only.** A skill passes `stream=True` to
  `ctx.generate`; nothing streams otherwise. `stream=True` with `output=`
  raises `DefinitionError`, since partial JSON is not a validated result.
  Routing, planning and judge calls never stream.
- **One small runtime surface.** `Runtime.run(..., on_text=sink)` takes an
  async `TextSink`; models implement the optional `StreamingModel.stream`
  (Ollama, Foundry and `ScriptedModel` do), and other models answer whole. No
  event union is chosen; the AG-UI adapter (#258) builds on this surface.
- **Whole-output checks.** With any `ExecutionPolicy` output check, nothing is
  streamed: the complete answer is released once, after the checks pass.
  Input checks, authorization and validation run before any text is released.
- **Every limit applies.** Tokens, model/tool calls and the deadline are
  unchanged. Streamed bytes count toward `max_response_bytes`; once exceeded,
  the task fails and no more text is sent. The local sink is awaited per piece,
  so a slow `on_text` consumer applies backpressure to the model call within
  the deadline. Over A2A the SDK queues events per subscriber and drops one
  more than 1024 behind, so the first piece is sent at once, later pieces are
  combined into updates of at least 64 bytes and at most 512 updates are sent
  per task; the rest arrives with the final answer.
- **No retry after release.** A model call that already passed text on is never
  retried (released text cannot be withdrawn); failures before the first piece
  keep the usual retries. Text released before a later failure stays released
  and the task fails; there is never a truncated success.
- **A2A mapping.** Text arrives as artifact updates (`append=true`), then one
  final update (`append=false`, `lastChunk=true`) holding the whole validated
  answer, so the stored task equals a `SendMessage` result. Size,
  authentication, deduplication and admission refusals run first; a dedup
  repeat yields the original task once. The card advertises
  `capabilities.streaming`, which also enables the SDK's `SubscribeToTask`.
- **Disconnect.** A2A clients that disconnect do not cancel the task (A2A
  semantics; `CancelTask` does). The AG-UI rule "disconnect cancels" below
  applies to the frontend endpoint only.

## AG-UI endpoint (implemented)

Implemented in 0.5.0 (#258) with the official `ag-ui-protocol` SDK
(`>=1.0.0,<2`, AG-UI protocol 1.0) behind the optional `ag-ui` extra; it
depends only on Pydantic. Decisions:

- **Same host, same checks.** `AgentApp(ag_ui=AGUIConfig(enabled=True))`
  serves `POST /ag-ui` (HTTP + SSE binding). Size, authentication, the
  caller's rate limit, input validation, shutdown and the shared run slots
  refuse with HTTP errors before any event; each run then goes through the
  same `Runtime` as `/a2a`, so permissions, the execution policy and every
  limit apply unchanged. Not on the Agent Card.
- **Free text only.** The frontend's `user` and `assistant` text messages are
  the untrusted conversation the `SkillRouter` (or `default_skill`) sees;
  enabling AG-UI without either is a `DefinitionError`. Other message roles,
  frontend tools, state, context, forwarded properties and resume entries are
  not used, never promoted to trusted instructions.
- **Identity.** `threadId`/`runId` are echoed on lifecycle events only; the
  run has its own server-generated `invocation_id` (in event `metadata`), and
  a verified caller's `threadId` keys its server-side history per caller.
- **Event profile.** `RUN_STARTED` (with `protocolVersion`); text pieces as
  `TEXT_MESSAGE_*`; each offered tool call as `TOOL_CALL_START` (registered
  name, a new call ID) and `TOOL_CALL_END`, never arguments or results; a
  `str` result the latest streamed message does not end with, or a router's question, as one
  whole message; other results as `RUN_FINISHED.result` in JSON mode;
  `RUN_ERROR` with the safe message and the error class name as `code`.
  The runtime reports tool status through a package-internal hook, so no
  public event union was needed.
- **Whole-output checks, limits, no retry after release:** as for A2A above.
  Events are written straight to the connection, so a slow consumer applies
  backpressure within the deadline instead of growing a buffer.
- **Disconnect cancels; no reconnect or resume.** The endpoint watches the
  connection while the run is quiet and cancels the run as soon as the client
  goes; no terminal event is sent to a closed stream. Shutdown cancels runs
  still open after the grace with a `cancelled` `RUN_FINISHED`, closing open
  messages and tool calls first, as the lifecycle specification requires.
- **Not yet:** frontend tools, shared state, interrupts/resume, reasoning
  events, usage on terminal events, run deduplication and other transports.
  Each needs its own scope decision.

Every event in the tests is parsed with the SDK's own `Event` union, so a
schema-invalid stream fails; there is no official Python AG-UI client to run
against yet.

## Protocol scope and sources

AG-UI's official [introduction](https://docs.ag-ui.com/introduction) describes an
event-based connection between agents and user-facing applications, separately
from A2A agent-to-agent and MCP tool communication. Its "today & upcoming"
feature list is not an AgentFrame support matrix.

The official [documentation index](https://docs.ag-ui.com/llms.txt) was consulted
on 2026-11-10. Concrete event/transport claims below cite its 1.0 specification
pages as research evidence, **not selection of a supported AgentFrame protocol
version**. Pin a compatible protocol/schema/SDK and verify behavior with an
independent client before claiming compatibility. Drafts, SDK docs and deployed
versions must not be assumed identical.

The [official Python SDK overview](https://docs.ag-ui.com/sdk/python/core/overview.md)
identifies `ag-ui-protocol` and the `ag_ui.core` types. The
[official SDK manifest](https://github.com/ag-ui-protocol/ag-ui/blob/main/sdks/python/pyproject.toml)
declares Pydantic as its runtime dependency, not a major agent framework.
AgentFrame reuses its native types and encoder (1.0.0 selected in #258) as an
optional protocol dependency; model-free/A2A-only users never install it.

## Initial adapter scope

**Decided and implemented (#258):** `AgentApp(..., ag_ui=AGUIConfig(enabled=True))`
serves `/ag-ui` on the same server under the same auth, permissions and limits.
The initial profile streams run start/finish, text deltas, tool names/status
(not raw arguments), and safe errors. A typed (non-text) result is
`RUN_FINISHED.result` in JSON mode, sent only after validation; partial JSON is
never a validated structured result.

Do not automatically enable frontend tool execution, shared-state persistence/
synchronization, human approval interrupts, replay/resume, multimodality,
subagents, generative UI, or raw provider/reasoning events. These need independent
scope decisions and conformance work. Application-owned human approval
workflows remain outside first-release built-in pause/resume; A2A tasks and
AG-UI run events do not supply that workflow or its storage.

### Runtime-to-event mapping

The names here are native AG-UI events, not AgentFrame methods. The
implemented mapping is in [AG-UI endpoint (implemented)](#ag-ui-endpoint-implemented);
this table keeps the release and trust constraints it follows.

| Managed runtime fact | Candidate AG-UI representation | Release/trust constraints |
| --- | --- | --- |
| Admitted invocation begins | `RUN_STARTED` | Authenticate/admit first; native thread/run identifiers are not server authority or invocation IDs. |
| Invocation completes or fails | `RUN_FINISHED` or `RUN_ERROR`, with selected-version terminal semantics | Complete validation/output checks before reporting a successful final result. Sanitize failures; never infer success from HTTP status alone. |
| Approved assistant text | `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`, `TEXT_MESSAGE_END` | Preserve native message IDs and ordering; never expose text ahead of required output-policy approval. |
| Backend tool progress | Selected-version tool lifecycle/status mapping | Initial surface is names/status, not raw arguments by default; exact schema-valid projection open. No frontend execution grant. |
| Backend tool result | Internal continuation; later explicit release mapping if chosen | Initial profile does not automatically publish raw results. Validate and policy-filter any future projection before release. |
| Safe progress phases | Optional step events if mapped and tested | Operational status only, not private reasoning, hidden prompts or arbitrary logs. |
| Usage/correlation | Selected-version optional usage and approved correlation mapping | Derive usage from independent accounting, preserving unknowns and avoiding double counts; exact public metadata mapping remains open. |

Sources: [run lifecycle/terminal and usage semantics](https://docs.ag-ui.com/spec/1.0/events/lifecycle.md),
[text event ordering](https://docs.ag-ui.com/spec/1.0/events/text-messages.md),
and [tool event semantics](https://docs.ag-ui.com/spec/1.0/events/tool-calls.md).
The tool specification distinguishes backend results in the same run from
frontend tool execution across runs; supporting backend visibility does not
grant the latter. No tool acts on incomplete streamed arguments.

## Transport and completion

The cited [1.0 HTTP/SSE binding](https://docs.ag-ui.com/spec/1.0/basic/transports/http-sse.md)
uses a JSON run-input POST and `text/event-stream` response, with one JSON
protocol event per SSE data payload. Pre-stream rejection uses HTTP errors;
after the stream opens, failures use protocol events. A connection ending
without a terminal event is truncated, not successful.

That binding explicitly has **no broken-stream resumption** and does not use
SSE `Last-Event-ID`. Do not promise reconnection/replay from generic SSE support
or silently rerun side-effecting work. Any later replay/new-run deduplication
contract must address authorization, storage, event retention and idempotency
without claiming stream resumption that the chosen binding lacks. WebSocket/
other transport support is not implied by the introduction's broader examples.

Cancellation is distinct from disconnect. The
[lifecycle specification](https://docs.ag-ui.com/spec/1.0/events/lifecycle.md)
distinguishes producer-reported cancellation from a truncated consumer stream.
**Decided and implemented (#258):** client disconnect cancels the run; no
reconnect/resume. Cancellation propagates to managed work, without promising
rollback or zero remote charges; a disconnected client receives no terminal
event. There is no cancellation endpoint. Events are written straight to the
connection (backpressure, no unbounded buffer); the run's deadline bounds a
slow consumer, and no separate write timeout is set. At shutdown, runs still
open after the grace (30 seconds by default) are cancelled and closed in
order with a `cancelled` `RUN_FINISHED`, whether the app or the server
(Uvicorn under `agentframe run`) cancels them.

AG-UI streaming does not automatically enable model streaming on every
deployment or make MCP async behavior conformant; A2A streaming is advertised
because it is implemented and tested (#257).
Each adapter must separately implement and verify what it advertises.

## Identity, input and application state

The [run-input specification](https://docs.ag-ui.com/spec/1.0/basic/run-input.md)
defines native `threadId`/`runId`, conversation messages, and optional application
tools, state, context and forwarded properties. These are client data, not a
verified `Principal`, permission grant, trusted system instruction, or an
AgentFrame dependency container.

Authenticate at ingress and authorize admission, selected skill/resources and
consequential actions. Bind thread/history/run access to verified caller/tenant;
isolate any buffered events or stored state by that authority. Maintain native
message/tool/thread/run IDs and an explicit mapping to a **server-generated**
invocation ID. Never reuse arbitrary frontend IDs as trusted correlation or
deduplication keys across tenants. OTel trace IDs are a separate optional
diagnostic relationship, not native AG-UI identifiers or identity.
Reuse the decided host `auth` / `Authenticator` contract, not a parallel
identity scheme. A2A's 10-minute caller/message-ID deduplication does not
automatically define AG-UI run deduplication or stream replay.

Validate/bound frontend history, roles, state/patches, tool declarations/results,
context, metadata and forwarded properties. Preserve their provenance as
untrusted input; do not promote frontend system/developer messages or context
into the application's trusted instructions, or accept client-authored grants,
dependencies/model settings/host ceilings. Ignore no business-critical data
silently and never trust a returned frontend tool result as server authorization.
The initially planned profile does not adopt frontend tools or persistence just
because those fields exist.

Before conformance claims, reconcile AgentFrame's supported-input/admission
policy with the selected AG-UI version's receive/downgrade rules. For example,
the cited 1.0 input rules describe handling unsupported content and unknown
members; this proposal does not replace them with a fabricated strict wire
schema or claim a text-only implementation already conforms to every case.
Use [native schemas plus behavioral requirements](https://docs.ag-ui.com/spec/1.0/schema-files.md);
schema-valid events alone cannot prove lifecycle/ordering/conformance.

## Policy-gated output and independent accounting

The first release returned only complete validated results. For streaming,
configured output checks must run before affected content/events reach a client.
Skills whose checks require the whole answer send complete text at the end,
after validation/policy, rather than text deltas. Structured-output checks
likewise never release invalid partial JSON; transport events
may describe safe progress without releasing unchecked partials. A successful
terminal event cannot certify previously unchecked fragments retroactively.

Apply the same protection to tool names/arguments/results, errors, snapshots and
any future metadata. Never expose credentials, hidden instructions, raw provider
payloads, raw/private reasoning or chain of thought, including through `RAW`,
custom events, input echoes or SDK raw-event fields. Do not forward every
runtime/OTel event to the browser. Output denial cannot undo prior actions or
content release; tool/action authorization must precede effects.

Accounting, invocation IDs, token/attempt/deadline limits and required checks
work without tracing or a connected frontend. Model routing and tool
continuations spend the same remaining allowance; buffering/backpressure cannot
reset it. Export only approved available usage, never fill unknown counts with
zero or imply a hard remote billing guarantee.

## Open contracts and delivery gate

`AGUIConfig(enabled=True)`, `AgentApp(ag_ui=...)`, `/ag-ui`, the initial event
profile, disconnect cancellation and no reconnect/resume are implemented
(#258), pinned to AG-UI 1.0 over HTTP/SSE with `ag-ui-protocol>=1.0.0,<2`.
The runtime streaming surface (`ctx.generate(stream=True)`,
`Runtime.run(on_text=)`, `StreamingModel`) is implemented (#257); no public
AG-UI event union or explicit cancellation method was needed. The endpoint
consumes managed execution facts (text pieces, tool status, the validated
result), never SDK/provider events that bypass validation/policy, and maps the
safe error hierarchy to `RUN_ERROR`, never provider error payloads.
`Runtime.run` / `RunResult`, `ctx.generate(..., output=Summary)` and
`ctx.generate(..., tools=[...])` stay the complete-result surfaces.

The delivery gate was met with tests over real localhost HTTP whose events are
parsed by the official SDK's own `Event` types: lifecycle ordering, text
deltas, tool names/status without arguments or results, authentication and
input refusals before any event, permission denial, shared rate limit and
run slots, response-size limits, whole-output-check buffering and denial,
generic skill errors with no secrets, disconnect cancellation with no
terminal event, and shutdown's cancelled outcome. There is no official
Python AG-UI client yet; checking against the TypeScript client remains
worthwhile before claiming wider conformance. Frontend-executed tools,
shared state, interrupts/resume and terminal usage are later, explicit opt-in
only. Do not advertise absent A2A features.

See [delivery milestones and post-M7 follow-ups](../roadmap.md#delivery-milestones),
[runtime reference](../api/runtime.md), [model contracts](../api/models.md),
[authorization](../guides/authorization.md), [safeguarding](prompt-safeguarding.md),
[limits](../guides/limits.md), and [tracing](tracing.md). Discovery/remote invocation belongs
to the next core multi-agent release (M8+) after M7, not an optional follow-up.
Streaming remains a separate optional delivery; its timing does not demote that
core release or imply a workflow/graph engine.
