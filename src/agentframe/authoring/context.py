"""Per-run context passed to skills."""

from dataclasses import dataclass, field
from typing import overload

from pydantic import BaseModel

from agentframe.runtime.generation import GenerationState
from agentframe.security.principal import Principal


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

    @overload
    async def generate(self, text: str, *, instructions: str | None = None) -> str: ...

    @overload
    async def generate[T: BaseModel](
        self, text: str, *, instructions: str | None = None, output: type[T]
    ) -> T: ...

    async def generate(
        self,
        text: str,
        *,
        instructions: str | None = None,
        output: type[BaseModel] | None = None,
    ) -> str | BaseModel:
        """Generate text, or a validated ``output`` instance, with the agent's model.

        The agent's ``instructions`` and then ``instructions`` are sent as
        trusted ``system`` messages; ``text`` is sent as a separate ``user``
        message and never merged into them. Keeping them separate does not
        guarantee resistance to prompt injection.

        Args:
            text: Untrusted content for the model, such as caller input.
            instructions: Trusted instructions for this call only. Never put
                untrusted content here.
            output: A Pydantic ``BaseModel`` subclass. When given, the model
                is asked for JSON matching its schema and the reply is
                validated into an instance. Invalid output is never repaired
                or re-requested.

        Returns:
            The model's complete answer, or an ``output`` instance when
            ``output`` is given.

        Raises:
            DefinitionError: ``output`` is not a ``BaseModel`` subclass, or is
                a ``RootModel``.
            UnsupportedCapabilityError: The agent has no model configured, or
                the model lacks something the request needs, such as
                structured output. Raised before anything is sent.
            OutputValidationError: The structured reply is not valid JSON or
                does not match ``output``.
            ModelRefusalError: The model refused, or a content filter blocked
                its output.
            IncompleteOutputError: The output was cut short at the token
                limit or was empty. Partial text or objects are never returned.
            ProviderError: The model provider call failed.
            LimitExceededError: The call does not fit the run's limits, or the
                run's timeout expired during the call.
        """
        if output is None:
            return await self._generation.generate_text(
                text, instructions, self.deadline
            )
        return await self._generation.generate_output(
            text, instructions, output, self.deadline
        )
