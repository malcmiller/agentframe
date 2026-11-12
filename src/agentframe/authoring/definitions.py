"""Validated agent, skill and tool definitions built from decorated code."""

import inspect
import re
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

# Model providers accept tool names of this shape (OpenAI's function-name rule).
_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


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
class ToolDefinition:
    """A validated model-callable tool: its description, flags and call contract.

    Built by ``@tool`` when a function is decorated. Building one grants
    nothing: a tool is only offered to a model once registered on an agent
    and selected by a ``ctx.generate`` call.
    """

    name: str
    description: str
    side_effects: bool
    idempotent: bool
    function: Callable[..., Any]
    takes_context: bool
    argument_names: tuple[str, ...]
    injected: dict[str, object]
    arguments_adapter: TypeAdapter[Any]
    return_adapter: TypeAdapter[Any]

    def json_schema(self) -> dict[str, Any]:
        """Return the JSON Schema of the business arguments a model supplies.

        ``ctx`` and ``Inject[T]`` parameters never appear in it.
        """
        return self.arguments_adapter.json_schema()


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
    # Drop ``self``: skills are always instance methods.
    parameters = _named_parameters(
        where, list(inspect.signature(method).parameters.values())[1:]
    )
    hints = _type_hints(where, method)
    _check_annotations(where, parameters, hints)
    if "ctx" not in {parameter.name for parameter in parameters}:
        raise DefinitionError(f"{where} needs a 'ctx: RunContext' parameter")
    _check_tags(where, metadata.tags)
    arguments, injected = _split_parameters(where, parameters, hints)

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


def build_tool_definition(
    target: object,
    *,
    description: str | None,
    side_effects: bool,
    idempotent: bool,
) -> ToolDefinition:
    """Inspect a module-level or static function and build its tool definition.

    Args:
        target: The decorated function, or a ``staticmethod`` wrapping one.
        description: Explicit description; defaults to the docstring.
        side_effects: Whether calling the tool has consequential effects.
        idempotent: Whether repeating a call has no further effect.

    Raises:
        DefinitionError: If the function breaks a tool rule.
    """
    function = _tool_function(target)
    name = function.__name__
    where = f"Tool '{name}'"
    if not _TOOL_NAME_PATTERN.match(name):
        raise DefinitionError(
            f"{where}: name must be 1-64 ASCII letters, digits, '_' or '-'"
        )

    parameters = _named_parameters(
        where, list(inspect.signature(function).parameters.values())
    )
    hints = _type_hints(where, function)
    _check_annotations(where, parameters, hints)
    arguments, injected = _split_parameters(where, parameters, hints)

    tool_description = description or _docstring(function)
    if not tool_description:
        raise DefinitionError(f"{where} needs a description or docstring")

    try:
        arguments_adapter = _arguments_adapter(name, arguments, hints)
        return_adapter = TypeAdapter(hints["return"])
    except PydanticSchemaGenerationError:
        raise DefinitionError(f"{where} uses an unsupported type annotation") from None

    return ToolDefinition(
        name=name,
        description=tool_description,
        side_effects=side_effects,
        idempotent=idempotent,
        function=function,
        takes_context="ctx" in {parameter.name for parameter in parameters},
        argument_names=tuple(parameter.name for parameter in arguments),
        injected=injected,
        arguments_adapter=arguments_adapter,
        return_adapter=return_adapter,
    )


def _tool_function(target: object) -> Callable[..., Any]:
    """Return the plain function behind ``target``; no lambdas or methods."""
    is_static = isinstance(target, staticmethod)
    function = target.__func__ if isinstance(target, staticmethod) else target
    if not inspect.isfunction(function):
        raise DefinitionError(
            f"@tool needs a module-level or static function, got {function!r}"
        )
    if function.__name__ == "<lambda>":
        raise DefinitionError("@tool cannot decorate a lambda; use a named function")
    # A function written directly in a class body has a qualname "Class.name";
    # one nested in a function has "outer.<locals>.name".
    enclosing = function.__qualname__.split(".")[:-1]
    if not is_static and enclosing and enclosing[-1] != "<locals>":
        raise DefinitionError(
            f"Tool '{function.__name__}': methods cannot be tools; use a "
            "module-level function, or put @tool above @staticmethod"
        )
    return function


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


def _named_parameters(
    where: str, parameters: list[inspect.Parameter]
) -> list[inspect.Parameter]:
    """Return ``parameters``, rejecting ``*args``, ``**kwargs`` and positional-only."""
    for parameter in parameters:
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise DefinitionError(
                f"{where}: parameter '{parameter.name}' must be a named parameter"
            )
    return parameters


def _split_parameters(
    where: str, parameters: list[inspect.Parameter], hints: dict[str, Any]
) -> tuple[list[inspect.Parameter], dict[str, object]]:
    """Separate business arguments from ``Inject[T]`` services.

    ``ctx`` belongs to neither group: it is supplied by the runtime.

    Returns:
        The business parameters, and the injected service types by parameter name.
    """
    arguments: list[inspect.Parameter] = []
    injected: dict[str, object] = {}
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
    return arguments, injected


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
