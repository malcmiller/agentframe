"""An assistant that understands free text: the built-in router picks the skill.

Free-text A2A messages go to ``ModelRouter``, which asks the model which skill
the caller wants and what its arguments are, or asks the caller a question
when something is missing. Callers that name a skill with the skill-call
extension skip the router and its model calls. The model comes from the same
environment variables as ``examples/assistant.py``::

    AGENTFRAME_MODEL_PROVIDER=ollama AGENTFRAME_OLLAMA_MODEL=llama3.2:3b \\
        agentframe run examples.routed_assistant:create_app

Routing takes two model calls per message, charged to the run like the
skill's own calls, so they count toward the agent's limits.
"""

import os

from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    HostConfig,
    Model,
    ModelRouter,
    ModelSettings,
    RunContext,
    RunLimits,
    agent,
    skill,
)
from examples.assistant import create_model


class Temperature(BaseModel):
    """A temperature in degrees Celsius and Fahrenheit."""

    celsius: float
    fahrenheit: float


@agent(version="1.0.0")
class RoutedAssistant(Agent):
    """Answers general questions and converts temperatures."""

    instructions = "Answer in one short sentence."
    # Applies to the routing calls too; reasoning models count their reasoning.
    model_settings = ModelSettings(max_output_tokens=512)
    # Foundry does not report a deployment's context window, so cap the input.
    limits = RunLimits(max_input_tokens=4000)

    @skill("answer", tags=["questions"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer a general knowledge question briefly."""
        return await ctx.generate(question)

    @skill("to_fahrenheit", tags=["units"])
    async def to_fahrenheit(self, celsius: float, ctx: RunContext) -> Temperature:
        """Convert a temperature from degrees Celsius to degrees Fahrenheit."""
        return Temperature(celsius=celsius, fahrenheit=celsius * 9 / 5 + 32)


def create_app(model: Model | None = None) -> AgentApp:
    """Serve the assistant over A2A, routing free text with its own model.

    Args:
        model: The model for both the skills and the router. Defaults to
            ``create_model()``; it lives as long as the server process.
    """
    model = model or create_model()
    host = HostConfig(
        public_url=os.environ.get("ASSISTANT_PUBLIC_URL", "http://127.0.0.1:8000"),
        # Development placeholder: anonymous access, for a localhost server
        # only. Production hosts need real authentication and authorization.
        auth=AllowAnonymous(),
    )
    return AgentApp(
        agent=RoutedAssistant(model=model),
        host=host,
        # Opt-in: without a router, free text is refused. Sharing the agent's
        # model is explicit; any model with structured output works.
        router=ModelRouter(model),
    )
