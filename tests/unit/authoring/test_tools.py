"""@tool definitions.

Rejection tests define tools only to trigger definition errors, so those
functions are never called.
"""

from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any, Literal, Protocol

import pytest
from pydantic import BaseModel, Field

from agentframe import DefinitionError, Inject, RunContext, ToolDefinition, tool
from agentframe.authoring.definitions import tool_definition


class OrderStatus(BaseModel):
    order_id: str
    status: Literal["open", "shipped", "cancelled"]


class OrderStore(Protocol):
    async def status(self, order_id: str) -> OrderStatus: ...


@tool(description="Look up an order's status.", idempotent=True)
async def lookup_order(
    order_id: str, orders: Inject[OrderStore], ctx: RunContext
) -> OrderStatus:
    return await orders.status(order_id)


@tool(side_effects=True)
def to_fahrenheit(celsius: float, precision: int = 1) -> float:
    """Convert Celsius to Fahrenheit."""
    return round(celsius * 9 / 5 + 32, precision)


class Conversions:
    @tool()
    @staticmethod
    def to_celsius(fahrenheit: float) -> float:
        """Convert Fahrenheit to Celsius."""
        return (fahrenheit - 32) * 5 / 9


def test_decorated_functions_stay_directly_callable() -> None:
    assert to_fahrenheit(100) == 212.0
    assert Conversions.to_celsius(212) == 100.0


async def test_async_tools_stay_awaitable() -> None:
    class FakeStore:
        async def status(self, order_id: str) -> OrderStatus:
            return OrderStatus(order_id=order_id, status="open")

    result = await lookup_order("o-1", FakeStore(), ctx=None)  # type: ignore[arg-type]

    assert result == OrderStatus(order_id="o-1", status="open")


def test_definition_describes_the_tool() -> None:
    definition = tool_definition(lookup_order)

    assert isinstance(definition, ToolDefinition)
    assert definition.name == "lookup_order"
    assert definition.description == "Look up an order's status."
    assert definition.function is lookup_order
    assert definition.takes_context
    assert definition.argument_names == ("order_id",)
    assert definition.injected == {"orders": OrderStore}


def test_description_defaults_to_docstring() -> None:
    definition = tool_definition(to_fahrenheit)

    assert definition.description == "Convert Celsius to Fahrenheit."
    assert not definition.takes_context
    assert tool_definition(Conversions.to_celsius).name == "to_celsius"


def test_flags_default_to_false_and_may_both_be_true() -> None:
    @tool(description="Plain.")
    def plain() -> None: ...

    @tool(description="Both.", side_effects=True, idempotent=True)
    def both() -> None: ...

    plain_definition = tool_definition(plain)
    both_definition = tool_definition(both)

    assert not plain_definition.side_effects
    assert not plain_definition.idempotent
    assert both_definition.side_effects
    assert both_definition.idempotent
    assert tool_definition(to_fahrenheit).side_effects
    assert tool_definition(lookup_order).idempotent


def test_schema_matches_the_business_parameters_only() -> None:
    class LookupArguments(BaseModel):
        order_id: str

    class ConvertArguments(BaseModel):
        celsius: float
        precision: int = Field(default=1)

    lookup_schema = tool_definition(lookup_order).json_schema()
    convert_schema = tool_definition(to_fahrenheit).json_schema()

    assert (
        lookup_schema["properties"] == LookupArguments.model_json_schema()["properties"]
    )
    assert lookup_schema["required"] == ["order_id"]
    assert "orders" not in lookup_schema["properties"]
    assert "ctx" not in lookup_schema["properties"]
    assert (
        convert_schema["properties"]
        == ConvertArguments.model_json_schema()["properties"]
    )
    assert convert_schema["required"] == ["celsius"]


def test_arguments_and_return_values_are_validated() -> None:
    definition = tool_definition(lookup_order)

    arguments = definition.arguments_adapter.validate_json('{"order_id": "o-1"}')
    assert arguments.order_id == "o-1"
    with pytest.raises(ValueError):
        definition.arguments_adapter.validate_json('{"order_id": "o-1", "x": 1}')
    with pytest.raises(ValueError):
        definition.return_adapter.validate_python({"order_id": "o-1", "status": "?"})


def test_undecorated_function_has_no_definition() -> None:
    with pytest.raises(DefinitionError, match="not decorated with @tool"):
        tool_definition(len)


def _define(function: Callable[..., Any], description: str | None = "A tool.") -> None:
    tool(description=description)(function)


def test_rejects_missing_description_and_docstring() -> None:
    def undocumented(text: str) -> str:
        return text

    with pytest.raises(DefinitionError, match="'undocumented' needs a description"):
        _define(undocumented, description=None)


def test_rejects_missing_annotations() -> None:
    def untyped(text) -> str:  # type: ignore[no-untyped-def]
        return str(text)

    def no_return(text: str):  # type: ignore[no-untyped-def]
        return text

    with pytest.raises(DefinitionError, match="parameter 'text' needs a type"):
        _define(untyped)
    with pytest.raises(DefinitionError, match="needs a return annotation"):
        _define(no_return)


def test_rejects_variadic_and_positional_only_parameters() -> None:
    def variadic(*texts: str) -> str:
        return "".join(texts)

    def keywords(**options: str) -> str:
        return ""

    def positional(text: str, /) -> str:
        return text

    for function in (variadic, keywords, positional):
        with pytest.raises(DefinitionError, match="must be a named parameter"):
            _define(function)


def test_rejects_names_providers_cannot_accept() -> None:
    def café(text: str) -> str:
        return text

    long_name = "x" * 65
    too_long = _renamed(café, long_name)

    def lookup(text: str) -> str:
        return text

    # A trailing newline must not slip past the name check.
    newline = _renamed(lookup, "lookup\n")

    for function in (café, too_long, newline):
        with pytest.raises(DefinitionError, match="name must be 1-64 ASCII"):
            _define(function)


def _renamed(function: Callable[..., Any], name: str) -> Callable[..., Any]:
    function.__name__ = name
    return function


def test_rejects_lambdas_methods_and_other_callables() -> None:
    class Shop:
        def lookup(self, order_id: str) -> str:
            return order_id

    with pytest.raises(DefinitionError, match="cannot decorate a lambda"):
        _define(lambda text: text)
    with pytest.raises(DefinitionError, match="methods cannot be tools"):
        _define(Shop.lookup)
    with pytest.raises(DefinitionError, match="module-level or static function"):
        _define(Shop().lookup)


def test_rejects_staticmethod_below_tool_order() -> None:
    with pytest.raises(DefinitionError, match="put @tool above @staticmethod"):

        class Shop:
            @staticmethod
            @tool(description="Look up.")
            def lookup(order_id: str) -> str:
                return order_id


def test_rejects_bad_context_and_reserved_names() -> None:
    def wrong_ctx(ctx: str) -> str:
        return ctx

    def reserved(principal: str) -> str:
        return principal

    with pytest.raises(DefinitionError, match="'ctx' must be annotated RunContext"):
        _define(wrong_ctx)
    with pytest.raises(DefinitionError, match="'principal' is a reserved name"):
        _define(reserved)


def test_rejects_unsupported_annotations() -> None:
    class Opaque:
        pass

    def opaque(value: Opaque) -> str:
        return ""

    with pytest.raises(DefinitionError, match="unsupported type annotation"):
        _define(opaque)


def test_rejects_generators() -> None:
    def numbers(count: int) -> Iterator[int]:
        yield from range(count)

    async def stream(count: int) -> AsyncIterator[int]:
        for number in range(count):
            yield number

    for function in (numbers, stream):
        with pytest.raises(DefinitionError, match="generators cannot be tools"):
            _define(function)
