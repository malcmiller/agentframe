# Streaming surface

[Reference status conventions](README.md). **Decision:** complete validated
results by default; streaming is opt-in. **Implemented (0.5.0, #257):** the
[runtime streaming surface](#runtime-streaming-surface), streaming in the
Ollama and Foundry adapters and A2A `SendStreamingMessage` with text deltas
([A2A streaming](hosting.md#a2a-streaming)). **Not yet implemented (#258):**
the optional AG-UI agent-to-frontend adapter below; it is a planned protocol
surface, not support for every AG-UI feature. A2A/Agent Cards stay
first-class; MCP and tracing stay separate.

## Optional AG-UI adapter

**Purpose / decision:** map policy-approved managed execution events to a scoped
native AG-UI event stream for frontend clients, without another agent framework
or UI framework dependency.

**Signature status:** `AgentApp(ag_ui=AGUIConfig(enabled=True))` and `/ag-ui`
are decided for a later release (#258). It is meant to build on the implemented
[runtime streaming surface](#runtime-streaming-surface); no AG-UI event iterator
is selected.

| Concern | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| Protocol/schema/SDK and transport | Compatible official AG-UI versions; not selected | Future optional adapter, not first release | 1.0 documentation is research evidence, not a pinned compatibility commitment. Evaluate native Python `ag-ui-protocol` types/encoding; no other framework required. |
| Authentication/caller scope | Shared `Authenticator` / policy; adapter binding open | Explicit auth required remotely | Reuse the decided host auth choice; frontend thread/run IDs are not verified identity; isolate tenant/history/events/state. |
| Runtime event mapping | Typed managed events; union open | Run start/finish, text deltas, tool names/status and safe errors | No raw arguments by default, frontend execution, shared state or approval/resume. |
| Output/release policy | Existing authorization/content checks plus adapter gating; syntax open | Required checks precede affected release | Buffer whole validated results or decline streaming when whole-output policy requires it; no unchecked partial JSON. |
| Buffer/cancel/disconnect/reconnect controls | Detailed interfaces/bounds open | Disconnect cancels run; no reconnect/resume initially | Propagate cancellation; no rollback/free-inference guarantee. Whole-answer checks emit complete text at end, not deltas. |

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

**Related:** [AG-UI design, event mapping and primary citations](../design/streaming.md),
[`AgentApp`](hosting.md#agentapp), [`ExecutionPolicy`](authorization.md#executionpolicy).

## `AGUIConfig`

**Purpose / decision:** enable the optional frontend protocol adapter on the
shared host, separate from A2A/MCP and optional tracing.

**Decided call:** `AGUIConfig(enabled=True)` on `AgentApp(ag_ui=...)`.
Served at `/ag-ui`; other constructor fields/path configurability unspecified.

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `enabled` | `bool` | Explicit opt-in; omitted-object behavior stays off | No implicit streaming from model injection or A2A capability declarations. |
| Route | `/ag-ui` | Decided served endpoint | Same server/auth/permissions/limits; not per-skill routes or separate grants. |

```python
from agentframe import AGUIConfig, Agent, AgentApp, HostConfig, Runtime


def frontend_app(agent: Agent, host: HostConfig, runtime: Runtime) -> AgentApp:
    return AgentApp(
        agent=agent, host=host, runtime=runtime, ag_ui=AGUIConfig(enabled=True),
    )
```

`host` contains an explicit authenticator and `runtime` contains applicable
resource policy. This is future syntax, not a runnable SDK.

**Behavior / failures:** stream safe lifecycle/text/tool-name-status/error events.
Whole-output checks buffer complete text until approved. Disconnect cancels;
first version has no reconnect/resume. Frontend tools/shared state are later
opt-in, never automatic. Protocol version/transport and exact event projection
remain open. A2A streaming is implemented and advertised separately (#257).

**Related:** [`AgentApp`](hosting.md#agentapp), [policy gating](../design/streaming.md#policy-gated-output-and-independent-accounting).

## Runtime streaming surface

**Purpose / decision:** pass a run's answer text on as it is generated, under
the same validation, authorization, tool/action checks and limits as a
complete-result run. Implemented in 0.5.0 (#257); A2A `SendStreamingMessage`
uses it, and the AG-UI adapter (#258) is meant to build on it. This is not an
OTel trace stream or automatic multi-agent orchestration.

**Signature status:** implemented (#257). Three small pieces, no event union:

| Piece | Where | Meaning |
| --- | --- | --- |
| `ctx.generate(text, stream=True)` | [`RunContext.generate`](agents.md#runcontextgenerate) | The skill opts in per call; text only (`output=` with `stream=True` raises `DefinitionError`). |
| `Runtime.run(..., on_text=sink)` | [`Runtime.run`](runtime.md#runtimerun) | The caller receives the pieces through a [`TextSink`](models.md#textsink); `RunResult.value` is still the whole validated result. |
| `StreamingModel.stream(request, on_text)` | [`StreamingModel`](models.md#streamingmodel) | Implemented by `FoundryModel`, `OllamaModel` and `ScriptedModel`; other models answer whole. |

```python
from agentframe import Agent, RunContext, RunResult, Runtime, agent, skill


@agent(name="Writer", description="Drafts short texts.", version="0.1.0")
class Writer(Agent):
    @skill(description="Drafts a paragraph.", tags=["text"])
    async def draft(self, ctx: RunContext, topic: str) -> str:
        return await ctx.generate(f"Write a paragraph about {topic}.", stream=True)


async def print_as_generated(runtime: Runtime, writer: Writer) -> RunResult:
    async def show(text: str) -> None:
        print(text, end="", flush=True)

    return await runtime.run(
        writer, skill="draft", arguments={"topic": "tides"}, on_text=show
    )
```

**Behavior / failures:**

- Text is passed on only after input validation, authorization and input
  checks. Routing, planning and structured-output calls never stream.
- **Whole-output checks:** when the runtime's `ExecutionPolicy` has output
  checks, nothing is streamed; the complete answer is released only after the
  checks pass (A2A sends it as one final artifact).
- Every limit applies unchanged: tokens, model and tool calls, deadline and,
  over A2A, `max_response_bytes`, which streamed bytes count toward. The sink
  is awaited for each piece, so a slow consumer slows the model call
  (backpressure) within the run's deadline instead of buffering without bound.
  A2A delivery combines pieces into fewer updates and caps them per task (see
  [A2A streaming](hosting.md#a2a-streaming)).
- A model call that already passed text on is never retried, because the text
  cannot be withdrawn. Text passed on before a later failure stays released;
  the run still fails with the decided error, never a truncated success.
- An exception from the sink fails the run; cancelling the run cancels the
  model call.
- Refusals, reasoning and tool-call arguments are never streamed. With
  `tools=`, text the model writes before its tool calls is streamed.

**Still open (for #258):** the AG-UI event mapping (tool names/status, run
lifecycle events), disconnect cancellation for frontend streams and any
structured-result envelope. Native A2A streaming is advertised separately on
the Agent Card ([A2A streaming](hosting.md#a2a-streaming)); AG-UI support alone
does not enable it.

**Related:** [`Runtime.run`](runtime.md#runtimerun),
[`RunContext.generate`](agents.md#runcontextgenerate),
[streaming delivery gate](../design/streaming.md#open-contracts-and-delivery-gate).