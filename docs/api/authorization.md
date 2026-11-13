# Authorization and content policy

[Reference index and status conventions](README.md). These are mostly
unimplemented design contracts. M1 implements `Principal` and permission checks;
M2 story #4 implements `Authenticator`, `APIKeyAuth` and `AllowAnonymous` as
standalone units, which the HTTP host [`AgentApp`](hosting.md#agentapp) (#3)
wires in. M2 story #19 implements
`AuthorizationPolicy` with the local `Runtime`; M5 story #62 implements
`ToolAuthorizationPolicy` and `@tool(requires_permissions=...)`. M7 stories #77 and
#78 implement `ExecutionPolicy` input, model-request, tool and output checks.
M8+ story #92 implements outbound identity for remote calls: `Service`,
`OutboundCredential`, `APIKeyCredential` and `EntraAppCredential`.
Authentication, authorization,
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
[`ExecutionPolicy`](#executionpolicy), which runs its input checks after this
policy allows.

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
there is no built-in pause/resume.
[`ExecutionPolicy`](#executionpolicy) tool checks (M7 #78) run after this
authorization, before step 6, and cannot waive it.

**Related:** [`@tool`](decorators.md#tool),
[`Runtime`](runtime.md#runtime),
[fail-closed pipeline](../guides/authorization.md#fail-closed-execution-pipeline),
[prompt safeguarding](../design/prompt-safeguarding.md#responsibility-split).

## `ExecutionPolicy`

**Purpose / decision:** optional application checks on a run's content at its
boundaries: the input, every outbound model request, every tool call and the
output. They add
to authorization and never replace it, and they are not a universal prompt
injection detector.

**Signature status:** implemented in M7 (#77; tool checks #78) as a frozen,
keyword-only dataclass, configured with `Runtime(policy=...)` or
`AgentApp(policy=...)`.

```text
ExecutionPolicy(
    *,
    input_checks: Sequence[InputCheck] = (),
    model_request_checks: Sequence[ModelRequestCheck] = (),
    tool_checks: Sequence[ToolCheck] = (),
    output_checks: Sequence[OutputCheck] = (),
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `input_checks` | `Sequence[InputCheck]` | Optional; default none | Run after argument validation and the [`AuthorizationPolicy`](#authorizationpolicy), before services are resolved or the skill runs. Receive the validated arguments, read-only. |
| `model_request_checks` | `Sequence[ModelRequestCheck]` | Optional; default none | Run before every model request is sent and before its tokens are reserved: text, structured output, each tool-loop continuation and every [`ModelRouter`](hosting.md#modelrouter) or custom-router `RoutingContext.generate` call (with `skill=None`). Checked once per logical request, not again when a transient failure is retried. |
| `tool_checks` | `Sequence[ToolCheck]` | Optional; default none | Run for every tool call the model asks for, in every tool-loop continuation: after argument validation and the [`ToolAuthorizationPolicy`](#toolauthorizationpolicy), before the tool's services are resolved or the tool runs. Receive the tool's name and validated arguments, read-only. Tool calls are never retried, so no path runs a tool without its checks. |
| `output_checks` | `Sequence[OutputCheck]` | Optional; default none | Run after the skill's value is validated, before it is returned or published. |

Each check is an async callable taking positional arguments, the skill, the
principal and the content (for tool checks, the tool name and its arguments),
and returning `bool`. Any async function or
object with a matching `__call__` works; no inheritance is needed:

```text
InputCheck:        (skill: SkillDefinition, principal: Principal | None,
                    arguments: Mapping[str, Any]) -> bool
ModelRequestCheck: (skill: SkillDefinition | None, principal: Principal | None,
                    request: ModelRequest) -> bool
ToolCheck:         (skill: SkillDefinition, principal: Principal | None,
                    tool: str, arguments: Mapping[str, Any]) -> bool
OutputCheck:       (skill: SkillDefinition, principal: Principal | None,
                    value: Any) -> bool
```

`skill` is `None` for model-request checks during routing, which runs before a
skill is selected. `request` is the complete
[`ModelRequest`](models.md#modelrequest): messages, settings, output schema and
tool specs.

```python
from collections.abc import Mapping
from typing import Any

from agentframe import (
    ExecutionPolicy, ModelRequest, Principal, Runtime, SkillDefinition,
)


async def no_card_numbers(
    skill: SkillDefinition | None, principal: Principal | None, request: ModelRequest
) -> bool:
    # Application-supplied placeholder rule: block requests that look like
    # they contain a payment card number.
    return not any("4111 1111" in message.content for message in request.messages)


async def short_answers(
    skill: SkillDefinition, principal: Principal | None, value: Any
) -> bool:
    return not isinstance(value, str) or len(value) < 2_000


async def no_bulk_refunds(
    skill: SkillDefinition,
    principal: Principal | None,
    tool: str,
    arguments: Mapping[str, Any],
) -> bool:
    # Application-supplied placeholder rule: large refunds need a human.
    return tool != "refund" or arguments["amount"] <= 100


runtime = Runtime(
    policy=ExecutionPolicy(
        model_request_checks=[no_card_numbers],
        tool_checks=[no_bulk_refunds],
        output_checks=[short_answers],
    )
)
```

**Order in a run:** declared permissions -> argument validation ->
authorization policy -> **input checks** -> skill (every model request ->
**model-request checks**; every tool call -> tool argument validation ->
tool authorization -> **tool checks** -> tool) -> output validation ->
**output checks**. Hosted free-text requests pass their routing model
requests through the same model-request checks first.

**Behavior / failures:** checks in each group run in order and stop at the
first failure; the run fails closed.

- Only an explicit `True` allows. Any other value, including truthy ones,
  raises `PermissionDeniedError` ("Skill 'x' was blocked by an execution
  policy check"; "The request was blocked ..." while routing; "Tool 't' was
  blocked ..." for a tool call). A denied model request is never sent and
  reserves no tokens; a denied tool call never runs; a denied output is never
  returned. Over A2A a denial ends the task `REJECTED`.
- A check that raises, or is still running at the run's deadline, raises
  `PolicyUnavailableError` (A2A task `FAILED`); a blocked model request is
  never sent and a blocked tool never runs.
- A blocked tool call fails the whole run; it is never fed back to the model
  as a tool result, so a model cannot probe the policy.
- Check time counts against the run's timeout. Messages name only the skill
  or tool, never the checked content, and AgentFrame never logs that content.
- With no policy, runs are unchanged.

No external screening service is enabled by default; approve destinations and
data handling explicitly and apply needed local checks before approved
egress. Schema-valid output is not necessarily safe or factual. Output
rejection cannot undo actions or released content, so checks/approvals must
precede effects. Tracing is not required for policy.

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

**Signature status:** implemented (M7 story #76; on-behalf-of parameters M8+
#93). Requires the optional extra `agentframe-py[entra]`, which pins
`PyJWT[crypto]`, `httpx` and (for on-behalf-of) `azure-identity`; importing
`EntraIDAuth` without it raises `ImportError` naming the extra.

```text
EntraIDAuth(
    *,
    tenant_id: str,
    audience: str,
    client_id: str | None = None,
    client_credential: str | bytes | Callable[[], str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    token_transport: azure.core.pipeline.transport.AsyncHttpTransport | None = None,
)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `tenant_id` | `str` | Required | The tenant (directory) ID, a GUID. Domain names and `common`/`organizations` are refused (`DefinitionError`): only tokens from this one tenant are accepted. |
| `audience` | `str` | Required | The protected API's **application (client) ID**, a GUID. In v2.0 access tokens `aud` is the API's client ID, not its `api://` App ID URI (callers still request the token with the `api://.../.default` scope). Any other value, including an `api://` URI, raises `DefinitionError` naming this rule. |
| `client_id` | `str \| None` | Optional; needed for [`OnBehalfOf`](#onbehalfof) remotes | The API's own application (client) ID, used to exchange user tokens. Must equal `audience` (case-insensitive), because Entra only exchanges a token whose `aud` is the app asking; otherwise `DefinitionError`. Give it together with `client_credential` or not at all. |
| `client_credential` | `str \| bytes \| Callable[[], str] \| None` | Optional; needed for `OnBehalfOf` remotes | How the API proves its identity to Entra for on-behalf-of exchanges: a client secret (`str`), a PEM or PKCS12 certificate including its private key, without a password (`bytes`), or a function returning a client assertion (`Callable[[], str]`, for example a federated managed identity token). Load it from a secret store; `repr` never shows it. |
| `transport` | `httpx.AsyncBaseTransport \| None` | Optional; default real HTTPS | Transport for fetching the tenant's metadata and signing keys, for example a test double or an outbound proxy. |
| `token_transport` | `azure-core` async transport \| `None` | Optional; default real HTTPS | Transport for on-behalf-of token requests, for example a test double or an outbound proxy. |

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

A delegated user token (one with an `scp` claim and no `idtyp: "app"`) is kept
as the private `VerifiedCaller.user_assertion`, which only the host uses, for
[`OnBehalfOf`](#onbehalfof) remotes. App-only tokens never are. It never
reaches `Principal`, `RunContext`, logs or errors.

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

For M8+ remote calls, [`OnBehalfOf`](#onbehalfof) is the recommended mode
for Entra hosts: it exchanges the user's token for the target's scope and never
forwards the incoming token. [`Service`](#service) uses the agent's own
identity and is always explicit; there is no fallback between them. See
[identity propagation](../guides/authorization.md#cross-agent-identity-propagation-m8).

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

## `Service`

**Purpose / decision:** per-remote outbound identity: call a remote agent with
the calling agent's own credential (`service` mode). Implemented in M8+ (#92).
Entra on-behalf-of ([`OnBehalfOf`](#onbehalfof), #93) is the other mode; there
is no implicit mode and no fallback between modes.

**Signature status:** implemented as `Service(credential: OutboundCredential)`,
a frozen dataclass, configured through `remotes=` on
[`Runtime`](runtime.md#runtime) or [`AgentApp`](hosting.md#agentapp).

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `credential` | [`OutboundCredential`](#outboundcredential) | Required | What every request to that remote carries, such as [`APIKeyCredential`](#apikeycredential) or [`EntraAppCredential`](#entraappcredential). |
| `remotes=` | `Mapping[str, Service \| OnBehalfOf]` on `Runtime` / `AgentApp` | Optional; default no credentials | Keys are directory names, as in `Remote[Literal["name"]]`. Same rule as `services=`: pass it to `AgentApp` or to its `Runtime`, not both. Kept separate from discovery, so credentials never live in registries or cards. Entries for names no skill uses are allowed. |

```python
import os

from azure.identity.aio import ManagedIdentityCredential

from agentframe import (
    AgentDirectory, APIKeyCredential, EntraAppCredential, Runtime, Service,
)


def planner_runtime(
    directory: AgentDirectory, credential: ManagedIdentityCredential,
) -> Runtime:
    return Runtime(
        directory=directory,
        remotes={
            "research-agent": Service(
                credential=APIKeyCredential(os.environ["RESEARCH_API_KEY"])
            ),
            "review-agent": Service(
                credential=EntraAppCredential(credential, scope="api://review/.default")
            ),
        },
    )
```

`directory` is the application's opened
[`AgentDirectory`](discovery.md#agentdirectory); the credential is the
application's own and is never closed by AgentFrame.

**Behavior / failures:**

- The remote authorizes the calling agent, not the caller the run serves. It
  is the only mode for agents whose callers use API keys or no authentication.
  Service mode is a deliberate application choice, never a fallback for a
  denied user.
- Outbound headers come only from the configured credential. Incoming
  `Authorization`, `X-API-Key` or any other caller header is never copied.
- `credential.headers()` is called before every request (`SendMessage`, each
  `GetTask` poll and a best-effort `CancelTask`), and its headers go per
  request only to the card's validated interface URL, which has the same
  origin as the registry's card URL. They never go into the shared directory
  client's defaults and never with a card fetch. Redirects are not followed:
  a redirect fails the call with `RemoteAgentError`, and startup refuses a
  remote with credentials when the directory's `http_client` follows
  redirects.
- **Startup check** ([`Runtime.validate_remotes`](runtime.md#runtimevalidate_remotes)):
  a card that declares no security is called without credentials when the
  remote has no entry. A card that declares security needs an entry whose
  credential satisfies every scheme of one of the card's
  `security_requirements` (or one of its `security_schemes` when it lists no
  requirements); otherwise `DefinitionError` naming the remote and the
  credential type, never the secret. A remote with an entry but an open card
  is still sent the credential.
- A credential whose `headers()` raises fails the call with
  `AuthenticationError`; its error is logged by type only.
- Invalid `remotes=` (a value that is neither a `Service` nor an
  `OnBehalfOf`, or a credential that does not implement `OutboundCredential`)
  raises `DefinitionError` at construction.

**Related:** [identity propagation](../guides/authorization.md#cross-agent-identity-propagation-m8),
[`Remote`](discovery.md#remoteliteralname).

## `OnBehalfOf`

**Purpose / decision:** per-remote outbound identity that calls a remote agent
on behalf of the signed-in user, through the standard Microsoft Entra
[on-behalf-of flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow).
The remote applies the user's own permissions, so no agent gains more
authority than the user has. It is the recommended mode for agents whose
callers sign in with [`EntraIDAuth`](#entraidauth); `Service` stays explicit.
Implemented in M8+ (#93).

**Signature status:** implemented as `OnBehalfOf(scope: str)`, a frozen
dataclass, configured through `remotes=` on [`AgentApp`](hosting.md#agentapp)
(or its [`Runtime`](runtime.md#runtime)). The middle tier's own client
credential lives on the inbound `EntraIDAuth(client_id=..., client_credential=...)`,
because the exchange redeems the token that authenticator verified.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `scope` | `str` | Required, non-empty | The remote's scope, `api://<target-client-id>/.default`. The exchanged token carries the delegated permissions the calling app was granted (and consented) on the target. |

```python
import os

from agentframe import (
    Agent, AgentApp, AgentDirectory, EntraIDAuth, HostConfig, OnBehalfOf,
)


def planner_app(directory: AgentDirectory, planner: Agent) -> AgentApp:
    planner_client_id = os.environ["PLANNER_CLIENT_ID"]
    return AgentApp(
        agent=planner,  # an @agent instance with Remote[...] skill parameters
        host=HostConfig(
            public_url="https://planner.example.com",
            auth=EntraIDAuth(
                tenant_id=os.environ["TENANT_ID"],
                audience=planner_client_id,
                client_id=planner_client_id,  # must equal audience
                client_credential=os.environ["PLANNER_CLIENT_SECRET"],
            ),
        ),
        directory=directory,
        remotes={
            "research-agent": OnBehalfOf(
                scope=f"api://{os.environ['RESEARCH_CLIENT_ID']}/.default"
            ),
        },
    )
```

Prefer a certificate (`bytes`) or a federated client assertion
(`Callable[[], str]`) over a client secret in production.

**Behavior / failures:**

- **Startup** (`AgentApp` construction): an `OnBehalfOf` remote whose host
  `auth` is not `EntraIDAuth` (for example `APIKeyAuth` or `AllowAnonymous`),
  or an `EntraIDAuth` without `client_id`/`client_credential`, raises
  `DefinitionError`; `client_id != audience` already fails in `EntraIDAuth`.
  Like `Service`, startup also checks the target's card: `OnBehalfOf` meets
  OAuth 2.0, OpenID Connect and HTTP `bearer` schemes, so it fits
  `EntraIDAuth` targets but not API-key cards.
- **Each run** that binds an `OnBehalfOf` remote creates one
  `azure.identity.aio.OnBehalfOfCredential` for the caller's verified token,
  shared by all of that run's `OnBehalfOf` remotes and closed when the run
  ends. Every request (`SendMessage`, `GetTask` polls, best-effort
  `CancelTask`) sends `Authorization: Bearer <exchanged token>`; tokens are
  cached until they near expiry, so one run normally exchanges once per
  scope. Headers go only to the remote's validated interface origin, as for
  `Service`. The incoming token is never forwarded.
- **No signed-in user:** OBO works only for user principals. An app-only
  caller (a token without `scp`, or with `idtyp: "app"`) and every local
  `Runtime.run` fail with `PermissionDeniedError` when the run binds the
  remote; hosted tasks end `REJECTED`. Nothing is exchanged or sent.
- **Exchange failure** (Entra refuses the token, missing consent, an outage)
  fails the call with `AuthenticationError`; hosted tasks end `FAILED`. The
  error is logged by type only. There is never a fallback to `Service` or to
  sending without credentials.
- Tokens and the client credential never appear in logs, errors,
  `Principal` or `RunContext`. The host also drops credential headers
  (`Authorization`, `Proxy-Authorization`, `Cookie`, `X-API-Key`) from the A2A
  SDK's call context, which the SDK logs at debug level.
- **What the target sees:** the exchanged token's `oid` is the user, `azp` is
  the calling agent's client ID, and `scp` lists the delegated permissions.
  `EntraIDAuth` targets authorize on the user's roles and scopes through
  `role_permissions`. Checking the calling app (`azp`) at the target is the
  target's own application policy; AgentFrame does not add actor checks.

**One-time setup: two app registrations** (Microsoft Entra admin center). The
target (specialist) registration is the one described under
[`EntraIDAuth`](#entraidauth), for example `agentframe-entra-test`, exposing
`Documents.Read` with v2.0 tokens.

1. **Orchestrator registration.** Register a second application (for example
   `agentframe-obo-planner`) and note its client ID. In its **Manifest** set
   `requestedAccessTokenVersion` to `2`.
2. **Expose an API.** Set its Application ID URI to `api://<planner-client-id>`
   and add a delegated scope, for example `Plans.Run`, so users can get a
   token for the orchestrator.
3. **Client credential.** Under **Certificates & secrets**, add a client
   secret (or certificate or federated credential). This is
   `EntraIDAuth(client_credential=...)`; keep it in a secret store.
4. **API permission to the target.** Under **API permissions**, add
   **My APIs** -> the target registration -> **Delegated permissions** ->
   `Documents.Read`, then **Grant admin consent** (OBO has no user
   interaction, so consent must exist up front). Alternatively, list the
   orchestrator's client ID in the target's `knownClientApplications` for
   combined consent.
5. **User token for the orchestrator.** To get a token as yourself with the
   Azure CLI, add the Azure CLI (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`) as an
   authorized client application for `Plans.Run` on the orchestrator, then:
   `az login` and
   `az account get-access-token --tenant <tenant-id> --scope api://<planner-client-id>/.default`.

**Opt-in real test** (never in CI; `tests/integration/test_on_behalf_of.py`):

```bash
AGENTFRAME_TEST_ENTRA_OBO=1 AGENTFRAME_ENTRA_TENANT_ID=<tenant-id> \
  AGENTFRAME_ENTRA_AUDIENCE=<target-client-id> \
  AGENTFRAME_ENTRA_OBO_CLIENT_ID=<planner-client-id> \
  AGENTFRAME_ENTRA_OBO_CLIENT_SECRET=<planner-client-secret> \
  uv run pytest -m entra tests/integration/test_on_behalf_of.py
```

It serves the specialist (`EntraIDAuth` for the target) and the orchestrator
(`EntraIDAuth` with the client secret and `OnBehalfOf`) on localhost, calls
the orchestrator with your Azure CLI user token and checks the specialist
answered for your `oid`. Your account needs `Documents.Read` consented as above.

**Related:** [`EntraIDAuth`](#entraidauth), [`Service`](#service),
[identity propagation](../guides/authorization.md#cross-agent-identity-propagation-m8).

## `OutboundCredential`

**Purpose / decision:** the structural protocol for what a `Service` sends.
Implement it for another kind of credential; no base class is needed.

**Signature status:** implemented (#92), `runtime_checkable`:

```text
class OutboundCredential(Protocol):
    async def headers(self) -> Mapping[str, str]: ...
    def satisfies(self, scheme: a2a.types.SecurityScheme) -> bool: ...
```

| Method | Returns | Meaning and constraints |
| --- | --- | --- |
| `headers()` | `Mapping[str, str]` | The HTTP headers carrying the credential. Called before every request, so a credential can refresh an expired token. Never log the values. |
| `satisfies(scheme)` | `bool` | Whether these headers meet one native security scheme from the remote's Agent Card. Used only by the startup check; never guess `True` for schemes the credential cannot meet. |

```python
from collections.abc import Mapping

from a2a.types import SecurityScheme


class BasicCredential:
    def __init__(self, encoded: str) -> None:
        self._encoded = encoded

    async def headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Basic {self._encoded}"}

    def satisfies(self, scheme: SecurityScheme) -> bool:
        return (
            scheme.WhichOneof("scheme") == "http_auth_security_scheme"
            and scheme.http_auth_security_scheme.scheme.lower() == "basic"
        )
```

`satisfies` was added to the issue's proposed `headers()`-only protocol so the
decided startup check can match custom credentials without guessing.

**Related:** [`Service`](#service).

## `APIKeyCredential`

**Purpose / decision:** send an API key in one header, as
[`APIKeyAuth`](#apikeyauth) expects. Implemented (#92).

**Signature status:** `APIKeyCredential(key: str, *, header: str = "X-API-Key")`.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `key` | `str` | Required, non-empty | The key. Load it from the environment or a secret store; `repr` never shows it. |
| `header` | `str` | `"X-API-Key"` | The header that carries it. |

```python
import os

from agentframe import APIKeyCredential, Service

research = Service(credential=APIKeyCredential(os.environ["RESEARCH_API_KEY"]))
```

**Behavior / failures:** satisfies an `apiKey` scheme located in a header with
the same name (case-insensitive); not query or cookie keys. An empty key or
header raises `DefinitionError`.

**Related:** [`Service`](#service), [`APIKeyAuth`](#apikeyauth).

## `EntraAppCredential`

**Purpose / decision:** send a Microsoft Entra token for the calling agent's
own app identity (client credentials), for example to call a Foundry A2A agent
or an [`EntraIDAuth`](#entraidauth) agent. Implemented (#92).

**Signature status:** `EntraAppCredential(credential, *, scope: str)`.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `credential` | Any `azure.core` async token credential | Required | For example `azure.identity.aio.ClientSecretCredential`, `CertificateCredential` or `ManagedIdentityCredential`. Typed structurally (only `get_token(scope)` is used), so AgentFrame's core install does not need `azure-identity`. It stays yours: AgentFrame never closes it. |
| `scope` | `str` | Required, non-empty | The remote's scope: `api://<client-id>/.default` for an `EntraIDAuth` agent, `https://ai.azure.com/.default` for Foundry agents. |

```python
from azure.identity.aio import ManagedIdentityCredential

from agentframe import EntraAppCredential, Service

identity = ManagedIdentityCredential()
review = Service(
    credential=EntraAppCredential(identity, scope="api://review/.default")
)
```

**Behavior / failures:** every request calls `credential.get_token(scope)` and
sends `Authorization: Bearer <token>`; `azure-identity` credentials cache the
token until it nears expiry. Satisfies OAuth 2.0, OpenID Connect and HTTP
`bearer` card schemes (`EntraIDAuth` cards declare OpenID Connect). A failed
token request fails the call with `AuthenticationError`. The token is never
logged, put in errors or exposed on `RunContext`. This is not on-behalf-of: the
remote sees the calling app, not the user; see [`OnBehalfOf`](#onbehalfof).

For an `EntraIDAuth` target, grant the calling app an app role on the target's
app registration (application permission, admin consent), map that role with
`role_permissions`, and use `scope="api://<target-client-id>/.default"`; the
caller's `oid` is the calling app's service principal. For Foundry agents, give
the calling identity a role that may invoke agent endpoints (see
[`FoundryAgentRegistry`](discovery.md#foundryagentregistry) roles).

**Related:** [`Service`](#service), [`EntraIDAuth`](#entraidauth).
