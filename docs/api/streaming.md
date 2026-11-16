# Streaming surface

[Reference status conventions](README.md). **Decision:** complete validated
results by default; streaming is opt-in. **Implemented (0.5.0):** the
[runtime streaming surface](#runtime-streaming-surface), streaming in the
Ollama and Foundry adapters and A2A `SendStreamingMessage` with text deltas
([A2A streaming](hosting.md#a2a-streaming), #257), and the optional
[AG-UI endpoint](#ag-ui-endpoint) for frontends (#258). The AG-UI endpoint is
a scoped profile of AG-UI 1.0, not support for every AG-UI feature.
A2A/Agent Cards stay first-class; MCP and tracing stay separate.

## AG-UI endpoint

**Purpose / decision:** serve frontends a native AG-UI event stream for one
agent, on the same server and under the same checks as `/a2a`, using the
official `ag-ui-protocol` SDK's types and encoder, without another agent
framework or a UI framework.

**Status:** implemented (#258). `AgentApp(ag_ui=AGUIConfig(enabled=True))`
serves `POST /ag-ui`: AG-UI 1.0 (`ag-ui-protocol>=1.0.0,<2`, the `ag-ui`
extra) over HTTP with server-sent events. Install it with
`pip install "agentframe-py[ag-ui]"`.

```python
from agentframe import (
    AGUIConfig, Agent, AgentApp, APIKey, APIKeyAuth, HostConfig, RunContext,
    agent, skill,
)


@agent(name="Assistant", description="Answers questions.", version="0.1.0")
class Assistant(Agent):
    @skill(description="Answers a question.", tags=["chat"])
    async def answer(self, ctx: RunContext, question: str) -> str:
        return await ctx.generate(question, stream=True)


def create_app(assistant: Assistant, frontend_key: str) -> AgentApp:
    host = HostConfig(
        public_url="https://assistant.example.com",
        # Development placeholder: load keys from a secret store.
        auth=APIKeyAuth(keys={frontend_key: APIKey("web-frontend")}),
    )
    return AgentApp(
        agent=assistant,
        host=host,
        default_skill="answer",
        ag_ui=AGUIConfig(enabled=True),
    )
```

**Request checks.** Each request is checked in this order; every refusal is an
HTTP error with a small JSON body, sent before any event, and runs nothing:

| Check | Refusal |
| --- | --- |
| Body within `HostConfig.max_request_bytes` | 413 |
| `HostConfig.auth` verifies the caller (same authenticator, role mapping and `OnBehalfOf` exchange as `/a2a`) | 401 with the card's `WWW-Authenticate` challenge |
| The caller's `HostConfig.rate_limit` allowance, shared with `/a2a` | 429 with `Retry-After` |
| A valid `RunAgentInput` whose `threadId` and `runId` are 1-256 characters and whose last `user`/`assistant` text message is a `user` message | 400 |
| Not shutting down, and a free run slot (`max_concurrent_runs`, shared with `/a2a`) | 503 with `Retry-After: 1` |

**Input.** A run is free text: the frontend's `user` and `assistant` text
messages, oldest first, become the conversation the app's
[`SkillRouter`](hosting.md#skillrouter) routes (a router's question ends the
run; the frontend's next run, carrying the question and its answer, is routed
again), or, without a router, the last user message's text goes to
[`default_skill`](hosting.md#default-skill). An app enabling AG-UI without
either raises `DefinitionError`. `system`, `developer`, `tool`, reasoning and
activity messages, and `tools`, `state`, `context`, `forwardedProps` and
`resume`, are not used: frontend content is never trusted instructions, and
frontend tools, shared state and interrupts are not supported. A verified
caller's `threadId` is the run's `conversation_id`, so
[`ctx.history()`](agents.md#runcontexthistory) is kept per agent, caller and thread;
anonymous callers get no history.

**Events.** Every event is a native `ag_ui.core` event, one per SSE `data:`
line:

| Runtime fact | Events |
| --- | --- |
| The run starts | `RUN_STARTED` echoing `threadId` and `runId`, with `protocolVersion` `"1.0"` and the server's own `invocation_id` in `metadata`. The frontend's IDs are never identity, correlation or deduplication keys. |
| Model text from `ctx.generate(..., stream=True)` | `TEXT_MESSAGE_START` (role `assistant`), one `TEXT_MESSAGE_CONTENT` per piece, `TEXT_MESSAGE_END` before a tool call or the end. |
| A tool call the model makes | `TOOL_CALL_START` with the tool's registered name and a new call ID (never the provider's), then `TOOL_CALL_END` once it returned. Never its arguments or result; a name the caller was not offered is never reported. |
| A `str` result | Nothing more when the latest streamed message ends with it. Otherwise (no streaming, output checks, or a different final text) one whole assistant message. |
| Any other result | `RUN_FINISHED.result`, the value in Pydantic JSON mode. |
| A router's question | One assistant message, then `RUN_FINISHED`. |
| Success | `RUN_FINISHED` with no outcome (success). |
| Failure | `RUN_ERROR` with the error's safe message, the AgentFrame error class name as `code` (for example `PermissionDeniedError`) and `invocation_id` in `metadata`. A skill's own exception is `"The skill failed."`; details stay in the operator log. |
| Shutdown cancels the run | Open message and tool calls closed, then `RUN_FINISHED` with the `cancelled` outcome. |

**Behavior / failures:**

- Authorization, validation, input checks, limits and the execution policy
  apply exactly as for `/a2a`, since both run through the same `Runtime`.
- **Whole-output checks:** with any `ExecutionPolicy` output check, nothing
  streams; the whole answer is sent once, after the checks pass. A denied
  answer ends the run with `RUN_ERROR` and is never sent.
- Streamed text counts toward `HostConfig.max_response_bytes`, as does a whole
  answer, question or JSON result; over it, the run ends with `RUN_ERROR`
  (`LimitExceededError`) and no more text is sent. Text already sent stays sent.
- Events are written straight to the connection, so a slow client slows the
  model call (backpressure) within the run's deadline; nothing is buffered
  without bound.
- **A client disconnect cancels the run** at once, even while it is quiet;
  per-run services are cleaned up and no terminal event is sent. There is no
  reconnect, replay or resume, and no deduplication of runs.
- Shutdown refuses new runs with 503, waits up to `shutdown_grace_seconds`
  for open runs, then cancels the rest with a cancelled `RUN_FINISHED`, also
  when the server cancels the requests itself at the end of its grace, as
  `agentframe run`'s Uvicorn does.
- With `tracing=`, each run is recorded like an A2A message or MCP tool call:
  a server `invoke_agent` span continuing the request's `traceparent`, run
  metrics, and busy or shutting-down refusals counted. The valid trace
  context is also forwarded to remote agents.
- `/ag-ui` is not on the Agent Card, and enabling it does not change A2A.
- Not supported: frontend tools, shared state, interrupts and resume, reasoning
  or raw events, tool arguments or results, usage on terminal events,
  WebSockets or other transports.

**Related:** [AG-UI design and primary sources](../design/streaming.md),
[`AgentApp`](hosting.md#agentapp), [`ExecutionPolicy`](authorization.md#executionpolicy),
[limits](../guides/limits.md).

## `AGUIConfig`

**Purpose / decision:** enable the optional AG-UI endpoint on the shared
host, separate from A2A/MCP and optional tracing.

**Signature status:** implemented (#258) as `AGUIConfig(enabled=False)`, a
frozen dataclass passed as `AgentApp(ag_ui=...)`.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `enabled` | `bool` | Optional; default `False` | `True` serves `POST /ag-ui`. `AGUIConfig()` and `ag_ui=None` serve nothing; nothing turns it on implicitly. |

The path `/ag-ui` is fixed. See the [example above](#ag-ui-endpoint).

**Behavior / failures:** `AgentApp` raises `DefinitionError` when it is
enabled without `router` or `default_skill`, or without the `ag-ui` extra
installed (`pip install "agentframe-py[ag-ui]"`).

**Related:** [AG-UI endpoint](#ag-ui-endpoint), [`AgentApp`](hosting.md#agentapp).
## Runtime streaming surface

**Purpose / decision:** pass a run's answer text on as it is generated, under
the same validation, authorization, tool/action checks and limits as a
complete-result run. Implemented in 0.5.0 (#257); A2A `SendStreamingMessage`
and the [AG-UI endpoint](#ag-ui-endpoint) (#258) use it. This is not an
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

The [AG-UI endpoint](#ag-ui-endpoint) (#258) adds run lifecycle events and
tool names and status on top of this surface. Native A2A streaming is
advertised separately on the Agent Card ([A2A streaming](hosting.md#a2a-streaming));
AG-UI support does not change it.

**Related:** [`Runtime.run`](runtime.md#runtimerun),
[`RunContext.generate`](agents.md#runcontextgenerate),
[streaming delivery gate](../design/streaming.md#open-contracts-and-delivery-gate).
