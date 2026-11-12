"""The Agent base class."""

from typing import ClassVar

from agentframe.authoring.definitions import AgentDefinition
from agentframe.errors import DefinitionError
from agentframe.models.base import Model, ModelSettings
from agentframe.runtime.limits import RunLimits

DEFINITION_ATTRIBUTE = "__agentframe_definition__"


class Agent:
    """Base class for agents: declare skills with @skill, describe it with @agent.

    Args:
        model: The model ``ctx.generate`` calls. Model-free agents omit it.
            The application owns the model and closes it.

    Attributes:
        limits: Default execution limits for this agent's skills. Fields it
            sets override the library defaults; fields a skill or run sets
            override it in turn.
        instructions: Trusted instructions sent as the first ``system`` message
            of every ``ctx.generate`` call. Never put untrusted content here.
        model_settings: Default settings for this agent's model calls, merged
            field by field like ``limits``.
        model: The model passed to the constructor, or ``None``.
    """

    limits: ClassVar[RunLimits] = RunLimits()
    instructions: ClassVar[str | None] = None
    model_settings: ClassVar[ModelSettings] = ModelSettings()
    # A class default keeps subclasses that do not call super().__init__() working.
    model: Model | None = None

    def __init__(self, *, model: Model | None = None) -> None:
        self.model = model

    @classmethod
    def definition(cls) -> AgentDefinition:
        """Return the validated definition created by ``@agent``.

        Raises:
            DefinitionError: If this exact class was not decorated with ``@agent``.
        """
        # Look only at this class so an undecorated subclass cannot silently
        # reuse its parent's definition and skills.
        definition = cls.__dict__.get(DEFINITION_ATTRIBUTE)
        if not isinstance(definition, AgentDefinition):
            raise DefinitionError(
                f"Agent '{cls.__name__}' is not decorated with @agent"
            )
        return definition
