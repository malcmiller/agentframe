# Authorization and content policy

[Reference index and status conventions](README.md). These are mostly
unimplemented design contracts. M1 implements `Principal` and permission checks;
M2 story #4 implements `Authenticator`, `APIKeyAuth` and `AllowAnonymous` as
standalone units, which the HTTP host [`AgentApp`](hosting.md#agentapp) (#3)
wires in. M2 story #19 implements
`AuthorizationPolicy` with the local `Runtime`; M5 story #62 implements
`ToolAuthorizationPolicy` and `@tool(requires_permissions=...)`. Authentication, authorization,
and prompt/content checks are separate responsibilities.

## `Principal`

**Purpose / decision:** immutable verified caller identity supplied through
trusted host/local context, exposed to skills as `ctx.principal`.

**Signature status:** M1 implements a minimal frozen dataclass
`Principal(id: str, permissions: frozenset[str] = frozenset())`, constructed by
trusted application code. `Runtime` enforces all-of `requires_permissions`
against `permissions`; a missing principal or permission raises
`PermissionDeniedError`. Further identity fields and host construction/trust APIs
remain open; the concepts below are not finalized constructor keywords.

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
[identity design](../guides/authorization.md#proposed-server-composition).

## `AuthorizationPolicy`

**Purpose / decision:** injected application policy evaluating verified caller
grants and resource-specific rules, alongside mandatory declared skill
permissions.

**Signature status:** decided and implemented in M2 (#19) for local
`Runtime.run`/`invoke` as a structural `Protocol` with one async method; any
class with a matching `check` works without inheritance. A richer decision type
than `bool` remains open. Hosted wiring through
[`AgentApp(..., authorization=policy)`](hosting.md#agentapp) is implemented
(#3) and re-checks every A2A continuation message (#6). Over A2A a denial
ends the task in `REJECTED` and a policy outage in `FAILED`
([mapping](hosting.md#result-and-failure-mapping), #7).

```python
class AuthorizationPolicy(Protocol):
    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `principal` | `Principal \| None` | Always passed; `None` when the run has no identity | Verified caller supplied by trusted code, never business input. |
| `skill` | `SkillDefinition` | Always passed | Definition of the selected skill, including its `id` and declared `requires_permissions`. |
| `arguments` | `Mapping[str, object]` | Always passed | The skill's validated business arguments as a read-only mapping; never `ctx`, injected services or principal. |
| Return | `bool` | Required | Only `True` allows the run. `False` or any other value denies it. |

The policy checks the actual resource. This one allows `read` only for
documents the caller owns; `ownership` stands for application-owned
authoritative data:

```python
from collections.abc import Mapping

from agentframe import AuthorizationPolicy, Principal, Runtime, SkillDefinition


class OwnerOnly(AuthorizationPolicy):
    def __init__(self, ownership: Mapping[str, str]) -> None:
        self.ownership = ownership  # document ID -> owner principal ID

    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        document_id = arguments["document_id"]
        if principal is None:
            return False
        if skill.id != "read" or not isinstance(document_id, str):
            return False
        return self.ownership.get(document_id) == principal.id


runtime = Runtime(authorization=OwnerOnly({"doc_123": "alice"}))
```

`Runtime` runs the policy after argument validation and the declared
`requires_permissions` check, and before scoped services are resolved or the
skill body runs:

- `True` lets the run continue.
- Any other result raises `PermissionDeniedError` with a generic message that
  does not reveal whether the requested resource exists.
- An exception from the policy, or the run's deadline expiring while the
  policy is still deciding, raises `PolicyUnavailableError`. It fails closed and
  never allows. Policy time counts against the run's timeout, so a slow policy
  leaves less time for the skill body.

Without `authorization=`, `Runtime` performs no resource checks; this is only
appropriate for unprotected local use. Content checks belong to
[`ExecutionPolicy`](#executionpolicy), which remains a later milestone.

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
[fail-closed pipeline](../guides/authorization.md#fail-closed-execution-pipeline).

## `ToolAuthorizationPolicy`

**Purpose / decision:** injected application policy that authorizes every tool
call a model asks for, using the verified caller and the call's actual
arguments, so a model (or a prompt injection) can never make a tool act beyond
the caller's authority. Model output and instructions are never authority.

**Signature status:** decided and implemented in M5 (#62, decisions confirmed
2026-11-12) as a structural `Protocol` with one async method, passed as
`Runtime(tool_authorization=...)`. It is separate from
[`AuthorizationPolicy`](#authorizationpolicy), so existing skill policies are
unchanged; one class may implement both. Hosted apps use the runtime's policy:
`AgentApp(runtime=Runtime(..., tool_authorization=...))`; there is no separate
host setting.

```python
class ToolAuthorizationPolicy(Protocol):
    async def check_tool(
        self,
        principal: Principal | None,
        tool: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> bool: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `principal` | `Principal \| None` | Always passed; `None` when the run has no identity | The run's verified caller (`ctx.principal`), never model or business input. |
| `tool` | [`ToolDefinition`](decorators.md#tooldefinition) | Always passed | Definition of the tool about to run, including `name`, `side_effects` and `requires_permissions`. |
| `arguments` | `Mapping[str, object]` | Always passed | The tool's validated business arguments as a read-only mapping; never `ctx` or injected services. |
| Return | `bool` | Required | Only `True` allows the call. `False` or any other value, including truthy ones, denies it. |

This policy lets callers cancel only orders they own; `ownership` stands for
application-owned authoritative data:

```python
from collections.abc import Mapping

from agentframe import (
    Inject,
    Principal,
    Runtime,
    ToolAuthorizationPolicy,
    ToolDefinition,
    tool,
)


class OrderStore:  # Application-defined; registered through Services.
    async def cancel(self, order_id: str) -> None: ...


@tool(
    description="Cancel an order.",
    side_effects=True,
    requires_permissions=["orders:write"],
)
async def cancel_order(order_id: str, orders: Inject[OrderStore]) -> None:
    await orders.cancel(order_id)


class OrderToolPolicy(ToolAuthorizationPolicy):
    def __init__(self, ownership: Mapping[str, str]) -> None:
        self.ownership = ownership  # order ID -> owner principal ID

    async def check_tool(
        self,
        principal: Principal | None,
        tool: ToolDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        if principal is None:
            return False
        if tool.name != "cancel_order":
            return True
        return self.ownership.get(str(arguments["order_id"])) == principal.id


runtime = Runtime(tool_authorization=OrderToolPolicy({"A1": "alice"}))
```

For every tool call inside `ctx.generate(..., tools=[...])`:

1. Tools whose `requires_permissions` the principal does not all hold (or any
   requirement with no principal) are left out of the request's `tools`, so
   the model is never offered them.
2. A call naming a tool that was not selected, or with invalid arguments, runs
   nothing and the model is told what was wrong, as before.
3. A call naming a selected tool the caller lacks permissions for fails the run
   with `PermissionDeniedError`, before its arguments are even validated.
4. After argument validation, `check_tool` runs under the run's deadline.
   `True` lets the call continue; any other result raises
   `PermissionDeniedError` with a generic message that does not reveal whether
   the requested resource exists.
5. An exception from the policy, or the run's deadline expiring while it is
   still deciding, raises `PolicyUnavailableError`. It fails closed and never
   allows. Cancelling the run while the policy decides still cancels it.
6. Only then are the tool's services resolved and the tool run.

Denials and outages fail the run; they are never fed back to the model as a
tool result, so a model cannot probe the policy. Over A2A a denial ends the
task in `REJECTED` and an outage in `FAILED`
([mapping](hosting.md#result-and-failure-mapping)). Without
`tool_authorization=`, only `requires_permissions` are checked. A policy is not
required for `side_effects=True` tools, but give consequential tools a policy
or `requires_permissions`, or both. Approval workflows stay application-owned;
there is no built-in pause/resume. Future
[`ExecutionPolicy`](#executionpolicy) tool checks (#78) run after this
authorization and cannot waive it.

**Related:** [`@tool`](decorators.md#tool),
[`Runtime`](runtime.md#runtime),
[fail-closed pipeline](../guides/authorization.md#fail-closed-execution-pipeline),
[prompt safeguarding](../design/prompt-safeguarding.md#responsibility-split).

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
[safeguarding design](../design/prompt-safeguarding.md).

## `Authenticator`

**Purpose / decision:** common interface for verified remote identity, with
built-in Entra ID, API-key and explicit anonymous implementations.

**Signature status:** implemented as a structural protocol (M2 story #4) and
wired into HTTP by `AgentApp` (#3): missing or invalid credentials on `/a2a`
get HTTP 401 with no task. `EntraIDAuth` is implemented (M7 #76).

```text
class Authenticator(Protocol):
    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller | None
```

| Name | Type | Meaning and constraints |
| --- | --- | --- |
| `headers` | `Mapping[str, str]` | The request's HTTP headers. Header names should be matched case-insensitively. |
| Return | `VerifiedCaller \| None` | The verified caller, or `None` only for explicitly allowed anonymous callers. |
| `VerifiedCaller.id` | `str` | Stable caller identifier; becomes `Principal.id`. |
| `VerifiedCaller.roles` | `frozenset[str]` | Verified roles, mapped to permissions by `HostConfig(role_permissions=...)`. |

Missing or invalid credentials raise `AuthenticationError`, which the host maps
to HTTP 401 before creating any task or revealing skill details. The 401 carries
a `WWW-Authenticate` header with one challenge per scheme from `card_security()`
(#7): `APIKey location="header", name="X-API-Key"` for an API key, the scheme
name (such as `Bearer`) for HTTP auth, and `Bearer` for OAuth 2 / OpenID Connect.
The host builds
the `Principal` from the verified caller: its permissions are the union of
`role_permissions` for the caller's roles, and unknown roles grant nothing.
Every task operation requires authentication; the public Agent Card does not.
Tasks are scoped to the verified caller's ID. Anonymous callers share one task
namespace and cannot list tasks; see [hosting](hosting.md#agentapp).

```python
from collections.abc import Mapping

from a2a.types import APIKeySecurityScheme, SecurityScheme

from agentframe import (
    AuthenticationError,
    Authenticator,
    DeclaresCardSecurity,
    VerifiedCaller,
)


class ServiceTokenAuth(Authenticator, DeclaresCardSecurity):
    """Custom authenticator; inheriting the protocols is optional."""

    def __init__(self, tokens: Mapping[str, str]) -> None:
        self._tokens = tokens  # token -> caller ID, from secure configuration

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
        caller_id = self._tokens.get(headers.get("x-service-token", ""))
        if caller_id is None:
            raise AuthenticationError("Invalid credentials.")
        return VerifiedCaller(id=caller_id, roles=frozenset({"Reader"}))

    def card_security(self) -> dict[str, SecurityScheme]:
        scheme = APIKeySecurityScheme(location="header", name="X-Service-Token")
        return {"serviceToken": SecurityScheme(api_key_security_scheme=scheme)}
```

For brevity this sketch assumes lower-case header names and uses a plain
dictionary lookup; production authenticators should match header names
case-insensitively and compare secrets in constant time, as `APIKeyAuth` does.

**Behavior / failures:** a remote host with no auth choice refuses startup
(`DefinitionError`). Invalid credentials cause `AuthenticationError`; verified
grants still pass through skill/resource policy. Custom authenticators and
resource policies plug into the same enforcement interfaces.

**Related:** [`HostConfig`](hosting.md#hostconfig), [`Principal`](#principal),
[`DeclaresCardSecurity`](#declarescardsecurity).

## `DeclaresCardSecurity`

**Purpose / decision:** let an authenticator describe how callers authenticate
on the public Agent Card. In A2A 1.0 a card without security schemes means no
credentials are needed, so hosting refuses an authenticator that cannot describe
itself rather than publish a card that looks open.

**Signature status:** implemented as a runtime-checkable protocol (M2 story #2).

```text
class DeclaresCardSecurity(Protocol):
    def card_security(self) -> dict[str, SecurityScheme]
```

| Name | Type | Meaning and constraints |
| --- | --- | --- |
| Return | `dict[str, a2a.types.SecurityScheme]` | Card security schemes keyed by name, using the official SDK type. Callers must satisfy all of them. `{}` declares open access. |

`APIKeyAuth` returns an `apiKey` scheme for the `X-API-Key` header,
`EntraIDAuth` an OpenID Connect `entraId` scheme and `AllowAnonymous` returns
`{}`. See the `ServiceTokenAuth` example under [`Authenticator`](#authenticator)
for a custom implementation.

**Behavior / failures:** card generation raises `DefinitionError` naming the
authenticator class when it does not implement `card_security()`. The
declaration is descriptive; the authenticator still enforces credentials.

**Related:** [Agent Card fields](hosting.md#agentapp).

## `EntraIDAuth`

**Purpose / decision:** authenticate callers with Microsoft Entra v2.0 access
tokens and map their verified app roles and scopes to permissions.

**Signature status:** implemented (M7 story #76). Requires the optional extra
`agentframe[entra]`, which pins `PyJWT[crypto]` and `httpx`; importing
`EntraIDAuth` without it raises `ImportError` naming the extra.

```text
EntraIDAuth(*, tenant_id: str, audience: str, transport: httpx.AsyncBaseTransport | None = None)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `tenant_id` | `str` | Required | The tenant (directory) ID, a GUID. Domain names and `common`/`organizations` are refused (`DefinitionError`): only tokens from this one tenant are accepted. |
| `audience` | `str` | Required | The protected API's **application (client) ID**, a GUID. In v2.0 access tokens `aud` is the API's client ID, not its `api://` App ID URI (callers still request the token with the `api://.../.default` scope). Any other value, including an `api://` URI, raises `DefinitionError` naming this rule. |
| `transport` | `httpx.AsyncBaseTransport \| None` | Optional; default real HTTPS | Transport for fetching the tenant's metadata and signing keys, for example a test double or an outbound proxy. |

```python
from agentframe import EntraIDAuth, HostConfig


def entra_host(tenant_id: str, writer_api_client_id: str) -> HostConfig:
    return HostConfig(
        public_url="https://writer.example.com",
        auth=EntraIDAuth(tenant_id=tenant_id, audience=writer_api_client_id),
        role_permissions={
            "Editor": ["documents:write"],  # an app role
            "Documents.Read": ["documents:read"],  # a delegated scope
        },
    )
```

**Validation:** following Microsoft's
[access token validation guidance](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#validate-tokens),
a request is accepted only when all of these hold:

- The `Authorization` header is `Bearer <jwt>`. A missing header or another
  scheme raises `AuthenticationError`.
- The token is signed with `RS256` (never `none` or HMAC) by a key in the
  tenant's key set, found through the `jwks_uri` in
  `https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration`.
- `iss` is exactly `https://login.microsoftonline.com/{tenant_id}/v2.0`,
  `tid` equals `tenant_id`, `aud` equals `audience` and `ver` is `"2.0"`.
- `exp` is present and `exp`/`nbf` hold with 60 seconds of clock skew.
- `oid` (the stable object ID of the user or service principal) is present.

`VerifiedCaller.id` is `oid`. Its roles are the `roles` claim plus the
space-separated `scp` scopes, in one namespace for `role_permissions`: both are
defined on the API's own app registration. v1.0 tokens are not accepted.

**Signing keys:** fetched lazily on the first request (construction does no
I/O), cached for 24 hours, and refetched at most once every 5 minutes when a
token names an unknown key ID, which picks up rotated keys. If the keys cannot
be fetched (an Entra outage) requests fail closed with the normal 401 and a
warning log that names the tenant, not the token.

**Agent Card:** `card_security()` returns an OpenID Connect scheme named
`entraId` pointing at the tenant's v2.0 metadata URL, so A2A clients such as
the official SDK send `Authorization: Bearer <token>`; 401 responses carry the
`WWW-Authenticate: Bearer` challenge.

**Behavior / failures:** every rejection is `AuthenticationError`, which the
host maps to HTTP 401 with no task. Tokens never appear in logs or error
messages; the rejection reason is logged at debug level. Only the global Azure
cloud authority (`login.microsoftonline.com`) is supported. Mapped grants do
not bypass resource policy, and `role_permissions` is application-owned.

**One-time app registration setup** (in the Microsoft Entra admin center, for
the API the agent hosts):

1. Register an application for the API and note its application (client) ID:
   that GUID is `audience`.
2. Under **Expose an API**, set the Application ID URI (for example
   `api://<client-id>`) and add delegated scopes such as `Documents.Read`.
   Callers request tokens with the `api://<client-id>/.default` or a specific
   scope; the issued token's `aud` is still the client ID.
3. Under **App roles**, add roles such as `Editor` for users, groups or
   applications, and assign them.
4. In the **Manifest**, set `requestedAccessTokenVersion` to `2` (shown as
   `accessTokenAcceptedVersion` in the older manifest format) so Entra issues
   v2.0 tokens for this API; otherwise tokens are v1.0 and rejected.
5. To run the opt-in `entra` test, add the Azure CLI
   (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) as an authorized client application
   for a scope, grant `Documents.Read` to your account if needed, run
   `az login`, then:

```bash
AGENTFRAME_TEST_ENTRA=1 AGENTFRAME_ENTRA_TENANT_ID=<tenant-id> \
  AGENTFRAME_ENTRA_AUDIENCE=<api-client-id> uv run pytest -m entra
```

`AGENTFRAME_ENTRA_RESOURCE` overrides the requested resource (default
`api://<audience>`). The test is never run in CI.

**Related:** [`Authenticator`](#authenticator), [identity guide](../guides/authorization.md).

For M8+ remote calls, per-target `on_behalf_of` is the Entra-ingress default
when a delegated user assertion is supported. Exchange for the target audience;
never forward the incoming token. Explicit `service` uses agent identity.
Exchange failure fails, never elevates via fallback. API-key/anonymous ingress
supports only explicit service mode. Per-target configuration and claim/provider
details remain open; see [OBO boundaries](../guides/authorization.md#cross-agent-identity-propagation-m8).

## `APIKeyAuth`

**Purpose / decision:** explicit API-key authentication for development or
simple service-to-service callers, not credentials embedded in source/cards.

**Signature status:** implemented (M2 story #4).

```text
APIKey(caller_id: str, roles: Iterable[str] = ())
APIKeyAuth(*, keys: Mapping[str, APIKey])
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `keys` | `Mapping[str, APIKey]` | Required, non-empty | Maps each accepted key to its identity. Load keys from the environment or a secret store, never source code. |
| `APIKey.caller_id` | `str` | Required, non-empty | Becomes `Principal.id`; the key itself never does. Several keys may share one `caller_id`, for example during rotation. |
| `APIKey.roles` | `Iterable[str]`, stored as `frozenset[str]` | `()` | Role names mapped to permissions by `HostConfig(role_permissions=...)`. A single string is rejected. |

Clients send the key in the `X-API-Key` header.

```python
import os

from agentframe import APIKey, APIKeyAuth, HostConfig

auth = APIKeyAuth(
    keys={
        os.environ["BILLING_KEY"]: APIKey(caller_id="billing-svc", roles=["Reader"]),
    }
)
host = HostConfig(
    public_url="https://docs.example.com",
    auth=auth,
    role_permissions={"Reader": ["documents:read"]},
)
```

**Behavior / failures:** invalid configuration raises `DefinitionError`. A
missing header raises `AuthenticationError("Authentication required.")`; an
unknown key raises `AuthenticationError("Invalid credentials.")`. Keys are
compared in constant time and never logged, included in errors or shown by
`repr`. HTTPS, rotation and grant/resource policy remain application
responsibilities. This built-in does not claim conforming MCP OAuth or Foundry
incoming-A2A authentication, which have their own protocol requirements.

**Related:** [`Authenticator`](#authenticator), [`HostConfig`](hosting.md#hostconfig).

## `AllowAnonymous`

**Purpose / decision:** explicit opt-in to anonymous remote ingress, never default.

**Signature status:** implemented as `AllowAnonymous()` with no parameters
(M2 story #4). Its `authenticate` returns `None`, ignoring any credentials sent.

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

**Related:** [`Authenticator`](#authenticator), [anonymous access](../guides/authorization.md#exposure-anonymous-access-and-delegation).
