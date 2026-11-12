# Future streaming surface

[Reference status conventions](README.md). **Decision:** complete validated
results first; streaming later with optional AG-UI agent-to-frontend support.
This is a planned protocol surface, not a new implemented class or support for
every AG-UI feature. A2A/Agent Cards stay first-class; MCP and tracing stay separate.

## Optional AG-UI adapter

**Purpose / decision:** map policy-approved managed execution events to a scoped
native AG-UI event stream for frontend clients, without another agent framework
or UI framework dependency.

**Signature status:** no AgentFrame public class/constructor, endpoint path,
method, or field names have been selected. Do not infer `AGUIConfig`,
`AgentApp(agui=...)`, or an approved streaming API from this direction.

| Concern | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| Protocol/schema/SDK and transport | Compatible official AG-UI versions; not selected | Future optional adapter, not first release | 1.0 documentation is research evidence, not a pinned compatibility commitment. Evaluate native Python `ag-ui-protocol` types/encoding; no other framework required. |
| Authentication/caller scope | Shared `Authenticator` / policy; adapter binding open | Explicit auth required remotely | Reuse the decided host auth choice; frontend thread/run IDs are not verified identity; isolate tenant/history/events/state. |
| Runtime event mapping | Typed managed events; union open | Only supported, tested events | Lifecycle/text and safe backend-tool visibility planned; no automatic frontend tool execution, state persistence, approval interrupts or replay/resume. |
| Output/release policy | Existing authorization/content checks plus adapter gating; syntax open | Required checks precede affected release | Buffer whole validated results or decline streaming when whole-output policy requires it; no unchecked partial JSON. |
| Buffer/cancel/disconnect/reconnect controls | Exact interfaces/defaults open | Finite bounds and explicit behavior required | Do not imply SSE reconnect resumes work or that disconnect guarantees cancellation/rollback. |

This dependency-only example uses the **official Python SDK** input type,
documented in its [core overview](https://docs.ag-ui.com/sdk/python/core/overview.md),
and an existing proposed `Runtime`. It does not construct an adapter, bypass
runtime checks, or claim installed packages/compatibility.

```python
from ag_ui.core import RunAgentInput
from agentframe import Runtime


def describe_frontend_boundary(
    runtime: Runtime, frontend_input: RunAgentInput,
) -> tuple[str, str]:
    return type(runtime).__name__, type(frontend_input).__name__
```

**Behavior / failures:** eventual admission/validation/auth failures and
in-stream errors must follow the selected protocol/binding; HTTP success is not
run success. The cited
[1.0 HTTP/SSE binding](https://docs.ag-ui.com/spec/1.0/basic/transports/http-sse.md)
does not resume broken streams. No partial/truncated output becomes a validated
successful result. No secrets/raw private reasoning or raw provider/OTel
payloads in events. Map the decided safe error taxonomy to the selected binding;
in-stream mapping, input downgrade handling and capability declarations still
need verification before conformance claims.

**Related:** [AG-UI design, event mapping and primary citations](../streaming.md),
[`AgentApp`](hosting.md#agentapp), [`ExecutionPolicy`](authorization.md#executionpolicy).

## Runtime streaming boundary

**Purpose / decision:** later supply execution events under the same validation,
authorization, tool/action checks and limits as complete-result invocation.
This is not an OTel trace stream or automatic multi-agent orchestration.

**Signature status:** no `Runtime.stream`, `RunContext.stream` or stream iterator
has been selected. Complete-result `Runtime.run` / `RunResult` and
`ctx.generate(..., output=Summary)` are decided, not a streaming event API.

| Contract concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Event/result model | Typed execution event/result representation; open | Future streaming contract | Distinguish partials, validated completion, refusal, incomplete generation, errors and cancellation. |
| Identity/usage metadata | Native protocol IDs plus server invocation metadata; mapping open | Independent of optional tracing | Never substitute frontend IDs or trace IDs for trusted caller identity; unknown usage stays unknown. |
| Tool continuation/action state | AgentFrame-owned normalized contracts; field layouts open | First-release `tools=[...]` and bounded continuations decided | Future tool visibility grants no action authority, frontend tools or built-in approval pause/resume. |

```python
from agentframe import Agent, Runtime


async def complete_echo(runtime: Runtime, echo: Agent) -> str:
    return await runtime.invoke(
        echo, skill="echo", arguments={"text": "Complete results first"},
    )
```

Supply the [`Echo`](agents.md#agent) instance; no streaming method is invented.

**Behavior / failures:** enforce budget/deadlines with buffering/slow consumers;
never silently weaken required policy to stream. Cancellation/disconnect,
backpressure, reconnect/idempotency and graceful shutdown remain explicit open
contracts. Native A2A streaming must be separately implemented/verified before
card advertisement; AG-UI support alone does not enable it.

**Related:** [`Runtime.invoke`](runtime.md#runtimeinvoke),
[`RunContext.generate`](agents.md#runcontextgenerate),
[streaming delivery gate](../streaming.md#open-contracts-and-delivery-gate).
