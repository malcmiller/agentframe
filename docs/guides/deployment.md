# Deploying to production

This guide takes one agent from `agentframe run` on a laptop to a production
container on Azure Container Apps, with notes for any Kubernetes cluster. It
is guidance only: AgentFrame builds the ASGI app and its `agentframe run`
command serves it, but it does not build images, provision infrastructure or
host anything. Your platform does that.

The cloud commands below are **illustrative**: they show which setting goes
where, not a tested script. Check them against your platform's current
documentation, and replace every `<placeholder>`.

## The production sample

[`examples/production.py`](../../examples/production.py) serves the
[assistant](../../examples/assistant.py) agent, unchanged, with production
host settings. Unlike the development examples it never calls
[`dev_settings()`](../api/hosting.md#dev_settings) and has no development
defaults: every setting comes from an environment variable, and a missing
one stops the process before it serves anything (`DefinitionError`).

| Variable | Meaning |
| --- | --- |
| `ASSISTANT_PUBLIC_URL` | The `https://` origin callers use, advertised on the Agent Card. |
| `ASSISTANT_ENTRA_TENANT_ID` | Your Microsoft Entra tenant ID. |
| `ASSISTANT_ENTRA_AUDIENCE` | The client ID of the agent's API app registration. |
| `AGENTFRAME_MODEL_PROVIDER`, `AGENTFRAME_FOUNDRY_ENDPOINT`, `AGENTFRAME_FOUNDRY_DEPLOYMENT`, `AGENTFRAME_MODEL_CONTEXT_WINDOW` | The model, read by [`model_from_env()`](../api/models.md); see [`examples/assistant.py`](../../examples/assistant.py). |
| `AZURE_CLIENT_ID` | Optional: the client ID of a user-assigned managed identity, for `DefaultAzureCredential`. |

Its factory is short (comments trimmed; `model` and `auth` let tests pass
stand-ins):

```python
def create_app(
    model: Model | None = None, auth: Authenticator | None = None
) -> AgentApp:
    host = HostConfig(
        public_url=required(PUBLIC_URL_VARIABLE),
        auth=auth or entra_auth(),
        limits_ceiling=RunLimits(timeout_seconds=60, max_total_tokens=20_000),
        max_concurrent_runs=16,
        rate_limit=RateLimit(requests=60, per_seconds=60),
        shutdown_grace_seconds=20,
    )
    tracing = TracingConfig()
    if model is not None:
        return AgentApp(agent=Assistant(model=model), host=host, tracing=tracing)
    model = model_from_env()
    return AgentApp(
        agent=Assistant(model=model), host=host, tracing=tracing, resources=[model]
    )
```

None of it is platform specific; the rest of this guide explains each setting
and what the platform must do around it. Install
`agentframe-py[server,foundry,entra,otel]` to run it.

## Packaging

Run one agent per container. Build your project as a wheel that depends on
`agentframe-py` with the extras it needs, and install it in an image that runs
as a non-root user. [`agentframe compose`](development.md#containers)
writes such a `Dockerfile` for development; for production add the command:

```dockerfile
FROM python:3.13-slim@sha256:<digest>
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY dist/*.whl /tmp/wheels/
RUN python -m pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels
USER 10001:10001
EXPOSE 8000
CMD ["agentframe", "run", "myagent.app:create_app", "--host", "0.0.0.0", \
     "--port", "8000", "--log-level", "info", "--log-format", "json"]
```

- Use the exec form of `CMD`, so `agentframe run` is the container's main
  process and receives the platform's `SIGTERM` directly (see
  [shutdown](#graceful-shutdown)).
- `--host 0.0.0.0` listens on every interface inside the container only; the
  platform decides what is reachable from outside.
- `--log-format json` writes one JSON object per log record to standard
  error, with each run's `invocation_id`; Container Apps and most Kubernetes
  log pipelines collect it as is. See
  [JSON log records](../api/hosting.md#json-log-records).
- Never copy secrets, `.env` files or development keys into the image.

## TLS termination and `public_url`

Serve callers over HTTPS only. On Container Apps the ingress terminates TLS
and forwards plain HTTP to the container's target port (8000 above); on
Kubernetes an ingress controller or gateway usually does the same. The
container itself does not need a certificate.

`HostConfig.public_url` is the address **callers** use, not the address the
server binds to. It is advertised on the Agent Card, and A2A clients send
their requests to it, so set it to the external `https://` origin: the
app's ingress FQDN (`https://<app>.<environment-domain>`) or your custom
domain. Behind TLS termination the server sees plain HTTP, but the card
still advertises `public_url`.

`AgentApp` logs a warning at construction when a non-loopback `public_url`
uses plain `http://` with credential-bearing authentication, or when it
accepts anonymous callers on a non-loopback address.

Agents that only other agents call can use internal ingress, so they are
reachable only inside the Container Apps environment or cluster. That limits
who can reach them; it is not authentication, which every agent still needs.

## Authentication and managed identity

The sample authenticates callers with [`EntraIDAuth`](../api/authorization.md#entraidauth):
callers send `Authorization: Bearer <token>` for the agent's API, and any
other request gets HTTP 401 before it reaches the agent. The one-time app
registration setup is in the [`EntraIDAuth` reference](../api/authorization.md#entraidauth).
Two production notes:

- Any application in the tenant can request a token for an API unless the
  registration says otherwise. Either turn on **Assignment required** on the
  API's enterprise application, so only assigned users and apps get tokens,
  or give skills [`requires_permissions`](authorization.md) and map app roles
  to permissions with `HostConfig(role_permissions=...)`. Doing both is
  better.
- `EntraIDAuth` fetches the tenant's signing keys on the first request; when
  they cannot be fetched, requests fail closed with 401 and a warning.

**The agent's own identity** should be a managed identity, so the container
holds no long-lived secret:

- **Model calls.** `model_from_env()` signs in to Foundry with
  `DefaultAzureCredential`, which uses the container's managed identity. Give
  that identity a role that may call the deployment, such as
  **Cognitive Services OpenAI User** on the resource. For a user-assigned
  identity, set `AZURE_CLIENT_ID` to its client ID.
- **Calls to other agents as the agent itself.** Use
  [`EntraAppCredential`](../api/authorization.md#entraappcredential) with an
  `azure.identity.aio.ManagedIdentityCredential`, inside a
  [`Service`](../api/authorization.md#service) in `remotes=`, and list the credential in
  `resources=` so the app closes it.
- **Calls to other agents on behalf of the signed-in user.**
  [`OnBehalfOf`](../api/authorization.md#onbehalfof) remotes need
  `EntraIDAuth(client_id=..., client_credential=...)`. Instead of a client
  secret, add the managed identity as a **federated credential** on the
  agent's app registration and pass a function that returns a managed
  identity token for the token-exchange audience:

```python
import os

from azure.identity import ManagedIdentityCredential

from agentframe import EntraIDAuth

# Illustrative: the managed identity is a federated credential of the
# agent's app registration, so no client secret exists anywhere.
identity = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])


def managed_identity_assertion() -> str:
    return identity.get_token("api://AzureADTokenExchange/.default").token


auth = EntraIDAuth(
    tenant_id=os.environ["ASSISTANT_ENTRA_TENANT_ID"],
    audience=os.environ["ASSISTANT_ENTRA_AUDIENCE"],
    client_id=os.environ["ASSISTANT_ENTRA_AUDIENCE"],  # must equal audience
    client_credential=managed_identity_assertion,
)
```

The incoming token is never forwarded; each run exchanges it for a token to
the remote agent. See the [authorization guide](authorization.md) for how
permissions travel between agents.

## Secrets

With managed identity and Entra, the sample has no secrets at all: its
environment holds only IDs and URLs. When an agent does need one (an API key
for `APIKeyAuth`, a Redis or database connection string, a certificate for
on-behalf-of exchanges):

- keep it in a secret store such as Azure Key Vault, never in the image,
  source control or a checked-in configuration file;
- on Container Apps, declare a secret that references Key Vault through the
  app's managed identity and expose it as an environment variable with
  `secretref:`; on Kubernetes, use a `Secret` or the Secrets Store CSI driver;
- prefer Entra authentication to backing services where they support it, so
  there is no password to rotate.

AgentFrame never logs credentials, tokens or `Authorization` headers, and
error messages from `model_from_env()` name a variable but never its value.
Environment variables are still readable by anyone who can inspect the
container, so restrict who can.

## Health and readiness probes

Every `AgentApp` serves two endpoints that need no credentials and reveal
nothing about the agent:

| Endpoint | Answers 200 when | Use as |
| --- | --- | --- |
| `/health` | The process is serving HTTP. | Liveness probe. |
| `/ready` | Startup checks passed (resources opened, the model's context window known, `Remote` agents found) and shutdown has not started. | Readiness probe, and startup probe. |

A replica that fails its startup checks never becomes ready, so the platform
keeps traffic on the old revision. Point probes at the container port, not
the public URL.

## Graceful shutdown

When the platform stops a replica (a new revision, scale-in, node
maintenance), it stops routing new traffic to it, sends `SIGTERM`, waits a
termination grace period, then kills the process. On `SIGTERM`,
`agentframe run`:

1. stops accepting new connections (Uvicorn closes its listening socket).
   On connections that are already open, `/ready` stops answering 200 and
   new messages are refused with HTTP 503 and `Retry-After: 1`, while task
   reads and cancels keep working;
2. waits up to `HostConfig.shutdown_grace_seconds` (default 30) for running
   runs to finish and their final task states to be stored;
3. cancels what is still running, waiting up to 5 more seconds for those
   tasks to end `CANCELED`; then
4. closes the app's `resources`.

So shutdown can take `shutdown_grace_seconds` plus 5 seconds plus the time
to close resources. Keep that **below** the platform's grace period.
Container Apps and Kubernetes both default to 30 seconds, so the sample
uses 20. To give long runs more time, raise the platform's
`terminationGracePeriodSeconds` first, then `shutdown_grace_seconds`. Keep
`RunLimits.timeout_seconds` in mind: a run that may take 60 seconds cannot
finish in a 20-second grace period.

Requests the platform still routes to the replica in the moment after
`SIGTERM` get a connection error, and callers that poll a task on this
replica can no longer reach it; A2A callers should retry, and with
[shared stores](#shared-task-and-deduplication-stores) a retry reaches the
same task on another replica. A run that is cancelled is not resumed
elsewhere. Its caller sees a cancelled task and may retry.

## Scaling with `max_concurrent_runs`

Each replica runs at most `HostConfig.max_concurrent_runs` messages at once
(default 32; the sample uses 16). Another message is refused immediately
with HTTP 503 and `Retry-After: 1`; nothing is queued, so a replica never
builds up a backlog it cannot finish. A task waiting for input holds no slot.

- Size the value from a replica's CPU and memory and from what the model
  provider allows; runs mostly wait on the model, so it can be well above
  the CPU count.
- Scale out before replicas refuse work: set the platform's concurrency
  target (the Container Apps HTTP scale rule's concurrent requests, or a
  Kubernetes autoscaler) somewhat **below** `max_concurrent_runs`.
- Keep at least one replica running if callers should not wait for a cold
  start, and because a stopped replica loses its in-memory tasks.
- `HostConfig(rate_limit=RateLimit(...))` counts each caller's requests per
  replica. For one limit across replicas, rate-limit at a gateway such as
  Azure API Management as well.
- `limits_ceiling` caps every run's time and tokens, whatever the agent or a
  caller asks for, so one caller cannot hold a slot for long.

More than one replica needs the shared stores below.

## Shared task and deduplication stores

By default each replica keeps its A2A tasks and its message-deduplication
claims in memory. That is right for one replica, but with several:

- a caller that polls `GetTask` (AgentFrame's own `Remote` calls do) may
  reach a replica that never saw the task and get "task not found";
- a retried message that reaches another replica is not recognized as a
  repeat and runs again;
- every task is lost when its replica stops.

Give every replica the same **task store** and the same **dedup store**:
`A2AConfig(task_store=...)` takes any `a2a-sdk` `TaskStore`, such as the SDK's
own `DatabaseTaskStore` (install `a2a-sdk[postgresql]` yourself), and
[`RedisMessageDedupStore`](../api/hosting.md#redismessagededupstore)
(`agentframe-py[redis]`, Redis 7.0 or later) shares deduplication claims.
Pass the store clients in `resources=` so the app closes them on shutdown.

```python
import os

from a2a.server.tasks import DatabaseTaskStore
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentframe import A2AConfig, Agent, AgentApp, HostConfig, RedisMessageDedupStore


class DisposeEngine:
    """Closes the database engine's connections when the server stops."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def aclose(self) -> None:
        await self.engine.dispose()


def shared_app(agent: Agent, host: HostConfig) -> AgentApp:
    # Illustrative: both connection strings are secrets from your platform.
    engine = create_async_engine(os.environ["ASSISTANT_DATABASE_URL"])
    redis = Redis.from_url(os.environ["ASSISTANT_REDIS_URL"])
    return AgentApp(
        agent=agent,
        host=host,
        a2a=A2AConfig(
            task_store=DatabaseTaskStore(engine),
            dedup_store=RedisMessageDedupStore(redis, key_prefix="assistant:dedup:"),
        ),
        resources=[DisposeEngine(engine), redis],
    )
```

A persistent task store keeps tasks readable across replicas and restarts.
It does not make work durable: a run still belongs to the replica running it,
and a replica that stops cancels its runs. Deduplication is still not
exactly-once; see [`RedisMessageDedupStore`](../api/hosting.md#redismessagededupstore)
and [retries and duplicate requests](limits.md#retries-and-duplicate-requests).
The default in-memory task store's retention settings do not apply to a
store you pass; it owns its own retention.

## Traces, metrics and logs

The sample passes `tracing=TracingConfig()`, which records spans and metrics
through OpenTelemetry's global providers. AgentFrame never configures
exporters; the application does, in one of these ways:

- run the server under `opentelemetry-instrument` (from
  `opentelemetry-distro` with `opentelemetry-exporter-otlp`), which reads the
  standard `OTEL_*` environment variables such as `OTEL_SERVICE_NAME` and
  `OTEL_EXPORTER_OTLP_ENDPOINT`:
  `CMD ["opentelemetry-instrument", "agentframe", "run", ...]`;
- or build providers yourself in `create_app()` and pass them:
  `TracingConfig(tracer_provider=..., meter_provider=...)`.

On Container Apps, the environment's managed
[OpenTelemetry agent](https://learn.microsoft.com/azure/container-apps/opentelemetry-agents)
can receive OTLP and forward it to Azure Monitor or another destination; it
sets the OTLP endpoint variables for the app. Spans never contain messages,
arguments, results or credentials; see the
[tracing guide](tracing.md) for every span and metric.

Logs are separate from tracing: `--log-level info --log-format json` writes
each run's start and end with its invocation IDs to standard error, which the
platform collects.

## Azure Container Apps walkthrough

**Illustrative commands** (Azure CLI with the `containerapp` extension). They
assume a resource group, a Container Apps environment, a registry holding
your image, a user-assigned managed identity with pull access to the
registry and access to the Foundry deployment, and the API app registration
described above.

```shell
# Create the app with external HTTPS ingress to port 8000, one to five
# replicas, and a new replica for every 10 concurrent requests (below
# max_concurrent_runs=16).
az containerapp create \
  --name assistant --resource-group <rg> --environment <environment> \
  --image <registry>.azurecr.io/assistant:<tag> \
  --user-assigned <identity-resource-id> \
  --registry-server <registry>.azurecr.io --registry-identity <identity-resource-id> \
  --ingress external --target-port 8000 \
  --min-replicas 1 --max-replicas 5 \
  --scale-rule-name http --scale-rule-type http --scale-rule-http-concurrency 10 \
  --env-vars \
    ASSISTANT_ENTRA_TENANT_ID=<tenant-id> \
    ASSISTANT_ENTRA_AUDIENCE=<api-client-id> \
    AZURE_CLIENT_ID=<identity-client-id> \
    AGENTFRAME_MODEL_PROVIDER=foundry \
    AGENTFRAME_FOUNDRY_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/ \
    AGENTFRAME_FOUNDRY_DEPLOYMENT=<deployment> \
    AGENTFRAME_MODEL_CONTEXT_WINDOW=128000 \
    ASSISTANT_PUBLIC_URL=https://<custom-domain-or-placeholder>

# Once the FQDN is known (or use your custom domain), set the advertised URL.
az containerapp show --name assistant --resource-group <rg> \
  --query properties.configuration.ingress.fqdn
az containerapp update --name assistant --resource-group <rg> \
  --set-env-vars ASSISTANT_PUBLIC_URL=https://<fqdn>
```

Probes and the termination grace period are part of the app's template; set
them in the YAML or Bicep you deploy with:

```yaml
# Excerpt of properties.template (illustrative).
terminationGracePeriodSeconds: 30
containers:
  - name: assistant
    probes:
      - type: Liveness
        httpGet: {path: /health, port: 8000}
      - type: Readiness
        httpGet: {path: /ready, port: 8000}
      - type: Startup
        httpGet: {path: /ready, port: 8000}
```

A secret from Key Vault, read through the managed identity, for example a
Redis connection string for the [shared stores](#shared-task-and-deduplication-stores):

```shell
az containerapp secret set --name assistant --resource-group <rg> \
  --secrets "redis-url=keyvaultref:<key-vault-secret-uri>,identityref:<identity-resource-id>"
az containerapp update --name assistant --resource-group <rg> \
  --set-env-vars ASSISTANT_REDIS_URL=secretref:redis-url
```

## Kubernetes notes

The same container runs on any Kubernetes cluster. Map each setting to the
cluster's own objects:

- **Workload:** a `Deployment` with the container above, a `Service` to port
  8000, and an `Ingress` or Gateway with a TLS certificate for `public_url`.
- **Probes:** `livenessProbe` on `/health`; `readinessProbe` and
  `startupProbe` on `/ready`.
- **Shutdown:** `terminationGracePeriodSeconds` on the pod (default 30) above
  `shutdown_grace_seconds` plus 5 seconds plus closing time. Endpoint
  removal can lag `SIGTERM` by a moment, and the server stops accepting
  connections at `SIGTERM`; a short `preStop` sleep keeps it serving while
  removal catches up. The sleep counts against the same grace period, so
  raise `terminationGracePeriodSeconds` by as much.
- **Identity:** on AKS, use Microsoft Entra Workload ID: label the pod
  `azure.workload.identity/use: "true"` and run it under a service account
  annotated with the identity's client ID. `DefaultAzureCredential` then
  signs in with it; for on-behalf-of, build the assertion function above
  with `azure.identity.WorkloadIdentityCredential` instead of
  `ManagedIdentityCredential`.
- **Secrets:** Kubernetes `Secret`s, or the Secrets Store CSI driver with
  Key Vault, exposed as environment variables.
- **Scaling:** a HorizontalPodAutoscaler on CPU is a rough proxy because runs
  mostly wait on the model; scaling on request concurrency (for example with
  KEDA) matches `max_concurrent_runs` better. Add a `PodDisruptionBudget` so
  voluntary disruptions leave replicas serving.
- **Several replicas** need the [shared stores](#shared-task-and-deduplication-stores).

## Checklist

- [ ] `public_url` is the external `https://` origin; TLS terminates at the
      platform.
- [ ] Real authentication (`EntraIDAuth`), assignment or permissions limit
      who may call; no `AllowAnonymous`, development keys or `dev_settings()`.
- [ ] The agent's own identity is a managed identity; no secret in the image
      or source control.
- [ ] Probes: liveness `/health`, readiness and startup `/ready`.
- [ ] `shutdown_grace_seconds` plus 5 seconds, plus any `preStop` sleep,
      below the platform's grace period.
- [ ] `max_concurrent_runs` sized per replica; the scale target below it;
      `limits_ceiling` set.
- [ ] One replica, or a shared task store and dedup store.
- [ ] `tracing=TracingConfig()` with exporters configured by the application,
      and `--log-format json`.
