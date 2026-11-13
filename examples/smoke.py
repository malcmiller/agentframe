"""Serve every example and check each returns a complete, validated result.

Each example app is served in-process on a free localhost port and called with
the official A2A client, the way any A2A caller would. ``ScriptedModel`` stands
in for Ollama and Foundry, so nothing leaves localhost. From the directory that
contains ``examples/``, with AgentFrame installed with its ``server`` extra::

    python -m examples.smoke
"""

import asyncio
import json
import os
import socket
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from a2a.client import Client, ClientCallContext, ClientConfig, ClientFactory
from a2a.client.service_parameters import (
    ServiceParametersFactory,
    with_a2a_extensions,
)
from a2a.helpers import get_artifact_text, get_data_parts, new_data_part, new_text_part
from a2a.types import Message, Part, Role, SendMessageRequest, Task, TaskState

import agentframe
from agentframe import AgentApp, ModelCapabilities, ToolCall
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.testing import ScriptedModel
from examples import assistant, echo, orders, routed_assistant

STRUCTURED = ModelCapabilities(context_window=8192, structured_output=True)
TOOLS = ModelCapabilities(context_window=8192, tools=True)


@asynccontextmanager
async def serve(create_app: Callable[[str], AgentApp]) -> AsyncIterator[str]:
    """Serve an app on a free localhost port and yield its base URL.

    ``create_app`` receives the base URL, so the Agent Card advertises the
    address the server actually listens on.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(base_url), log_level="warning"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        yield base_url
    finally:
        server.should_exit = True
        await serving
        listener.close()


@asynccontextmanager
async def connect(base_url: str, api_key: str | None = None) -> AsyncIterator[Client]:
    """Connect with the official A2A client from the app's public Agent Card."""
    headers = {"X-API-Key": api_key} if api_key else {}
    async with httpx.AsyncClient(headers=headers) as http:
        factory = ClientFactory(ClientConfig(httpx_client=http))
        yield await factory.create_from_url(base_url)


async def send(client: Client, message: Message, *, skill_call: bool) -> Task:
    """Send one message and return the finished task."""
    context = None
    if skill_call:
        extensions = with_a2a_extensions([SKILL_CALL_EXTENSION_URI])
        context = ClientCallContext(
            service_parameters=ServiceParametersFactory.create([extensions])
        )
    request = SendMessageRequest(message=message)
    [response] = [r async for r in client.send_message(request, context=context)]
    return response.task


async def call_skill(client: Client, skill_id: str, arguments: dict[str, Any]) -> Task:
    """Name a skill with JSON arguments through the skill-call extension."""
    return await send(
        client,
        user_message(new_data_part(arguments), metadata={"agentframe.skill": skill_id}),
        skill_call=True,
    )


async def say(client: Client, text: str) -> Task:
    """Send free text, for a router to pick the skill."""
    return await send(client, user_message(new_text_part(text)), skill_call=False)


def user_message(part: Part, metadata: dict[str, Any] | None = None) -> Message:
    return Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[part],
        metadata=metadata,
    )


def text_result(task: Task) -> str:
    """Return a completed task's text result."""
    expect_completed(task)
    return get_artifact_text(task.artifacts[0])


def data_result(task: Task) -> Any:
    """Return a completed task's structured result, the data part's ``value``."""
    expect_completed(task)
    [data] = get_data_parts(task.artifacts[0].parts)
    return data["value"]


def expect_completed(task: Task) -> None:
    if task.status.state != TaskState.TASK_STATE_COMPLETED:
        raise AssertionError(
            f"Task {task.id} ended {TaskState.Name(task.status.state)}"
        )


def expect(actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"Expected {expected!r}, got {actual!r}")


async def check_echo() -> None:
    """The model-free agent, called with a development API key."""
    os.environ["ECHO_API_KEY"] = "smoke-echo-key"

    def create_app(base_url: str) -> AgentApp:
        os.environ["ECHO_PUBLIC_URL"] = base_url
        return echo.create_app()

    async with (
        serve(create_app) as base_url,
        connect(base_url, "smoke-echo-key") as a2a,
    ):
        expect(text_result(await call_skill(a2a, "echo", {"text": "hi"})), "hi")
        greeting = await call_skill(a2a, "greet", {"name": "Ada"})
        expect(text_result(greeting), "Hello, Ada!")


async def check_assistant() -> None:
    """Text generation, structured output and a model-free skill."""
    summary = assistant.Summary(text="Cats sleep a lot.", key_points=["sleep"])
    model = ScriptedModel(
        ["Paris.", summary.model_dump_json()], capabilities=STRUCTURED
    )

    def create_app(base_url: str) -> AgentApp:
        os.environ["ASSISTANT_PUBLIC_URL"] = base_url
        return assistant.create_app(model)

    async with serve(create_app) as base_url, connect(base_url) as a2a:
        answer = await call_skill(a2a, "answer", {"question": "Capital of France?"})
        expect(text_result(answer), "Paris.")
        summarized = await call_skill(a2a, "summarize", {"text": "Cats nap all day."})
        expect(assistant.Summary.model_validate(data_result(summarized)), summary)
        counted = await call_skill(a2a, "count_words", {"text": "one two three"})
        expect(data_result(counted), 3)


async def check_orders() -> None:
    """Tool calling, with a development API key."""
    os.environ["ORDERS_API_KEY"] = "smoke-orders-key"
    lookup = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')
    model = ScriptedModel([lookup, "Order A1 has shipped."], capabilities=TOOLS)

    def create_app(base_url: str) -> AgentApp:
        os.environ["ORDERS_PUBLIC_URL"] = base_url
        return orders.create_app(model)

    async with (
        serve(create_app) as base_url,
        connect(base_url, "smoke-orders-key") as a2a,
    ):
        task = await call_skill(a2a, "order_help", {"request": "Where is order A1?"})
        expect(text_result(task), "Order A1 has shipped.")
        expect(task.metadata["usage"]["tool_calls"], 1)


async def check_routed_assistant() -> None:
    """Free text routed by the built-in ``ModelRouter``."""
    model = ScriptedModel(
        [
            json.dumps({"skill": "to_fahrenheit", "question": None}),
            json.dumps({"arguments": {"celsius": 100}, "question": None}),
        ],
        capabilities=STRUCTURED,
    )

    def create_app(base_url: str) -> AgentApp:
        os.environ["ASSISTANT_PUBLIC_URL"] = base_url
        return routed_assistant.create_app(model)

    async with serve(create_app) as base_url, connect(base_url) as a2a:
        task = await say(a2a, "What is 100 degrees Celsius in Fahrenheit?")
        temperature = routed_assistant.Temperature.model_validate(data_result(task))
        expect(temperature, routed_assistant.Temperature(celsius=100, fahrenheit=212))


async def main() -> None:
    """Check every example in turn; raise ``AssertionError`` on the first failure."""
    print(f"agentframe {agentframe.__version__} from {agentframe.__file__}")
    for check in (check_echo, check_assistant, check_orders, check_routed_assistant):
        await check()
        print(f"ok  {check.__doc__}")
    print("All examples returned complete, validated results.")


if __name__ == "__main__":
    asyncio.run(main())
