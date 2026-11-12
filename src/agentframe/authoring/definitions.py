"""Validated agent and skill definitions built from decorated classes."""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_args, get_origin, get_type_hints

from pydantic import (
    ConfigDict,
    PydanticSchemaGenerationError,
    TypeAdapter,
    create_model,
)

from agentframe.authoring.context import RunContext
from agentframe.authoring.services import Inject
from agentframe.errors import DefinitionError
from agentframe.models.base import ModelSettings
from agentframe.runtime.limits import RunLimits

# Callers can never supply these as business arguments.
_RESERVED_ARGUMENT_NAMES = frozenset({"ctx", "principal"})


@dataclass(frozen=True)
class SkillMetadata:
    """Metadata recorded by ``@skill`` before the class is complete."""

    id: str
    name: str | None
    description: str | None
    tags: tuple[str, ...]
    requires_permissions: frozenset[str]
    limits: RunLimits | None
    model_settings: ModelSettings | None


@dataclass(frozen=True)
class SkillDefinition:
    """A validated skill: advertised metadata plus its call contract."""

    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    requires_permissions: frozenset[str]
    limits: RunLimits | None
    model_settings: ModelSettings | None
    method_name: str
    argument_names: tuple[str, ...]
    injected: dict[str, object]
    arguments_adapter: TypeAdapter[Any]
    return_adapter: TypeAdapter[Any]


@dataclass(frozen=True)
class AgentDefinition:
    """A validated agent: identity plus its skills keyed by skill id."""

    name: str
    description: str
    version: str
    skills: dict[str, SkillDefinition]


def build_skill_definition(
    agent_name: str, method: Callable[..., Any], metadata: SkillMetadata
) -> SkillDefinition:
    """Inspect a decorated skill method and build its definition.

    Raises:
        DefinitionError: If the method breaks a skill rule.
    """
    method_name = method.__name__
    where = f"Agent '{agent_name}': skill '{method_name}'"

    if not inspect.iscoroutinefunction(method):
        raise DefinitionError(f"{where} is not async")
    parameters = _business_and_ctx_parameters(where, method)
    hints = _type_hints(where, method)
    _check_annotations(where, parameters, hints)
    if "ctx" not in {parameter.name for parameter in parameters}:
        raise DefinitionError(f"{where} needs a 'ctx: RunContext' parameter")
    _check_tags(where, metadata.tags)

    injected: dict[str, object] = {}
    arguments: list[inspect.Parameter] = []
    for parameter in parameters:
        annotation = hints[parameter.name]
        if parameter.name == "ctx":
            if annotation is not RunContext:
                raise DefinitionError(f"{where}: 'ctx' must be annotated RunContext")
        elif get_origin(annotation) is Inject:
            injected[parameter.name] = get_args(annotation)[0]
        elif parameter.name in _RESERVED_ARGUMENT_NAMES:
            raise DefinitionError(f"{where}: '{parameter.name}' is a reserved name")
        else:
            arguments.append(parameter)

    description = metadata.description or _docstring(method)
    if not description:
        raise DefinitionError(f"{where} needs a description or docstring")

    try:
        arguments_adapter = _arguments_adapter(method_name, arguments, hints)
        return_adapter = TypeAdapter(hints["return"])
    except PydanticSchemaGenerationError:
        raise DefinitionError(f"{where} uses an unsupported type annotation") from None

    return SkillDefinition(
        id=metadata.id,
        name=metadata.name or method_name.replace("_", " ").title(),
        description=description,
        tags=metadata.tags,
        requires_permissions=metadata.requires_permissions,
        limits=metadata.limits,
        model_settings=metadata.model_settings,
        method_name=method_name,
        argument_names=tuple(parameter.name for parameter in arguments),
        injected=injected,
        arguments_adapter=arguments_adapter,
        return_adapter=return_adapter,
    )


def _check_tags(where: str, tags: tuple[str, ...]) -> None:
    """Require at least one non-blank tag.

    A2A 1.0 marks ``AgentSkill.tags`` REQUIRED, and required arrays must
    contain at least one element (spec section 5.7).
    """
    if not tags:
        raise DefinitionError(
            f"{where} needs at least one tag; A2A 1.0 requires skill tags"
        )
    if any(not tag.strip() for tag in tags):
        raise DefinitionError(f"{where} has a blank tag")


def _business_and_ctx_parameters(
    where: str, method: Callable[..., Any]
) -> list[inspect.Parameter]:
    """Return the parameters after ``self``, rejecting unsupported kinds."""
    parameters = list(inspect.signature(method).parameters.values())[1:]
    for parameter in parameters:
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise DefinitionError(
                f"{where}: parameter '{parameter.name}' must be a named parameter"
            )
    return parameters


def _type_hints(where: str, method: Callable[..., Any]) -> dict[str, Any]:
    try:
        return get_type_hints(method, include_extras=True)
    except (NameError, TypeError) as error:
        raise DefinitionError(
            f"{where}: cannot resolve annotations ({error})"
        ) from None


def _check_annotations(
    where: str, parameters: list[inspect.Parameter], hints: dict[str, Any]
) -> None:
    for parameter in parameters:
        if parameter.name not in hints:
            raise DefinitionError(f"{where}: parameter '{parameter.name}' needs a type")
    if "return" not in hints:
        raise DefinitionError(f"{where} needs a return annotation")


def _arguments_adapter(
    method_name: str, arguments: list[inspect.Parameter], hints: dict[str, Any]
) -> TypeAdapter[Any]:
    """Build a validator for the business arguments; unknown names are rejected."""
    fields: dict[str, Any] = {}
    for parameter in arguments:
        default = (
            ... if parameter.default is inspect.Parameter.empty else parameter.default
        )
        fields[parameter.name] = (hints[parameter.name], default)
    model = create_model(
        f"{method_name}_arguments",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return TypeAdapter(model)


def _docstring(target: object) -> str:
    # Read __doc__ directly: inspect.getdoc would inherit a parent's docstring.
    doc = getattr(target, "__doc__", None)
    return inspect.cleandoc(doc) if doc else ""


def class_docstring(cls: type) -> str:
    """Return the docstring written on ``cls`` itself, not inherited."""
    doc = cls.__dict__.get("__doc__")
    return inspect.cleandoc(doc) if doc else ""
