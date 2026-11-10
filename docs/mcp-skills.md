# A2A hosting and optional MCP skill exposure

This is a proposed design, not an implemented server or MCP compatibility claim.
A2A remains first-class. MCP is an optional adapter for explicit skill invocation,
not another agent framework underneath AgentFrame.

## Different interaction models

| Interface | Discovery | Invocation |
| --- | --- | --- |
| A2A | Native Agent Card with descriptive skills and truthful endpoint capabilities | Standard messages, tasks, and artifacts; an explicit router selects among multiple skills when needed. |
| MCP | `tools/list` with tool descriptions and input schemas | `tools/call` explicitly selects a published tool. |
| Local runtime | Registered skill metadata | `Runtime.invoke(agent, skill=..., arguments=...)`. |

A custom REST skill API is deferred unless a concrete client needs it. Do not
implement `/v1/skills/{id}:invoke` as a default third protocol. MCP is not a
REST route per skill, and its payloads must not be replaced with a custom JSON
success envelope.

## Three separate exposure decisions

1. `@skill` registers agent behavior and A2A skill metadata.
2. `MCPConfig.exposed_skills` explicitly publishes selected registered skills as
   external MCP tools.
3. `@tool` plus registration supplies internal model-callable functions.

None of these automatically enables the others. A skill published through MCP
may execute model inference; MCP tools are not limited to deterministic functions.
It must still enforce the same authorization and execution limits.

## Proposed server composition

This example uses `WritingAssistant` from [agent examples](agent-examples.md).
`model`, `tracer_provider`, `authentication`, `authorization_policy`,
`execution_policy`, and `application_router` are
application-supplied dependencies. Constructor fields and transport choices
are proposals, not stable signatures.

```python
from agentframe import (
    A2AConfig,
    AgentApp,
    AgentConfig,
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
        authentication=authentication,
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

The proposed HTTP deployment publishes the native Agent Card at
`/.well-known/agent-card.json`, advertises the chosen A2A binding at `/a2a`, and
serves MCP through Streamable HTTP at `/mcp`. A2A and MCP initialization,
version negotiation, sessions, and response semantics stay protocol-native.
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
  `self`, injected `ctx`, credentials, instructions, and execution internals.
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
  -> select skill: A2A router or explicit MCP tool name
  -> authorize the selected skill and validate arguments
  -> common runtime: context, limits, execution, output validation
  -> protocol-native response
```

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

MCP follows the minimal A2A authoring slice rather than blocking it. Before
claiming support, pin an SDK/protocol version and test initialization, discovery,
allowlisted invocation, authorization, schema mapping, failure handling,
cancellation behavior, isolation, limits, and tracing with an independent client.
Final authentication hooks, tool-result mappings, and router signatures remain
open until implemented and verified.
