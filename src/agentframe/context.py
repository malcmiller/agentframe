"""Per-run context passed to skills."""

from dataclasses import dataclass, field

from agentframe.generation import GenerationState
from agentframe.principal import Principal


@dataclass(frozen=True)
class RunContext:
    """Per-run information passed to every skill. Never shared between runs.

    Attributes:
        invocation_id: Library-generated identifier of this run.
        principal: Verified caller identity, or ``None`` when none was supplied.
        deadline: Event-loop time (``loop.time()``) when the run times out.
    """

    invocation_id: str
    principal: Principal | None
    deadline: float
    # Private per-run model state; the context is frozen but its accounting
    # must change as the run makes model calls.
    _generation: GenerationState = field(repr=False, compare=False)

    async def generate(self, text: str, *, instructions: str | None = None) -> str:
        """Generate text with the agent's model.

        The agent's ``instructions`` and then ``instructions`` are sent as
        trusted ``system`` messages; ``text`` is sent as a separate ``user``
        message and never merged into them. Keeping them separate does not
        guarantee resistance to prompt injection.

        Args:
            text: Untrusted content for the model, such as caller input.
            instructions: Trusted instructions for this call only. Never put
                untrusted content here.

        Returns:
            The model's complete answer.

        Raises:
            UnsupportedCapabilityError: The agent has no model configured, or
                the model lacks something the request needs.
            ModelRefusalError: The model refused, or a content filter blocked
                its output.
            IncompleteOutputError: The output was cut short at the token
                limit or was empty. Partial text is never returned.
            ProviderError: The model provider call failed.
            LimitExceededError: The run's timeout expired during the call.
        """
        return await self._generation.generate_text(text, instructions, self.deadline)
