# Authorization and content policy

[Reference index and status conventions](README.md). These are unimplemented
design contracts, with decided authenticator names/fields below and still-open
custom interfaces, not implemented security mechanisms. Authentication, authorization,
and prompt/content checks are separate responsibilities.

## `Principal`

**Purpose / decision:** immutable verified caller identity supplied through
trusted host/local context, exposed to skills as `ctx.principal`.

**Signature status:** fields and construction/trust APIs remain open. The
concepts below are not finalized constructor keywords.

| Information | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Subject/issuer | Verified identity representation open | Trusted identity needed for protected work | Authentication integration validates its selected mechanism; do not trust request arguments. |
| Tenant, claims, permission grants | Exact fields/types open | Depends on application identity contract | Trusted grants and authoritative resource checks, not caller/model-asserted roles or ownership. |

```python
from agentframe import Agent, Principal, Runtime


async def read_document(
    runtime: Runtime, documents: Agent, principal: Principal,
) -> str:
    return await runtime.invoke(
        documents,
        skill="read",
        arguments={"document_id": "doc_123"},
        principal=principal,
    )
```

`documents` is an instance of [`Documents`](decorators.md#skill). The application
supplies a trusted principal and a runtime with authorization integration;
this is not an example of deserializing identity or granting permissions.

**Behavior / failures:** client-supplied principals, roles, permissions,
tenant claims, trace IDs, task IDs, and MCP sessions do not establish trust.
Missing identity must fail protected operations, never elevate to a service
identity. Unprotected local examples need no identity provider; anonymous
remote access requires explicit `HostConfig(auth=AllowAnonymous())`, never
omission. Python execution in the host process is not sandboxed.

**Related:** [`RunContext`](agents.md#runcontext),
[`Runtime.invoke`](runtime.md#runtimeinvoke),
[identity design](../authorization.md#proposed-server-composition).

## `AuthorizationPolicy`

**Purpose / decision:** injected application policy evaluating verified caller
grants and resource-specific rules, alongside mandatory declared skill
permissions.

**Signature status:** structural interface intended; constructor, method names,
arguments, and typed decisions remain open. Do not infer an `authorize()` method
from the responsibility description.

| Policy input/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Verified caller | `Principal` | Required for protected work | Never supplied as user/model business input. |
| Skill requirements | All-of `requires_permissions` declaration | Mandatory when declared | Custom policy may restrict further but cannot waive requirements. |
| Validated resource arguments and authoritative state | Application data; exact policy interface open | Needed for applicable resource checks | Tenant, ownership, approval, destination, and current resource state are application-owned decisions. |

```python
from agentframe import AuthorizationPolicy, Runtime


def protected_runtime(authorization_policy: AuthorizationPolicy) -> Runtime:
    return Runtime(authorization=authorization_policy)
```

The application implements/supplies the policy; no default role directory,
identity database, token issuer, wildcard grants, or `admin` bypass is implied.

**Behavior / failures:** authenticate/authorize admission before sensitive
routing work; restrict discovery/routing candidates and re-check selected
skill/resource authorization at invocation. Declared permissions are necessary,
not universal resource access. Re-check consequential actions and protected
task reads/cancellation/resubscription/output access at their boundaries.
Avoid stale decisions with application-supported atomic checks/updates.

Missing identity/integration, insufficient grants, denial, timeout, or policy
outage fails protected work with safe explicit failures. Empty permission
lists, MCP publication, discovery visibility, and loopback binding do not
grant anonymous access. Delegation cannot amplify caller authority through
broader server credentials; cross-service trust needs an explicit contract.
Use `AuthenticationError`, `PermissionDeniedError` or `PolicyUnavailableError`
as applicable, with safe message and `invocation_id`; exact transport mappings
remain open within the [decided error contract](runtime.md#agentframeerror).

Applications own human approval workflows; first release has no built-in
approval pause/resume. A required approval or action check must prevent execution
until satisfied, not create an implicit durable workflow from an A2A task.

**Related:** [`@skill`](decorators.md#skill), [`HostConfig`](hosting.md#hostconfig),
[fail-closed pipeline](../authorization.md#fail-closed-execution-pipeline).

## `ExecutionPolicy`

**Purpose / decision:** optional supplementary checks at content/action
boundaries. It cannot replace authorization or guarantee injection prevention.

**Proposed configuration shape:**

```text
ExecutionPolicy(
    *, input_checks = ..., model_request_checks = ...,
    tool_checks = ..., output_checks = ...,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `input_checks` | Collection of application checks; callback/decision types open | Optional configuration; concrete default open | Validate incoming content at its boundary, not a source of identity or permissions. |
| `model_request_checks` | Collection of application checks; types open | Optional configuration; concrete default open | Inspect every outbound request, including model-backed routing, retrieval/history/tool continuations and data egress. |
| `tool_checks` | Collection of application checks; types open | Optional configuration; concrete default open | Check proposed tools/actions before side effects, alongside mandatory authorization and limits. |
| `output_checks` | Collection of application checks; types open | Optional configuration; concrete default open | Inspect validated final output before release; whole-output rules may require buffering or declining streaming. |

```python
from agentframe import ExecutionPolicy, Runtime


def checked_runtime(policy: ExecutionPolicy) -> Runtime:
    return Runtime(policy=policy)
```

To configure the proposed collections, the design uses
`ExecutionPolicy(input_checks=[request_policy], model_request_checks=[egress_policy],
tool_checks=[action_policy], output_checks=[response_policy])`. These names are
application-supplied check placeholders, not built-ins. Callback signatures
and decision types must be settled before implementing them.

**Behavior / failures:** required checks are bounded by invocation deadlines;
denial, timeout, or unavailability stops the affected operation. Never silently
drop checks or turn rejection into successful generated text. No external
screening service is enabled by default; approve destinations/data handling
explicitly and apply needed local checks before approved egress.

Keep trusted instructions separate from untrusted user/retrieval/tool content;
no universal injection detector is promised. Never store secrets in prompts.
Schema-valid output is not necessarily safe or factual. Output rejection cannot
undo actions or released content, so checks/approvals must precede effects.
Tracing is not required for policy; diagnostics must not leak blocked content.

Complete validated results are first-release behavior. Later
[AG-UI streaming](streaming.md) applies release checks to text, tool arguments/
results and other frontend events; whole-output rules require buffering or
declining content streaming. No private reasoning/raw provider events, implicit
frontend tools, persistent state or human approval resume. Frontend messages/
state/tool declarations are untrusted, not `Principal` or trusted dependencies.

**Related:** [`AuthorizationPolicy`](#authorizationpolicy),
[`RunContext.generate`](agents.md#runcontextgenerate),
[safeguarding design](../prompt-safeguarding.md).

## `Authenticator`

**Purpose / decision:** common interface for verified remote identity, with
built-in Entra ID, API-key and explicit anonymous implementations.

**Signature status:** interface name decided; custom method/decision types open.

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Verified request identity | `Principal`; detailed method contract open | Required remote auth choice | Validate the selected mechanism; never accept request-supplied grants as verified. |

```python
from agentframe import Authenticator, HostConfig


def protected_host(auth: Authenticator) -> HostConfig:
    return HostConfig(public_url="https://agent.example.com", auth=auth)
```

**Behavior / failures:** a remote host with no auth choice refuses startup
(`DefinitionError`). Invalid credentials cause `AuthenticationError`; verified
grants still pass through skill/resource policy. Custom authenticators and
resource policies plug into the same enforcement interfaces.

**Related:** [`HostConfig`](hosting.md#hostconfig), [`Principal`](#principal).

## `EntraIDAuth`

**Purpose / decision:** validate Microsoft Entra tokens and map verified
roles/scopes to permissions.

**Decided constructor fields:**
`EntraIDAuth(tenant_id=..., audience=...)`.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `tenant_id` | `str` | Required | Trusted configured tenant; validate issuer/tenant, not untrusted arguments. |
| `audience` | `str` | Required | Expected token audience; validate signature, expiry and audience. |

```python
from agentframe import EntraIDAuth, HostConfig


def entra_host(tenant_id: str, audience: str) -> HostConfig:
    return HostConfig(
        public_url="https://writer.example.com",
        auth=EntraIDAuth(tenant_id=tenant_id, audience=audience),
        role_permissions={"Editor": ["documents:write"]},
    )
```

**Behavior / failures:** invalid tokens fail authentication; mapped grants do
not bypass current resource policy. `role_permissions` is application-owned on
`HostConfig`, not an organization role hardcoded on skills. Exact scope mapping,
claim selection, key refresh and permission-merge rules remain open.

**Related:** [`Authenticator`](#authenticator), [identity guide](../authorization.md).

For M8+ remote calls, per-target `on_behalf_of` is the Entra-ingress default
when a delegated user assertion is supported. Exchange for the target audience;
never forward the incoming token. Explicit `service` uses agent identity.
Exchange failure fails, never elevates via fallback. API-key/anonymous ingress
supports only explicit service mode. Per-target configuration and claim/provider
details remain open; see [OBO boundaries](../authorization.md#cross-agent-identity-propagation-m8).

## `APIKeyAuth`

**Purpose / decision:** explicit API-key authentication for development or
simple service-to-service callers, not credentials embedded in source/cards.

**Decided field:** `APIKeyAuth(keys={...})`; inner key/identity/grant mapping
schema and request credential location remain open.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `keys` | Mapping; entry schema open | Required trusted configuration | Load keys securely; do not infer a key-to-permission schema from the name. |

```python
from collections.abc import Mapping
from agentframe import APIKeyAuth


def development_auth(keys: Mapping[str, object]) -> APIKeyAuth:
    return APIKeyAuth(keys=keys)
```

`keys` is supplied by secure application configuration, not literal example
secrets. This generic placeholder does not finalize accepted entry types.

**Behavior / failures:** unknown/invalid credentials fail; never log raw keys.
HTTPS, rotation and grant/resource policy remain application responsibilities.
This built-in does not claim conforming MCP OAuth or Foundry incoming-A2A
authentication, which have their own protocol requirements.

**Related:** [`Authenticator`](#authenticator), [`HostConfig`](hosting.md#hostconfig).

## `AllowAnonymous`

**Purpose / decision:** explicit opt-in to anonymous remote ingress, never default.

**Decided construction:** `AllowAnonymous()`; no parameters selected.

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Anonymous access choice | Explicit authenticator | Required to allow anonymous ingress | Does not grant protected permissions or waive resource/action policy. |

```python
from agentframe import AllowAnonymous, HostConfig


local_demo_host = HostConfig(
    public_url="http://127.0.0.1:8000", auth=AllowAnonymous(),
)
```

This is an intentionally anonymous local demo, not a production-safe default.

**Behavior / failures:** remote startup refuses an omitted `auth`, even on
loopback. Anonymous requests cannot satisfy protected grants by default;
enabling this never disables validation, limits, accounting or checks.

**Related:** [`Authenticator`](#authenticator), [anonymous access](../authorization.md#exposure-anonymous-access-and-delegation).
