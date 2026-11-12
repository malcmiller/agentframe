# Future streaming and AG-UI

**Confirmed direction:** the first usable release returns complete validated
results. Streaming is a later deliverable and should support **AG-UI** through a
scoped optional agent-to-frontend protocol adapter. A2A/native Agent Cards remain
first-class; MCP remains a separate tool interface. No UI framework, other agent
framework, automatic orchestration, or blanket AG-UI conformance is authorized.
The [surface reference](api/streaming.md) records decided configuration and
still-open event/wire details.

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
currently declares Pydantic as its runtime dependency, not a major agent
framework. Reusing these native types/encoding is an appropriate optional
protocol dependency to evaluate; do not install it now, copy its schemas,
wrap another agent framework, or require model-free/A2A-only users to install a
frontend integration. Re-check the dependency graph when selecting a version.

## Initial future adapter scope

**Decided configuration:** `AgentApp(..., ag_ui=AGUIConfig(enabled=True))`
serves `/ag-ui` on the same server under the same auth, permissions and limits.
The initial profile streams run start/finish, text deltas, tool names/status
(not raw arguments by default), and safe errors. Typed final-result serialization needs a
documented mapping; partial JSON is not a validated structured result.
This is a future target, not a claim that any adapter already exists.

Do not automatically enable frontend tool execution, shared-state persistence/
synchronization, human approval interrupts, replay/resume, multimodality,
subagents, generative UI, or raw provider/reasoning events. These need independent
scope decisions and conformance work. Application-owned human approval
workflows remain outside first-release built-in pause/resume; A2A tasks and
AG-UI run events do not supply that workflow or its storage.

### Candidate runtime-to-event mapping

The names here are native AG-UI events, not proposed AgentFrame methods.
Exact runtime event types/mappings remain open; the configuration above is decided.

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
**Decided:** client disconnect cancels the run; no reconnect/resume in the first
streaming version. Cancellation propagates to managed work, without promising
rollback or zero remote charges; a disconnected client may not receive a terminal
event. Exact cancellation control, terminal-event mapping, bounded buffering,
slow-consumer/backpressure behavior and write timeouts remain open.
The host already has a 30-second shutdown grace by default; exact stream
drain/terminal behavior within that bound remains to specify.
Do not invent a cancellation endpoint or guarantee rollback/free cancellation.
Clean termination must follow the selected version's ordering rules.

Future AG-UI streaming does not automatically enable model streaming on every
deployment, advertise A2A streaming in a card, or make MCP async behavior conformant.
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

The first release returns only complete validated results. For later streaming,
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
profile, disconnect cancellation and no reconnect/resume are decided.
No runtime stream method, event union, structured-result envelope or explicit
cancellation method has been selected. A future adapter consumes managed execution events, not SDK/provider
events that bypass validation/policy.
`Runtime.run` / `RunResult`, `ctx.generate(..., output=Summary)` and
`ctx.generate(..., tools=[...])` are decided complete-result surfaces; they do
not choose a streaming API or finalize the combination of output and tools.
Reuse the safe error hierarchy
with a future verified event mapping, not provider error payloads.

Before delivery, pin versions/binding and implement the decided feature profile;
native/runtime ID mapping, validated router argument extraction and typed
result serialization, admission/tenant isolation, byte/concurrency limits,
policy gating, cancellation/shutdown details and backpressure remain to specify.
Frontend-executed tools and shared state are later, explicit opt-in only.
Prove disconnect cancellation and no reconnect/resume alongside ordering/errors/truncation, refusal/
incomplete-result behavior, safe side effects, isolation and no secrets/private
reasoning with an independent AG-UI client. Do not advertise absent A2A features.

See [delivery milestones and post-M7 follow-ups](initial-outline.md#delivery-milestones),
[runtime reference](api/runtime.md), [model contracts](api/models.md),
[authorization](authorization.md), [safeguarding](prompt-safeguarding.md),
[limits](limits.md), and [tracing](tracing.md). Discovery/remote invocation belongs
to the next core multi-agent release (M8+) after M7, not an optional follow-up.
Streaming remains a separate optional delivery; its timing does not demote that
core release or imply a workflow/graph engine.
