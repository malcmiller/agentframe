"""Send free-text A2A messages to one named skill, without a model."""

from dataclasses import dataclass

from a2a.helpers import get_message_text
from a2a.types import Message

from agentframe.authoring.definitions import AgentDefinition, text_skill_argument
from agentframe.errors import InputValidationError
from agentframe.runtime.routing import SkillCall


@dataclass(frozen=True)
class DefaultSkill:
    """The skill that receives free text when the app has no router.

    Attributes:
        skill: ID of the skill.
        argument: Its only business parameter, a ``str``, which receives
            the message's text.
    """

    skill: str
    argument: str

    @classmethod
    def for_agent(cls, agent: AgentDefinition, skill_id: str) -> "DefaultSkill":
        """Check that ``skill_id`` can take plain text and return its default skill.

        Raises:
            DefinitionError: The agent has no such skill, or its only business
                parameter is not a ``str``.
        """
        return cls(skill_id, text_skill_argument(agent, skill_id))

    def call(self, message: Message) -> SkillCall:
        """Return the call that passes the message's text to the skill.

        The text parts are joined with newlines; other parts are ignored.
        The runtime still validates the argument and checks permissions,
        authorization and policy before the skill runs.

        Raises:
            InputValidationError: The message has no text.
        """
        text = get_message_text(message)
        if not text.strip():
            raise InputValidationError("The message has no text for the skill")
        return SkillCall(self.skill, {self.argument: text})
