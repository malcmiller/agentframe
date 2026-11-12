import pytest
from pydantic import BaseModel

from agentframe import (
    Agent,
    Inject,
    InputValidationError,
    OutputValidationError,
    RunContext,
    Runtime,
    Services,
    agent,
    skill,
)

SECRET = "s3cret-value-never-shown"


class Clock:
    def now(self) -> str:
        return "noon"


class Address(BaseModel):
    city: str
    zip_code: str


@agent(version="1.0.0")
class Forms(Agent):
    """Handles typed forms."""

    @skill(id="double")
    async def double(self, number: int, ctx: RunContext) -> int:
        """Double a number."""
        return number * 2

    @skill(id="city")
    async def city(self, address: Address, ctx: RunContext) -> str:
        """Return the city of an address."""
        return address.city

    @skill(id="greet")
    async def greet(self, name: str, ctx: RunContext, punctuation: str = "!") -> str:
        """Greet with optional punctuation."""
        return f"Hello, {name}{punctuation}"

    @skill(id="time")
    async def time(self, clock: Inject[Clock], ctx: RunContext) -> str:
        """Tell the time."""
        return clock.now()

    @skill(id="broken")
    async def broken(self, ctx: RunContext) -> int:
        """Return the wrong type."""
        return SECRET  # type: ignore[return-value]


@pytest.fixture
def runtime() -> Runtime:
    services = Services()
    services.add(Clock, Clock())
    return Runtime(services=services)


async def test_arguments_use_lax_conversion(runtime: Runtime) -> None:
    assert (
        await runtime.invoke(Forms(), skill="double", arguments={"number": "5"}) == 10
    )


async def test_dict_arguments_become_models(runtime: Runtime) -> None:
    address = {"city": "Lagos", "zip_code": "100001"}

    value = await runtime.invoke(Forms(), skill="city", arguments={"address": address})

    assert value == "Lagos"


async def test_parameter_defaults_apply(runtime: Runtime) -> None:
    value = await runtime.invoke(Forms(), skill="greet", arguments={"name": "Ada"})

    assert value == "Hello, Ada!"


async def test_unknown_argument_is_rejected(runtime: Runtime) -> None:
    with pytest.raises(
        InputValidationError, match="factor: Extra inputs are not permitted"
    ):
        await runtime.invoke(
            Forms(), skill="double", arguments={"number": 1, "factor": 3}
        )


async def test_missing_argument_is_rejected(runtime: Runtime) -> None:
    with pytest.raises(InputValidationError, match="number: Field required"):
        await runtime.invoke(Forms(), skill="double")


@pytest.mark.parametrize("reserved", ["ctx", "principal", "clock"])
async def test_reserved_and_injected_names_are_unknown_arguments(
    runtime: Runtime, reserved: str
) -> None:
    with pytest.raises(InputValidationError, match=reserved):
        await runtime.invoke(Forms(), skill="time", arguments={reserved: "x"})


async def test_input_error_does_not_echo_submitted_values(runtime: Runtime) -> None:
    with pytest.raises(InputValidationError) as caught:
        await runtime.invoke(Forms(), skill="double", arguments={"number": SECRET})

    assert "number" in caught.value.message
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None


async def test_wrong_return_type_is_rejected(runtime: Runtime) -> None:
    with pytest.raises(OutputValidationError) as caught:
        await runtime.invoke(Forms(), skill="broken")

    assert SECRET not in str(caught.value)
