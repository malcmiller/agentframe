"""The README example, kept working as written."""

from agentframe import Agent, Inject, RunContext, Runtime, Services, agent, skill


class Greeter:
    def greet(self, name: str) -> str:
        return f"Hello, {name}!"


@agent(version="0.1.0")
class Echo(Agent):
    """Returns supplied text or a greeting."""

    @skill(id="echo", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Return the text unchanged."""
        return text

    @skill(id="greet")
    async def greet(self, name: str, greeter: Inject[Greeter], ctx: RunContext) -> str:
        """Greet someone by name."""
        return greeter.greet(name)


async def test_readme_example() -> None:
    services = Services()
    services.add(Greeter, Greeter())
    runtime = Runtime(services=services)

    value = await runtime.invoke(Echo(), skill="echo", arguments={"text": "hi"})
    result = await runtime.run(Echo(), skill="greet", arguments={"name": "Ada"})

    assert value == "hi"
    assert result.value == "Hello, Ada!"
