# Prompt safeguarding and content-policy boundaries

This is a proposed shared-responsibility model, not an implemented prompt filter
or a promise to prevent every prompt injection. AgentFrame should make safe
composition the default and enforce checks outside the model. Applications
define domain-specific rules and acceptable data/action boundaries.

## Responsibility split

| Concern | AgentFrame responsibility | Application responsibility |
| --- | --- | --- |
| Instruction integrity | Keep trusted instructions distinct from user content, retrieval, and tool results; preserve provenance. | Choose trusted instruction sources and avoid interpolating untrusted content into privileged instructions. |
| Prompt injection | Provide screening hooks without trusting model output as authority. | Assess data sources and configure domain-specific mitigations; no generic filter is a reliable security boundary. |
| Sensitive data | Keep credentials outside model-visible context and content out of logs by default. | Classify/minimize data, choose approved providers, and configure redaction/egress policy. |
| Tool misuse | Validate arguments and enforce authorization, limits, and approvals before actions. | Define permitted tools, destinations, resource scope, and consequential-action approvals. |
| Invalid/unsafe output | Validate output contracts and enforce configured output checks. | Define acceptable content and handling rules; schema validity is not factual correctness or safety. |
| Prompt disclosure | Avoid publishing instructions through cards, discovery, errors, or telemetry. | Never store secrets in instructions; models may reproduce instruction content. |

Separating message roles helps preserve intended trust boundaries; it does not
make a model immune to conflicting instructions embedded in untrusted content.
The strongest protection is limiting what a compromised agent can access or do.

## Proposed policy hooks

```python
from agentframe import ExecutionPolicy, Runtime

policy = ExecutionPolicy(
    input_checks=[request_policy],
    model_request_checks=[sensitive_data_policy],
    tool_checks=[action_policy],
    output_checks=[response_policy],
)

runtime = Runtime(
    authorization=authorization_policy,
    policy=policy,
)
```

All named policies are application-supplied dependencies. `ExecutionPolicy`
provides additional boundary checks; the required permission/resource checks
in `authorization_policy` remain mandatory and cannot be overridden by an
input filter or a model's assessment. Exact hook signatures and typed decision
objects remain open. No external screening service is enabled by default.

## Check at the boundary, not just once

```text
authenticate and authorize admission
  -> validate incoming content and apply input checks
  -> route among eligible skills, if needed
  -> validate selected arguments and authorize the skill/resource
  -> assemble a model request with trust boundaries preserved
  -> apply outbound data checks before each model request
  -> validate and authorize every proposed tool/action before execution
  -> validate final output and apply output checks
  -> return the protocol-native result
```

Model-backed routing must use the same outbound data checks before sending any
content to its model. Later model requests containing tool results, retrieved
documents, and conversation history must also pass checks; the first prompt
alone is not sufficient coverage.

First release returns complete validated results. For later streamed responses,
including the proposed [AG-UI adapter](streaming.md), configured output checks
must run before affected content is released. Buffer or decline streaming when the policy requires
whole-output inspection; a later rejection cannot retract already sent data.
Output rejection also cannot undo actions already performed, so action checks
and approvals must occur before side effects.
This includes frontend-visible tool arguments/results and error/progress events,
not only assistant text. Partial structured output is not a validated result.
Human approval workflows belong to the application; there is no built-in
first-release approval pause/resume. Required action checks still fail closed.

Checks must honor invocation deadlines, have bounded resource use, and surface
explicit failures. Denial or unavailability of a required check stops the
affected operation. Do not silently drop a check, fall back to another provider,
or turn rejection into a successful-looking answer.
Use safe `AgentFrameError` failures with invocation correlation, including
`PermissionDeniedError` for authorization denial and `PolicyUnavailableError`
for a required unavailable policy. Exact content-screening denial subtype
and transport mapping remain open. Provider refusals raise `ModelRefusalError`,
unsupported capabilities fail before sending, and truncated output raises
`IncompleteOutputError` unless explicitly permitted (syntax open, validation
still required). None may be disguised as valid generated text.

Applications must explicitly approve any external screening destination and its
data handling. Screening is itself data egress: do not send sensitive content
to a classifier to discover that it should never have left the process.
Use local deterministic checks where needed before approved external screening.

Authorization and content-policy decisions work without tracing. Diagnostics
should record safe decision categories and correlation, not blocked secrets,
raw prompts, private reasoning, or full policy-service responses.

See [authorization](authorization.md), [limits](limits.md), and
[tracing](tracing.md). AgentFrame does not supply an identity directory, universal
content policy, or guaranteed prompt-injection detector.

Next-core-release [discovery](discovery.md) treats registry metadata and fetched cards
as data, not privileged instructions, trusted destinations or permission grants.
Selecting trusted sources does not eliminate the need for destination/DNS/
redirect checks, bounded fetches, variant isolation and current authorization.
Do not automatically expose discovered skills as model tools or forward
credentials to card-advertised origins. Sending card content to a selector model
is explicit egress subject to the same outbound checks and budgets; discovery
alone never delegates work.

AG-UI frontend messages, roles, context, state, tools and forwarded properties
are untrusted input, never application-owned instructions/dependencies or
authorization. No automatic frontend tool execution, persistence or resumability.
Do not expose hidden prompts, secrets, raw provider payloads or private reasoning
through native/custom/raw events, input echoes or tool visibility. A protocol
adapter is not a universal injection defense or an OTel event exporter.
