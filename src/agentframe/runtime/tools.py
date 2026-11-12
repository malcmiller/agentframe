"""Running the tool calls a model asks for during ``ctx.generate``."""

import inspect
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agentframe.authoring.services import ServiceScope
from agentframe.errors import OutputValidationError, describe_validation_error
from agentframe.models.base import Message, ModelResponse, ToolCall, ToolSpec

if TYPE_CHECKING:
    # Imported only for type checking: both modules import this one.
    from agentframe.authoring.context import RunContext
    from agentframe.authoring.definitions import ToolDefinition


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


async def run_tool_call(
    call: ToolCall,
    tools: Mapping[str, "ToolDefinition"],
    services: ServiceScope,
    ctx: "RunContext",
) -> str:
    """Run one tool call the model asked for and return its result as JSON text.

    A call naming a tool that was not offered, or with invalid arguments,
    runs nothing: its result tells the model what was wrong, using field
    locations only. Exceptions raised by the tool propagate unchanged and
    fail the run; their text is never sent to the model.

    Args:
        call: The model's tool call.
        tools: The tools offered to the model, by name.
        services: The run's services, for ``Inject[T]`` parameters.
        ctx: The run's context, passed to tools that take ``ctx``.

    Raises:
        OutputValidationError: The tool returned a value that does not match
            its return annotation.
        DefinitionError: A service the tool needs is not registered.
    """
    tool = tools.get(call.name)
    if tool is None:
        return error_result("Unknown tool. Call only the tools you were offered.")
    try:
        arguments = parse_arguments(tool, call.arguments)
    except ValidationError as error:
        return error_result(f"Invalid arguments: {describe_validation_error(error)}")
    # Action authorization (#62) and execution-policy checks (#78) go here,
    # after argument validation and before the tool runs.
    injected = await services.resolve(tool.injected)
    if tool.takes_context:
        injected["ctx"] = ctx
    value = await call_tool(tool, arguments | injected)
    return serialize_result(tool, value)


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
