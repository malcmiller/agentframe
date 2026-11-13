"""The ``@agent``, ``@skill`` and ``@tool`` decorators."""

from collections.abc import Callable, Iterable
from typing import Any

from agentframe.authoring.agent import DEFINITION_ATTRIBUTE, Agent
from agentframe.authoring.definitions import (
    TOOL_ATTRIBUTE,
    AgentDefinition,
    SkillDefinition,
    SkillMetadata,
    build_skill_definition,
    build_tool_definition,
    class_docstring,
)
from agentframe.errors import DefinitionError
from agentframe.models.base import ModelSettings
from agentframe.runtime.limits import RunLimits

_SKILL_ATTRIBUTE = "_skill_metadata"


# noinspection PyShadowingBuiltins
def skill[F: Callable[..., Any]](
    id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    tags: Iterable[str] = (),
    requires_permissions: Iterable[str] = (),
    limits: RunLimits | None = None,
    model_settings: ModelSettings | None = None,
) -> Callable[[F], F]:
    """Mark an async ``Agent`` method as an advertised skill.

    The method is inspected later by ``@agent``, once its class exists.

    Args:
        id: Stable skill identifier that callers select.
        name: Display name. Defaults to the title-cased method name.
        description: What the skill does. Defaults to the method docstring.
        tags: Descriptive discovery tags. At least one non-blank tag is
            required (A2A 1.0); ``@agent`` rejects a skill without one.
        requires_permissions: Permissions the caller must all hold.
        limits: Execution limits for this skill. Each field it sets overrides
            the agent's value; unset fields are inherited.
        model_settings: Model settings for this skill. Each field it sets
            overrides the agent's value; unset fields are inherited.

    Returns:
        A decorator that records the metadata and returns the same function.

    Raises:
        DefinitionError: If ``id`` is empty, or ``tags`` or
            ``requires_permissions`` is a single string.
    """
    if not id:
        raise DefinitionError("Skill id must not be empty")
    if isinstance(tags, str) or isinstance(requires_permissions, str):
        raise DefinitionError(
            f"Skill '{id}': tags and requires_permissions must be lists of strings"
        )
    metadata = SkillMetadata(
        id=id,
        name=name,
        description=description,
        tags=tuple(tags),
        requires_permissions=frozenset(requires_permissions),
        limits=limits,
        model_settings=model_settings,
    )

    def decorator(func: F) -> F:
        setattr(func, _SKILL_ATTRIBUTE, metadata)
        return func

    return decorator


def agent[A: type[Agent]](
    version: str, *, name: str | None = None, description: str | None = None
) -> Callable[[A], A]:
    """Describe an ``Agent`` subclass and validate its skills.

    Args:
        version: Agent version string, for example ``"0.1.0"``.
        name: Agent name. Defaults to the class name.
        description: What the agent does. Defaults to the class docstring.

    Returns:
        A decorator that stores the definition and returns the same class.

    Raises:
        DefinitionError: If the class or any of its skills is invalid.
    """

    def decorator(cls: A) -> A:
        if not (isinstance(cls, type) and issubclass(cls, Agent)):
            raise DefinitionError("@agent can only decorate Agent subclasses")
        agent_name = name or cls.__name__
        agent_description = description or class_docstring(cls)
        if not agent_description:
            raise DefinitionError(
                f"Agent '{agent_name}' needs a description or docstring"
            )
        definition = AgentDefinition(
            name=agent_name,
            description=agent_description,
            version=version,
            skills=_collect_skills(agent_name, cls),
        )
        setattr(cls, DEFINITION_ATTRIBUTE, definition)
        return cls

    return decorator


def _collect_skills(agent_name: str, cls: type) -> dict[str, SkillDefinition]:
    skill_methods = _skill_methods(agent_name, cls)
    if not skill_methods:
        raise DefinitionError(f"Agent '{agent_name}' has no skills")

    skills: dict[str, SkillDefinition] = {}
    for method in skill_methods.values():
        metadata: SkillMetadata = getattr(method, _SKILL_ATTRIBUTE)
        if metadata.id in skills:
            raise DefinitionError(
                f"Agent '{agent_name}': skill id '{metadata.id}' is used by both "
                f"'{skills[metadata.id].method_name}' and '{method.__name__}'"
            )
        skills[metadata.id] = build_skill_definition(agent_name, method, metadata)
    return skills


def _skill_methods(agent_name: str, cls: type) -> dict[str, Callable[..., Any]]:
    """Return skill methods by name, walking from the base class to ``cls``."""
    methods: dict[str, Callable[..., Any]] = {}
    for klass in reversed(cls.__mro__):
        for attribute_name, value in vars(klass).items():
            if hasattr(value, _SKILL_ATTRIBUTE):
                methods[attribute_name] = value
            elif attribute_name in methods:
                raise DefinitionError(
                    f"Agent '{agent_name}': '{klass.__name__}.{attribute_name}' "
                    "overrides a skill without @skill"
                )
    return methods


def tool[F: Callable[..., Any]](
    *,
    description: str | None = None,
    side_effects: bool = False,
    idempotent: bool = False,
    requires_permissions: Iterable[str] = (),
) -> Callable[[F], F]:
    """Mark a typed function as a tool a model may call.

    Always call it with parentheses: ``@tool()`` or ``@tool(description=...)``.
    The function is validated immediately and stays directly callable. Sync
    functions run inline on the event loop, so use ``async def`` for any I/O.
    Decorating grants nothing: the tool must still be registered on an agent
    (``Agent(tools=[...])``) and selected by ``ctx.generate(..., tools=[...])``.

    Args:
        description: What the tool does, shown to the model. Defaults to the
            function docstring; one of them is required.
        side_effects: Whether a call has consequential effects, such as
            cancelling an order.
        idempotent: Whether repeating a call with the same arguments has no
            further effect. Not a retry grant: tools are never auto-retried.
        requires_permissions: Permissions the caller must all hold. A tool
            the caller lacks any of is not offered to the model and fails the
            run if the model calls it anyway.

    Returns:
        A decorator that attaches a ``ToolDefinition`` and returns the same
        function (or ``staticmethod``).

    Raises:
        DefinitionError: If the function breaks a tool rule: it is a lambda or
            method, is a generator, has an invalid name, unannotated,
            ``*args``/``**kwargs`` or positional-only parameters, an
            unsupported type, or no description; or if
            ``requires_permissions`` is a single string.
    """
    if isinstance(requires_permissions, str):
        raise DefinitionError("Tool requires_permissions must be a list of strings")
    permissions = frozenset(requires_permissions)

    def decorator(func: F) -> F:
        definition = build_tool_definition(
            func,
            description=description,
            side_effects=side_effects,
            idempotent=idempotent,
            requires_permissions=permissions,
        )
        setattr(definition.function, TOOL_ATTRIBUTE, definition)
        return func

    return decorator
