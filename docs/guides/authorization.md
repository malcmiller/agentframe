# Skill authorization and caller identity

These are approved authentication/authorization design contracts, not an
implemented security mechanism. Authentication
establishes who is calling; authorization decides what that caller may do.
AgentFrame provides shared enforcement, while the application supplies trusted
identity integration and domain policy.

## Permissions on skills, roles in the application

Declare required permissions on skills rather than organization-specific role
names. Roles are one application-controlled way to grant permissions.

| Concept | Proposed responsibility |
| --- | --- |
| `Authenticator` | Trusted host interface, with `EntraIDAuth`, `APIKeyAuth` and explicit `AllowAnonymous()` built-ins. `async authenticate(headers) -> VerifiedCaller \| None` is implemented with `APIKeyAuth`/`AllowAnonymous` (M2) and `EntraIDAuth` (M7 #76). |
| `Principal` | Immutable invocation identity with trusted subject, issuer, tenant where applicable, and verified claims/permission grants. Exact fields remain open. |
| `@skill(requires_permissions=[...])` | Declare stable permission requirements. All listed permissions are required; metadata does not enforce itself. |
| `AuthorizationPolicy` | Injected policy interface evaluating principal, skill requirements, and validated resource arguments against authoritative application data. |
| `Runtime` | Enforce the decision before skill execution across local, A2A, and MCP entry points. |

AgentFrame does not own a user database, role directory, token issuer, or
organization's permission assignments. Do not invent a built-in `admin` bypass.

Configure application mappings through `HostConfig(role_permissions=...)`,
using only verified roles/scopes, never roles supplied as skill arguments.
`EntraIDAuth` (optional extra `agentframe-py[entra]`) accepts only Microsoft Entra
v2.0 access tokens for one tenant: it checks the `RS256` signature against the
tenant's signing keys, the issuer, `tid`, audience (the API's client ID),
expiry/not-before with 60 seconds of skew and `ver`. The caller's `oid` becomes
the principal ID; its app `roles` and delegated `scp` scopes share one namespace
in `role_permissions`, and the principal's permissions are their union. Signing
keys that cannot be fetched fail closed with 401. See
[`EntraIDAuth`](../api/authorization.md#entraidauth) for the app registration
setup. An illustrative application mapping:

| Role | Permissions |
| --- | --- |
| Reader | `documents:read` |
| Editor | `documents:read`, `documents:write` |
| Publisher | `documents:read`, `documents:write`, `documents:publish` |

Permission strings are application-defined and must match exactly unless a
separately specified policy defines other semantics. Do not infer wildcard
grants, role hierarchies, or trusted claims from arbitrary request fields.

## Proposed skill declaration

```python
from typing import Protocol
from agentframe import Agent, Inject, RunContext, agent, skill


class Publisher(Protocol):
    async def publish(self, document_id: str) -> str: ...


@agent(
    name="PublishingAgent",
    description="Publishes approved documents.",
    version="0.1.0",
)
class PublishingAgent(Agent):
    @skill(
        id="publish",
        name="Publish content",
        description="Publish an approved document.",
        tags=["publishing"],
        requires_permissions=["documents:publish"],
    )
    async def publish(
        self, document_id: str, publisher: Inject[Publisher], ctx: RunContext,
    ) -> str:
        return await publisher.publish(document_id)
```

`Publisher` is an application-defined protocol injected via `Services`, not a built-in
publishing service. The runtime checks skill authorization before entering the
method. The authoritative publishing operation must also enforce current resource
policy at the action boundary; the permission declaration alone does not ensure
that a document belongs to the caller's tenant or is approved for publication.
`Inject` parameters never appear in Agent Cards, MCP schemas or caller arguments.
Missing required registrations fail startup with `DefinitionError`.

Requirements are mandatory constraints, not just advice to a policy plugin.
A custom policy may impose additional restrictions but cannot silently waive a
declared permission. Explicit inherited-skill overrides must not accidentally
discard requirements; override/restriction rules need tests before implementation.
An empty permission list is not a declaration of public remote access.

## Proposed server composition

```python
from agentframe import AgentApp, EntraIDAuth, HostConfig, Runtime, Services

services = Services()
services.add(Publisher, publisher_service)

runtime = Runtime(
    authorization=authorization_policy,
    services=services,
)

app = AgentApp(
    agent=PublishingAgent(),
    runtime=runtime,
    services=services,
    host=HostConfig(
        public_url="https://publisher.example.com",
        auth=EntraIDAuth(tenant_id=tenant_id, audience=audience),
        role_permissions={"Publisher": ["documents:publish"]},
    ),
)
```

`tenant_id`, `audience`, `publisher_service` and `authorization_policy` are trusted
application-supplied configuration/services, not request data or production defaults.
The application owns/closes singleton services. `Services.add_scoped(Type, factory)`
creates a fresh per-run instance that AgentFrame closes; exact factory/cleanup
interfaces remain open. The same `services` container is used here on app/runtime.
Host authentication creates the trusted principal and makes it available as
`ctx.principal`; callers do not set it through skill arguments. Missing required
authorization integration must prevent protected execution, not permit it.
This composition also works with optional [MCP exposure](../design/mcp-skills.md).

For in-process calls, the application must explicitly establish trusted caller
context when invoking protected skills. A proposed local shape is:

```python
from agentframe import Principal, Runtime


async def publish_document(
    runtime: Runtime,
    publisher: PublishingAgent,
    service_principal: Principal,
) -> str:
    return await runtime.invoke(
        publisher,
        skill="publish",
        arguments={"document_id": "doc_123"},
        principal=service_principal,
    )
```

This snippet illustrates a trusted application boundary; principal fields and
construction APIs are not finalized. Do not accept a serialized
principal from an HTTP/MCP argument and pass it through as trusted local context.
An application that allows arbitrary Python execution in its own process can
bypass Python methods; AgentFrame is not a sandbox against that application.

## Fail-closed execution pipeline

1. Authenticate at remote ingress and authorize admission before model-backed
   routing, retrieval, or other sensitive work.
2. Limit discovery/routing candidates to skills the caller is eligible to use.
   Discovery filtering is not the final resource authorization decision.
   Implemented for routing in M2 (#6) and used by the built-in
   [`ModelRouter`](../api/hosting.md#modelrouter) (M6 #68, #69): a router is
   offered only skills whose `requires_permissions` the caller holds, so the
   model never sees the others' descriptions. A caller with no eligible skill
   gets `REJECTED` without a model call; the selected skill is authorized
   again when it runs.
3. Select a skill and validate its arguments without performing unauthorized
   resource access. Do not trust model-selected resource ownership or tenant IDs.
4. Require the declared permissions and evaluate application resource policy
   using verified identity and authoritative data.
5. Execute with bounded context. Re-check each proposed tool/action and relevant
   resource state at the actual boundary where the side effect happens.
   Implemented in M5 (#62): tools whose `@tool(requires_permissions=...)` the
   caller lacks are never offered to the model and fail the run if called;
   every other call is checked by the runtime's
   [`ToolAuthorizationPolicy`](../api/authorization.md#toolauthorizationpolicy)
   with the validated arguments before it runs. Denial raises
   `PermissionDeniedError`, an outage or missed deadline
   `PolicyUnavailableError`; neither is fed back to the model.
6. Validate and apply output policy before returning information.

[`ExecutionPolicy`](../api/authorization.md#executionpolicy) content checks
(M7 #77, #78) slot into this pipeline without replacing any step: input
checks after step 4, model-request checks before every model request
(including routing in step 2, with `skill=None`, and each tool-loop
continuation), tool checks after each tool call's authorization in step 5 and
before the tool runs, and output checks after output validation in step 6.
Each fails closed.

`documents:publish` is necessary but does not mean "publish all documents."
Policy must consider the actual resource, tenant, ownership, approval state,
and any required human approval. Perform security-sensitive state checks and
updates atomically where the application supports it to avoid stale decisions.

The application owns approval workflows and their persistence. First release
has no built-in human approval pause/resume; required approval/authorization
must be established before the action or the action must not run. Native A2A
tasks do not imply resumable human workflows. First-release registered tool
loops must re-check each action and never retry side effects implicitly.

Missing identity, insufficient permissions, explicit denial, policy timeout,
or policy unavailability must fail protected operations. Errors must be safe and
protocol-native; distinguish invalid authentication, denied authorization, and
policy-service failure without leaking private resource existence. The decided
errors are `AuthenticationError`, `PermissionDeniedError` and
`PolicyUnavailableError`, carrying `invocation_id` and a safe message. A2A maps
failures to standard failed/rejected states where appropriate; exact per-error
protocol mappings remain open. Raw provider exceptions/stack traces/secrets
are never sent to callers; operational diagnostics are redacted.

## Exposure, anonymous access, and delegation

- Remote execution requires an explicit authentication/access configuration.
  Loopback binding does not itself authenticate a caller. Anonymous access must
  be deliberately enabled for specific behavior and cannot satisfy protected
  permission requirements by default. Choose `HostConfig(auth=AllowAnonymous())`
  deliberately; a remote host without an `auth` choice refuses startup even on
  loopback. `APIKeyAuth(keys=...)` is available for development/simple services;
  key-entry schema is still open and keys must come from secure configuration.
- Unprotected model-free local examples need no identity provider. Missing
  identity is never elevated into a trusted service identity for protected work.
- MCP allowlists control publication, not access. Authorize `tools/call` even if
  the name was hidden from discovery. Apply the same rule to A2A invocation.
- Public Agent Cards need not expose internal permission vocabularies. Do not
  invent standard per-skill A2A permission fields; any wire representation
  needs an explicit supported contract.
- Client-supplied roles, permissions, principal objects, context IDs, trace IDs,
  and MCP sessions do not establish authorization.
- Models and routers cannot grant permissions or impersonate callers. Restrict
  their candidate sets and enforce policy again after selection.
- Preserve or reduce effective authority during tool use and delegation.
  A server's broader model/service credentials must not become the caller's
  permission to act. Cross-service identity/delegation needs an explicitly
  trusted mechanism, not forwarded unsigned role metadata.
- Re-authorize protected task reads, cancellation, resubscription, conversation
  access, and output retrieval; an authorized initial call is not a permanent
  grant to everyone who knows its task ID.
  Returning a deduplicated A2A task also requires current task-access checks:
  the 10-minute caller/message-ID cache cannot cross tenant/caller boundaries.

## Cross-agent identity propagation (M8+)

**Decided per-remote modes:** `on_behalf_of` defaults when the incoming caller
used Entra. Use the standard Entra
[On-Behalf-Of flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow)
to obtain a token scoped to the target, not the original incoming bearer token.
The target verifies original user and calling-agent identity and applies its
own permissions; the caller must never grant more than that user possesses.
This requires that delegated user-token/consent conditions are supported; an Entra
app-only token is not automatically an OBO-capable user assertion.

`service` explicitly uses the calling agent's own identity for background/no-user
work; target permissions apply to that agent. With `APIKeyAuth` or
`AllowAnonymous` ingress, only service mode is available. Service mode is a
deliberate application policy, not an automatic fallback for a denied user.
Anonymous/API-key access does not itself grant authority to spend broader
service credentials: still authorize the remote action at the caller boundary.

Required exchange failure fails the call, never falls back to a more powerful
identity. Never forward the incoming token as-is, copy unsigned local principals
into remote arguments, or accept extension permissions as authentication.
Target-scoped credentials must match approved destinations, including redirects.
Entra targets verify issuer/audience/expiry/signature and user/actor context;
service targets independently authenticate and authorize the calling agent.

**Implemented service mode (#92):** configure each remote in `remotes=` on
`Runtime` or `AgentApp`, kept separate from discovery so credentials never live
in registries or cards:

```python
import os

from azure.identity.aio import ManagedIdentityCredential

from agentframe import (
    AgentDirectory, APIKeyCredential, EntraAppCredential, Runtime, Service,
)


def planner_runtime(
    directory: AgentDirectory, identity: ManagedIdentityCredential,
) -> Runtime:
    return Runtime(
        directory=directory,
        remotes={
            "research-agent": Service(
                credential=APIKeyCredential(os.environ["RESEARCH_API_KEY"])
            ),
            "review-agent": Service(
                credential=EntraAppCredential(identity, scope="api://review/.default")
            ),
        },
    )
```

Outbound headers are built only from the configured credential, per request,
and go only to the remote's validated interface origin (the card URL's
origin); redirects are never followed with them. A remote without an entry is
called without credentials, which works only when its card declares no
security. Startup fails with `DefinitionError`, naming the remote but never
the secret, when a card's security cannot be met by the configured credential.
See [`Service`](../api/authorization.md#service) for the exact rules, and
[`EntraAppCredential`](../api/authorization.md#entraappcredential) for the
app-role setup an Entra-protected target needs.

The `OnBehalfOf` mode (#93) adds consent/scope and actor-claim handling;
OBO is not a universal mechanism for every A2A peer; incompatible targets fail
explicitly under the configured mode rather than silently switching identity.
See [remote calls](../design/discovery.md#planned-remote-calls-and-cross-agent-context).

## Checks required before release

Next-core-release [agent discovery](../design/discovery.md) must restrict eligible candidates,
isolate caller/tenant/authenticated-card variants, and enforce trusted outbound
authentication and destination/redirect policy. A matched reference, fresh card
or card security declaration is not permission or endpoint trust. Authorize
again at remote invocation/actions; models and routers cannot grant authority.
Successful source reconciliation removes entries from new eligibility but does
not automatically cancel tasks or promise immediate revocation before TTL/source
refresh. Source failures are not proof of removal.

Future [AG-UI streaming](../design/streaming.md) uses the same explicit ingress access,
caller/tenant isolation and action boundaries. Frontend thread/run IDs,
messages/state/tools or returned tool results never establish a principal or
authority. Protocol tool visibility does not grant frontend execution or
approval; emitted events must pass release policy and expose no secrets.

Verify missing/insufficient grants, all-of requirements, tenant/resource
boundaries, anonymous behavior, policy outages/timeouts, discovery versus direct
invocation, inherited declarations, tool execution, delegation, and background
task access. Prove that denial prevents skill/action side effects and that local,
A2A, and MCP managed invocation use the same enforcement.

See [prompt safeguarding](../design/prompt-safeguarding.md) for complementary content
policies, [limits](limits.md) for bounded execution, and [tracing](../design/tracing.md)
for safe diagnostics. Content screening never replaces authorization.
