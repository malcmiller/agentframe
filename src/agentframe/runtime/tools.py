"""Running the tool calls a model asks for during ``ctx.generate``."""

import asyncio
import inspect
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agentframe.authoring.services import ServiceScope
from agentframe.errors import (
    OutputValidationError,
    PermissionDeniedError,
    PolicyUnavailableError,
    describe_validation_error,
)
from agentframe.models.base import Message, ModelResponse, ToolCall, ToolSpec
from agentframe.security.execution_policy import ExecutionPolicy, enforce_tool_checks
from agentframe.security.principal import Principal

if TYPE_CHECKING:
    # Imported only for type checking: these modules import this one.
    from agentframe.authoring.context import RunContext
    from agentframe.authoring.definitions import SkillDefinition, ToolDefinition
    from agentframe.security.authorization import ToolAuthorizationPolicy


def tool_spec(tool: "ToolDefinition") -> ToolSpec:
    """Return how the model sees ``tool``: its name, description and schema."""
    return ToolSpec(
        name=tool.name, description=tool.description, parameters=tool.json_schema()
    )


def assistant_message(response: ModelResponse) -> Message:
    """Return the model's tool-call response as a message for the next request.

    Keeps the response's ``provider_items`` so the adapter can replay them.
    """
    return Message(
        role="assistant",
        content=response.text,
        tool_calls=response.tool_calls,
        provider_items=response.provider_items,
    )


def tool_message(call: ToolCall, result: str) -> Message:
    """Return ``result`` as the ``tool`` message answering ``call``.

    Tool results are untrusted content, so they never go in a ``system`` message.
    """
    return Message(
        role="tool", content=result, tool_call_id=call.id, tool_name=call.name
    )


def is_permitted(tool: "ToolDefinition", principal: Principal | None) -> bool:
    """Return whether ``principal`` holds every permission ``tool`` requires."""
    if not tool.requires_permissions:
        return True
    return principal is not None and tool.requires_permissions <= principal.permissions


async def run_tool_call(
    call: ToolCall,
    tools: Mapping[str, "ToolDefinition"],
    services: ServiceScope,
    ctx: "RunContext",
    authorization: "ToolAuthorizationPolicy | None",
    policy: ExecutionPolicy | None,
    skill: "SkillDefinition | None",
) -> str:
    """Run one tool call the model asked for and return its result as JSON text.

    A call naming a tool that was not selected, or with invalid arguments,
    runs nothing: its result tells the model what was wrong, using field
    locations only. A call the caller is not authorized for, or that an
    execution policy tool check blocks, fails the run instead; the model is
    never told. Exceptions raised by the tool propagate
    unchanged and fail the run; their text is never sent to the model.

    Args:
        call: The model's tool call.
        tools: The tools selected for this generation, by name.
        services: The run's services, for ``Inject[T]`` parameters.
        ctx: The run's context, passed to tools that take ``ctx``.
        authorization: Application policy checked before every tool call.
            ``None`` means only ``requires_permissions`` are checked.
        policy: Execution policy whose tool checks run after authorization,
            or ``None`` for no checks.
        skill: The running skill, passed to the tool checks.

    Raises:
        PermissionDeniedError: The caller lacks a permission the tool
            requires, or the authorization policy or a tool check denied
            the call.
        PolicyUnavailableError: The authorization policy or a tool check
            raised an error or did not answer before the run's deadline.
        OutputValidationError: The tool returned a value that does not match
            its return annotation.
        DefinitionError: A service the tool needs is not registered.
    """
    tool = tools.get(call.name)
    if tool is None:
        return error_result("Unknown tool. Call only the tools you were offered.")
    # Checked before the arguments, so a model that names a tool it was not
    # offered learns nothing about that tool.
    if not is_permitted(tool, ctx.principal):
        raise PermissionDeniedError(
            f"Tool '{tool.name}' requires permissions the caller does not hold"
        )
    try:
        arguments = parse_arguments(tool, call.arguments)
    except ValidationError as error:
        return error_result(f"Invalid arguments: {describe_validation_error(error)}")
    if authorization is not None:
        await check_tool_policy(authorization, tool, arguments, ctx)
    if policy is not None and policy.tool_checks:
        # Tools are offered only inside a skill; routing never offers them.
        assert skill is not None
        await enforce_tool_checks(
            policy.tool_checks,
            skill,
            ctx.principal,
            tool.name,
            MappingProxyType(arguments),
            ctx.deadline,
        )
    injected = await services.resolve(tool.injected)
    if tool.takes_context:
        injected["ctx"] = ctx
    value = await call_tool(tool, arguments | injected)
    return serialize_result(tool, value)


async def check_tool_policy(
    authorization: "ToolAuthorizationPolicy",
    tool: "ToolDefinition",
    arguments: dict[str, Any],
    ctx: "RunContext",
) -> None:
    """Ask the tool authorization policy about one call, failing closed.

    Raises:
        PermissionDeniedError: The policy returned anything but ``True``.
        PolicyUnavailableError: The policy raised an error or did not answer
            before the run's deadline.
    """
    try:
        allowed = await authorization.check_tool(
            ctx.principal, tool, MappingProxyType(arguments)
        )
    except asyncio.CancelledError:
        # The run's own timeout cancels the policy at the deadline. Running out
        # of time while waiting for the policy is a policy outage, not
        # permission and not a skill timeout. A cancellation before the
        # deadline is a real one and propagates.
        if asyncio.get_running_loop().time() < ctx.deadline:
            raise
        raise PolicyUnavailableError(
            f"Tool authorization policy for tool '{tool.name}' did not answer "
            "before the run's deadline"
        ) from None
    except Exception as error:
        raise PolicyUnavailableError(
            f"Tool authorization policy for tool '{tool.name}' is unavailable"
        ) from error
    # Fail closed: only an explicit True allows the call, not any truthy value.
    # noinspection PySimplifyBooleanCheck
    if allowed is not True:
        # Deliberately generic so a denial never reveals whether the
        # requested resource exists.
        raise PermissionDeniedError(
            f"Tool '{tool.name}' was denied by the tool authorization policy"
        )


def parse_arguments(tool: "ToolDefinition", arguments: str) -> dict[str, Any]:
    """Validate the model's raw JSON ``arguments`` against ``tool``'s parameters.

    Raises:
        ValidationError: The JSON is malformed or does not match.
    """
    model = tool.arguments_adapter.validate_json(arguments)
    return {name: getattr(model, name) for name in tool.argument_names}


async def call_tool(tool: "ToolDefinition", arguments: dict[str, Any]) -> Any:
    """Call the tool's function, awaiting it if async. Sync tools run inline."""
    if inspect.iscoroutinefunction(tool.function):
        return await tool.function(**arguments)
    return tool.function(**arguments)


def serialize_result(tool: "ToolDefinition", value: Any) -> str:
    """Validate ``value`` against the tool's return annotation; return it as JSON.

    Raises:
        OutputValidationError: ``value`` does not match the annotation.
    """
    try:
        validated = tool.return_adapter.validate_python(value)
    except ValidationError as error:
        raise OutputValidationError(
            f"Tool '{tool.name}' returned an invalid value: "
            f"{describe_validation_error(error)}"
        ) from None
    return tool.return_adapter.dump_json(validated).decode()


def error_result(message: str) -> str:
    """Return a safe JSON tool result telling the model why its call did not run."""
    return json.dumps({"error": message})
