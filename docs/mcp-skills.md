# A2A hosting and optional MCP skill exposure

This is a proposed design, not an implemented server or MCP compatibility claim.
A2A remains first-class. MCP is an optional adapter for explicit skill invocation,
not another agent framework underneath AgentFrame.

## One server, audience-specific endpoints

Use one server and one shared runtime, with an endpoint per caller audience,
not per skill: A2A is the main agent-facing interface; optional MCP serves LLM
apps/tools through an explicit skill allowlist; future AG-UI serves frontends.
All entry points use the same authorization, validation, limits, accounting and
tracing pipeline. Protocol-specific authentication and responses remain native.
This composition does not move MCP or AG-UI into the M7 release or select an
AG-UI route/constructor. Agent Cards are discovery documents, not per-skill routes.

## Different interaction models

| Interface | Discovery | Invocation |
| --- | --- | --- |
| A2A | Native Agent Card with descriptive skills, supported extension declaration and truthful endpoint capabilities | Preferred structured skill/arguments request through an activated extension, or free-text request through a custom/opt-in model router; both use native messages/tasks/artifacts. |
| MCP | `tools/list` with tool descriptions and input schemas | `tools/call` explicitly selects a published tool. |
| Local runtime | Registered skill metadata | `Runtime.invoke(agent, skill=..., arguments=...)`. |

Future [AG-UI streaming](streaming.md) is a distinct optional frontend event
adapter, not an MCP replacement or publication of internal/frontend tools.
First release returns complete validated results, including typed structured
generation and bounded registered-tool continuations; protocol tool/schema
mapping is still separate from those internal execution features.

A custom REST skill API is deferred unless a concrete client needs it. Do not
implement `/v1/skills/{id}:invoke` as a default third protocol. MCP is not a
REST route per skill, and its payloads must not be replaced with a custom JSON
success envelope.

## A2A request styles and extension boundary

**Structured requests are preferred:** the caller names a registered skill and
sends JSON business arguments in a native A2A data part. Selection is
deterministic and bypasses the router, with no selection-model call or inference
cost for routing. The selected skill can still use a model and incur execution
cost. Validate arguments against the skill's typed signature and apply the same
authorization, policies, limits, accounting and tracing. Unknown skills and
invalid arguments fail explicitly; do not reinterpret a failed structured
request as a prompt or silently run another skill.

This uses A2A 1.0's extension mechanism, not a new endpoint, RPC method or
standard built-in `skill` field. Source-checked on 2026-11-11:

| Mechanism | Native A2A 1.0 contract | AgentFrame decision / still open |
| --- | --- | --- |
| Declaration | [`AgentCapabilities.extensions`](https://a2a-protocol.org/latest/specification/#443-agentcapabilities) contains `AgentExtension` declarations; [extension declaration](https://a2a-protocol.org/latest/topics/extensions/#extension-declaration) describes `uri`, `description`, `required` and `params`. | Implemented (M2 #2): the card declares `https://github.com/malcmiller/agentframe/extensions/skill-call/v1` with `required: false`; `params.skills` maps each skill ID to the JSON Schema of its business arguments (no `ctx` or `Inject[T]` parameters). This is an AgentFrame extension, not an official standard one. Configuration surface remains open. |
| Activation | The [extension activation guide](https://a2a-protocol.org/latest/topics/extensions/#extension-activation) says extensions are inactive by default. An HTTP client requests activation using comma-separated URIs in `A2A-Extensions`; the response SHOULD identify activated extensions with that header. Other bindings use their specified mechanisms. | Implemented (M2 #5): the caller activates the URI per request in `A2A-Extensions`, and the response acknowledges it with `A2A-Extensions: <uri>`. The extension stays `required: false`; a message without activation is free text, never a skill call. |
| Arguments and selection metadata | [`Part.data`](https://a2a-protocol.org/latest/specification/#416-part) carries a JSON value, with exactly one content member per part. [`Message.extensions` and `Message.metadata`](https://a2a-protocol.org/latest/specification/#414-message) identify contributing extensions and carry extension context; [extension points](https://a2a-protocol.org/latest/specification/#462-extensions-points) allow message metadata. | Implemented (M2 #5): the skill ID goes in message metadata under `agentframe.skill`; the message has exactly one part, a data part with the arguments object, validated against the card's `params.skills` schema. No skill ID, other parts or unknown `agentframe.` metadata keys are a JSON-RPC `InvalidParamsError` with no task; unknown skills or invalid arguments end the task without running anything else. No custom top-level protocol fields; metadata is never trusted identity. |
| Clarification | [`TASK_STATE_INPUT_REQUIRED`](https://a2a-protocol.org/latest/specification/#413-taskstate) is the A2A 1.0 interrupted state for additional input; [`TaskStatus.message`](https://a2a-protocol.org/latest/specification/#412-taskstatus) can convey the agent's question. | For free-text ambiguity or missing arguments, ask back through an input-required task rather than guess. Exact question/missing-field schema, follow-up mapping and router decision API remain open. This is not a successful skill result or built-in approval/durable workflow. |

A structured `SendMessage` for `summarize`, sent with the header
`A2A-Extensions: https://github.com/malcmiller/agentframe/extensions/skill-call/v1`
(full rules in the [wire format reference](api/hosting.md#structured-skill-call-wire-format)):

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "5d0c7f8e-2b1a-4c9e-9f3d-7a6b5c4d3e2f",
      "role": "ROLE_USER",
      "parts": [{"data": {"text": "The document to summarize."}}],
      "metadata": {"agentframe.skill": "summarize"}
    }
  }
}
```

**Free-text requests** go through a configured custom router (M2) or explicitly
opted-in built-in model router (M6). The router selects an authorized candidate
and extracts business arguments; the runtime validates them before invocation.
Ask back through the input-required state for ambiguity or missing arguments,
never guess. Invalid decisions, refusal, authorization failure and exhaustion
remain explicit failures rather than clarification-shaped success.
Callers that do not use the extension, including non-AgentFrame agents, use
this prompt path; configuring an agent model does not enable a model router.

If typed AgentFrame-to-AgentFrame calls are added in M8+, they always use the
structured extension path. The idea `research.find_sources(topic=...)` is still
unapproved syntax, not a proprietary RPC contract. An external caller can also
use structured requests if it implements the advertised extension. A2A stays
interoperable, but base A2A alone does not standardize AgentFrame skill selection.

## Three separate exposure decisions

1. `@skill` registers agent behavior and A2A skill metadata.
2. `MCPConfig.exposed_skills` explicitly publishes selected registered skills as
   external MCP tools.
3. `@tool` plus registration supplies internal model-callable functions.

None of these automatically enables the others. A skill published through MCP
may execute model inference; MCP tools are not limited to deterministic functions.
It must still enforce the same authorization and execution limits.

As a guideline, tool-like skills (deterministic, typed, quick and single-shot)
may also be published through MCP; agent-like skills involving judgment,
multiple turns, clarification or long-running work usually stay A2A-only.
This is not an enforced classifier or a ban on model-backed MCP tools.
MCP remains opt-in per skill through `exposed_skills`, empty by default.

[Registry/directory discovery](discovery.md) is core to the next multi-agent
release (M8+), but remains a separate concern: finding a remote card never
publishes its skills through MCP or
registers them as internal model tools. Discovery itself does not delegate;
remote invocation uses a separately authorized native A2A client.
M8+ skills use `Remote["name"].call(...)` or `.ask(text)`; remote annotations
are excluded from schemas just like `Inject[T]`. `call` activates the same
structured extension, whose schema also carries remaining deadline/token
allowance, reported peer usage and default max-5-hop depth. `ask` works with
extension-unaware peers, with local time bounds and unknown usage, never zero.
Input-required raises local `ClarificationNeededError`, not automatic dialogue.
Ordinary A2A correlation still exposes only invocation ID; negotiated usage
exchange is not internal `RunResult` disclosure. See [remote contracts](api/discovery.md#remotecall).

## Proposed server composition

This example uses `WritingAssistant` from [agent examples](agent-examples.md).
`model`, `tracer_provider`, `tenant_id`, `audience`, `authorization_policy`,
`execution_policy`, and `application_router` are
application-supplied dependencies. Constructor fields and transport choices
are proposals, not stable signatures.
The custom router placeholder can be replaced by an explicitly configured
built-in model router once its construction API is settled; merely injecting a
model into the agent never enables routing. Router argument extraction and
clarification remain open.

```python
from agentframe import (
    A2AConfig,
    AgentApp,
    AgentConfig,
    EntraIDAuth,
    HostConfig,
    MCPConfig,
    ModelSettings,
    RunLimits,
    Runtime,
    TracingConfig,
)

writer = WritingAssistant(
    model=model,
    config=AgentConfig(
        model_settings=ModelSettings(max_output_tokens=800),
        limits=RunLimits(
            max_input_tokens=6_000,
            max_total_tokens=12_000,
            max_model_calls=4,
            timeout_seconds=30,
        ),
    ),
)

runtime = Runtime(
    authorization=authorization_policy,
    policy=execution_policy,
    tracing=TracingConfig(
        enabled=True,
        tracer_provider=tracer_provider,
        capture_content=False,
    )
)

app = AgentApp(
    agent=writer,
    runtime=runtime,
    host=HostConfig(
        public_url="https://writer.example.com",
        auth=EntraIDAuth(tenant_id=tenant_id, audience=audience),
        limits_ceiling=RunLimits(
            max_input_tokens=8_000,
            max_total_tokens=16_000,
            max_model_calls=6,
            timeout_seconds=45,
        ),
    ),
    a2a=A2AConfig(enabled=True, path="/a2a"),
    mcp=MCPConfig(
        enabled=True,
        path="/mcp",
        exposed_skills=["summarize", "rewrite"],
    ),
    router=application_router,
)
```

The ASGI server/ingress separately configures binding and TLS. Both remote
interfaces must integrate authentication appropriate to their selected protocol
versions and transport; sharing a host does not make an arbitrary bearer-token
middleware a conforming MCP authorization implementation.
A remote host refuses startup without `auth`; explicit `AllowAnonymous()` is
the only anonymous built-in choice, never the default. Shared runtime protections
also include `Inject[T]` service validation, host admission/bytes/shutdown and
typed safe errors. A2A message deduplication is not MCP replay semantics.

The proposed HTTP deployment publishes the native Agent Card at
`/.well-known/agent-card.json`, advertises the chosen A2A binding at `/a2a`, and
serves MCP through Streamable HTTP at `/mcp`. A2A and MCP initialization,
version negotiation, sessions, and response semantics stay protocol-native.
MCP's transport name does not enable partial skill/model output: first-release
invocations still return complete validated results.
MCP stdio, if added, is a separate transport, not an ASGI route.

A2A is enabled by default for `AgentApp`; MCP is disabled by default.
An enabled MCP adapter with an empty allowlist exposes no skill tools.
Unknown skill IDs, duplicate/colliding tool names, unsupported schemas, or missing
optional dependencies must fail configuration validation rather than be skipped.
Do not use a wildcard or auto-expose inherited or newly added skills.

## Schema and call mapping

For the example `summarize(self, text: str, ctx: RunContext) -> str`:

- The initial mapping uses the stable skill ID as the MCP tool name. Validate
  against the selected MCP version's naming rules; reject collisions rather
  than silently rename tools.
- Build the input schema from the skill's typed business parameters. Exclude
  `self`, injected `ctx`, every `Inject[T]` service parameter, credentials,
  instructions and execution internals. Missing registered services fail startup.
- Reject unknown arguments and attempts to inject context or policy options.
- Validate the result against the Python return contract. A proposed structured
  output wrapper is `{"value": ...}` so scalar results can map to an object
  schema. Publish an accurate MCP output schema when supported and provide
  compatible content blocks.

An illustrative `tools/call` request, after normal MCP initialization:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "summarize",
    "arguments": {
      "text": "The document to summarize."
    }
  }
}
```

Return the selected SDK's native MCP tool result. Use protocol errors for
protocol failures and tool execution errors for applicable execution failures,
following the pinned MCP version. Do not return an error as successful summary
text, leak raw provider exceptions, or stuff an A2A task into an MCP result.
Long-running/async MCP behavior needs a separately specified, version-supported
contract; A2A task support does not imply MCP task support.

## Shared execution and security

```text
authenticated transport request
  -> validate protocol envelope and permitted exposure
  -> select skill: activated structured A2A extension, prompt router, or MCP name
  -> authorize the selected skill and validate arguments
  -> common runtime: context, limits, execution, output validation
  -> protocol-native response
```

Structured A2A bypasses routing, not security or execution controls.
Authorize admission before model-backed routing, restrict the selector to
eligible skills, and re-check the selected skill at invocation. MCP allowlisting
is publication policy, not caller authorization. Filter discovery as required
and enforce policy again on `tools/call`; hidden tools must not remain callable.
Session and conversation identifiers are not proof of caller identity.

Use a verified `Principal` and require all skill `requires_permissions` grants
plus application resource policy on each invocation. MCP allowlisting does not
override these checks. Missing identity or unavailable required policy fails
protected operations. Anonymous access is an explicit host/application decision,
not inferred from an empty skill permission list.

Role-to-permission mapping lives in the application, not MCP arguments or the
Agent Card. Re-check protected task/resource access and consequential tool/action
execution; model output is not authorization. See
[authorization](authorization.md) and [prompt safeguarding](prompt-safeguarding.md)
for caller identity and the additional content-policy boundaries.

Generate and preserve invocation correlation through the runtime. Inference,
retries, and internal tool execution share the same accounting whether entered
through A2A, MCP, or local invocation. Request options may tighten allowances only
through an explicitly supported contract; do not invent standard MCP budget fields.

The same tracing provider observes both interfaces with content capture off by
default. Trace propagation never grants identity or execution permission.
See [limits](limits.md) and [tracing](tracing.md) for the enforcement boundaries.

## Dependency and delivery boundary

Use the official MCP Python SDK as an optional protocol dependency; do not pull
in another agent framework to obtain MCP support. Preserve model-free, A2A-only
use without MCP or telemetry packages.

MCP is a separate follow-up after M7 rather than blocking the first usable release. Before
claiming support, pin an SDK/protocol version and test initialization, discovery,
allowlisted invocation, authorization, schema mapping, failure handling,
cancellation behavior, isolation, limits, and tracing with an independent client.
Final authentication hooks, tool-result mappings, and router signatures remain
open until implemented and verified.
Internal model tools/structured outputs are first-release requirements, unlike
optional adapter delivery. A2A task support does not add built-in human approval
pause/resume; application workflows own approval state. AG-UI or provider
streaming cannot justify advertising unimplemented A2A streaming.
