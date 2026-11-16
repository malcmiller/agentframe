"""The assistant, configured for a production host such as Azure Container Apps.

Unlike the development examples, this factory never calls ``dev_settings()``
and has no development defaults: every setting comes from environment
variables that the platform supplies, and a missing one stops startup. The
agent is ``examples/assistant.py``'s, unchanged; only the hosting differs.
docs/guides/deployment.md walks through deploying it.

Install ``agentframe-py[server,foundry,entra,otel]``, then, with values for
your own tenant and Foundry resource::

    ASSISTANT_PUBLIC_URL=https://assistant.example.com \\
    ASSISTANT_ENTRA_TENANT_ID=<tenant-id> \\
    ASSISTANT_ENTRA_AUDIENCE=<api-client-id> \\
    AGENTFRAME_MODEL_PROVIDER=foundry \\
    AGENTFRAME_FOUNDRY_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/ \\
    AGENTFRAME_FOUNDRY_DEPLOYMENT=<deployment> \\
    AGENTFRAME_MODEL_CONTEXT_WINDOW=128000 \\
        agentframe run examples.production:create_app --host 0.0.0.0 \\
        --port 8000 --log-level info --log-format json

Callers send ``Authorization: Bearer <Entra access token>`` for the API whose
client ID is ``ASSISTANT_ENTRA_AUDIENCE``. The Foundry model signs in with
``DefaultAzureCredential``: the container's managed identity in Azure (set
``AZURE_CLIENT_ID`` for a user-assigned one). Spans and metrics go to
OpenTelemetry's global providers, which the process sets up, for example
with ``opentelemetry-instrument``; with none, they are discarded.
"""

import os

from agentframe import (
    AgentApp,
    Authenticator,
    DefinitionError,
    EntraIDAuth,
    HostConfig,
    Model,
    RateLimit,
    RunLimits,
    TracingConfig,
    model_from_env,
)
from examples.assistant import Assistant

PUBLIC_URL_VARIABLE = "ASSISTANT_PUBLIC_URL"
TENANT_ID_VARIABLE = "ASSISTANT_ENTRA_TENANT_ID"
AUDIENCE_VARIABLE = "ASSISTANT_ENTRA_AUDIENCE"


def create_app(
    model: Model | None = None, auth: Authenticator | None = None
) -> AgentApp:
    """Serve the assistant with production settings read from the environment.

    Args:
        model: The model to use, which stays the caller's to close. Defaults
            to ``model_from_env()``, which the app closes when the server stops.
        auth: How callers are authenticated. Defaults to ``EntraIDAuth`` for
            the tenant and API named by the environment.

    Raises:
        DefinitionError: A required environment variable is unset.
    """
    host = HostConfig(
        public_url=required(PUBLIC_URL_VARIABLE),
        auth=auth or entra_auth(),
        # Hard caps on every run, whatever the agent or a caller asks for.
        limits_ceiling=RunLimits(timeout_seconds=60, max_total_tokens=20_000),
        # Size each replica for this many runs at once; scale out for more.
        max_concurrent_runs=16,
        # Each caller may send 60 messages a minute to each replica.
        rate_limit=RateLimit(requests=60, per_seconds=60),
        # With up to 5 seconds to cancel leftover runs, this stays below the
        # platform's 30-second termination grace period and leaves time to
        # close the model.
        shutdown_grace_seconds=20,
    )
    tracing = TracingConfig()
    if model is not None:
        return AgentApp(agent=Assistant(model=model), host=host, tracing=tracing)
    model = model_from_env()
    return AgentApp(
        agent=Assistant(model=model), host=host, tracing=tracing, resources=[model]
    )


def entra_auth() -> EntraIDAuth:
    """Accept Microsoft Entra access tokens issued for this agent's API."""
    return EntraIDAuth(
        tenant_id=required(TENANT_ID_VARIABLE),
        audience=required(AUDIENCE_VARIABLE),
    )


def required(name: str) -> str:
    """Return environment variable ``name``, refusing to start without it."""
    value = os.environ.get(name)
    if not value:
        raise DefinitionError(f"Set the {name} environment variable")
    return value
