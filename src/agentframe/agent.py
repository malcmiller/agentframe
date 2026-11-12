"""The Agent base class."""

from typing import ClassVar

from agentframe.definitions import AgentDefinition
from agentframe.errors import DefinitionError
from agentframe.limits import RunLimits

DEFINITION_ATTRIBUTE = "__agentframe_definition__"


class Agent:
    """Base class for agents: declare skills with @skill, describe it with @agent.

    Attributes:
        limits: Default execution limits for this agent's skills. A skill's own
            ``limits`` take precedence.
    """

    limits: ClassVar[RunLimits] = RunLimits()

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
