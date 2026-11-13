"""Build a native A2A Agent Card from an agent definition and host settings.

Card generation is a pure function: it reads only the definition and the host
configuration, does no I/O and returns the same card for the same inputs.
"""

from typing import Any

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol

from agentframe.authoring.definitions import AgentDefinition, SkillDefinition
from agentframe.errors import DefinitionError
from agentframe.hosting.host import HostConfig
from agentframe.hosting.skill_call import (
    SKILL_CALL_EXTENSION_URI as SKILL_CALL_EXTENSION_URI,
)
from agentframe.security.authentication import Authenticator, DeclaresCardSecurity

A2A_PATH = "/a2a"
# Callers send free text or JSON arguments, and receive text or JSON results.
MEDIA_TYPES = ["text/plain", "application/json"]


def build_agent_card(definition: AgentDefinition, host: HostConfig) -> AgentCard:
    """Build the public Agent Card for an agent served by ``host``.

    The card advertises only what the host implements: the JSON-RPC binding for
    A2A 1.0, the skill-call extension, no streaming or push notifications, and
    the security scheme of the configured authenticator. Instructions, injected
    services, ``ctx`` and other internals never appear on it.

    Args:
        definition: The agent's validated definition.
        host: Where and how the agent is served.

    Returns:
        The native A2A Agent Card.

    Raises:
        DefinitionError: The host's authenticator cannot describe its security
            scheme (it does not implement ``DeclaresCardSecurity``).
    """
    skills = list(definition.skills.values())
    security_schemes, security_requirements = _security(host.auth)
    return AgentCard(
        name=definition.name,
        description=definition.description,
        version=definition.version,
        supported_interfaces=[
            AgentInterface(
                url=host.public_url.rstrip("/") + A2A_PATH,
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version=PROTOCOL_VERSION_1_0,
            )
        ],
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            extensions=[_skill_call_extension(skills)],
        ),
        security_schemes=security_schemes,
        security_requirements=security_requirements,
        default_input_modes=MEDIA_TYPES,
        default_output_modes=MEDIA_TYPES,
        skills=[_agent_skill(skill) for skill in skills],
    )


def skill_arguments_schema(skill: SkillDefinition) -> dict[str, Any]:
    """Return the JSON Schema of the arguments a caller sends to ``skill``.

    The schema covers business arguments only; ``ctx`` and ``Inject[T]``
    parameters are never part of it.

    Args:
        skill: The skill definition.

    Returns:
        A JSON Schema object describing the arguments.
    """
    schema = skill.arguments_adapter.json_schema()
    # The generated title is derived from the Python method name, an internal.
    schema.pop("title", None)
    return schema


def _agent_skill(skill: SkillDefinition) -> AgentSkill:
    return AgentSkill(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        tags=skill.tags,
    )


def _skill_call_extension(skills: list[SkillDefinition]) -> AgentExtension:
    return AgentExtension(
        uri=SKILL_CALL_EXTENSION_URI,
        description=(
            "Call a skill by id with JSON arguments. The 'skills' parameter maps "
            "each skill id to the JSON Schema of its arguments."
        ),
        required=False,
        params={
            "skills": {skill.id: skill_arguments_schema(skill) for skill in skills}
        },
    )


def _security(
    auth: Authenticator,
) -> tuple[dict[str, SecurityScheme], list[SecurityRequirement]]:
    """Return the card's security schemes and the requirement to satisfy them all.

    Raises:
        DefinitionError: ``auth`` does not implement ``DeclaresCardSecurity``.
    """
    if not isinstance(auth, DeclaresCardSecurity):
        raise DefinitionError(
            f"Authenticator {type(auth).__name__} must implement "
            "card_security() so the Agent Card can describe how callers "
            "authenticate; without it the card would advertise open access."
        )
    schemes = auth.card_security()
    if not schemes:
        return {}, []
    requirement = SecurityRequirement(schemes={name: StringList() for name in schemes})
    return schemes, [requirement]
