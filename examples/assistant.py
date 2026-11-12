"""One agent, served over A2A with local Llama or a Foundry GPT deployment.

The agent class never names a model host. ``create_app()`` picks the model
from environment variables, so switching hosts is configuration only::

    # Local Llama through Ollama (the model tag must already be installed)
    AGENTFRAME_MODEL_PROVIDER=ollama AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \\
        agentframe run examples.assistant:create_app

    # A Foundry GPT deployment, signed in with Microsoft Entra ID (az login)
    AGENTFRAME_MODEL_PROVIDER=foundry \\
        AGENTFRAME_FOUNDRY_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/ \\
        AGENTFRAME_FOUNDRY_DEPLOYMENT=<deployment> \\
        agentframe run examples.assistant:create_app

Each provider is imported only when selected, so an install with just the
``ollama`` (or just the ``foundry``) extra can run the example. A2A clients
apply their own request timeout (5 seconds by default in the official
``a2a-sdk`` client); model calls, and the first Entra token fetch, can take
longer, so callers may need to raise it.
"""

import os

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    HostConfig,
    Model,
    ModelSettings,
    RunContext,
    RunLimits,
    agent,
    skill,
)


@agent(version="1.0.0")
class Assistant(Agent):
    """Answers questions and counts words."""

    instructions = "Answer in one short sentence."
    model_settings = ModelSettings(max_output_tokens=512)
    # Foundry does not report a deployment's context window, so cap the input.
    limits = RunLimits(max_input_tokens=4000)

    @skill("answer", tags=["questions"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer a question briefly."""
        return await ctx.generate(question)

    @skill("count_words", tags=["text"])
    async def count_words(self, text: str, ctx: RunContext) -> int:
        """Count the words in some text, without calling the model."""
        return len(text.split())


def create_model() -> Model:
    """Return the model that ``AGENTFRAME_MODEL_PROVIDER`` selects.

    Raises:
        KeyError: A required environment variable is not set.
        ValueError: The provider is neither ``ollama`` nor ``foundry``.
    """
    provider = os.environ["AGENTFRAME_MODEL_PROVIDER"]
    if provider == "ollama":
        from agentframe.models.ollama import DEFAULT_ENDPOINT, OllamaModel

        return OllamaModel(
            model=os.environ["AGENTFRAME_OLLAMA_MODEL"],
            endpoint=os.environ.get("AGENTFRAME_OLLAMA_ENDPOINT", DEFAULT_ENDPOINT),
            context_window=8192,
        )
    if provider == "foundry":
        from azure.identity.aio import AzureCliCredential

        from agentframe.models.foundry import FoundryModel

        # Local development signs in with `az login`; production can use
        # DefaultAzureCredential or managed identity. Like the model, the
        # credential lives as long as the server process; an application that
        # stops serving earlier closes both.
        return FoundryModel(
            endpoint=os.environ["AGENTFRAME_FOUNDRY_ENDPOINT"],
            deployment=os.environ["AGENTFRAME_FOUNDRY_DEPLOYMENT"],
            credential=AzureCliCredential(),
        )
    raise ValueError(f"Unknown AGENTFRAME_MODEL_PROVIDER {provider!r}")


def create_app(model: Model | None = None) -> AgentApp:
    """Serve the assistant over A2A.

    Args:
        model: The model to use. Defaults to ``create_model()``; the model
            lives as long as the server process.
    """
    host = HostConfig(
        public_url=os.environ.get("ASSISTANT_PUBLIC_URL", "http://127.0.0.1:8000"),
        # Development placeholder: anonymous access, for a localhost server
        # only. Production hosts need real authentication and authorization.
        auth=AllowAnonymous(),
    )
    return AgentApp(agent=Assistant(model=model or create_model()), host=host)
