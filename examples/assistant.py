"""One agent, served over A2A with local Llama or a Foundry GPT deployment.

The agent class never names a model host. ``create_app()`` picks the model
from environment variables with ``model_from_env()``, so switching hosts is
configuration only::

    # Local Llama through Ollama (the model tag must already be installed)
    AGENTFRAME_MODEL_PROVIDER=ollama AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \\
        agentframe run examples.assistant:create_app

    # A Foundry GPT deployment, signed in with DefaultAzureCredential (az login
    # locally, managed identity in Azure). Foundry does not report a
    # deployment's context window, so configure it with the model.
    AGENTFRAME_MODEL_PROVIDER=foundry \\
        AGENTFRAME_FOUNDRY_ENDPOINT=https://<resource>.openai.azure.com/openai/v1/ \\
        AGENTFRAME_FOUNDRY_DEPLOYMENT=<deployment> \\
        AGENTFRAME_MODEL_CONTEXT_WINDOW=128000 \\
        agentframe run examples.assistant:create_app

Each provider is imported only when selected, so an install with just the
``ollama`` (or just the ``foundry``) extra can run the example. A2A clients
apply their own request timeout (5 seconds by default in the official
``a2a-sdk`` client); model calls, and the first Entra token fetch, can take
longer, so callers may need to raise it.
"""

import os

from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    HostConfig,
    Model,
    ModelSettings,
    RunContext,
    agent,
    model_from_env,
    skill,
)


class Summary(BaseModel):
    """A one-sentence summary of some text and its key points."""

    text: str
    key_points: list[str]


@agent(version="1.0.0")
class Assistant(Agent):
    """Answers questions, summarizes text and counts words."""

    instructions = "Answer in one short sentence."
    model_settings = ModelSettings(max_output_tokens=512)

    @skill(tags=["questions"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer a question briefly."""
        return await ctx.generate(question)

    @skill(tags=["text"])
    async def summarize(self, text: str, ctx: RunContext) -> Summary:
        """Summarize some text as a sentence and its key points."""
        return await ctx.generate(
            text,
            # Ollama sees only the schema, so describe the shape here too.
            instructions="Reply with JSON: a one-sentence text and 1-3 key_points.",
            output=Summary,
        )

    @skill(tags=["text"])
    async def count_words(self, text: str, ctx: RunContext) -> int:
        """Count the words in some text, without calling the model."""
        return len(text.split())


def create_app(model: Model | None = None) -> AgentApp:
    """Serve the assistant over A2A.

    Args:
        model: The model to use. Defaults to ``model_from_env()``; the
            model lives as long as the server process.
    """
    host = HostConfig(
        public_url=os.environ.get("ASSISTANT_PUBLIC_URL", "http://127.0.0.1:8000"),
        # Development placeholder: anonymous access, for a localhost server
        # only. Production hosts need real authentication and authorization.
        auth=AllowAnonymous(),
    )
    return AgentApp(agent=Assistant(model=model or model_from_env()), host=host)
